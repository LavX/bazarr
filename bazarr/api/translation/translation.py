# coding=utf-8
"""The typed API of the v2 translation runtime: engine instances, connections, pointers and their secrets.

Every route answers 404 while the ``translator.runtime_v2`` switch is off, exactly as if it did not exist
(the old ``/api/translator/*`` routes are a different namespace and are not touched). Secrets go in through
``values`` and never come out: a response says whether a secret is set, never what it is.
"""
from functools import wraps

from flask import request
from flask_restx import Namespace, Resource

from subtitles.tools.translate.runtime.settings_service import (InvalidValuesError, ServiceError,
                                                                TranslatorSettingsService)

from ..utils import authenticate
from .guard import runtime_v2_only

api_ns_translation = Namespace(
    'Translation', description='Engine instances and back-end connections of the v2 translation runtime')


def get_service():
    """The settings service on the application's database. Tests replace this."""
    from app.database import database
    from subtitles.tools.translate.runtime import workers
    from subtitles.tools.translate.runtime.probe import worker_options, worker_probe
    from subtitles.tools.translate.runtime.store import TranslatorStore

    return TranslatorSettingsService(TranslatorStore(database), legacy=True, connection_probe=worker_probe,
                                     options_source=worker_options, test_ready=workers.is_bound)


def refusal(method):
    """Turn a refused request into its status and a body a person can read."""
    @wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ServiceError as error:
            return error.payload(), error.status

    return wrapper


def _body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ServiceError("The request body must be a JSON object.")
    return payload


@api_ns_translation.route('translation/state')
class TranslationState(Resource):
    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation.response(200, 'Success')
    @api_ns_translation.response(401, 'Not Authenticated')
    @api_ns_translation.response(404, 'The translation runtime is not enabled')
    def get(self):
        """The engine pointers, every engine instance with its schema, values and connections. Never a secret."""
        return get_service().snapshot()


@api_ns_translation.route('translation/pointers')
class TranslationPointers(Resource):
    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation.response(200, 'Success')
    @api_ns_translation.response(400, 'Refused')
    @api_ns_translation.response(404, 'The translation runtime is not enabled')
    def put(self):
        """Choose the selected engine and the fallback engine (instance ids, or null for none)."""
        service = get_service()
        notices = service.set_pointers(_body())
        engine, fallback = service.pointers()
        return {"engine": engine, "fallback_engine": fallback, "notices": notices}


@api_ns_translation.route('translation/engines/<int:instance_id>')
class TranslationEngine(Resource):
    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation.response(200, 'Saved')
    @api_ns_translation.response(400, 'The values are not valid')
    @api_ns_translation.response(404, 'No such engine instance')
    @api_ns_translation.response(409, 'Stale revision, or a new address needs consent')
    def patch(self, instance_id):
        """Save an engine instance: its settings and secrets, and its on/off switch."""
        return get_service().save_engine(instance_id, _body())


@api_ns_translation.route('translation/engines/<int:instance_id>/connections')
class TranslationConnections(Resource):
    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation.response(201, 'Created')
    @api_ns_translation.response(400, 'The values are not valid')
    @api_ns_translation.response(409, 'A new address needs consent')
    def post(self, instance_id):
        """Add a back-end connection at the end of the engine's list."""
        return get_service().create_connection(instance_id, _body()), 201


@api_ns_translation.route('translation/engines/<int:instance_id>/connections/order')
class TranslationConnectionOrder(Resource):
    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation.response(200, 'Reordered')
    @api_ns_translation.response(400, 'The order must list every connection once')
    def put(self, instance_id):
        """Set the order of the engine's connections: ``{"order": [connection ids]}``."""
        return {"connections": get_service().reorder_connections(instance_id, _body().get("order"))}


@api_ns_translation.route('translation/engines/<int:instance_id>/options')
class TranslationOptions(Resource):
    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation.response(200, 'Success')
    def post(self, instance_id):
        """The live choices of an ``options`` field. ``available`` is false when they cannot be fetched."""
        payload = _body()
        field = payload.get("field")
        backend_type = payload.get("backend_type") or ""
        draft = payload.get("values") or {}
        if not isinstance(field, str) or not isinstance(backend_type, str) or not isinstance(draft, dict):
            raise ServiceError("field and backend_type must be text and values an object")
        return get_service().options(instance_id, backend_type, field, draft)


@api_ns_translation.route('translation/connections/<int:connection_id>')
class TranslationConnection(Resource):
    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation.response(200, 'Saved')
    @api_ns_translation.response(400, 'The values are not valid')
    @api_ns_translation.response(404, 'No such connection')
    @api_ns_translation.response(409, 'A new address needs consent')
    def patch(self, connection_id):
        """Save a connection: its model, prices, settings, secrets and on/off switch."""
        return get_service().update_connection(connection_id, _body())

    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation.response(204, 'Removed')
    @api_ns_translation.response(404, 'No such connection')
    def delete(self, connection_id):
        """Remove a connection and its secrets."""
        get_service().delete_connection(connection_id, request.args.get("expected_revision"))
        return '', 204


@api_ns_translation.route('translation/connections/<int:connection_id>/test')
class TranslationConnectionTest(Resource):
    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation.response(200, 'Answered, whether or not the back end did')
    @api_ns_translation.response(400, 'The request carries a draft instead of an empty body')
    @api_ns_translation.response(404, 'No such connection')
    @api_ns_translation.response(409, 'The plugin or the engine is not running')
    def post(self, connection_id):
        """Send one small request to a saved back end. A back end that does not answer is a 200 with ``ok: false``.

        The test uses the connection as it is saved and the rules a save keeps: a key goes only to the address
        it was entered under, and a response never carries one. A priced test counts against the cost limits.

        The answer is ``ok``, ``message``, ``latency_ms`` and ``outcome``. ``outcome`` tells a failed back end
        from a test that never ran: ``answered``, ``failed`` (a request went out, or may have), ``not_ready``
        (nothing was sent because the engine was busy or not started, a test of this back end was already
        running, or the test stopped before its request was handed over; try again in a moment) or
        ``refused`` (nothing was sent because what is saved, or a cost cap, does not allow it).
        """
        payload = request.get_json(silent=True)
        if payload is not None and not isinstance(payload, dict):
            raise InvalidValuesError("The request body must be a JSON object.")
        return get_service().test_connection(connection_id, payload)
