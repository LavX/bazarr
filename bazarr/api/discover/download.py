from io import BytesIO
from functools import wraps
import hashlib
import hmac
import secrets

from flask import request, send_file
from flask_restx import Namespace, Resource
from werkzeug.exceptions import Unauthorized

from app.config import settings
from discover.download import ExpiredResultError, ResultAuthority, UnauthorizedResultError, download_result, preview_result
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


def _scope(credential):
    return hmac.new(_SCOPE_SECRET, str(credential).encode("utf-8"), hashlib.sha256).digest()


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


def _retrieve(preview=False):
    try:
        authority = _authority()
        if any(len(request.args.getlist(key)) != 1 for key in ("result_id", "search_id")):
            raise ExpiredResultError("Discover result expired")
        identity = (request.args.get("result_id"), request.args.get("search_id"))
        if preview:
            payload = preview_result(*identity, authority=authority)
            if not authority.is_current():
                raise UnauthorizedResultError("Authentication changed")
            return payload, 200, _HEADERS
        content, filename = download_result(*identity, authority=authority)
        if not authority.is_current():
            raise UnauthorizedResultError("Authentication changed")
    except UnauthorizedResultError:
        return {"message": "Authentication changed. Sign in again."}, 401, _HEADERS
    except ExpiredResultError:
        return {"message": "This result has expired. Search again for this selection.",
                "reason": "result_expired", "recoverable": True}, 410, _HEADERS
    except Exception:
        return {"message": "The provider could not return a valid subtitle. Retry or choose another result.",
                "reason": "preview_failed" if preview else "download_failed", "recoverable": True}, 502, _HEADERS
    response = send_file(BytesIO(content), mimetype="application/x-subrip", as_attachment=True,
                         download_name=filename, etag=False, conditional=False)
    response.headers.update(_HEADERS)
    return response


@api_ns_discover_download.route("discover/download")
class DiscoverDownload(Resource):
    @_private_auth_errors
    @authenticate
    def get(self):
        return _retrieve()


@api_ns_discover_download.route("discover/preview")
class DiscoverPreview(Resource):
    @_private_auth_errors
    @authenticate
    def get(self):
        return _retrieve(preview=True)
