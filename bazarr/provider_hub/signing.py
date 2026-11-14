# coding=utf-8
"""Signature verification for the official plugin catalog.

This module is standalone: it reads nothing from disk, talks to no network,
keeps no state and imports nothing from the rest of Provider Hub. Callers hand
it the exact catalog bytes, the detached signature envelope, the key file and
the root public keys they trust, plus the current time and the last counter
they stored for the channel. It hands back a typed result. Every failure a
hostile or stale feed can cause is a result, never an exception.

Trust model
-----------
* One or more root keys are baked into the host and passed in by the caller.
  A root key never signs a catalog. It only vouches for the CI signing key list and
  revokes them.
* The key file lists CI signing keys and revocations. A trusted root signs the
  whole file (keys and revocations together) and numbers it with a
  ``key_file_version`` that only goes up. A CI key counts only when it is in a
  key file a trusted root signed, and a revocation counts only inside such a
  file, so a stolen CI key cannot revoke anything and a revocation cannot be
  stripped out of a signed file. The caller passes the highest key file version
  it has accepted; an older file is refused (``KEY_FILE_ROLLBACK``), which
  stops a replayed older file, or an emptier one, from hiding a revocation.
* A catalog is signed by a listed CI key over its exact bytes. The signed
  document carries its own channel, a per-channel ``signed_version`` counter, a
  signing time (``not_before``) and an expiry.
* A revoked key is rejected outright, for every catalog it signed, whatever
  date the catalog claims. The key holder controls the catalog's own dates, so
  no date comparison can be trusted. Recovery is re-signing with a new key.

Checks run in this order and the first failure decides the status: unsigned,
malformed input, key file not signed by a trusted root, key file rollback,
bad signature, revoked key, channel mismatch,
validity window, then the freshness rules below. Input is parsed strictly:
UTF-8 only, duplicate JSON object keys refused, timestamps and key ids ASCII.

Freshness
---------
Expiry and ``not_before`` both tolerate ``CLOCK_SKEW`` (24 hours). Past that,
a catalog is ``EXPIRED`` (host clock too late) or ``NOT_YET_VALID`` (``not_before``
further ahead than the skew), neither is usable, and the result carries both
the host time and the catalog time so the caller can show them. When the host
clock reads earlier than the running release's build time, the clock cannot be
trusted: expiry and ``not_before`` are not evaluated and the replay counter
alone decides. That outcome is reported as ``CLOCK_WRONG`` so it stays distinct
from a healthy ``OK``. ``usable`` is true for ``OK`` and ``CLOCK_WRONG``.

Replay counter
--------------
``signed_version`` is compared against the value the caller stored for that
one channel (see ``compare_counter``). Equal is accepted, because refreshing
an unchanged catalog is normal. Lower is ``REPLAYED``. This module never
stores the counter.

Baked catalogs
--------------
A catalog shipped inside an image (the release floor and the seed) is verified
with ``baked=True``: signature, chain, revocation and channel are checked, but
expiry, ``not_before`` and the replay counter are not, and the result is marked so the
caller never latches on it or raises the stored counter.

Wire format
-----------
The shape of the key file, of the signature envelope and of the signed
metadata is provisional. The catalog publishing side has not frozen it. All
knowledge of field names, encodings and signed statements lives in the
"Provisional wire format" section below and is reached through the single
function ``parse_wire_format``. When the final format is settled, that
section is the only part of this module that changes.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CLOCK_SKEW = timedelta(hours=24)
MAX_VALIDITY = timedelta(days=366)


class Status(StrEnum):
    OK = "ok"
    UNSIGNED = "unsigned"
    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    KEY_FILE_ROLLBACK = "key_file_rollback"
    REVOKED_KEY = "revoked_key"
    CHANNEL_MISMATCH = "channel_mismatch"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    REPLAYED = "replayed"
    CLOCK_WRONG = "clock_wrong"


class CounterVerdict(StrEnum):
    FIRST = "first"
    ADVANCED = "advanced"
    UNCHANGED = "unchanged"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of ``verify_catalog``.

    ``root_key_id`` and ``key_file_version`` are set once the key file's root
    signature checked out. ``signer_key_id`` is set once a catalog signature
    checked out.
    The signed metadata fields (``channel``, ``signed_version``, ``not_before``,
    ``expires``) are set only after the signature and revocation checks pass,
    so they are never taken from unverified bytes. ``host_time`` is the ``now``
    the caller passed, kept so a message can show both clocks.
    """

    status: Status
    detail: str = ""
    signer_key_id: str | None = None
    root_key_id: str | None = None
    key_file_version: int | None = None
    channel: str | None = None
    signed_version: int | None = None
    not_before: datetime | None = None
    expires: datetime | None = None
    host_time: datetime | None = None
    counter: CounterVerdict | None = None
    baked: bool = False

    @property
    def ok(self) -> bool:
        return self.status is Status.OK

    @property
    def usable(self) -> bool:
        """True when the content is authentic and acceptable.

        That is ``OK``, or ``CLOCK_WRONG`` where the host clock is unreliable
        and the replay counter stood in for expiry.
        """
        return self.status in (Status.OK, Status.CLOCK_WRONG)

    @property
    def may_advance_state(self) -> bool:
        """True when the caller may raise the stored counter and latch the channel.

        Never true for a baked catalog.
        """
        return self.usable and not self.baked


def compare_counter(signed_version: int, stored: int | None) -> CounterVerdict:
    """Compare a catalog's ``signed_version`` with the stored value for its channel.

    Pure: the caller looks up the stored value for the right channel and passes
    it in, ``None`` when nothing is stored yet.
    """
    if stored is None:
        return CounterVerdict.FIRST
    if signed_version > stored:
        return CounterVerdict.ADVANCED
    if signed_version == stored:
        return CounterVerdict.UNCHANGED
    return CounterVerdict.REPLAYED


def key_id(public_key: bytes) -> str:
    """First 16 hex digits of SHA-256 over the raw 32-byte public key."""
    return hashlib.sha256(public_key).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Provisional wire format. Nothing outside this section knows a field name.
# ---------------------------------------------------------------------------


class WireFormatError(ValueError):
    """The key file, envelope or signed metadata does not have the expected shape."""


@dataclass(frozen=True)
class SigningKey:
    key_id: str
    public_key: bytes
    added: str


@dataclass(frozen=True)
class UnknownKey:
    """A key entry for an algorithm this build does not know.

    It never verifies anything, but it stays in the statement the root signed,
    so a key file that lists a second algorithm still checks out.
    """

    key_id: str
    algorithm: str
    public_key: str
    added: str


@dataclass(frozen=True)
class Revocation:
    key_id: str
    revoked_at: str


@dataclass(frozen=True)
class KeyFile:
    version: int
    root_key_id: str
    signature: bytes
    keys: tuple[SigningKey, ...]
    revocations: tuple[Revocation, ...]
    unknown_keys: tuple[UnknownKey, ...] = ()


@dataclass(frozen=True)
class SignatureEntry:
    key_id: str
    signature: bytes


@dataclass(frozen=True)
class SignedMetadata:
    channel: str
    signed_version: int
    not_before: datetime
    expires: datetime


@dataclass(frozen=True)
class WireDocuments:
    key_file: KeyFile
    signatures: tuple[SignatureEntry, ...]
    metadata: SignedMetadata


_KEY_FILE_CONTEXT = b"bazarr-catalog-key-file-v1\n"
_ENVELOPE_SCHEMA_VERSION = 1
_ALGORITHM = "ed25519"
_MAX_COUNTER = 2**63
_MAX_SIGNATURES = 16
_MAX_FIELD = 256
_MAX_DETAIL = 300
_KEY_ID_RE = re.compile(r"[0-9a-f]{16}", re.ASCII)
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)


def key_file_statement(
    root_key_id: str,
    key_file_version: int,
    keys: Iterable[Mapping[str, str]],
    revocations: Iterable[Mapping[str, str]],
) -> bytes:
    """The bytes a root key signs to vouch for a whole key file.

    Covers the root key id, the version, every signing key (``key_id``,
    ``public_key`` as base64, ``added``; ``algorithm`` defaults to ed25519) and
    every revocation (``key_id``,
    ``revoked_at``). Entries are sorted by key id, so the order in the file does
    not matter, and the encoding is canonical JSON so whitespace does not
    matter either.
    """
    body = {
        "root_key_id": root_key_id,
        "key_file_version": key_file_version,
        "keys": sorted(
            (
                {"added": k["added"], "algorithm": k.get("algorithm", _ALGORITHM), "key_id": k["key_id"], "public_key": k["public_key"]}
                for k in keys
            ),
            key=lambda k: k["key_id"],
        ),
        "revocations": sorted(
            ({"key_id": r["key_id"], "revoked_at": r["revoked_at"]} for r in revocations),
            key=lambda r: (r["key_id"], r["revoked_at"]),
        ),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _KEY_FILE_CONTEXT + encoded.encode("ascii")


def statement_for(key_file: KeyFile) -> bytes:
    """The statement the root signed for a parsed key file, unknown algorithms included."""
    keys = [
        {"key_id": k.key_id, "public_key": base64.b64encode(k.public_key).decode("ascii"), "added": k.added}
        for k in key_file.keys
    ]
    keys.extend(
        {"key_id": k.key_id, "algorithm": k.algorithm, "public_key": k.public_key, "added": k.added}
        for k in key_file.unknown_keys
    )
    return key_file_statement(
        key_file.root_key_id,
        key_file.version,
        keys,
        [{"key_id": r.key_id, "revoked_at": r.revoked_at} for r in key_file.revocations],
    )


def parse_wire_format(key_file: Any, envelope: Any, catalog: bytes) -> WireDocuments:
    """Parse the key file, the signature envelope and the signed metadata.

    This is the one place that knows the provisional field names. It does no
    cryptography and trusts nothing it reads. ``key_file`` and ``envelope`` may
    be parsed JSON objects, or JSON text or bytes. ``catalog`` is the exact
    signed byte string; its top-level ``channel``, ``signed_version``,
    ``not_before`` and ``expires`` are the signed metadata.

    Raises ``WireFormatError`` on any deviation.

    Key file: ``{"key_file_version": 1, "root_key_id", "signature", "keys":
    [{"key_id", "algorithm", "public_key", "added"}], "revocations":
    [{"key_id", "revoked_at"}]}``. ``revocations`` may be absent. Public keys
    and the signature are base64. A key entry with another algorithm is kept
    for the signed statement but never used to verify, so a second algorithm
    can be added later. The root signs ``key_file_statement`` over the
    whole file. Unknown extra fields are ignored (and so are not covered by the
    signature).

    Envelope: ``{"schema_version": 1, "signatures": [{"algorithm": "ed25519",
    "key_id", "signature"}]}`` with at least one 64-byte signature over the
    exact catalog bytes. Entries with any other algorithm are skipped so a
    second algorithm can be added later; an envelope with no ed25519 entry is
    malformed (deliberately not "unsigned", so it never takes the legacy path).
    More than 16 entries, or two ed25519 entries for one key id, are malformed.

    All JSON is strict: UTF-8 only, duplicate object keys refused.

    Timestamps are ``YYYY-MM-DDTHH:MM:SSZ`` in UTC.
    """
    return WireDocuments(
        key_file=_parse_key_file(_as_object(key_file, "key file")),
        signatures=_parse_envelope(_as_object(envelope, "signature envelope")),
        metadata=_parse_metadata(catalog),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate object key")
        result[name] = value
    return result


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _parse_float(text: str) -> float:
    # ``1e999`` is not a JSON constant, but Python reads it as infinity.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _loads(raw: Any, what: str) -> Any:
    """Strict JSON: UTF-8 bytes only, no duplicate object keys, no NaN, Infinity or overflowing number."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except (ValueError, RecursionError, TypeError) as exc:
        raise WireFormatError(f"{what} is not valid JSON") from exc


def _as_object(value: Any, what: str) -> Mapping[str, Any]:
    if isinstance(value, (bytes, bytearray, str)):
        value = _loads(value, what)
    if not isinstance(value, Mapping):
        raise WireFormatError(f"{what} must be a JSON object")
    return value


def _text(container: Mapping[str, Any], name: str, what: str) -> str:
    value = container.get(name)
    if not isinstance(value, str) or not value:
        raise WireFormatError(f"{what} needs a non-empty string {name}")
    return value


def _key_id_field(container: Mapping[str, Any], name: str, what: str) -> str:
    value = _text(container, name, what)
    if not _KEY_ID_RE.fullmatch(value):
        raise WireFormatError(f"{what} {name} must be 16 lowercase hex digits")
    return value


def _base64_field(container: Mapping[str, Any], name: str, what: str, length: int) -> bytes:
    value = _text(container, name, what)
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WireFormatError(f"{what} {name} is not valid base64") from exc
    if len(raw) != length:
        raise WireFormatError(f"{what} {name} must decode to {length} bytes")
    return raw


def _timestamp(value: Any, what: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise WireFormatError(f"{what} must look like 2026-12-31T00:00:00Z")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise WireFormatError(f"{what} is not a real date") from exc


def _object_list(container: Mapping[str, Any], name: str, what: str, *, required: bool) -> list[Mapping[str, Any]]:
    if name not in container:
        if required:
            raise WireFormatError(f"{what} needs {name}")
        return []
    value = container[name]
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise WireFormatError(f"{what} {name} must be a list of objects")
    return value


def _is_counter(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value < _MAX_COUNTER


def _parse_key_file(document: Mapping[str, Any]) -> KeyFile:
    version = document.get("key_file_version")
    if not _is_counter(version):
        raise WireFormatError("key_file_version must be a positive integer below 2**63")
    keys: list[SigningKey] = []
    unknown: list[UnknownKey] = []
    seen: set[str] = set()
    for entry in _object_list(document, "keys", "key file", required=True):
        algorithm = entry.get("algorithm")
        if not isinstance(algorithm, str) or not algorithm:
            raise WireFormatError("each signing key needs an algorithm")
        if algorithm != _ALGORITHM:
            # A future algorithm: kept for the signed statement, never used.
            fields = {name: _text(entry, name, "signing key") for name in ("key_id", "public_key", "added")}
            if not algorithm.isascii() or any(len(v) > _MAX_FIELD for v in (algorithm, *fields.values())):
                raise WireFormatError("a signing key field is too long or not ASCII")
            if fields["key_id"] in seen:
                raise WireFormatError("a key is listed twice")
            seen.add(fields["key_id"])
            unknown.append(UnknownKey(algorithm=algorithm, **fields))
            continue
        kid = _key_id_field(entry, "key_id", "signing key")
        public_key = _base64_field(entry, "public_key", "signing key", 32)
        if key_id(public_key) != kid:
            raise WireFormatError(f"key_id {kid} does not match its public key")
        if kid in seen:
            raise WireFormatError(f"key {kid} is listed twice")
        seen.add(kid)
        keys.append(SigningKey(key_id=kid, public_key=public_key, added=_text(entry, "added", "signing key")))
    revocations: list[Revocation] = []
    for entry in _object_list(document, "revocations", "key file", required=False):
        revoked_at = _text(entry, "revoked_at", "revocation")
        _timestamp(revoked_at, "revoked_at")
        revocations.append(Revocation(key_id=_key_id_field(entry, "key_id", "revocation"), revoked_at=revoked_at))
    return KeyFile(
        version=version,
        root_key_id=_key_id_field(document, "root_key_id", "key file"),
        signature=_base64_field(document, "signature", "key file", 64),
        keys=tuple(keys),
        revocations=tuple(revocations),
        unknown_keys=tuple(unknown),
    )


def _parse_envelope(document: Mapping[str, Any]) -> tuple[SignatureEntry, ...]:
    version = document.get("schema_version")
    if isinstance(version, bool) or version != _ENVELOPE_SCHEMA_VERSION:
        raise WireFormatError(f"signature schema_version must be {_ENVELOPE_SCHEMA_VERSION}")
    entries = _object_list(document, "signatures", "signature envelope", required=True)
    if not entries:
        raise WireFormatError("a signature envelope needs at least one signature")
    if len(entries) > _MAX_SIGNATURES:
        raise WireFormatError(f"a signature envelope holds at most {_MAX_SIGNATURES} signatures")
    parsed = []
    listed: set[str] = set()
    for entry in entries:
        algorithm = entry.get("algorithm")
        if not isinstance(algorithm, str) or not algorithm:
            raise WireFormatError("each signature needs an algorithm")
        if algorithm != _ALGORITHM:
            continue  # a future algorithm: skipped, never fatal
        kid = _key_id_field(entry, "key_id", "signature")
        if kid in listed:
            raise WireFormatError(f"key {kid} signs the catalog twice")
        listed.add(kid)
        parsed.append(SignatureEntry(key_id=kid, signature=_base64_field(entry, "signature", "signature", 64)))
    if not parsed:
        raise WireFormatError("the signature envelope has no ed25519 signature")
    return tuple(parsed)


def _parse_metadata(catalog: bytes) -> SignedMetadata:
    document = _loads(catalog, "the signed catalog")
    if not isinstance(document, Mapping):
        raise WireFormatError("the signed catalog must be a JSON object")
    signed_version = document.get("signed_version")
    if not _is_counter(signed_version):
        raise WireFormatError("signed_version must be a positive integer below 2**63")
    return SignedMetadata(
        channel=_text(document, "channel", "signed catalog"),
        signed_version=signed_version,
        not_before=_timestamp(document.get("not_before"), "not_before"),
        expires=_timestamp(document.get("expires"), "expires"),
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_catalog(
    data: bytes,
    envelope: Any,
    key_file: Any,
    *,
    roots: Mapping[str, bytes],
    channel: str,
    now: datetime,
    stored_counter: int | None = None,
    stored_key_file_version: int | None = None,
    host_build_time: datetime | None = None,
    baked: bool = False,
) -> VerificationResult:
    """Verify a catalog against its detached signature envelope.

    ``data`` is the exact byte string that was signed: ``bytes``, ``bytearray``
    or ``memoryview``. Anything else, text included, is ``MALFORMED``. ``envelope`` is the
    detached signature document (parsed, or JSON text or bytes). Only ``None``
    or blank text means the catalog came without one (``UNSIGNED``); an empty
    object or any other document that is not a valid envelope is ``MALFORMED``. ``key_file`` is the CI key file.
    ``roots`` maps root key ids to raw 32-byte public keys and is the only
    thing trusted without proof. ``channel`` is the channel the caller asked
    for; the signed document must name the same one. ``now`` and
    ``host_build_time`` are timezone-aware datetimes. ``stored_counter`` is the
    last ``signed_version`` accepted for that channel, if any.
    ``stored_key_file_version`` is the highest ``key_file_version`` the caller
    has accepted, if any; an older key file is refused. It is checked in baked
    mode too, so a caller verifying a baked catalog against the image's older
    key file should pass ``None`` or the image's own version.

    Expected failures come back as a result with a non-OK status. Passing a
    naive datetime is a programming error and raises ``ValueError``.
    """
    _require_aware(now, "now")
    if host_build_time is not None:
        _require_aware(host_build_time, "host_build_time")

    # The signature is over bytes. Text that merely parses as the catalog is not
    # what was signed, so it is refused here instead of failing deeper down.
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    elif not isinstance(data, bytes):
        return VerificationResult(
            Status.MALFORMED,
            f"the catalog must be bytes, not {type(data).__name__}",
            host_time=now,
            baked=baked,
        )

    if envelope is None or (isinstance(envelope, (bytes, bytearray, str)) and not envelope.strip()):
        return VerificationResult(Status.UNSIGNED, "the catalog has no signature", host_time=now, baked=baked)

    try:
        wire = parse_wire_format(key_file, envelope, data)
    except WireFormatError as exc:
        return VerificationResult(Status.MALFORMED, str(exc), host_time=now, baked=baked)

    metadata = wire.metadata
    key_file_doc = wire.key_file

    def early(status: Status, detail: str, **fields: Any) -> VerificationResult:
        return VerificationResult(status, detail, host_time=now, baked=baked, **fields)

    root = roots.get(key_file_doc.root_key_id)
    try:
        statement = statement_for(key_file_doc)
    except (ValueError, TypeError, RecursionError) as exc:
        return early(Status.MALFORMED, f"the key file cannot be serialised: {exc.__class__.__name__}")
    if root is None or not _ed25519_verify(root, key_file_doc.signature, statement):
        return early(Status.BAD_SIGNATURE, "the key file is not signed by a trusted root")
    listed_ids = {k.key_id for k in key_file_doc.keys} | {k.key_id for k in key_file_doc.unknown_keys}
    root_publics = {value for value in roots.values() if isinstance(value, bytes)}
    if listed_ids & set(roots) or any(k.public_key in root_publics for k in key_file_doc.keys):
        return early(Status.MALFORMED, "a root key is listed as a signing key; root keys never sign catalogs")
    if stored_key_file_version is not None and key_file_doc.version < stored_key_file_version:
        return early(
            Status.KEY_FILE_ROLLBACK,
            f"key file version {key_file_doc.version} is older than the accepted {stored_key_file_version}",
            root_key_id=key_file_doc.root_key_id,
            key_file_version=key_file_doc.version,
        )

    signer, revoked, problems = _find_signer(data, key_file_doc, wire.signatures)
    if signer is None:
        if revoked is not None:
            return early(
                Status.REVOKED_KEY,
                f"key {revoked.key_id} is revoked",
                signer_key_id=revoked.key_id,
                root_key_id=key_file_doc.root_key_id,
                key_file_version=key_file_doc.version,
            )
        detail = _bounded("; ".join(problems)) if problems else "no signature checked out"
        return early(
            Status.BAD_SIGNATURE,
            detail,
            root_key_id=key_file_doc.root_key_id,
            key_file_version=key_file_doc.version,
        )

    def result(status: Status, detail: str = "", counter: CounterVerdict | None = None) -> VerificationResult:
        return VerificationResult(
            status,
            detail,
            signer_key_id=signer.key_id,
            root_key_id=key_file_doc.root_key_id,
            key_file_version=key_file_doc.version,
            channel=metadata.channel,
            signed_version=metadata.signed_version,
            not_before=metadata.not_before,
            expires=metadata.expires,
            host_time=now,
            counter=counter,
            baked=baked,
        )

    if metadata.channel != channel:
        return result(Status.CHANNEL_MISMATCH, f"the catalog is signed for {metadata.channel}, not {channel}")
    if metadata.expires <= metadata.not_before or metadata.expires - metadata.not_before > MAX_VALIDITY:
        return result(Status.MALFORMED, "the validity window must be positive and at most 366 days")

    if baked:
        return result(Status.OK, "baked catalog: signature and chain only")

    counter = compare_counter(metadata.signed_version, stored_counter)
    if counter is CounterVerdict.REPLAYED:
        return result(
            Status.REPLAYED,
            f"signed_version {metadata.signed_version} is older than the stored {stored_counter}",
            counter,
        )

    if host_build_time is not None and now < host_build_time:
        return result(
            Status.CLOCK_WRONG,
            f"host time {_iso(now)} is earlier than this release was built ({_iso(host_build_time)}); "
            "expiry and not_before were not evaluated and the counter decided",
            counter,
        )

    # Differences, never shifted timestamps: shifting year 1 or year 9999 overflows.
    if metadata.not_before - now > CLOCK_SKEW:
        return result(
            Status.NOT_YET_VALID,
            f"the catalog is valid from {_iso(metadata.not_before)}, host time is {_iso(now)}",
            counter,
        )
    if now - metadata.expires > CLOCK_SKEW:
        return result(
            Status.EXPIRED,
            f"host time {_iso(now)} is past the catalog expiry {_iso(metadata.expires)}",
            counter,
        )
    return result(Status.OK, counter=counter)


def _bounded(text: str) -> str:
    return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 3] + "..."


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ed25519_verify(public_key: bytes, signature: bytes, data: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, data)
    except (InvalidSignature, ValueError):
        return False
    return True


def _find_signer(
    data: bytes,
    key_file: KeyFile,
    signatures: tuple[SignatureEntry, ...],
) -> tuple[SigningKey | None, SigningKey | None, list[str]]:
    """Return ``(signer, revoked, problems)``.

    ``signer`` is a listed, unrevoked key whose signature over ``data``
    verifies. ``revoked`` is set only when no such key exists but a listed key
    did sign validly and is revoked, whatever date the catalog claims.
    """
    keys = {entry.key_id: entry for entry in key_file.keys}
    revoked_ids = {entry.key_id for entry in key_file.revocations}
    revoked_signer: SigningKey | None = None
    problems: list[str] = []
    for entry in signatures:
        key = keys.get(entry.key_id)
        if key is None:
            problems.append(f"key {entry.key_id} is not in the key file")
            continue
        if not _ed25519_verify(key.public_key, entry.signature, data):
            problems.append(f"signature by {key.key_id} does not match the catalog")
            continue
        if key.key_id in revoked_ids:
            revoked_signer = revoked_signer or key
            continue
        return key, None, problems
    return None, revoked_signer, problems
