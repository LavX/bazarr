# coding=utf-8
"""The revocation list of the signed catalog: plugins and releases that must no longer be installed or run.

The list is the ``revocations`` array of the signed catalog body. It rides on the same bytes as the
plugins it concerns, so it is covered by the one signature check in ``catalog_feed`` (chain, channel,
expiry and replay counter), and nothing here verifies anything on its own. A catalog that fails that
check is refused whole, so its list is never read, and an unsigned catalog never carries one. What this
module holds is the shape of the list, how an entry matches a plugin and a version, and the text a
person reads when something is held back.

An entry names a plugin by ``id`` and, optionally, one ``version``. With no version it revokes every
release of that plugin. ``reason`` and ``revoked_at`` are optional and only ever shown or recorded::

    "revocations": [
        {"id": "example", "version": "1.2.0", "reason": "It sends credentials to the wrong host.",
         "revoked_at": "2026-12-01T00:00:00Z"},
        {"id": "retired-plugin"}
    ]

The host keeps the list from the last catalog it verified, on the official source record. A later catalog
that no longer lists an entry lifts it, because only a newer signed catalog can say so.
"""
from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .versions import version_key

FIELD_NAME = "revocations"
RECORD_KEY = "catalog_revocations"

MAX_ENTRIES = 2000
_MAX_ID = 128
_MAX_VERSION = 64
_MAX_REASON = 300
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)


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


def _optional_text(entry: Mapping[str, Any], name: str, limit: int) -> str | None:
    value = entry.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RevocationFormatError(f"a revocation {name} must be text")
    value = " ".join(value.split())
    if not value:
        return None
    if len(value) > limit:
        if name != "reason":
            raise RevocationFormatError(f"a revocation {name} is longer than {limit} characters")
        value = value[: limit - 3] + "..."
    return value


def parse_entry(entry: Any) -> Revocation:
    if not isinstance(entry, Mapping):
        raise RevocationFormatError("each revocation must be an object")
    plugin_id = entry.get("id")
    if not isinstance(plugin_id, str) or not plugin_id or plugin_id != plugin_id.strip() or len(plugin_id) > _MAX_ID:
        raise RevocationFormatError("each revocation needs the id of the plugin it revokes")
    revoked_at = _optional_text(entry, "revoked_at", _MAX_VERSION)
    if revoked_at is not None and not _TIMESTAMP_RE.fullmatch(revoked_at):
        raise RevocationFormatError("a revocation revoked_at must look like 2026-12-31T00:00:00Z")
    return Revocation(
        plugin_id=plugin_id,
        version=_optional_text(entry, "version", _MAX_VERSION),
        reason=_optional_text(entry, "reason", _MAX_REASON),
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


def to_record(channel: str, signed_version: int | None, entries: Iterable[Revocation]) -> dict[str, Any]:
    """What the official source record keeps of the list a verified catalog carried."""
    return {"channel": channel, "signed_version": signed_version, "entries": [item.as_record() for item in entries]}


def from_source(source: Any) -> tuple[Revocation, ...]:
    """The list this host last verified, from the official source record. Nothing stored means none."""
    record = source.get(RECORD_KEY) if isinstance(source, Mapping) else None
    items = record.get("entries") if isinstance(record, Mapping) else None
    found = []
    for item in items if isinstance(items, list) else ():
        try:
            found.append(parse_entry(item))
        except RevocationFormatError:
            continue
    return tuple(found)


def same_version(left: Any, right: Any) -> bool:
    """Whether two version strings name one release: equal text, or equal by version order (``1.0`` is ``1.0.0``)."""
    if left is None or right is None:
        return False
    if str(left).strip() == str(right).strip():
        return True
    left_key, right_key = version_key(left), version_key(right)
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


def _subject(entry: Revocation, name: str, version: Any) -> str:
    if entry.version is None:
        return f"Every version of {name}"
    return f"Version {version or entry.version} of {name}"


def _why(entry: Revocation) -> str:
    if not entry.reason:
        return ""
    return f": {entry.reason.rstrip('.')}"


def refusal(entry: Revocation, name: str, version: Any, action: str) -> str:
    """Why ``action`` ("installed", "updated to" or "switched on") is refused, in words a person reads."""
    return f"{_subject(entry, name, version)} was revoked by the official catalog{_why(entry)}, so it cannot be {action}."


def held_back(entry: Revocation, name: str, version: Any) -> str:
    """What an installed release shows once it is revoked: it does not run, and what to do next."""
    return (f"{_subject(entry, name, version)} was revoked by the official catalog{_why(entry)}. It will not "
            "start again. Update it to a newer version when one is listed, or remove it.")
