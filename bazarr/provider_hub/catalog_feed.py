# coding=utf-8
"""The signed feed of the official catalog: the one place that checks it.

The official catalog publishes, beside its legacy ``catalog.json``, one signed file per
channel (``signed/<channel>/catalog.json``) with a detached signature and a root-signed key
file. This module is the host's only caller of ``signing.verify_catalog``. Every origin the
bytes can come from (the raw repository, the Pages mirror, the image's own snapshot) hands
them to ``verify_fetched`` before anything parses them, and gets back a verified catalog or
a typed refusal.

What a refresh keeps per channel lives in the state document under ``catalog_feeds``: the
highest ``signed_version`` and ``key_file_version`` it accepted. The same two numbers are
mirrored to a small file outside the database, and the larger of the two copies counts, so
restoring an old database backup cannot take the replay counter backwards.

Cutover: a channel that has never accepted a signed catalog still reads the legacy
``catalog.json`` when its commit has no signed file, and the source shows
"unsigned (legacy)". The first valid signed catalog latches the channel, and from then on a
signature is mandatory. A ``plugins`` list is taken from a verified catalog only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import signing
from .signing import Status

logger = logging.getLogger(__name__)

CHANNEL_MAIN = "main"
CHANNEL_BETA = "beta"

ORIGIN_RAW = "raw"
ORIGIN_MIRROR = "mirror"
ORIGIN_FLOOR = "floor"
ORIGINS = (ORIGIN_RAW, ORIGIN_MIRROR, ORIGIN_FLOOR)

SIGNED_CATALOG_PATH = "signed/{channel}/catalog.json"
SIGNED_ENVELOPE_PATH = "signed/{channel}/catalog.json.sig"
KEY_FILE_PATH = "keys/catalog-signing-keys.json"

SIGNATURE_VERIFIED = "verified"
SIGNATURE_LEGACY = "unsigned (legacy)"

FEEDS_KEY = "catalog_feeds"
COUNTER_FILE_NAME = "feed-counter.json"
_COUNTER_FILE_VERSION = 1

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class FeedRefused(ValueError):
    """A signed catalog was not accepted. The text says why and is safe to show."""

    def __init__(self, status: Status, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class FetchedFeed:
    """What one origin served for a channel: the signed bytes, their envelope and the key file.

    ``envelope`` and ``key_file`` are None when the origin has no such file.
    """

    catalog: bytes
    envelope: bytes | None
    key_file: bytes | None


@dataclass(frozen=True)
class ChannelRecord:
    """The numbers a host has accepted for one channel. None means nothing accepted yet."""

    signed_version: int | None = None
    key_file_version: int | None = None

    @property
    def latched(self) -> bool:
        return self.signed_version is not None


@dataclass(frozen=True)
class VerifiedFeed:
    """A catalog whose signature, chain, channel and freshness checked out."""

    catalog: dict[str, Any]
    channel: str
    source_commit: str
    result: signing.VerificationResult
    origin: str

    @property
    def may_advance_state(self) -> bool:
        return self.result.may_advance_state


def feed_channel(dev_ref: Any) -> str | None:
    """The signed channel an official source reads, or None when it reads none.

    The official source on its default branch (named or not) is ``main`` and on ``beta`` is ``beta``.
    Any other ``dev_ref`` points at a branch or commit that no signing run answers for, so it reads
    the legacy catalog only and never carries plugins.
    """
    if not dev_ref or dev_ref == CHANNEL_MAIN:
        return CHANNEL_MAIN
    if dev_ref == CHANNEL_BETA:
        return CHANNEL_BETA
    return None


def trusted_roots() -> Mapping[str, bytes]:
    from . import signing_roots

    return signing_roots.SIGNING_ROOTS


def signed_feed_enabled() -> bool:
    """Whether this build can verify a signed catalog at all.

    With no root baked in, nothing a catalog carries could verify, so a refresh does not ask for the
    signed files and reads the legacy catalog, the way every release before this one does.
    """
    return bool(trusted_roots())


def current_time() -> datetime:
    return datetime.now(timezone.utc)


def build_time() -> datetime | None:
    """When this release was built, from the image's build stamp, or None for a checkout."""
    try:
        from utilities.build import read_build_stamp

        text = read_build_stamp().get("date")
        if not text:
            return None
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        return moment.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# --- the counter, in the state document and in a file beside the database -------------------


def counter_file() -> Path:
    """``<config>/plugins/feed-counter.json``. Beside the state file when a test or a tool points it elsewhere."""
    override = os.environ.get("BAZARR_PROVIDER_HUB_STATE")
    if override:
        base = Path(override).parent
    else:
        try:
            from app.get_args import args

            base = Path(args.config_dir)
        except Exception:
            base = Path(".")
    return base / "plugins" / COUNTER_FILE_NAME


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def read_counter_file() -> dict[str, ChannelRecord]:
    """The mirrored counters. A missing or unreadable file holds nothing."""
    try:
        document = json.loads(counter_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("The catalog feed counter file could not be read, so it is ignored")
        return {}
    channels = document.get("channels") if isinstance(document, dict) else None
    records: dict[str, ChannelRecord] = {}
    for channel, item in (channels.items() if isinstance(channels, dict) else ()):
        if channel in (CHANNEL_MAIN, CHANNEL_BETA) and isinstance(item, dict):
            records[channel] = ChannelRecord(
                _positive_int(item.get("signed_version")), _positive_int(item.get("key_file_version")))
    return records


def write_counter_file(records: Mapping[str, ChannelRecord]) -> None:
    """Mirror the accepted counters, keeping the larger of what the file already holds.

    A failure is logged and goes no further: the database copy still holds the numbers.
    """
    path = counter_file()
    try:
        merged = {**read_counter_file()}
        for channel, record in records.items():
            merged[channel] = _larger(merged.get(channel), record)
        document = {
            "schema_version": _COUNTER_FILE_VERSION,
            "channels": {
                channel: {"signed_version": record.signed_version, "key_file_version": record.key_file_version}
                for channel, record in sorted(merged.items()) if record.signed_version is not None
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".feed-counter-", dir=str(path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except Exception:
        logger.warning("The catalog feed counter file could not be written", exc_info=True)


def _larger(first: ChannelRecord | None, second: ChannelRecord | None) -> ChannelRecord:
    first = first or ChannelRecord()
    second = second or ChannelRecord()

    def top(left: int | None, right: int | None) -> int | None:
        return right if left is None else left if right is None else max(left, right)

    return ChannelRecord(top(first.signed_version, second.signed_version),
                         top(first.key_file_version, second.key_file_version))


def feeds(state: Mapping[str, Any]) -> dict[str, Any]:
    value = state.get(FEEDS_KEY)
    return value if isinstance(value, dict) else {}


def stored_record(state: Mapping[str, Any], channel: str) -> ChannelRecord:
    """What this host has accepted for ``channel``: the larger of the database and the file.

    The key file is one file for every channel, so its version is the highest accepted on any of them.
    """
    file_records = read_counter_file()
    key_file_version = None
    signed_version = None
    for name in (CHANNEL_MAIN, CHANNEL_BETA):
        item = feeds(state).get(name)
        item = item if isinstance(item, dict) else {}
        database = ChannelRecord(
            _positive_int(item.get("signed_version")), _positive_int(item.get("key_file_version")))
        merged = _larger(database, file_records.get(name))
        key_file_version = _larger(
            ChannelRecord(None, key_file_version), ChannelRecord(None, merged.key_file_version)).key_file_version
        if name == channel:
            signed_version = merged.signed_version
    return ChannelRecord(signed_version, key_file_version)


def record_accepted(state: dict[str, Any], verified: VerifiedFeed, now: datetime) -> ChannelRecord:
    """Store an accepted catalog's numbers and times for its channel and return them.

    Only a catalog that may advance state gets here, so a baked snapshot never latches a channel.
    """
    result = verified.result
    store = state.setdefault(FEEDS_KEY, {})
    store[verified.channel] = {
        "signed_version": result.signed_version,
        "key_file_version": result.key_file_version,
        "source_commit": verified.source_commit,
        "not_before": _iso(result.not_before),
        "expires": _iso(result.expires),
        "signer_key_id": result.signer_key_id,
        "accepted_at": _iso(now),
        "clock_wrong": result.status is Status.CLOCK_WRONG,
    }
    return ChannelRecord(result.signed_version, result.key_file_version)


def record_refusal(state: dict[str, Any], channel: str, refusal: FeedRefused, now: datetime) -> None:
    """Remember why the last check of ``channel`` failed. The last good catalog and its counters stay."""
    store = state.setdefault(FEEDS_KEY, {})
    item = store.get(channel) if isinstance(store.get(channel), dict) else {}
    item["last_refusal"] = {"status": str(refusal.status), "message": refusal.message, "at": _iso(now)}
    store[channel] = item


def clear_refusal(state: dict[str, Any], channel: str) -> None:
    item = feeds(state).get(channel)
    if isinstance(item, dict):
        item.pop("last_refusal", None)


# --- verification --------------------------------------------------------------------------


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _refusal_text(result: signing.VerificationResult) -> str:
    status = result.status
    detail = result.detail
    if status is Status.UNSIGNED:
        return "The catalog is not signed, and this channel only accepts a signed catalog."
    if status is Status.BAD_SIGNATURE:
        return f"The catalog signature is not valid (bad signature). {detail}".strip()
    if status is Status.REVOKED_KEY:
        return f"The catalog was signed with a key that was revoked (revoked key). {detail}".strip()
    if status is Status.KEY_FILE_ROLLBACK:
        return f"The catalog's key file is older than the one this host accepted. {detail}".strip()
    if status is Status.REPLAYED:
        return f"The catalog is older than one this host already accepted (replayed). {detail}".strip()
    if status is Status.EXPIRED:
        return (f"The signed catalog has expired (expired). {detail} "
                "Check that this host's clock is right.").strip()
    if status is Status.NOT_YET_VALID:
        return (f"This host's clock looks wrong (clock wrong). {detail} "
                "Check that this host's clock is right.").strip()
    if status is Status.CHANNEL_MISMATCH:
        return f"The signed catalog is for another channel. {detail}".strip()
    return f"The signed catalog could not be read (malformed). {detail}".strip()


def verify_fetched(
    fetched: FetchedFeed | None,
    *,
    origin: str,
    channel: str,
    stored: ChannelRecord,
    now: datetime,
    roots: Mapping[str, bytes] | None = None,
    host_build_time: datetime | None = None,
) -> VerifiedFeed | None:
    """Check what an origin served for ``channel``.

    Returns the verified catalog, or None when the commit has no signed file and the channel has
    never accepted one (the caller then reads the legacy catalog). Raises ``FeedRefused`` for
    everything else. Nothing is parsed before the signature, the chain, the channel and the
    freshness checked out.

    ``origin`` is ``raw``, ``mirror`` or ``floor``. The floor is the snapshot baked into the
    image: it is checked for signature and chain only and never advances the stored counters.
    """
    if origin not in ORIGINS:
        raise ValueError(f"unknown catalog origin: {origin}")
    baked = origin == ORIGIN_FLOOR
    if fetched is None:
        if stored.latched and not baked:
            raise FeedRefused(Status.UNSIGNED, _refusal_text(signing.VerificationResult(Status.UNSIGNED)))
        return None
    if roots is None:
        roots = trusted_roots()
    result = signing.verify_catalog(
        fetched.catalog,
        fetched.envelope,
        fetched.key_file,
        roots=roots,
        channel=channel,
        now=now,
        stored_counter=None if baked else stored.signed_version,
        stored_key_file_version=None if baked else stored.key_file_version,
        host_build_time=None if baked else host_build_time,
        baked=baked,
    )
    if not result.usable:
        raise FeedRefused(result.status, _refusal_text(result))
    try:
        catalog = json.loads(fetched.catalog.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise FeedRefused(Status.MALFORMED, "The signed catalog could not be read (malformed).") from error
    if not isinstance(catalog, dict):
        raise FeedRefused(Status.MALFORMED, "The signed catalog could not be read (malformed).")
    source_commit = catalog.get("source_commit")
    if not isinstance(source_commit, str) or not _COMMIT_RE.match(source_commit):
        raise FeedRefused(
            Status.MALFORMED,
            "The signed catalog does not name the commit it was built from (malformed source_commit).")
    return VerifiedFeed(catalog=catalog, channel=channel, source_commit=source_commit, result=result,
                        origin=origin)


# --- what the signed state means for installs ------------------------------------------------


def _expiry_text(expires: Any, channel: str, now: datetime, build: datetime | None) -> str | None:
    if build is not None and now < build:
        return None
    try:
        moment = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    if now - moment > signing.CLOCK_SKEW:
        return (f"The signed {channel} catalog expired on {expires} (host time {_iso(now)}), so "
                "installs and updates are blocked until it is renewed. Plugins that are installed keep running.")
    return None


def expiry_refusal(state: Mapping[str, Any], channel: str | None, now: datetime | None = None,
                   host_build_time: datetime | None = None) -> str | None:
    """Why installs and updates from ``channel`` are blocked by its stored expiry, or None."""
    if channel is None:
        return None
    item = feeds(state).get(channel)
    if not isinstance(item, dict) or not item.get("expires"):
        return None
    return _expiry_text(item["expires"], channel, now or current_time(),
                        host_build_time if host_build_time is not None else build_time())


STALE_CATALOG_REASON = (
    "The catalog this host holds is older than one it already accepted for the {channel} channel, so nothing "
    "installs from it. Refresh the catalog to install again."
)


def supply_channel(source: Mapping[str, Any]) -> str | None:
    """The signed channel the official source's cached entries came from.

    Recorded by the refresh that stored them. A source stored before that was recorded falls back to the
    channel its ref asks for now.
    """
    if "catalog_channel" in source:
        return source.get("catalog_channel")
    return feed_channel(source.get("dev_ref"))


def supply_refusal(state: Mapping[str, Any], source: Mapping[str, Any], now: datetime | None = None,
                   host_build_time: datetime | None = None) -> str | None:
    """Why the official source's cached catalog cannot be installed from right now, or None.

    Judged on the channel that supplied the cached entries, never on the ref the source asks for now. A
    cached catalog expired by its own date, or older than what this host's database or counter file says it
    accepted (a restored database, a latch the cache never saw), is not installable until a fresh verified
    refresh replaces it. A catalog read before any signed one existed, on a channel that has never latched,
    installs as it always did.
    """
    channel = supply_channel(source)
    if channel is None:
        return None
    now = now or current_time()
    build = host_build_time if host_build_time is not None else build_time()
    item = feeds(state).get(channel)
    expires = source.get("catalog_expires_at") or (item.get("expires") if isinstance(item, dict) else None)
    if source.get("catalog_signature") == SIGNATURE_VERIFIED and expires:
        expired = _expiry_text(expires, channel, now, build)
        if expired:
            return expired
    stored = stored_record(state, channel)
    if not stored.latched:
        return None
    cached = source.get("catalog_signed_version")
    if source.get("catalog_signature") != SIGNATURE_VERIFIED or not isinstance(cached, int) \
            or cached < stored.signed_version:
        return STALE_CATALOG_REASON.format(channel=channel)
    return None
