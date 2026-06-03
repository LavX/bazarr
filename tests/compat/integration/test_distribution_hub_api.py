"""Distribution Hub management API (api/distribution_hub)."""
import pytest
from flask import Flask
from flask_restx import Api

APIKEY = "admin-test-key"


@pytest.fixture
def client(compat_db):
    from app.config import settings
    settings["auth"]["apikey"] = APIKEY
    from api.distribution_hub import api_ns_list_distribution_hub
    app = Flask(__name__)
    api = Api(app)
    for ns in api_ns_list_distribution_hub:
        api.add_namespace(ns, "/")
    return app.test_client()


def _h():
    return {"X-API-KEY": APIKEY}


def test_requires_auth(client):
    assert client.get("/distribution-hub/keys").status_code == 401


def test_create_key_returns_token_once_then_masked(client):
    r = client.post("/distribution-hub/keys", json={"name": "site-a", "tier": "pro"},
                    headers=_h())
    assert r.status_code == 201
    body = r.get_json()
    assert body["token"].startswith("bzr_")
    assert body["tier"] == "pro" and body["name"] == "site-a"
    key_id = body["id"]

    # Listing never exposes the token or hash, only the prefix.
    lst = client.get("/distribution-hub/keys", headers=_h()).get_json()
    entry = next(k for k in lst["keys"] if k["id"] == key_id)
    assert "token" not in entry and "key_hash" not in entry
    assert entry["key_prefix"] == body["key_prefix"]
    assert "usage" in entry and "limits" in entry


def test_create_rejects_unknown_tier(client):
    r = client.post("/distribution-hub/keys", json={"name": "x", "tier": "ghost"},
                    headers=_h())
    assert r.status_code == 400


def test_patch_and_delete_key(client):
    key_id = client.post("/distribution-hub/keys", json={"name": "p"},
                         headers=_h()).get_json()["id"]
    r = client.patch(f"/distribution-hub/keys/{key_id}",
                     json={"tier": "basic", "enabled": False,
                           "excluded_providers": ["opensubtitles"]},
                     headers=_h())
    assert r.status_code == 200
    assert r.get_json()["tier"] == "basic"
    assert r.get_json()["enabled"] == 0
    assert r.get_json()["excluded_providers"] == ["opensubtitles"]

    assert client.delete(f"/distribution-hub/keys/{key_id}", headers=_h()).status_code == 204
    assert client.get(f"/distribution-hub/keys/{key_id}", headers=_h()).status_code == 404


def test_legacy_default_key_cannot_be_deleted(client):
    from compat import keyring
    from app.config import settings
    settings["compat_endpoint"]["token"] = "z" * 40
    keyring.seed_legacy_key()
    keyring.invalidate_cache()
    legacy = next(k for k in keyring.list_keys() if k["is_legacy"])
    r = client.delete(f"/distribution-hub/keys/{legacy['id']}", headers=_h())
    assert r.status_code == 400
    # still present
    assert any(k["is_legacy"] for k in keyring.list_keys())


def test_legacy_default_key_cannot_be_rotated(client):
    from compat import keyring
    from app.config import settings
    settings["compat_endpoint"]["token"] = "y" * 40
    keyring.seed_legacy_key()
    keyring.invalidate_cache()
    legacy = next(k for k in keyring.list_keys() if k["is_legacy"])
    r = client.post(f"/distribution-hub/keys/{legacy['id']}/rotate", headers=_h())
    assert r.status_code == 400


def test_rotate_changes_prefix(client):
    created = client.post("/distribution-hub/keys", json={"name": "r"},
                          headers=_h()).get_json()
    r = client.post(f"/distribution-hub/keys/{created['id']}/rotate", headers=_h())
    assert r.status_code == 200
    body = r.get_json()
    assert body["token"].startswith("bzr_")
    assert body["key_prefix"] != created["key_prefix"]


def test_tiers_get_and_put(client):
    r = client.get("/distribution-hub/tiers", headers=_h())
    assert r.status_code == 200
    assert "free" in r.get_json()["tiers"]
    new = {"default_tier": "basic",
           "tiers": {"free": {"label": "Free", "search": {"hour": 9},
                              "download": {"hour": 1}}}}
    r = client.put("/distribution-hub/tiers", json=new, headers=_h())
    assert r.status_code == 200
    assert r.get_json()["default_tier"] == "basic"
    assert r.get_json()["tiers"]["free"]["search"]["hour"] == 9
    # cleanup global config mutation
    from app.config import settings
    settings["compat_endpoint"]["tiers"] = {}
    settings["compat_endpoint"]["default_tier"] = "free"


def test_stats_overview_shape(client):
    from compat import meter
    cid = client.post("/distribution-hub/keys", json={"name": "s"},
                      headers=_h()).get_json()["id"]
    meter.record(cid, "search")
    meter.record(cid, "download")
    r = client.get("/distribution-hub/stats/overview", headers=_h())
    assert r.status_code == 200
    body = r.get_json()
    assert body["totals"]["today"]["search"] >= 1
    assert body["totals"]["d30"]["download"] >= 1
    assert "top_keys" in body and body["key_count"] >= 1


def test_stats_timeseries_zero_filled(client):
    r = client.get("/distribution-hub/stats/timeseries?range_days=7", headers=_h())
    assert r.status_code == 200
    body = r.get_json()
    assert body["range_days"] == 7
    assert len(body["series"]) == 7
    assert all(set(d) == {"date", "search", "download"} for d in body["series"])


def test_settings_get_and_patch(client):
    r = client.get("/distribution-hub/settings", headers=_h())
    assert r.status_code == 200
    assert "enabled" in r.get_json() and "default_tier" in r.get_json()
    r = client.patch("/distribution-hub/settings",
                     json={"search_rate_limit_enabled": False,
                           "usage_retention_days": 90}, headers=_h())
    assert r.status_code == 200
    assert r.get_json()["search_rate_limit_enabled"] is False
    assert r.get_json()["usage_retention_days"] == 90
    from app.config import settings
    settings["compat_endpoint"]["search_rate_limit_enabled"] = True
    settings["compat_endpoint"]["usage_retention_days"] = 400


def test_providers_list(client):
    r = client.get("/distribution-hub/providers", headers=_h())
    assert r.status_code == 200
    assert isinstance(r.get_json()["providers"], list)
