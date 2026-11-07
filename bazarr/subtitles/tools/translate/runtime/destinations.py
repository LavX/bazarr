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

from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class DestinationSchema:
    """What decides an owner's destinations: its manifest fields and fixed origins."""

    fields: tuple[Mapping[str, Any], ...] = ()
    endpoints: tuple[str, ...] = ()


def normalize_destination(value: Any) -> str:
    """A destination value in one spelling: trimmed, with the scheme and host in lower case.

    A value that is not a URL is kept as typed (trimmed). A trailing slash and a
    path stay as they are: two spellings that differ there are two destinations,
    which can only ask for a secret again, never hand one out.
    """
    text = str(value).strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.netloc:
        return text
    host = parts.netloc
    userinfo, at, hostport = host.rpartition("@")
    return urlunsplit((parts.scheme.lower(), userinfo + at + hostport.lower(), parts.path, parts.query, parts.fragment))


def _is_destination(field: Mapping[str, Any]) -> bool:
    return field.get("destination") is True


def _values(value: Any) -> Iterable[Any]:
    if isinstance(value, (list, tuple)):
        return value
    return (value,)


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
            text = normalize_destination(item)
            if text:
                found.add(text)
    for origin in schema.endpoints:
        text = normalize_destination(origin)
        if text:
            found.add(text)
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
