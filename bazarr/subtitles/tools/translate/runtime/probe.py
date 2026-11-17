# coding=utf-8
"""The two calls the Translation page makes to a plugin's worker: a connection test and a model list.

Both are the plugin's own ``test`` and ``options`` methods, sent through the worker port the dispatcher uses.
While no worker is bound the page does not offer the test; one that is refused before it is sent, whatever the reason, is
settled at nothing.

Where a probe runs. Every ``test`` and ``options`` request goes to a worker started for that one call (see
``workers.PoolWorkers.call``), never to the long-lived worker that serves translations. The page asks about drafts that are
not saved, so the address can name a port the long-lived worker was not started with, and a probe that outlives its deadline
against a plugin that ignores the cancel ends the worker it runs in.

Secrets. A test sends what a translation would: the engine's stored secrets and the connection's, each only to
the addresses it was entered under. The caller (``TranslatorSettingsService.test_connection``) cuts them and takes
the settings they were cut against in one pass, and the probe sends that snapshot and reads nothing again. A
model list is asked for with a draft the person has not saved, so it carries no stored secret at all: an engine
that needs a key for its list answers nothing, and the page offers a text field instead of a list.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from . import wire, workers
from .settings_service import OPEN_TEST, ProbeRequest

logger = logging.getLogger(__name__)

# What a plugin's answer to ``test`` may carry back to the page. Nothing else is read.
_TOKEN_KEYS = ("input_tokens", "output_tokens")


BUSY_TEST = "Other tests are running right now. Try the test again in a moment."
UNSTARTED_TEST = "The translation engine could not take the test. Try again in a moment."


def worker_probe(request: ProbeRequest) -> Mapping[str, Any]:
    """Ask the connection's plugin to check the credentials and model with one small request.

    The request is the whole snapshot: the settings, address, prices and secrets the caller checked together.
    Nothing is read again here, so the key that is sent is always the one cut for the address it goes to.

    Returns the answer ``TranslatorSettingsService`` reads: ``ok``, a ``message``, the tokens the request used
    when the plugin reports them, and ``sent: false`` for every refusal that happened before anything left
    Bazarr+ (no worker, too many one-call workers at once, a request that could not be encoded, one that would not
    start), so a priced test is settled at nothing.
    """
    port = workers.bound_port()
    if not workers.is_bound(port):
        return {"ok": False, "sent": False, "message": OPEN_TEST}
    config = {
        "settings": dict(request.engine_values),
        "connection": {"id": str(request.connection_id), "type": request.backend_type, "enabled": True,
                       "settings": {**dict(request.values), "model": request.model,
                                    "price_input_per_million": request.price_input_per_million,
                                    "price_output_per_million": request.price_output_per_million}},
        "limits": {"input_tokens": request.input_token_limit, "output_tokens": request.output_token_limit},
    }
    try:
        outcome = port.call(request.plugin_id, workers.TEST_OP, {"config": config}, dict(request.secrets),
                            timeout=request.timeout_seconds)
    except workers.NotSent as refused:
        return {"ok": False, "sent": False, "message": BUSY_TEST if refused.busy else UNSTARTED_TEST}
    value = wire.unwrap(outcome.result, outcome.data)
    if not isinstance(value, Mapping):
        return {"ok": False, "message": ""}
    answer: dict[str, Any] = {"ok": value.get("ok") is True,
                              "message": value.get("message") if isinstance(value.get("message"), str) else ""}
    for key in _TOKEN_KEYS:
        if key in value:
            answer[key] = value[key]
    return answer


def worker_options(instance_id: int, backend_type: str, field_key: str, values: Mapping[str, Any]):
    """The live choices a plugin offers for an ``options`` field, as ``{"items": [...]}``, or None when it has none.

    ``values`` are the draft's declared, non-secret values: the service has checked them against the manifest.
    """
    port = workers.bound_port()
    if not workers.is_bound(port):
        return None
    from app.database import database
    from .store import TranslatorStore

    # Read on the caller's own thread through the same scoped session the service reads through, so the session is
    # the caller's and is left alone: dropping it here would drop what the caller still holds.
    store = TranslatorStore(database)
    instance = store.get_instance(instance_id, fresh=True)
    if instance is None:
        return None
    plugin_id = instance.plugin_id
    engine_settings = dict(store.settings_of(instance))
    if backend_type:
        config: dict[str, Any] = {"settings": engine_settings,
                                  "connection": {"id": "", "type": backend_type, "enabled": True,
                                                 "settings": dict(values)}}
    else:
        config = {"settings": {**engine_settings, **dict(values)}}
    try:
        outcome = port.call(plugin_id, workers.OPTIONS_OP, {"field": field_key, "config": config}, {}, timeout=15.0)
    except workers.NotSent:
        return None
    except Exception as error:
        logger.debug("A model list could not be fetched: %s", type(error).__name__)
        return None
    items = wire.unwrap(outcome.result, outcome.data)
    return {"items": items} if isinstance(items, list) else None
