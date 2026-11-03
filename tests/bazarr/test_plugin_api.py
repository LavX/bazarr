# coding=utf-8
"""The kind-neutral plugin routes, and the provider-hub routes as the
Subtitle Hub's view of the subtitle provider kind."""
import time

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
    app.config["SECRET_KEY"] = "plugin-api-test-secret"  # pragma: allowlist secret
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


def test_an_unparseable_state_document_refuses_a_plugin_enable_change(client):
    from provider_hub.state import state_file

    state_path = state_file()
    state_path.write_text("{")
    before = state_path.read_bytes()

    response = client.patch("/api/plugins/installations/exampleservice", json={"enabled": False},
                            headers={"X-API-KEY": API_KEY})

    assert response.status_code == 400
    assert state_path.read_bytes() == before


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

    # With the API key and no password confirmation both families answer the
    # confirmation refusal: 403 and the reason `confirm_password`.
    keyed = {"X-API-KEY": API_KEY}
    plain = getattr(client, method)(path, json=body, headers=keyed)
    twin = getattr(client, method)(counterpart, json=dict(body, provider_id=body.get("id")), headers=keyed)
    assert plain.status_code == twin.status_code == 403
    assert plain.get_json()["code"] == twin.get_json()["code"] == "confirm_password"


def _prime(client):
    """Give the client a signed-in session that holds a live password confirmation."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["password_confirmed_until"] = time.time() + 600


@pytest.mark.parametrize("method,path,counterpart,body", GATED)
def test_a_confirmed_session_passes_the_gate_of_both_families(
        client, monkeypatch, method, path, counterpart, body):
    from app.config import settings
    from provider_hub import jobs as hub_jobs
    from provider_hub import service

    monkeypatch.setattr(settings.auth, "type", "form")
    monkeypatch.setattr(hub_jobs, "_enqueue", lambda label, func, kwargs: "job-1")
    monkeypatch.setattr(service, "add_catalog_source", lambda name, url: {"name": name})
    monkeypatch.setattr(service, "update_catalog_source", lambda name, **kwargs: {"id": name})
    monkeypatch.setattr(service, "get_catalog_source", lambda name: {"id": name})
    monkeypatch.setattr(service, "refresh_catalog", lambda: None)
    _prime(client)

    keyed = {"X-API-KEY": API_KEY}
    plain = getattr(client, method)(path, json=body, headers=keyed)
    twin = getattr(client, method)(counterpart, json=dict(body, provider_id=body.get("id")), headers=keyed)

    # A refusal from authentication or from the gate is 401 or 403. Past both,
    # each handler answers for itself.
    assert plain.status_code not in (401, 403)
    assert twin.status_code not in (401, 403)


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


def _undefined_kind_installation():
    """A row of a kind this build does not define."""
    return {"provider_id": "ghost_plugin", "kind": "unregistered_kind", "name": "Ghost",
            "active_version": "1.0.0", "state": "active", "pending_restart": False,
            "manifest": {"id": "ghost_plugin"}}


def test_installation_of_an_undefined_kind_answers_not_found(client, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings.auth, "type", "form")
    _prime(client)
    write_state(tmp_path / "state.json",
                installations={"ghost_plugin": _undefined_kind_installation()})
    keyed = {"X-API-KEY": API_KEY}

    assert _get(client, "/api/provider-hub/providers/ghost_plugin").status_code == 404
    assert client.post("/api/provider-hub/providers/ghost_plugin/test", headers=keyed).status_code == 404
    assert client.delete("/api/provider-hub/installations/ghost_plugin", headers=keyed).status_code == 404
    assert client.post("/api/provider-hub/updates/apply", data={"provider_id": "ghost_plugin"},
                       headers=keyed).status_code == 404

    assert _get(client, "/api/plugins/installations/ghost_plugin").status_code == 404
    assert client.delete("/api/plugins/installations/ghost_plugin", headers=keyed).status_code == 404

    assert client.post("/api/plugins/updates/apply", json={"id": "ghost_plugin"},
                       headers=keyed).status_code == 404


def test_patch_of_an_undefined_kind_installation_answers_not_found(client, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings.auth, "type", "form")
    _prime(client)
    state_path = write_state(tmp_path / "state.json",
                             installations={"ghost_plugin": _undefined_kind_installation()})
    keyed = {"X-API-KEY": API_KEY}

    before = state_path.read_bytes()
    assert client.patch("/api/provider-hub/providers/ghost_plugin", json={"enabled": False},
                        headers=keyed).status_code == 404
    assert state_path.read_bytes() == before

    before = state_path.read_bytes()
    assert client.patch("/api/plugins/installations/ghost_plugin", json={"enabled": False},
                        headers=keyed).status_code == 404
    assert state_path.read_bytes() == before


def test_listing_skips_installation_of_an_undefined_kind(client, tmp_path, caplog):
    write_state(tmp_path / "state.json", installations={
        "providerhub": {"provider_id": "providerhub", "kind": "subtitle_provider", "name": "Provider",
                        "active_version": "1.0.0", "state": "active", "pending_restart": False,
                        "manifest": provider_manifest_v1("providerhub")},
        "ghost_plugin": _undefined_kind_installation(),
    })

    response = _get(client, "/api/plugins/installations")

    assert response.status_code == 200
    listed = {item["provider_id"] for item in response.json["data"]}
    assert "ghost_plugin" not in listed
    assert "providerhub" in listed
    messages = [record.getMessage() for record in caplog.records
                if record.name == "provider_hub.service" and record.levelname == "WARNING"]
    assert any("is not defined by this build" in message for message in messages)


@pytest.mark.parametrize("body", [[1], []])
@pytest.mark.parametrize("method,path", [
    ("post", "/api/plugins/updates/apply"),
    ("post", "/api/plugins/catalog/sources"),
    ("patch", "/api/plugins/catalog/sources/official"),
    ("patch", "/api/plugins/installations/exampleservice"),
])
def test_a_json_array_body_is_refused_before_any_field_is_read(client, monkeypatch, method, path, body):
    from app.config import settings

    monkeypatch.setattr(settings.auth, "type", "form")
    _prime(client)

    response = getattr(client, method)(path, json=body, headers={"X-API-KEY": API_KEY})

    assert response.status_code == 400
    assert response.get_json() == "a JSON object is required"
