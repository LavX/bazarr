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
  A root key never signs a catalog. It only certifies CI signing keys and
  revokes them.
* The key file lists CI signing keys, each carrying a root certification, and
  root-signed revocations. A CI key counts only when a trusted root certified
  it. A revocation counts only when a trusted root signed it, so a stolen CI
  key cannot revoke anything and a forged revocation is ignored.
* A catalog is signed by a certified CI key over its exact bytes. The signed
  document carries its own channel, a per-channel ``signed_version`` counter, a
  signing time (``not_before``) and an expiry.
* A revoked key is rejected outright, for every catalog it signed, whatever
  date the catalog claims. The key holder controls the catalog's own dates, so
  no date comparison can be trusted. Recovery is re-signing with a new key.

Checks run in this order and the first failure decides the status: unsigned,
malformed input, bad signature or broken chain, revoked key, channel mismatch,
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
import re

from collections.abc import Mapping
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

    ``signer_key_id`` and ``root_key_id`` are set once a signature checked out.
    The signed metadata fields (``channel``, ``signed_version``, ``not_before``,
    ``expires``) are set only after the signature and revocation checks pass,
    so they are never taken from unverified bytes. ``host_time`` is the ``now``
    the caller passed, kept so a message can show both clocks.
    """

    status: Status
    detail: str = ""
    signer_key_id: str | None = None
    root_key_id: str | None = None
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
class CertifiedKey:
    key_id: str
    public_key: bytes
    added: str
    root_key_id: str
    certification: bytes


@dataclass(frozen=True)
class Revocation:
    key_id: str
    revoked_at: datetime
    revoked_at_text: str
    root_key_id: str
    signature: bytes


@dataclass(frozen=True)
class KeyFile:
    keys: tuple[CertifiedKey, ...]
    revocations: tuple[Revocation, ...]


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


_CERTIFICATION_CONTEXT = b"bazarr-catalog-key-certification-v1\n"
_REVOCATION_CONTEXT = b"bazarr-catalog-key-revocation-v1\n"
_ENVELOPE_SCHEMA_VERSION = 1
_ALGORITHM = "ed25519"
_KEY_ID_RE = re.compile(r"[0-9a-f]{16}", re.ASCII)
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)


def certification_statement(signing_key_id: str, public_key: bytes) -> bytes:
    """The bytes a root key signs to certify a CI signing key."""
    return _CERTIFICATION_CONTEXT + signing_key_id.encode("ascii") + b"\n" + public_key.hex().encode("ascii")


def revocation_statement(revoked_key_id: str, revoked_at: str) -> bytes:
    """The bytes a root key signs to revoke a CI signing key from ``revoked_at`` on."""
    return _REVOCATION_CONTEXT + revoked_key_id.encode("ascii") + b"\n" + revoked_at.encode("ascii")


def parse_wire_format(key_file: Any, envelope: Any, catalog: bytes) -> WireDocuments:
    """Parse the key file, the signature envelope and the signed metadata.

    This is the one place that knows the provisional field names. It does no
    cryptography and trusts nothing it reads. ``key_file`` and ``envelope`` may
    be parsed JSON objects, or JSON text or bytes. ``catalog`` is the exact
    signed byte string; its top-level ``channel``, ``signed_version``,
    ``not_before`` and ``expires`` are the signed metadata.

    Raises ``WireFormatError`` on any deviation.

    Key file: ``{"keys": [{"key_id", "algorithm", "public_key", "added",
    "certification": {"root_key_id", "signature"}}], "revocations": [{"key_id",
    "revoked_at", "root_key_id", "signature"}]}``. ``revocations`` may be
    absent. Public keys and signatures are base64. Unknown extra fields are
    ignored.

    Envelope: ``{"schema_version": 1, "signatures": [{"algorithm": "ed25519",
    "key_id", "signature"}]}`` with at least one 64-byte signature over the
    exact catalog bytes. Entries with any other algorithm are skipped so a
    second algorithm can be added later; an envelope with no ed25519 entry is
    malformed (deliberately not "unsigned", so it never takes the legacy path).

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


def _loads(raw: Any, what: str) -> Any:
    """Strict JSON: UTF-8 bytes only, no duplicate object keys, no NaN or Infinity."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
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


def _parse_key_file(document: Mapping[str, Any]) -> KeyFile:
    keys: list[CertifiedKey] = []
    seen: set[str] = set()
    for entry in _object_list(document, "keys", "key file", required=True):
        if entry.get("algorithm") != _ALGORITHM:
            raise WireFormatError("each signing key must use ed25519")
        kid = _key_id_field(entry, "key_id", "signing key")
        public_key = _base64_field(entry, "public_key", "signing key", 32)
        if key_id(public_key) != kid:
            raise WireFormatError(f"key_id {kid} does not match its public key")
        if kid in seen:
            raise WireFormatError(f"key {kid} is listed twice")
        seen.add(kid)
        certification = entry.get("certification")
        if not isinstance(certification, Mapping):
            raise WireFormatError(f"key {kid} needs a certification")
        keys.append(
            CertifiedKey(
                key_id=kid,
                public_key=public_key,
                added=_text(entry, "added", "signing key"),
                root_key_id=_key_id_field(certification, "root_key_id", "certification"),
                certification=_base64_field(certification, "signature", "certification", 64),
            )
        )
    revocations: list[Revocation] = []
    for entry in _object_list(document, "revocations", "key file", required=False):
        revoked_at_text = _text(entry, "revoked_at", "revocation")
        revocations.append(
            Revocation(
                key_id=_key_id_field(entry, "key_id", "revocation"),
                revoked_at=_timestamp(revoked_at_text, "revoked_at"),
                revoked_at_text=revoked_at_text,
                root_key_id=_key_id_field(entry, "root_key_id", "revocation"),
                signature=_base64_field(entry, "signature", "revocation", 64),
            )
        )
    return KeyFile(keys=tuple(keys), revocations=tuple(revocations))


def _parse_envelope(document: Mapping[str, Any]) -> tuple[SignatureEntry, ...]:
    version = document.get("schema_version")
    if isinstance(version, bool) or version != _ENVELOPE_SCHEMA_VERSION:
        raise WireFormatError(f"signature schema_version must be {_ENVELOPE_SCHEMA_VERSION}")
    entries = _object_list(document, "signatures", "signature envelope", required=True)
    if not entries:
        raise WireFormatError("a signature envelope needs at least one signature")
    parsed = []
    for entry in entries:
        algorithm = entry.get("algorithm")
        if not isinstance(algorithm, str) or not algorithm:
            raise WireFormatError("each signature needs an algorithm")
        if algorithm != _ALGORITHM:
            continue  # a future algorithm: skipped, never fatal
        parsed.append(
            SignatureEntry(
                key_id=_key_id_field(entry, "key_id", "signature"),
                signature=_base64_field(entry, "signature", "signature", 64),
            )
        )
    if not parsed:
        raise WireFormatError("the signature envelope has no ed25519 signature")
    return tuple(parsed)


def _parse_metadata(catalog: bytes) -> SignedMetadata:
    document = _loads(catalog, "the signed catalog")
    if not isinstance(document, Mapping):
        raise WireFormatError("the signed catalog must be a JSON object")
    signed_version = document.get("signed_version")
    if isinstance(signed_version, bool) or not isinstance(signed_version, int) or signed_version < 1:
        raise WireFormatError("signed_version must be a positive integer")
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
    host_build_time: datetime | None = None,
    baked: bool = False,
) -> VerificationResult:
    """Verify a catalog against its detached signature envelope.

    ``data`` is the exact byte string that was signed. ``envelope`` is the
    detached signature document (parsed, or JSON text or bytes), or ``None`` or
    empty when the catalog came without one. ``key_file`` is the CI key file.
    ``roots`` maps root key ids to raw 32-byte public keys and is the only
    thing trusted without proof. ``channel`` is the channel the caller asked
    for; the signed document must name the same one. ``now`` and
    ``host_build_time`` are timezone-aware datetimes. ``stored_counter`` is the
    last ``signed_version`` accepted for that channel, if any.

    Expected failures come back as a result with a non-OK status. Passing a
    naive datetime is a programming error and raises ``ValueError``.
    """
    _require_aware(now, "now")
    if host_build_time is not None:
        _require_aware(host_build_time, "host_build_time")

    if envelope is None or (isinstance(envelope, (bytes, bytearray, str)) and not envelope.strip()):
        return VerificationResult(Status.UNSIGNED, "the catalog has no signature", host_time=now, baked=baked)

    try:
        wire = parse_wire_format(key_file, envelope, data)
    except WireFormatError as exc:
        return VerificationResult(Status.MALFORMED, str(exc), host_time=now, baked=baked)

    metadata = wire.metadata
    signer, revoked, problems = _find_signer(data, wire, roots)
    if signer is None:
        if revoked is not None:
            revoked_key, revocation = revoked
            return VerificationResult(
                Status.REVOKED_KEY,
                f"key {revoked_key.key_id} was revoked at {revocation.revoked_at_text}",
                signer_key_id=revoked_key.key_id,
                root_key_id=revoked_key.root_key_id,
                host_time=now,
                baked=baked,
            )
        detail = "; ".join(problems) if problems else "no signature checked out"
        return VerificationResult(Status.BAD_SIGNATURE, detail, host_time=now, baked=baked)

    def result(status: Status, detail: str = "", counter: CounterVerdict | None = None) -> VerificationResult:
        return VerificationResult(
            status,
            detail,
            signer_key_id=signer.key_id,
            root_key_id=signer.root_key_id,
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
    wire: WireDocuments,
    roots: Mapping[str, bytes],
) -> tuple[CertifiedKey | None, tuple[CertifiedKey, Revocation] | None, list[str]]:
    """Return ``(signer, revoked, problems)``.

    ``signer`` is a certified, unrevoked key whose signature over ``data``
    verifies. ``revoked`` is set only when no such key exists but a certified
    key did sign validly and is revoked, whatever date the catalog claims; it
    holds the key and the revocation that applies.
    """
    keys = {entry.key_id: entry for entry in wire.key_file.keys}
    revoked_signer: tuple[CertifiedKey, Revocation] | None = None
    problems: list[str] = []
    for entry in wire.signatures:
        key = keys.get(entry.key_id)
        if key is None:
            problems.append(f"key {entry.key_id} is not in the key file")
            continue
        root = roots.get(key.root_key_id)
        if root is None or not _ed25519_verify(root, key.certification, certification_statement(key.key_id, key.public_key)):
            problems.append(f"key {key.key_id} is not certified by a trusted root")
            continue
        if not _ed25519_verify(key.public_key, entry.signature, data):
            problems.append(f"signature by {key.key_id} does not match the catalog")
            continue
        revocation = _applicable_revocation(key.key_id, wire.key_file.revocations, roots)
        if revocation is not None:
            revoked_signer = revoked_signer or (key, revocation)
            continue
        return key, None, problems
    return None, revoked_signer, problems


def _applicable_revocation(
    signing_key_id: str,
    revocations: tuple[Revocation, ...],
    roots: Mapping[str, bytes],
) -> Revocation | None:
    for revocation in revocations:
        if revocation.key_id != signing_key_id:
            continue
        root = roots.get(revocation.root_key_id)
        if root is not None and _ed25519_verify(
            root, revocation.signature, revocation_statement(revocation.key_id, revocation.revoked_at_text)
        ):
            return revocation
    return None
