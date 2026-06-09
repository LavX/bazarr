# coding=utf-8
"""Distribution Hub management + statistics REST namespace.

Admin-authenticated (X-API-KEY) surface for the Bazarr+ UI: named API keys,
editable rate-limit tiers, usage statistics, the excludable-provider list, and
the endpoint's own enable/timeout settings. The data-plane (the
OpenSubtitles-compat /api/v1/* endpoint) is untouched - this is the control
plane that manages it.
"""
from flask import request
from flask_restx import Namespace, Resource

from api.utils import authenticate
from app.config import settings, write_config
from compat import keyring, meter, tiers, limits, stats

api_ns_distribution_hub = Namespace(
    'Distribution Hub',
    description='Distribution Hub API keys, tiers, usage statistics')


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _public_key(rec: dict, with_usage: bool = True) -> dict:
    out = {
        "id": rec.get("id"),
        "name": rec.get("name"),
        "key_prefix": rec.get("key_prefix"),
        "tier": rec.get("tier"),
        "tier_label": tiers.get_tier(rec.get("tier")).get("label"),
        "enabled": int(rec.get("enabled") or 0),
        "is_legacy": int(rec.get("is_legacy") or 0),
        "timeout_seconds": rec.get("timeout_seconds"),
        "custom_limits": rec.get("custom_limits"),
        "excluded_providers": rec.get("excluded_providers"),
        "allowed_providers": rec.get("allowed_providers"),
        "created_at": _iso(rec.get("created_at")),
        "last_used_at": _iso(rec.get("last_used_at")),
        "note": rec.get("note"),
    }
    if with_usage and rec.get("id"):
        out["usage"] = meter.usage_for_key(int(rec["id"]))
        out["limits"] = {k: limits.effective_limits(rec, k)
                         for k in ("search", "download")}
    return out


def _set(name: str, value) -> None:
    settings["compat_endpoint"][name] = value


# ---------------------------------------------------------------- keys

@api_ns_distribution_hub.route('distribution-hub/keys')
class DistributionHubKeys(Resource):
    @authenticate
    def get(self):
        return {
            "keys": [_public_key(k) for k in keyring.list_keys()],
            "default_tier": tiers.default_tier_id(),
        }

    @authenticate
    def post(self):
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return {"message": "name is required"}, 400
        tier = body.get("tier") or tiers.default_tier_id()
        if tier not in tiers.all_tiers():
            return {"message": f"unknown tier: {tier}"}, 400
        key_id, token = keyring.create(
            name, tier=tier,
            custom_limits=body.get("custom_limits"),
            excluded_providers=body.get("excluded_providers"),
            allowed_providers=body.get("allowed_providers"),
            timeout_seconds=body.get("timeout_seconds"),
            note=body.get("note"))
        rec = keyring.get(key_id)
        payload = _public_key(rec)
        payload["token"] = token        # shown exactly once
        return payload, 201


@api_ns_distribution_hub.route('distribution-hub/keys/<int:key_id>')
class DistributionHubKey(Resource):
    @authenticate
    def get(self, key_id):
        rec = keyring.get(key_id)
        if rec is None:
            return {"message": "key not found"}, 404
        return _public_key(rec)

    @authenticate
    def patch(self, key_id):
        rec = keyring.get(key_id)
        if rec is None:
            return {"message": "key not found"}, 404
        body = request.get_json(silent=True) or {}
        fields = {}
        for k in ("name", "tier", "enabled", "timeout_seconds", "note",
                  "custom_limits", "excluded_providers", "allowed_providers"):
            if k in body:
                fields[k] = body[k]
        if "tier" in fields and fields["tier"] not in tiers.all_tiers():
            return {"message": f"unknown tier: {fields['tier']}"}, 400
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        keyring.update(key_id, **fields)
        return _public_key(keyring.get(key_id))

    @authenticate
    def delete(self, key_id):
        rec = keyring.get(key_id)
        if rec is None:
            return {"message": "key not found"}, 404
        # The legacy Default key maps the shared compat_endpoint.token that
        # existing integrations use. Deleting it would break them (and it gets
        # re-seeded on next boot anyway). Disable it instead, or rotate the
        # shared secret via /distribution-hub/regenerate.
        if int(rec.get("is_legacy") or 0) == 1:
            return {"message": "The legacy Default key cannot be deleted. "
                    "Disable it, or rotate it via regenerate."}, 400
        keyring.delete(key_id)
        return '', 204


@api_ns_distribution_hub.route('distribution-hub/keys/<int:key_id>/rotate')
class DistributionHubKeyRotate(Resource):
    @authenticate
    def post(self, key_id):
        rec = keyring.get(key_id)
        if rec is None:
            return {"message": "key not found"}, 404
        # Rotating the legacy Default key here would only change the DB row's
        # hash, leaving settings.compat_endpoint.token (the real shared secret)
        # untouched: the revealed token would be overwritten by seed on the next
        # restart, and the old shared token would still authorize. Rotate the
        # shared secret through /distribution-hub/regenerate instead.
        if int(rec.get("is_legacy") or 0) == 1:
            return {"message": "The legacy Default key cannot be rotated here. "
                    "Use regenerate to rotate the shared secret."}, 400
        token = keyring.rotate(key_id)
        return {"token": token, "key_prefix": keyring.get(key_id)["key_prefix"]}


# ---------------------------------------------------------------- tiers

@api_ns_distribution_hub.route('distribution-hub/tiers')
class DistributionHubTiers(Resource):
    @authenticate
    def get(self):
        return {"default_tier": tiers.default_tier_id(),
                "tiers": tiers.all_tiers()}

    @authenticate
    def put(self):
        body = request.get_json(silent=True) or {}
        new_tiers = body.get("tiers")
        default_tier = body.get("default_tier")
        # The effective tier ids are the built-in presets plus any posted
        # overrides; validate default_tier against that union before mutating.
        effective_ids = set(tiers.preset_tiers().keys())
        if isinstance(new_tiers, dict):
            effective_ids |= set(new_tiers.keys())
        if default_tier and default_tier not in effective_ids:
            return {"message": f"unknown default tier: {default_tier}"}, 400
        if isinstance(new_tiers, dict):
            _set("tiers", new_tiers)
        if default_tier:
            _set("default_tier", default_tier)
        write_config()
        keyring.invalidate_cache()
        return {"default_tier": tiers.default_tier_id(),
                "tiers": tiers.all_tiers()}


# ---------------------------------------------------------------- stats

@api_ns_distribution_hub.route('distribution-hub/stats/overview')
class DistributionHubStatsOverview(Resource):
    @authenticate
    def get(self):
        return stats.overview()


@api_ns_distribution_hub.route('distribution-hub/stats/timeseries')
class DistributionHubStatsTimeseries(Resource):
    @authenticate
    def get(self):
        range_days = request.args.get("range_days", default=30, type=int) or 30
        key_id = request.args.get("key_id", type=int)
        return stats.timeseries(range_days=range_days, key_id=key_id)


# ---------------------------------------------------------------- providers

@api_ns_distribution_hub.route('distribution-hub/providers')
class DistributionHubProviders(Resource):
    @authenticate
    def get(self):
        try:
            from app.get_providers import get_providers_sorted
            providers = get_providers_sorted() or []
        except Exception:
            providers = []
        names = sorted(providers)
        if bool(settings.compat_endpoint.serve_local_subs):
            names = [*names, "local"]
        return {"providers": names}


# ---------------------------------------------------------------- settings

_SETTINGS_FIELDS = ("enabled", "consent", "search_timeout_seconds",
                    "search_rate_limit_enabled", "usage_retention_days",
                    "default_tier", "downloads_per_window",
                    "downloads_window_seconds", "serve_local_subs")


@api_ns_distribution_hub.route('distribution-hub/settings')
class DistributionHubSettings(Resource):
    @authenticate
    def get(self):
        ce = settings.compat_endpoint
        # The /api/v1 blueprint (real vs disabled stub) is chosen once at boot
        # from the enabled flag. If the operator flips enabled on at runtime,
        # the routes are not remounted until restart - surface that so the UI
        # can tell the user instead of silently serving the stub.
        import compat
        enabled = bool(ce.enabled)
        restart_required = enabled and not getattr(compat, "compat_active", False)
        return {
            "enabled": enabled,
            "consent": bool(ce.consent),
            "search_timeout_seconds": int(ce.search_timeout_seconds),
            "search_rate_limit_enabled": bool(ce.search_rate_limit_enabled),
            "usage_retention_days": int(ce.usage_retention_days),
            "default_tier": tiers.default_tier_id(),
            "downloads_per_window": int(ce.downloads_per_window),
            "downloads_window_seconds": int(ce.downloads_window_seconds),
            "serve_local_subs": bool(ce.serve_local_subs),
            "has_token": bool(ce.token),
            "restart_required": restart_required,
        }

    @authenticate
    def patch(self):
        body = request.get_json(silent=True) or {}
        changed = False
        for k in _SETTINGS_FIELDS:
            if k in body:
                _set(k, body[k])
                changed = True
        if changed:
            settings.validators.validate()
            write_config()
        return self.get()


@api_ns_distribution_hub.route('distribution-hub/regenerate')
class DistributionHubRegenerate(Resource):
    @authenticate
    def post(self):
        from api.system.compat_admin import regenerate_all_secrets
        token = regenerate_all_secrets()
        keyring.seed_legacy_key()   # re-point the Default key at the new token
        return {"ok": True, "token": token}


@api_ns_distribution_hub.route('distribution-hub/legacy-token')
class DistributionHubLegacyToken(Resource):
    @authenticate
    def get(self):
        # Reveal the current shared legacy token so an operator can copy it to a
        # new client without rotating (which would break every client still on
        # the old value). The Default key maps this token; the keys table only
        # shows its prefix.
        return {"token": settings.compat_endpoint.token or ""}
