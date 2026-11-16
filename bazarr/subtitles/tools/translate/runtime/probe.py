# coding=utf-8
"""The two calls the Translation page makes to a plugin's worker: a connection test and a model list.

Both are the plugin's own ``test`` and ``options`` methods, sent through the worker port the dispatcher uses.
While no worker is bound the test says so and sends nothing, so it costs nothing and the page shows why.

Secrets. A test sends the connection's stored secrets only to the addresses they were entered under: the
caller (``TranslatorSettingsService.test_connection``) has already cut the set to the connection's present
destinations, and a request that would carry a stale secret never gets here. A model list is asked for with a
draft the person has not saved, so it carries no stored secret at all: an engine that needs a key for its list
answers nothing, and the page offers a text field instead of a list.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from . import wire, workers
from .settings_service import OPEN_TEST, ProbeRequest

logger = logging.getLogger(__name__)

# What a plugin's answer to ``test`` may carry back to the page. Nothing else is read.
_TOKEN_KEYS = ("input_tokens", "output_tokens")


def _unavailable(error: BaseException) -> bool:
    from provider_hub.runtime.pool import PoolUnavailable

    return isinstance(error, PoolUnavailable)


def worker_probe(request: ProbeRequest) -> Mapping[str, Any]:
    """Ask the connection's plugin to check the credentials and model with one small request.

    Returns the answer ``TranslatorSettingsService`` reads: ``ok``, a ``message``, the tokens the request used
    when the plugin reports them, and ``sent: false`` when nothing left Bazarr+ (no worker, or it refused to start).
    """
    port = workers.bound_port()
    if not workers.is_bound(port):
        return {"ok": False, "sent": False, "message": OPEN_TEST}
    from app.database import database
    from .store import TranslatorStore

    try:
        store = TranslatorStore(database)
        instance = store.get_instance(request.instance_id, fresh=True)
        connection = store.get_connection(request.connection_id, fresh=True)
        if instance is None or connection is None:
            return {"ok": False, "sent": False, "message": "This back end was removed."}
        config = {"settings": dict(store.settings_of(instance)),
                  "connection": wire.connection_wire(connection, store.settings_of(connection)),
                  "limits": {"input_tokens": request.input_token_limit, "output_tokens": request.output_token_limit}}
    finally:
        remove = getattr(database, "remove", None)
        if remove is not None:
            remove()
    try:
        outcome = port.call(request.plugin_id, workers.TEST_OP, {"config": config}, dict(request.secrets),
                            timeout=request.timeout_seconds)
    except Exception as error:
        if _unavailable(error):
            return {"ok": False, "sent": False, "message": OPEN_TEST}
        raise
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

    try:
        store = TranslatorStore(database)
        instance = store.get_instance(instance_id, fresh=True)
        if instance is None:
            return None
        plugin_id = instance.plugin_id
        engine_settings = dict(store.settings_of(instance))
    finally:
        remove = getattr(database, "remove", None)
        if remove is not None:
            remove()
    if backend_type:
        config: dict[str, Any] = {"settings": engine_settings,
                                  "connection": {"id": "", "type": backend_type, "enabled": True,
                                                 "settings": dict(values)}}
    else:
        config = {"settings": {**engine_settings, **dict(values)}}
    try:
        outcome = port.call(plugin_id, workers.OPTIONS_OP, {"field": field_key, "config": config}, {}, timeout=15.0)
    except Exception as error:
        if _unavailable(error):
            return None
        logger.debug("A model list could not be fetched: %s", type(error).__name__)
        return None
    items = wire.unwrap(outcome.result, outcome.data)
    return {"items": items} if isinstance(items, list) else None
