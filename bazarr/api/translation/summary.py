# coding=utf-8
"""What the Translation page's status band and Waiting list read: summary, cost limits and the waiting queue.

Like every route of this package, each answers 404 while the ``translator.runtime_v2`` switch is off. Money is
dollars as a JSON number; the ledger underneath counts whole micro-dollars.
"""
from functools import wraps

from flask import request
from flask_restx import Namespace, Resource

from subtitles.tools.translate.runtime.limits import LimitsError, StaleLimitsError, TranslationLimits

from ..utils import authenticate
from .guard import runtime_v2_only

api_ns_translation_summary = Namespace(
    'Translation Summary', description='Spend, cost limits and the waiting queue of the v2 translation runtime')


def get_summary():
    """The summary reader on the application's database. Tests replace this."""
    from app.database import database
    from subtitles.tools.translate.runtime.summary import TranslationSummary

    return TranslationSummary(database)


def get_limits():
    """The cost limits in config.yaml. Tests replace this."""
    return TranslationLimits()


def refusal(method):
    """Turn a refused limits request into its status and a body a person can read."""
    @wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except StaleLimitsError as error:
            return {"error": "stale_revision", "message": error.message, "revision": error.revision}, 409
        except LimitsError as error:
            body = {"error": "invalid", "message": error.message}
            if error.field_errors:
                body["field_errors"] = error.field_errors
            return body, 400

    return wrapper


@api_ns_translation_summary.route('translation/summary')
class TranslationSummaryRoute(Resource):
    @runtime_v2_only
    @authenticate
    @api_ns_translation_summary.response(200, 'Success')
    @api_ns_translation_summary.response(401, 'Not Authenticated')
    @api_ns_translation_summary.response(404, 'The translation runtime is not enabled')
    def get(self):
        """This month's spend against its cap, the per-job cap, and the translation queue counts"""
        return get_summary().summary()


@api_ns_translation_summary.route('translation/limits')
class TranslationLimitsRoute(Resource):
    @runtime_v2_only
    @authenticate
    @refusal
    @api_ns_translation_summary.response(200, 'Saved: the summary with the new limits')
    @api_ns_translation_summary.response(400, 'A limit is not a number of 0 or more, or null')
    @api_ns_translation_summary.response(404, 'The translation runtime is not enabled')
    @api_ns_translation_summary.response(409, 'The limits changed since the revision sent was loaded')
    def put(self):
        """Set the month and per-job cost caps. Both keys are sent; ``null`` clears a cap.

        ``expected_revision`` is optional: with it, a save made after the limits changed is refused with 409.
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise LimitsError("The request body must be a JSON object.")
        get_limits().update(payload, payload.get("expected_revision"))
        return get_summary().summary()


@api_ns_translation_summary.route('translation/queue')
class TranslationQueueRoute(Resource):
    @runtime_v2_only
    @authenticate
    @api_ns_translation_summary.response(200, 'Success')
    @api_ns_translation_summary.response(401, 'Not Authenticated')
    @api_ns_translation_summary.response(404, 'The translation runtime is not enabled')
    def get(self):
        """Translations that have not started, in the order they will run, at most 50"""
        return get_summary().queue()
