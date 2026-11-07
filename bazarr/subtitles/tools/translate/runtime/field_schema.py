# coding=utf-8
"""Check a settings payload against a manifest's declared fields.

Pure functions. A field is whatever the signed manifest declares: a ``key``, a ``type`` out of the schema 2
setting types, and optional attributes the renderer and this check both read:

``label``, ``help``, ``help_url``, ``group``, ``placeholder``  display only.
``default``                  the value used while nothing is entered; also the value of a destination field
                             that needs no consent.
``required``                 an empty value is refused (a stored secret counts as a value).
``min`` / ``max``            limits of ``number`` and ``integer``.
``max_length``               limit of ``text``, ``options`` and the items of the list types.
``max_items``                limit of ``list``, ``key_list`` and ``orderable_list``.
``choices``                  the fixed options of a ``choice``: strings, or ``{"value", "label", "sets"}``
                             where ``sets`` is a preset: values for other fields.
``items``                    the allowed members of an ``orderable_list`` (strings, or ``{"value", "label"}``).
``show_if``                  ``{"key": k, "equals": v}`` or ``{"key": k, "in": [v, ...]}``: the field applies
                             only while that other field has that value. A hidden field is not required.
``destination``              the value says where requests go (see ``destinations.py``).
``secret``                   a ``key_list`` or ``list`` whose members are secrets (a ``secret`` type always is).

Nothing is coerced. A value of the wrong JSON type is refused with a message for that field, whatever the
field is called: a ``key_list`` given a string is an error, never a one-item list.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Mapping, Sequence

from .destinations import DestinationError, normalize_destination

TEXT_LIKE = frozenset({"text", "options"})
LIST_TYPES = frozenset({"list", "key_list", "orderable_list"})


def is_secret_field(definition: Mapping[str, Any]) -> bool:
    """True for a field that holds a credential: type ``secret``, or ``secret: true`` on a list."""
    return definition.get("type") == "secret" or definition.get("secret") is True


def choice_values(definition: Mapping[str, Any], key: str = "choices") -> list[Any]:
    return [item.get("value") if isinstance(item, Mapping) else item for item in definition.get(key) or []]


def field_visible(definition: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    """Whether the field applies given the other fields' values (``show_if``)."""
    condition = definition.get("show_if")
    if not isinstance(condition, Mapping):
        return True
    current = values.get(condition.get("key"))
    if "equals" in condition:
        return current == condition["equals"]
    if isinstance(condition.get("in"), list):
        return current in condition["in"]
    return True


@dataclass
class ValidatedValues:
    """The outcome of checking one payload."""

    settings: dict[str, Any] = dataclass_field(default_factory=dict)
    # Secret field -> the new value (a string, or a list of strings for the list types). Absent: unchanged.
    secrets: dict[str, Any] = dataclass_field(default_factory=dict)
    errors: dict[str, str] = dataclass_field(default_factory=dict)


def is_finite_number(value: Any) -> bool:
    """A real, finite number. Not a bool, not NaN or an infinity, and an integer small enough to store."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= 2 ** 53
    return isinstance(value, float) and math.isfinite(value)


_is_number = is_finite_number


def _check_text(definition, value) -> str | None:
    if not isinstance(value, str):
        return "must be text"
    limit = definition.get("max_length")
    if isinstance(limit, int) and len(value) > limit:
        return f"must be at most {limit} characters"
    return None


def _check_list(definition, value) -> str | None:
    if not isinstance(value, list):
        return "must be a list"
    if any(not isinstance(item, str) for item in value):
        return "must be a list of text values"
    if definition.get("type") != "list" and any(not item.strip() for item in value):
        return "must not hold an empty entry"
    max_items = definition.get("max_items")
    if isinstance(max_items, int) and len(value) > max_items:
        return f"must hold at most {max_items} entries"
    max_length = definition.get("max_length")
    if isinstance(max_length, int) and any(len(item) > max_length for item in value):
        return f"entries must be at most {max_length} characters"
    if definition.get("type") == "orderable_list":
        if len(set(value)) != len(value):
            return "must not repeat an entry"
        allowed = choice_values(definition, "items")
        if allowed and any(item not in allowed for item in value):
            return "holds an entry that is not offered"
    return None


def _address_problem(value: str) -> str | None:
    """Why a destination value is refused, in words that never repeat the value, or None when it is clean."""
    try:
        normalize_destination(value)
    except DestinationError as error:
        return error.message
    return None


def _check_value(definition: Mapping[str, Any], value: Any) -> str | None:
    kind = definition.get("type")
    if kind in TEXT_LIKE or kind == "secret":
        problem = _check_text(definition, value)
        # A user name or password in an address is a credential kept in clear text and shown back.
        if problem is None and definition.get("destination") is True and value.strip():
            return _address_problem(value)
        return problem
    if kind in LIST_TYPES:
        problem = _check_list(definition, value)
        if problem is None and definition.get("destination") is True:
            return next((found for item in value if item.strip() for found in [_address_problem(item)] if found),
                        None)
        return problem
    if kind == "toggle":
        return None if isinstance(value, bool) else "must be on or off"
    if kind == "number" or kind == "integer":
        if kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return "must be a whole number"
        elif not _is_number(value):
            return "must be a number"
        low, high = definition.get("min"), definition.get("max")
        if _is_number(low) and value < low:
            return f"must be at least {low}"
        if _is_number(high) and value > high:
            return f"must be at most {high}"
        return None
    if kind == "choice":
        if not isinstance(value, str):
            return "must be one of the offered choices"
        return None if value in choice_values(definition) else "must be one of the offered choices"
    return "has an unsupported type"


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def validate_values(fields: Sequence[Mapping[str, Any]], values: Mapping[str, Any], *,
                    stored_secrets: frozenset[str] = frozenset(),
                    extra_keys: frozenset[str] = frozenset()) -> ValidatedValues:
    """Check ``values`` against ``fields``.

    ``stored_secrets`` names the secret fields that already hold a value, so a required secret left
    empty keeps it. ``extra_keys`` are keys the caller owns and has already taken out of ``values``
    (the host-owned model and prices); they are not errors here.
    """
    result = ValidatedValues()
    declared = {item["key"]: item for item in fields}
    for key in values:
        if key not in declared and key not in extra_keys:
            result.errors[str(key)] = "is not a setting of this engine"
    merged = {key: definition.get("default") for key, definition in declared.items()}
    merged.update({key: value for key, value in values.items() if key in declared})
    for key, definition in declared.items():
        secret = is_secret_field(definition)
        value = values.get(key)
        # An empty secret keeps what is stored; Clear is a separate request.
        # An empty value of any other field is dropped, so its default applies again.
        provided = key in values and not (_is_empty(value) and definition.get("type") != "toggle")
        if provided:
            problem = _check_value(definition, value)
            if problem:
                result.errors[key] = problem
                continue
            if secret:
                result.secrets[key] = value
            else:
                result.settings[key] = value
        if definition.get("required") is True and field_visible(definition, merged):
            if secret:
                has = key in result.secrets or key in stored_secrets
            else:
                has = not _is_empty(result.settings.get(key, definition.get("default")))
            if not has:
                result.errors.setdefault(key, "is required")
    return result
