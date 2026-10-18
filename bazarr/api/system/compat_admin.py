from __future__ import annotations
import secrets
from flask_restx import Namespace, Resource
from ..utils import authenticate
from app.config import settings, write_config
from compat.cache import compat_region
from compat import service as compat_service

api_ns_compat_admin = Namespace("compat_admin", description="Compat endpoint admin")

_stats = {
    "requests_total": 0, "cache_hits": 0, "cache_misses": 0,
    "inflight_depth": 0, "auth_failures": 0, "upstream_failures": 0,
}


def _set_compat_secret(name: str, value: str) -> None:
    """Mutate the underlying Dynaconf store so `as_dict()` + `get_settings()`
    reflect the new value on subsequent reads. `setattr()` on the section wrapper
    does NOT propagate to `settings.as_dict()` output in some Dynaconf versions.
    Using the dict-assignment path matches what `save_settings` does."""
    settings["compat_endpoint"][name] = value


def _clear_compat_decrypt_failures() -> None:
    """End any decrypt failure the compat secrets carry.

    The regeneration above replaced whatever was stored, so once the new
    values are on disk nothing here can fail to decrypt anymore. The
    token is the only one of the three the boot failure set tracks (the
    signing secrets are system secrets), but clearing all three costs
    nothing and stays right if that ever changes."""
    from secret_store.migration import clear_decrypt_failure_path

    for name in ("token", "jwt_secret", "file_id_secret"):
        clear_decrypt_failure_path(f"compat_endpoint.{name}")


def regenerate_all_secrets(write_fn=None) -> str:
    """Atomic 3-way rotation + cache invalidate AFTER file_id_secret write (B9).

    Order: token -> jwt_secret -> file_id_secret -> invalidate. This prevents
    a micro-window where the cache repopulates with the old file_id_secret.
    """
    new_token = secrets.token_urlsafe(32)
    new_jwt = secrets.token_urlsafe(32)
    new_fid = secrets.token_urlsafe(32)
    _set_compat_secret("token", new_token)
    _set_compat_secret("jwt_secret", new_jwt)
    _set_compat_secret("file_id_secret", new_fid)
    if write_fn is not None:
        write_fn("compat_endpoint.token", new_token)
        write_fn("compat_endpoint.jwt_secret", new_jwt)
        write_fn("compat_endpoint.file_id_secret", new_fid)
        _clear_compat_decrypt_failures()
    elif write_config() is True:
        # Only a write that landed ends the failures: a refused write keeps
        # the old ciphertext on disk, and its health issue with it.
        _clear_compat_decrypt_failures()
    compat_region.invalidate(hard=True)
    compat_service.reset_compat_pool()
    from compat.file_id_store import reset_store
    reset_store()
    return new_token


def ensure_secrets(write_fn=None) -> None:
    """Auto-generate any missing / too-short compat secret.

    Idempotent: if all 3 secrets are already present and >=32 chars, no-op.
    Called at blueprint registration when enabled=True.
    """
    changed = False
    for name in ("token", "jwt_secret", "file_id_secret"):
        current = getattr(settings.compat_endpoint, name, "") or ""
        if len(current) < 32:
            new_val = secrets.token_urlsafe(32)
            _set_compat_secret(name, new_val)
            changed = True
            if write_fn is not None:
                write_fn(f"compat_endpoint.{name}", new_val)
    if changed:
        if write_fn is not None:
            _clear_compat_decrypt_failures()
        # A token the boot withheld as empty (it would not decrypt) counts as
        # too short and is regenerated here; only a write that landed ends the
        # failure, so the health issue stops asking for a re-entry that has
        # already happened and survives nothing on a refused write.
        elif write_config() is True:
            _clear_compat_decrypt_failures()


@api_ns_compat_admin.route("/system/compat/regenerate")
class CompatRegenerate(Resource):
    @authenticate
    def post(self):
        new_token = regenerate_all_secrets()
        return {"token": new_token}, 200


@api_ns_compat_admin.route("/system/compat/clear_cache")
class CompatClearCache(Resource):
    @authenticate
    def post(self):
        compat_region.invalidate(hard=True)
        return "", 204


@api_ns_compat_admin.route("/system/compat/stats")
class CompatStats(Resource):
    @authenticate
    def get(self):
        return dict(_stats), 200


@api_ns_compat_admin.route("/system/compat/health")
class CompatHealth(Resource):
    def get(self):
        enabled = bool(settings.compat_endpoint.enabled)
        if not enabled:
            return {"status": "disabled"}, 200
        if len(settings.compat_endpoint.jwt_secret or "") < 32:
            return {"status": "misconfigured", "detail": "jwt_secret"}, 503
        return {"status": "ok"}, 200
