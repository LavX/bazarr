from io import BytesIO
from functools import wraps
import hashlib
import hmac
import logging
import secrets

from flask import request, send_file
from flask_restx import Namespace, Resource
from werkzeug.exceptions import Unauthorized

from app.config import settings
from discover.download import (ExpiredResultError, ResultAuthority, UnauthorizedResultError, classify_failure,
                               enqueue_download, fetch_ticket, preview_result)
from ..swaggerui import job_queued_model
from ..utils import _safe_apikey_compare, authenticate

api_ns_discover_download = Namespace("Discover", description="Exact subtitle attachments and preview")
_SCOPE_SECRET = secrets.token_bytes(32)
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}



def _private_auth_errors(actual_method):
    """Keep the normal authentication rejection out of browser/proxy caches too."""
    @wraps(actual_method)
    def wrapper(*args, **kwargs):
        try:
            return actual_method(*args, **kwargs)
        except Unauthorized:
            return {"message": "Unauthorized"}, 401, _HEADERS
    return wrapper


def _scope(api_key):
    """Return an unforgeable per-process handle for the API key in force.

    This is not password storage, and the right primitive is not a password
    hash. The handle binds an in-flight Discover download to the API key that
    authorised it, so rotating the key invalidates outstanding downloads, and it
    is what the result cache partitions on, so the key itself never has to be
    held in a long-lived object. What has to hold is unforgeable equality
    against a value this process already has in memory, which a keyed hash under
    a random per-process key gives. Offline crack resistance, the property a
    slow KDF buys, defends nothing here: the handle is never stored, never
    leaves the process, and is only ever read by hmac.compare_digest. A KDF
    would only add per-request cost.
    """
    return hmac.new(_SCOPE_SECRET, str(api_key).encode("utf-8"), hashlib.sha256).digest()


def _authority():
    expected = settings.auth.apikey
    # Preserve the existing valid-header, valid-query, valid-form fallback.
    presented = (request.headers.get("X-API-KEY"), request.args.get("apikey"), request.form.get("apikey"))
    if not any(_safe_apikey_compare(value, expected) for value in presented):
        raise UnauthorizedResultError("Authentication changed")
    captured = _scope(expected)
    authority = ResultAuthority(captured, lambda: hmac.compare_digest(captured, _scope(settings.auth.apikey)))
    if not authority.is_current():
        raise UnauthorizedResultError("Authentication changed")
    return authority


def _identity(source):
    if any(len(source.getlist(key)) != 1 for key in ("result_id", "search_id")):
        raise ExpiredResultError("Discover result expired")
    return source.get("result_id"), source.get("search_id")


_EXPIRED = ({"message": "This result has expired. Search again for this selection.",
             "reason": "result_expired", "recoverable": True}, 410, _HEADERS)
_UNAUTHORIZED = ({"message": "Authentication changed. Sign in again."}, 401, _HEADERS)


def _preview():
    try:
        authority = _authority()
        payload = preview_result(*_identity(request.args), authority=authority)
        if not authority.is_current():
            raise UnauthorizedResultError("Authentication changed")
        return payload, 200, _HEADERS
    except UnauthorizedResultError:
        return _UNAUTHORIZED
    except ExpiredResultError:
        return _EXPIRED
    except Exception as error:
        failure = classify_failure(error)
        logging.warning("Discover preview failed: reason=%s: %s", failure.reason, failure.detail or type(error).__name__)
        return {"message": "The provider could not return a valid subtitle. Retry or choose another result.",
                "reason": "preview_failed", "recoverable": True}, 502, _HEADERS


def _enqueue():
    """Queue the exact result as a standard job and answer with its id at once."""
    try:
        authority = _authority()
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            identity = body.get("result_id"), body.get("search_id")
            if not all(isinstance(value, str) for value in identity):
                raise ExpiredResultError("Discover result expired")
        else:
            identity = _identity(request.args)
        job_id = enqueue_download(*identity, authority=authority)
    except UnauthorizedResultError:
        return _UNAUTHORIZED
    except ExpiredResultError:
        return _EXPIRED
    except Exception as error:
        failure = classify_failure(error)
        logging.warning("Discover download could not be queued: reason=%s: %s", failure.reason,
                        failure.detail or type(error).__name__)
        return {"message": failure.message, "reason": failure.reason, "recoverable": True}, 503, _HEADERS
    return {"job_id": job_id}, 202, _HEADERS


def _ticket():
    """Hand over the file a finished download job kept, under the same key check."""
    try:
        authority = _authority()
        tickets = request.args.getlist("job")
        if len(tickets) != 1 or not tickets[0].isdigit():
            raise ExpiredResultError("Discover download expired")
        content, filename = fetch_ticket(int(tickets[0]), authority=authority)
        if not authority.is_current():
            raise UnauthorizedResultError("Authentication changed")
    except UnauthorizedResultError:
        return _UNAUTHORIZED
    except ExpiredResultError:
        return {"message": "This download is no longer available. Download it again from the results.",
                "reason": "ticket_expired", "recoverable": True}, 404, _HEADERS
    response = send_file(BytesIO(content), mimetype="application/x-subrip", as_attachment=True,
                         download_name=filename, etag=False, conditional=False)
    response.headers.update(_HEADERS)
    return response


@api_ns_discover_download.route("discover/download")
class DiscoverDownload(Resource):
    @_private_auth_errors
    @authenticate
    def get(self):
        """Fetch the file of a finished download job by its ticket (?job=<id>)."""
        return _ticket()

    post_job_model = api_ns_discover_download.model('JobQueued', job_queued_model)

    @_private_auth_errors
    @authenticate
    @api_ns_discover_download.response(202, "Download queued as a job", post_job_model)
    @api_ns_discover_download.response(401, "Not Authenticated")
    @api_ns_discover_download.response(410, "The result has expired. Search again.")
    @api_ns_discover_download.response(503, "The download could not be queued")
    def post(self):
        """Queue the download of one exact result as a job; answers with the job id."""
        return _enqueue()


@api_ns_discover_download.route("discover/preview")
class DiscoverPreview(Resource):
    @_private_auth_errors
    @authenticate
    def get(self):
        return _preview()
