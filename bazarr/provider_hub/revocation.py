# coding=utf-8
"""The revocation list of the signed catalog: plugins and releases that must no longer be installed or run.

The list is the ``revocations`` array of the signed catalog body. It rides on the same bytes as the
plugins it concerns, so it is covered by the one signature check in ``catalog_feed`` (chain, channel,
expiry and replay counter), and nothing here verifies anything on its own. A catalog that fails that
check is refused whole, so its list is never read, and an unsigned catalog never carries one. What this
module holds is the shape of the list, how an entry matches a plugin and a version, where a host keeps
what it learnt, and the text a person reads when something is held back.

The wire format, which the catalog's feed builder must match exactly::

    "revocations": [
        {"id": "example", "version": "1.2.0", "reason": "It sends credentials to the wrong host.",
         "revoked_at": "2026-12-01T00:00:00Z"},
        {"id": "retired_plugin"}
    ]

* The field is optional. Absent, ``null`` or ``[]`` revoke nothing. Anything else that is not a list is an error.
* At most ``MAX_ENTRIES`` (2000) entries. The builder sorts them by ``id``, then ``version`` (no version first),
  so two builds of one list are the same bytes.
* Each entry is an object with only these fields, none of them ``null``: leave a field out instead. Any other field makes the host refuse the catalog, so a
  field that one day narrows an entry can never be read here as "every release".
* ``id`` (required): text matching ``^[a-z][a-z0-9_]{1,63}$``, the plugin id rule of the catalog's
  ``sdk/cli.py`` (``_PROVIDER_ID_RE``) and of the host's manifest parser.
* ``version`` (optional): one release, as text of 1 to 64 characters, not blank. Leave the field out to revoke
  every release of the plugin; ``null``, an empty string and a blank string are errors, never "every release".
* ``reason`` (optional): text for people, whitespace collapsed, at most 300 characters (a longer one is cut,
  a blank one is none). It is shown in
  a sentence cut to the host's 200 character message limit, so the part of the reason beyond what fits is
  not shown. Write the point first.
* ``revoked_at`` (optional): ``YYYY-MM-DDTHH:MM:SSZ`` in UTC. Recorded only.

A release matches an entry when its version text equals the entry's, or when both read as the same
release by this rule (``normalize_version``): a leading ``v`` and ``+build`` metadata are dropped, case is
ignored, and a numeric core is compared with trailing ``.0`` parts removed (``1.0`` is ``1.0.0``). A
pre-release written the PEP 440 way and the semantic version way compare equal when they name the same
stage and number: ``1.0.0rc1``, ``1.0.0-rc.1`` and ``1.0.0-RC1`` are one release, and so are ``alpha``
and ``a``, ``beta`` and ``b``, ``c``, ``pre`` and ``preview`` and ``rc``. Any other suffix (a ``.post1``, a
``.dev2``, a dotted pre-release such as ``rc.1.2``) matches only a release with the same suffix, lower-cased.
Text that is not a version at all matches only itself, case-insensitively.

A host keeps the list of every channel it has verified (``main`` and ``beta``), each with the
``signed_version`` that carried it, in the state document and in a small file beside the counter file.
It enforces the union of the channels. A channel's list is replaced only by a newer signed catalog of that
channel, so only that catalog lifts an entry, and of the two copies of one channel the one with the higher
``signed_version`` counts, so restoring an older database cannot lift anything the file remembers.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

FIELD_NAME = "revocations"
RECORD_KEY = "catalog_revocations"
MIRROR_FILE_NAME = "feed-revocations.json"
_MIRROR_FILE_VERSION = 1
CHANNELS = ("main", "beta")

MAX_ENTRIES = 2000
MAX_REASON = 300
MESSAGE_LIMIT = 200
_MAX_VERSION = 64
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)
_ENTRY_FIELDS = frozenset({"id", "version", "reason", "revoked_at"})
_MIN_REASON = 24
_MAX_NAME = 40


class RevocationFormatError(ValueError):
    """The revocation list of a signed catalog does not have the expected shape."""


@dataclass(frozen=True)
class Revocation:
    plugin_id: str
    # None revokes every release of the plugin.
    version: str | None = None
    reason: str | None = None
    revoked_at: str | None = None

    def as_record(self) -> dict[str, str]:
        optional = {"version": self.version, "reason": self.reason, "revoked_at": self.revoked_at}
        return {"id": self.plugin_id, **{name: value for name, value in optional.items() if value is not None}}


@dataclass(frozen=True)
class ChannelList:
    """The list one channel's last verified catalog carried, and the ``signed_version`` that carried it."""

    signed_version: int | None
    entries: tuple[Revocation, ...] = ()


def _text(entry: Mapping[str, Any], name: str, limit: int) -> str:
    value = entry[name]
    if not isinstance(value, str):
        raise RevocationFormatError(f"a revocation {name} must be text")
    value = " ".join(value.split())
    if not value:
        raise RevocationFormatError(f"a revocation {name} must not be blank")
    if len(value) > limit:
        if name != "reason":
            raise RevocationFormatError(f"a revocation {name} is longer than {limit} characters")
        value = value[: limit - 3] + "..."
    return value


def _has_text(entry: Mapping[str, Any], name: str) -> bool:
    """Whether an optional note is given. A blank reason says nothing, which is not an error."""
    value = entry.get(name)
    return name in entry and not (isinstance(value, str) and not value.strip())


def parse_entry(entry: Any) -> Revocation:
    if not isinstance(entry, Mapping):
        raise RevocationFormatError("each revocation must be an object")
    unknown = sorted(str(name) for name in entry if name not in _ENTRY_FIELDS)
    if unknown:
        raise RevocationFormatError(f"a revocation has a field this host does not know: {', '.join(unknown)[:60]}")
    plugin_id = entry.get("id")
    if not isinstance(plugin_id, str) or not _ID_RE.fullmatch(plugin_id):
        raise RevocationFormatError("each revocation needs the id of the plugin it revokes, in the plugin id format")
    revoked_at = _text(entry, "revoked_at", _MAX_VERSION) if "revoked_at" in entry else None
    if revoked_at is not None and not _TIMESTAMP_RE.fullmatch(revoked_at):
        raise RevocationFormatError("a revocation revoked_at must look like 2026-12-31T00:00:00Z")
    return Revocation(
        plugin_id=plugin_id,
        version=_text(entry, "version", _MAX_VERSION) if "version" in entry else None,
        reason=_text(entry, "reason", MAX_REASON) if _has_text(entry, "reason") else None,
        revoked_at=revoked_at,
    )


def parse(value: Any) -> tuple[Revocation, ...]:
    """The list a verified catalog body carries. Absent means none. Any entry that cannot be read raises.

    A host cannot tell what an unreadable entry would have revoked, so the caller refuses the whole
    catalog instead of skipping the entry.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RevocationFormatError("revocations must be a list")
    if len(value) > MAX_ENTRIES:
        raise RevocationFormatError(f"revocations holds at most {MAX_ENTRIES} entries")
    return tuple(parse_entry(item) for item in value)


# --- what a host keeps -----------------------------------------------------------------------------------


def _channel_from(item: Any) -> ChannelList | None:
    if not isinstance(item, Mapping):
        return None
    version = item.get("signed_version")
    version = version if isinstance(version, int) and not isinstance(version, bool) and version >= 1 else None
    entries = []
    for raw in item.get("entries") if isinstance(item.get("entries"), list) else ():
        try:
            entries.append(parse_entry(raw))
        except RevocationFormatError:
            continue
    return ChannelList(version, tuple(entries))


def _lists_from(record: Any) -> dict[str, ChannelList]:
    channels = record.get("channels") if isinstance(record, Mapping) else None
    found = {}
    for channel, item in channels.items() if isinstance(channels, Mapping) else ():
        parsed = _channel_from(item) if channel in CHANNELS else None
        if parsed is not None:
            found[channel] = parsed
    return found


def from_source(source: Any) -> dict[str, ChannelList]:
    """The lists this host verified, by channel, from the official source record. Nothing stored means none."""
    return _lists_from(source.get(RECORD_KEY) if isinstance(source, Mapping) else None)


def to_record(held: Any, channel: str, signed_version: int | None, entries: Iterable[Revocation]) -> dict[str, Any]:
    """The source record after a verified catalog of ``channel`` carried ``entries``: that channel's list is
    replaced, the other channel's is kept as it was."""
    channels = dict(held.get("channels")) if isinstance(held, Mapping) and isinstance(held.get("channels"), Mapping) \
        else {}
    channels[channel] = {"signed_version": signed_version, "entries": [item.as_record() for item in entries]}
    return {"channels": channels}


def _newer(first: ChannelList | None, second: ChannelList | None) -> ChannelList | None:
    if first is None or second is None:
        return first or second
    if (second.signed_version or 0) > (first.signed_version or 0):
        return second
    return first


def effective(*copies: Mapping[str, ChannelList]) -> dict[str, ChannelList]:
    """Per channel, the copy with the higher ``signed_version``. A copy a restore took back loses to the other."""
    merged: dict[str, ChannelList] = {}
    for copy in copies:
        for channel, held in copy.items():
            merged[channel] = _newer(merged.get(channel), held)
    return merged


def entries_of(lists: Mapping[str, ChannelList]) -> tuple[Revocation, ...]:
    """What the host enforces: the union of the channels' entries."""
    seen: dict[Revocation, None] = {}
    for held in lists.values():
        for item in held.entries:
            seen.setdefault(item)
    return tuple(seen)


def mirror_path() -> Path:
    """``<config>/plugins/feed-revocations.json``, beside the counter file."""
    from . import catalog_feed

    return catalog_feed.counter_file().with_name(MIRROR_FILE_NAME)


_MIRROR_CACHE: tuple[tuple[str, int, int, int], dict[str, ChannelList]] | None = None


def read_mirror() -> dict[str, ChannelList]:
    """The lists mirrored outside the database. A missing or unreadable file holds nothing."""
    global _MIRROR_CACHE
    path = mirror_path()
    try:
        info = path.stat()
    except OSError:
        return {}
    key = (str(path), info.st_mtime_ns, info.st_size, info.st_ino)
    cached = _MIRROR_CACHE
    if cached is not None and cached[0] == key:
        return dict(cached[1])
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Remembered until the file changes, so a lookup per catalog entry reads it and says so once.
        _MIRROR_CACHE = (key, {})
        logger.warning("The mirrored revocation lists could not be read, so they are ignored")
        return {}
    lists = _lists_from(document)
    _MIRROR_CACHE = (key, lists)
    return dict(lists)


def _sync_directory(directory: Path) -> None:
    """Make the replacement durable too. A platform that cannot open a directory skips it."""
    try:
        handle = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def write_mirror(lists: Mapping[str, ChannelList]) -> None:
    """Mirror verified lists, keeping per channel the one with the higher ``signed_version``.

    Written atomically under a lock, like the counter file. A failure is logged and goes no further: the
    database copy still holds the lists.
    """
    from .state import process_lock

    path = mirror_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with process_lock(path.parent / ".feed-revocations.lock", wait=10.0) as held:
            if not held:
                logger.warning("The revocation list mirror was not written: its lock could not be taken")
                return
            merged = effective(read_mirror(), lists)
            document = {
                "schema_version": _MIRROR_FILE_VERSION,
                "channels": {
                    channel: {"signed_version": item.signed_version, "entries": [e.as_record() for e in item.entries]}
                    for channel, item in sorted(merged.items())
                },
            }
            handle, temporary = tempfile.mkstemp(prefix=".feed-revocations-", dir=str(path.parent))
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(document, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                _sync_directory(path.parent)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
    except Exception:
        logger.warning("The revocation list mirror could not be written", exc_info=True)


# --- matching --------------------------------------------------------------------------------------------

_CORE_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$", re.IGNORECASE)
_STAGE_RE = re.compile(r"^(alpha|a|beta|b|preview|pre|rc|c)[-_.]?(\d+)?$")
_STAGES = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "preview": "rc", "pre": "rc", "rc": "rc", "c": "rc"}


def normalize_version(version: Any) -> tuple[Any, ...] | None:
    """A version reduced to what makes two spellings one release, or None for text that is not a version."""
    text = str(version if version is not None else "").strip().split("+", 1)[0]
    found = _CORE_RE.match(text)
    if not found:
        return None
    core = [int(part) for part in found.group(1).split(".")]
    while len(core) > 1 and core[-1] == 0:
        core.pop()
    rest = found.group(2).lstrip("-_.").lower() if found.group(2) else ""
    if not rest:
        return (tuple(core), None)
    stage = _STAGE_RE.match(rest)
    if stage:
        return (tuple(core), (_STAGES[stage.group(1)], int(stage.group(2) or 0)))
    return (tuple(core), ("other", rest))


def same_version(left: Any, right: Any) -> bool:
    """Whether two version texts name one release by the rule in the module description."""
    if left is None or right is None:
        return False
    first, second = str(left).strip().lower(), str(right).strip().lower()
    if first == second:
        return True
    left_key, right_key = normalize_version(left), normalize_version(right)
    return left_key is not None and left_key == right_key


def lookup(entries: Iterable[Revocation], plugin_id: Any, version: Any = None) -> Revocation | None:
    """The entry that revokes ``version`` of ``plugin_id``, or None.

    With no version, only an entry that revokes every release of the plugin matches: a caller that has no
    release in hand asks whether the plugin as a whole may be used.
    """
    if not isinstance(plugin_id, str) or not plugin_id:
        return None
    found = None
    for item in entries:
        if item.plugin_id != plugin_id:
            continue
        if item.version is None:
            return item
        if found is None and same_version(item.version, version):
            found = item
    return found


# --- what a person reads ---------------------------------------------------------------------------------


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 3, 0)].rstrip() + "..."


def _compose(subject: str, entry: Revocation, tail: str, limit: int) -> str:
    """``subject was revoked by the official catalog[: reason]`` plus ``tail``, within ``limit`` characters.

    The reason is the only part that is cut. It is cut with an ellipsis, and left out when what room is left
    would show too little of it, so the sentence and what to do next always come whole. A reason that ends
    with its own ``!`` or ``?``, or with the ellipsis of a cut, is not given a second stop.
    """
    base = f"{subject} was revoked by the official catalog"
    reason = (entry.reason or "").strip().rstrip(".").strip()
    room = limit - len(base) - len(tail) - 2
    if not reason or room < _MIN_REASON:
        return f"{base}{tail}"
    shown = _cut(reason, room)
    if shown[-1] in "!?" or shown.endswith("..."):
        # The reason already ends with a stop: "wrong host!" then "It will not start." and
        # "wrong host! So it cannot be installed.", and a cut reason "wrong ho..." then "It will not start."
        tail = " So " + tail[5:] if tail.startswith(", so ") else tail[1:]
    return f"{base}: {shown}{tail}"


def _subject(entry: Revocation, name: str, version: Any) -> str:
    name = _cut(str(name), _MAX_NAME)
    if entry.version is None:
        return f"Every version of {name}"
    return f"{name} {_cut(str(version or entry.version), 24)}"


def refusal(entry: Revocation, name: str, version: Any, action: str, limit: int = MESSAGE_LIMIT) -> str:
    """Why ``action`` ("installed", "offered as an update" or "switched on") is refused."""
    return _compose(_subject(entry, name, version), entry, f", so it cannot be {action}.", limit)


# What follows the revocation in each state of the installed release.
HELD = "held"          # it does not start
RUNNING = "running"    # it still runs until the next restart
STAGED = "staged"      # a release that is not revoked waits for the restart


def _tail(entry: Revocation, state: str) -> str:
    # With every release revoked there is none to update to, so the only way out is to remove it.
    if state == HELD:
        return ". It will not start. " + ("Update it or remove it." if entry.version else "Remove it.")
    if state == RUNNING:
        return ". Restart Bazarr+ to stop it, then " + ("update or remove it." if entry.version else "remove it.")
    return ". Restart Bazarr+ to apply the staged update."


def held_back(entry: Revocation, name: str, version: Any, state: str = HELD, limit: int = MESSAGE_LIMIT) -> str:
    """What an installed revoked release shows: that it is revoked, and what to do next."""
    return _compose(_subject(entry, name, version), entry, _tail(entry, state), limit)


def with_reason(entry: Revocation, reason: str | None) -> Revocation:
    return replace(entry, reason=reason)
