# coding=utf-8
"""The model and the two prices of a back-end connection, and the one way a connection's values are checked.

The host keeps these three in columns of their own, outside the settings JSON, but they are fields like any
other: a manifest may declare them (limits, a default, required, a condition on another field), and a field
may be shown only for one model. Every place that checks a connection's values (a save, a create, turning it
on, a plugin update that activates a new release) goes through ``validate_connection``, with the model and
prices as they will be stored, so they all agree.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .field_schema import ValidatedValues, is_finite_number, validate_values

MODEL_KEY = "model"
PRICE_KEYS = ("price_input_per_million", "price_output_per_million")
HOST_KEYS = frozenset({MODEL_KEY, *PRICE_KEYS})

# The fields every back-end type gets, ahead of what the manifest declares. A manifest that declares its own
# ``model`` field keeps its declaration (label, help, limits) and only loses the duplicate.
HOST_FIELDS: tuple[dict[str, Any], ...] = (
    {"key": MODEL_KEY, "type": "options", "label": "Model", "host": True},
    {"key": PRICE_KEYS[0], "type": "number", "label": "Input price per million tokens", "min": 0, "default": 0,
     "group": "Prices", "host": True},
    {"key": PRICE_KEYS[1], "type": "number", "label": "Output price per million tokens", "min": 0, "default": 0,
     "group": "Prices", "host": True},
)


def host_definitions(fields: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """The effective definition of each host field: the manifest's own when it declares one, else the host's."""
    declared = {item["key"]: item for item in fields if item["key"] in HOST_KEYS}
    return {item["key"]: declared.get(item["key"], item) for item in HOST_FIELDS}


def host_default(definitions: Mapping[str, Mapping[str, Any]], key: str):
    found = definitions[key].get("default")
    if key == MODEL_KEY:
        return found if isinstance(found, str) else ""
    return found if is_finite_number(found) and found >= 0 else 0


def host_of(row) -> dict[str, Any]:
    """The model and prices of a stored connection, as the columns hold them."""
    return {MODEL_KEY: row.model, PRICE_KEYS[0]: row.price_input_per_million,
            PRICE_KEYS[1]: row.price_output_per_million}


def validate_connection(fields: Sequence[Mapping[str, Any]], settings: Mapping[str, Any],
                        host: Mapping[str, Any], stored_secrets: frozenset[str] = frozenset(),
                        stored_secret_values: Mapping[str, Any] | None = None) -> ValidatedValues:
    """Check a connection's settings and its host-kept model and prices together.

    ``settings`` are the values of the fields kept in the settings JSON; ``host`` is the model and prices as they
    will be stored. Each side reads the other for its conditions. The result's settings and secrets are the
    JSON side only; the errors cover both.
    """
    plain_fields = [item for item in fields if item["key"] not in HOST_KEYS]
    result = validate_values(plain_fields, settings, stored_secrets=stored_secrets, context=host,
                             stored_secret_values=stored_secret_values)
    defaults = {item["key"]: item.get("default") for item in plain_fields}
    context = {**defaults, **result.settings}
    checked = validate_values(list(host_definitions(fields).values()), dict(host), context=context)
    for key, message in checked.errors.items():
        result.errors.setdefault(key, message)
    return result


def validate_owner(fields: Sequence[Mapping[str, Any]], settings: Mapping[str, Any],
                   stored_secrets: frozenset[str] = frozenset(),
                   host: Mapping[str, Any] | None = None,
                   stored_secret_values: Mapping[str, Any] | None = None) -> ValidatedValues:
    """The one entry point for checking what an engine instance or a connection holds.

    ``host`` is the model and prices of a connection; an engine has none. A save, a create, turning something on
    and a plugin update that activates a new release all call this, so they cannot disagree.
    """
    if host is None:
        return validate_values(fields, settings, stored_secrets=stored_secrets,
                               stored_secret_values=stored_secret_values)
    return validate_connection(fields, settings, host, stored_secrets, stored_secret_values)
