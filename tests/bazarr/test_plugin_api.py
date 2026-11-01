# coding=utf-8
"""The kind-neutral plugin routes, and the provider-hub routes as the
Subtitle Hub's view of the subtitle provider kind."""
import pytest
from flask import Flask

from plugin_platform_helpers import (
    SERVICE_KIND,
    provider_manifest_v1,
    service_manifest,
    verified_official_source,
    write_state,
)

API_KEY = "plugin-api-test-key"  # pragma: allowlist secret


@pytest.fixture
def client(monkeypatch, tmp_path, service_kind):
    import requests

    def no_network(session, method, url, **kwargs):
        if method.lower() == "get" and url.endswith("announcements.json"):
            response = requests.Response()
            response.status_code = 200
            response._content = b"[]"
            return response
        raise AssertionError("Unexpected network access during plugin API tests")

    monkeypatch.setattr(requests.Session, "request", no_network)
    monkeypatch.setattr("app.check_update._fetch_repo_releases", lambda *args, **kwargs: [])
    from api import api_bp
    from app.config import settings

    monkeypatch.setattr(settings.auth, "apikey", API_KEY)
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(
        tmp_path / "state.json",
        catalog_sources={"official": verified_official_source()},
        catalog_entries={"official:plugins:exampleservice:1.0.0": {
            "source": "official", "feed": "plugins", "provider_id": "exampleservice",
            "plugin_id": "exampleservice", "kind": SERVICE_KIND, "name": "Exampleservice", "version": "1.0.0",
            "trusted": True, "verified": True, "manifest": service_manifest("exampleservice")}},
        installations={
            "providerhub": {"provider_id": "providerhub", "name": "Provider", "active_version": "1.0.0",
                            "state": "active", "pending_restart": False,
                            "manifest": provider_manifest_v1("providerhub")},
            "exampleservice": {"provider_id": "exampleservice", "kind": SERVICE_KIND, "name": "Service",
                               "active_version": "1.0.0", "state": "active", "pending_restart": False,
                               "manifest": {"id": "exampleservice"}},
        })))
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client()


def _get(client, path):
    return client.get(path, headers={"X-API-KEY": API_KEY})


def test_plugins_listing_covers_every_kind_and_filters_by_kind(client):
    everything = _get(client, "/api/plugins/installations").json["data"]
    services = _get(client, f"/api/plugins/installations?kind={SERVICE_KIND}").json["data"]

    assert {item["provider_id"] for item in everything} == {"providerhub", "exampleservice"}
    assert [item["provider_id"] for item in services] == ["exampleservice"]
    assert _get(client, "/api/plugins/installations?kind=nonsense").status_code == 400


def test_subtitle_hub_routes_see_providers_only(client):
    listed = _get(client, "/api/provider-hub/providers").json["data"]

    assert [item["provider_id"] for item in listed] == ["providerhub"]
    assert _get(client, "/api/provider-hub/providers/exampleservice").status_code == 404
    assert client.patch("/api/provider-hub/providers/exampleservice", json={"enabled": False},
                        headers={"X-API-KEY": API_KEY}).status_code == 404
    assert client.delete("/api/provider-hub/installations/exampleservice",
                         headers={"X-API-KEY": API_KEY}).status_code == 404
    assert _get(client, "/api/plugins/installations/exampleservice").status_code == 200


def test_plugins_patch_runs_the_plugins_own_kind(client, service_kind):
    response = client.patch("/api/plugins/installations/exampleservice", json={"enabled": False},
                            headers={"X-API-KEY": API_KEY})

    assert response.status_code == 200
    assert service_kind.calls == [("enabled", "exampleservice", False)]


def test_plugins_install_takes_a_catalog_reference_only(client, monkeypatch):
    from provider_hub import jobs as hub_jobs

    queued = []
    monkeypatch.setattr(hub_jobs, "_enqueue", lambda label, func, kwargs: queued.append((func, kwargs)) or "job-1")

    refused = client.post("/api/plugins/installations", json={"manifest": provider_manifest_v1()},
                          headers={"X-API-KEY": API_KEY})
    accepted = client.post("/api/plugins/installations",
                           json={"source": "official", "id": "exampleservice", "version": "1.0.0"},
                           headers={"X-API-KEY": API_KEY})

    assert refused.status_code == 400
    assert accepted.status_code == 202
    assert queued == [("install_provider", {"source_id": "official", "provider_id": "exampleservice",
                                            "version": "1.0.0", "name": "Exampleservice"})]


# The routes that start code. Whatever gate the provider-hub routes carry, the
# kind-neutral routes carry the same one: a gate is added to both families or
# to neither, and these tests are what keep them from drifting apart.
GATED = [
    ("post", "/api/plugins/installations", "/api/provider-hub/installations",
     {"source": "official", "id": "exampleservice", "version": "1.0.0"}),
    ("post", "/api/plugins/catalog/sources", "/api/provider-hub/catalog/sources",
     {"name": "community", "url": "https://github.com/example/providers/blob/main/catalog.json"}),
    ("patch", "/api/plugins/catalog/sources/official", "/api/provider-hub/catalog/sources/official",
     {"dev_ref": "beta"}),
    ("post", "/api/plugins/updates/apply", "/api/provider-hub/updates/apply", {"id": "exampleservice"}),
]


@pytest.mark.parametrize("method,path,counterpart,body", GATED)
def test_routes_that_start_code_carry_the_gate_of_their_provider_hub_counterpart(
        client, monkeypatch, method, path, counterpart, body):
    from app.config import settings

    monkeypatch.setattr(settings.auth, "type", "form")
    no_key = {}
    plain = getattr(client, method)(path, json=body, headers=no_key)
    twin = getattr(client, method)(counterpart, json=dict(body, provider_id=body.get("id")), headers=no_key)

    # Without the API key both families refuse the same way.
    assert plain.status_code == twin.status_code
    assert plain.status_code in (401, 403)


def _decorator_layers(view):
    """The wrapper code objects a view is built from, outermost first."""
    layers = []
    current = view
    while current is not None:
        code = getattr(current, "__code__", None)
        if code is not None:
            layers.append(code)
        current = getattr(current, "__wrapped__", None)
    return layers


def _decorator_names(view):
    from api.password_confirm import require_password_confirmation
    from api.utils import authenticate

    known = {
        authenticate(lambda: None).__code__: "authenticate",
        require_password_confirmation(lambda: None).__code__: "password_confirmation",
    }
    return [known[code] for code in _decorator_layers(view) if code in known]


@pytest.mark.parametrize("family,method,hub_name,plugin_name", [
    ("catalog sources", "post", "ProviderHubCatalogSources", "PluginsCatalogSources"),
    ("catalog source", "patch", "ProviderHubCatalogSource", "PluginsCatalogSource"),
    ("installations", "post", "ProviderHubInstallations", "PluginsInstallations"),
    ("local installations", "post", "ProviderHubLocalInstallations", "PluginsLocalInstallations"),
    ("updates apply", "post", "ProviderHubUpdatesApply", "PluginsUpdatesApply"),
])
def test_gated_routes_carry_the_same_decorators_as_their_provider_hub_counterpart(
        family, method, hub_name, plugin_name):
    from api.plugins import plugins
    from api.provider_hub import provider_hub

    hub_view = getattr(getattr(provider_hub, hub_name), method)
    plugin_view = getattr(getattr(plugins, plugin_name), method)

    assert _decorator_names(plugin_view) == _decorator_names(hub_view), family
    assert "password_confirmation" in _decorator_names(hub_view), family
