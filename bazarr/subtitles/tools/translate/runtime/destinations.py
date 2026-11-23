# coding=utf-8
"""Effective destinations: where an engine instance or a connection sends requests.

A secret belongs to the destinations its owner had when the secret was entered.
The set is the values, defaults applied, of the settings fields the manifest marks
as destinations (``"destination": true``), whatever those fields are called, plus
the fixed ``endpoints`` origins of the back-end type or plugin. A change that adds
a value the set did not hold is a new destination, and the stored secrets of that
owner stop applying until they are entered again.

Pure functions. Nothing here touches the database or the settings.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class DestinationSchema:
    """What decides an owner's destinations: its manifest fields and fixed origins."""

    fields: tuple[Mapping[str, Any], ...] = ()
    endpoints: tuple[str, ...] = ()

    @property
    def secret_fields(self) -> frozenset[str]:
        from .field_schema import is_secret_field

        return frozenset(item["key"] for item in self.fields if is_secret_field(item))


class DestinationError(ValueError):
    """A destination that cannot be accepted. The message is fixed and never carries the value."""

    CREDENTIALS = "must not contain a user name or password"
    MALFORMED = "must be a web address that starts with http:// or https://"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def normalize_destination(value: Any) -> str:
    """The one place a destination value is parsed. Returns it in one spelling, or raises ``DestinationError``.

    An address is a clean http or https URL with a host: nothing that urllib cannot split (a bad port, an
    unclosed bracket), no scheme other than http and https, no blanks or control characters, and no user
    name or password. The scheme and host come back in lower case. A path and a trailing slash stay as
    typed: two spellings that differ there are two destinations, which can only ask for a secret again,
    never hand one out. An empty value is ``""``; the caller decides what that means.
    """
    text = str(value).strip()
    if not text:
        return ""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise DestinationError(DestinationError.MALFORMED)
    try:
        parts = urlsplit(text)
        parts.port  # reading it is the check: a port that is not a number raises
        host = parts.hostname
    except ValueError:
        raise DestinationError(DestinationError.MALFORMED) from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise DestinationError(DestinationError.MALFORMED)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise DestinationError(DestinationError.CREDENTIALS)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment))


def _unusable(text: str) -> str:
    """Stands in for a stored or default value that is not a clean address.

    It matches nothing a secret was ever bound to, so a secret is never handed out for it, and it does
    not carry the value (which may hold a password).
    """
    return "invalid:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _is_destination(field: Mapping[str, Any]) -> bool:
    return field.get("destination") is True


def _values(value: Any) -> Iterable[Any]:
    if isinstance(value, (list, tuple)):
        return value
    return (value,)


def _effective(item: Any) -> str:
    try:
        return normalize_destination(item)
    except DestinationError:
        return _unusable(str(item).strip())


def effective_destinations(schema: DestinationSchema, settings: Mapping[str, Any] | None) -> frozenset[str]:
    """The destinations of one owner: its marked fields' values with defaults applied, and its origins."""
    settings = settings or {}
    found: set[str] = set()
    for field in schema.fields:
        if not _is_destination(field):
            continue
        key = field.get("key")
        value = settings.get(key) if isinstance(key, str) else None
        if value is None or value == "" or value == []:
            value = field.get("default")
        for item in _values(value):
            if item is None:
                continue
            found.add(_effective(item))
    for origin in schema.endpoints:
        found.add(_effective(origin))
    found.discard("")
    return frozenset(found)


def added_destinations(before: Iterable[str], after: Iterable[str]) -> frozenset[str]:
    """The destinations in ``after`` that ``before`` did not hold."""
    return frozenset(after) - frozenset(before)


def covers(bound: Iterable[str], current: Iterable[str]) -> bool:
    """True when every current destination is one the secret was bound to."""
    return frozenset(current) <= frozenset(bound)


def schema_for_engine(manifest: Mapping[str, Any]) -> DestinationSchema:
    """The destination schema of an engine instance, from a schema 2 manifest."""
    from provider_hub.plugin_manifest import parse_plugin_manifest

    plugin = parse_plugin_manifest(dict(manifest))
    return DestinationSchema(fields=plugin.settings_fields, endpoints=plugin.endpoints)


def schema_for_backend(manifest: Mapping[str, Any], backend_type: str) -> DestinationSchema:
    """The destination schema of a connection of one back-end type, from the manifest that declares it."""
    from provider_hub.plugin_manifest import parse_plugin_manifest

    plugin = parse_plugin_manifest(dict(manifest))
    fields = dict(plugin.backend_fields).get(backend_type, ())
    endpoints = dict(plugin.backend_endpoints).get(backend_type, ())
    return DestinationSchema(fields=fields, endpoints=endpoints)
