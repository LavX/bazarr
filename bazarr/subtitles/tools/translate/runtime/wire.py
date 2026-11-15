# coding=utf-8
"""The translator kind's request and result, as the host writes and reads them.

This is the one place on the host that knows the key names. The shapes follow the translator plugin's wire
module: a request carries the job, both languages (code, name, script, right-to-left), the lines as position
and text, the media context, the engine's settings with the one connection this attempt uses, the
checkpoints of earlier attempts and the cost budget. Secrets are not part of it: they travel in the worker
session's own ``secrets`` object, for this request only.

Pure functions. Nothing here touches a database, a worker or a setting.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .failure import INTERNAL, AttemptFailure
from .request import LanguageSpec

# The keys the plugin SDK reserves for how a result travels (see the SDK's result module): a result that is
# not an object comes wrapped, and one too large for the control frame comes as data with a marker.
WRAPPED_KEY = "__bazarr_sdk_value__"
IN_DATA_KEY = "__bazarr_sdk_json_in_data__"

COMPLETE = "complete"
PARTIAL = "partial"


def build_request(*, job_id: str, source: LanguageSpec, target: LanguageSpec, lines: Sequence[str],
                  media: Mapping[str, Any] | None, engine_settings: Mapping[str, Any],
                  connection: Mapping[str, Any] | None, checkpoints: Sequence[Mapping[str, Any]],
                  budget: float | None) -> dict[str, Any]:
    """The ``request`` argument of a translator ``translate`` call, without its secrets."""
    config: dict[str, Any] = {"settings": dict(engine_settings)}
    if connection is not None:
        config["connection"] = dict(connection)
    return {
        "job_id": job_id,
        "source": source.to_wire(),
        "target": target.to_wire(),
        "lines": [{"position": position, "text": text} for position, text in enumerate(lines)],
        "media": dict(media or {}),
        "config": config,
        "checkpoints": [dict(item) for item in checkpoints],
        "budget": budget,
    }


def connection_wire(connection, settings: Mapping[str, Any]) -> dict[str, Any]:
    """A stored connection as the request carries it.

    The model and the two prices are columns of their own on the host, but they are settings fields like any
    other to the plugin, which needs the prices to compute a call's worst-case charge.
    """
    return {
        "id": str(connection.id),
        "type": connection.backend_type,
        "enabled": bool(connection.enabled),
        "settings": {**dict(settings), "model": connection.model,
                     "price_input_per_million": connection.price_input_per_million,
                     "price_output_per_million": connection.price_output_per_million},
    }


@dataclass
class EngineResult:
    """What one attempt translated, read out of the plugin's answer."""

    status: str
    lines: dict[int, str]
    missing: list[int] = field(default_factory=list)
    model: str = ""
    tokens: int = 0
    cost: float = 0.0
    batch_models: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return self.status == PARTIAL


def _invalid(message: str) -> AttemptFailure:
    return AttemptFailure(INTERNAL, f"The plugin's answer is not valid: {message}", reason="invalid_result")


def unwrap(result, data: bytes = b"") -> Any:
    """The plugin's own return value from what the session handed back."""
    value: Any = result
    if isinstance(value, Mapping) and value.get(IN_DATA_KEY) is True:
        try:
            value = json.loads(data.decode("utf-8")) if data else None
        except (UnicodeDecodeError, ValueError):
            raise _invalid("its data does not decode") from None
    if isinstance(value, Mapping) and WRAPPED_KEY in value:
        value = value[WRAPPED_KEY]
    return value


def read_result(result, data: bytes, expected_lines: int) -> EngineResult:
    """Read and check the answer of ``translate``. Raises ``AttemptFailure`` (internal) when it is malformed."""
    value = unwrap(result, data)
    if not isinstance(value, Mapping):
        raise _invalid("it is not an object")
    status = value.get("status", COMPLETE)
    if status not in (COMPLETE, PARTIAL):
        raise _invalid("its status is neither complete nor partial")
    raw_lines = value.get("lines")
    if not isinstance(raw_lines, list):
        raise _invalid("it has no lines")
    lines: dict[int, str] = {}
    for item in raw_lines:
        if not isinstance(item, Mapping):
            raise _invalid("a line is not an object")
        position, text = item.get("position"), item.get("text")
        if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position < expected_lines:
            raise _invalid("a line has a position outside the request")
        if not isinstance(text, str):
            raise _invalid("a line has no text")
        if position in lines:
            raise _invalid("a position came back twice")
        lines[position] = text
    missing = value.get("missing") or []
    if (not isinstance(missing, list)
            or any(isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < expected_lines
                   for item in missing)):
        raise _invalid("its list of missing lines is not a list of positions")
    models = [str(batch.get("model")) for batch in (value.get("batches") or [])
              if isinstance(batch, Mapping) and batch.get("model")]
    tokens = value.get("tokens")
    cost = value.get("cost")
    return EngineResult(
        status=status, lines=lines, missing=sorted(set(missing)), model=str(value.get("model") or ""),
        tokens=tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 else 0,
        cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0 else 0.0,
        batch_models=models)
