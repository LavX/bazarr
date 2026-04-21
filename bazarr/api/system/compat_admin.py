from __future__ import annotations
import secrets
from flask_restx import Namespace, Resource
from ..utils import authenticate
from bazarr.app.config import settings
from bazarr.compat.cache import compat_region
from bazarr.compat import service as compat_service

api_ns_compat_admin = Namespace("compat_admin", description="Compat endpoint admin")

_stats = {
    "requests_total": 0, "cache_hits": 0, "cache_misses": 0,
    "inflight_depth": 0, "auth_failures": 0, "upstream_failures": 0,
}


def regenerate_all_secrets(write_fn=None) -> str:
    """Atomic 3-way rotation + cache invalidate AFTER file_id_secret write (B9).

    Order: token -> jwt_secret -> file_id_secret -> invalidate. This prevents
    a micro-window where the cache repopulates with the old file_id_secret.
    """
    new_token = secrets.token_urlsafe(32)
    new_jwt = secrets.token_urlsafe(32)
    new_fid = secrets.token_urlsafe(32)
    settings.compat_endpoint.token = new_token
    settings.compat_endpoint.jwt_secret = new_jwt
    settings.compat_endpoint.file_id_secret = new_fid
    if write_fn is not None:
        write_fn("compat_endpoint.token", new_token)
        write_fn("compat_endpoint.jwt_secret", new_jwt)
        write_fn("compat_endpoint.file_id_secret", new_fid)
    else:
        try:
            from bazarr.app.config import write_config
            write_config({
                "compat_endpoint.token": new_token,
                "compat_endpoint.jwt_secret": new_jwt,
                "compat_endpoint.file_id_secret": new_fid,
            })
        except ImportError:
            pass
    compat_region.invalidate(hard=True)
    compat_service.reset_compat_pool()
    return new_token


def ensure_secrets(write_fn=None) -> None:
    """Called on settings save-with-enabled-flip. Auto-generates any missing secret.

    Idempotent: if all 3 secrets are already present and >=32 chars, no-op.
    """
    for name in ("token", "jwt_secret", "file_id_secret"):
        current = getattr(settings.compat_endpoint, name, "") or ""
        if len(current) < 32:
            new_val = secrets.token_urlsafe(32)
            setattr(settings.compat_endpoint, name, new_val)
            if write_fn is not None:
                write_fn(f"compat_endpoint.{name}", new_val)
            else:
                try:
                    from bazarr.app.config import write_config
                    write_config({f"compat_endpoint.{name}": new_val})
                except ImportError:
                    pass


@api_ns_compat_admin.route("/api/system/compat/regenerate")
class CompatRegenerate(Resource):
    @authenticate
    def post(self):
        new_token = regenerate_all_secrets()
        return {"token": new_token}, 200


@api_ns_compat_admin.route("/api/system/compat/clear_cache")
class CompatClearCache(Resource):
    @authenticate
    def post(self):
        compat_region.invalidate(hard=True)
        return "", 204


@api_ns_compat_admin.route("/api/system/compat/stats")
class CompatStats(Resource):
    @authenticate
    def get(self):
        return dict(_stats), 200


@api_ns_compat_admin.route("/api/system/compat/health")
class CompatHealth(Resource):
    def get(self):
        enabled = bool(settings.compat_endpoint.enabled)
        if not enabled:
            return {"status": "disabled"}, 200
        if len(settings.compat_endpoint.jwt_secret or "") < 32:
            return {"status": "misconfigured", "detail": "jwt_secret"}, 503
        return {"status": "ok"}, 200
