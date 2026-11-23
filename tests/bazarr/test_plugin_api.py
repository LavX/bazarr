# coding=utf-8
"""The kind-neutral plugin routes, and the provider-hub routes as the
Subtitle Hub's view of the subtitle provider kind."""
import time
from pathlib import Path

import pytest
from flask import Flask

from plugin_platform_helpers import (
    SERVICE_KIND,
    provider_manifest_v1,
    service_manifest,
    verified_official_source,
    write_state,
)
from provider_hub_state_engines import hub_engine, hub_store  # noqa: F401

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
        catalog_entries={
            "official:plugins:exampleservice:1.0.0": {
                "source": "official", "feed": "plugins", "provider_id": "exampleservice",
                "plugin_id": "exampleservice", "kind": SERVICE_KIND, "name": "Exampleservice", "version": "1.0.0",
                "trusted": True, "verified": True, "manifest": service_manifest("exampleservice")},
            "official:catalogsubs:1.0.0": {
                "source": "official", "provider_id": "catalogsubs", "name": "Catalogsubs", "version": "1.0.0",
                "trusted": True, "manifest": provider_manifest_v1("catalogsubs")}},
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


def test_catalog_kind_filter_reads_the_kind_in_a_stored_entry_manifest(client, tmp_path):
    # The source counts as checked, so the listing does not try to refresh it.
    write_state(tmp_path / "state.json",
                catalog_sources={"official": verified_official_source(last_checked_at="2026-01-01T00:00:00Z")},
                catalog_entries={"official:exampleservice:1.0.0": {
                    "source": "official", "source_name": "Official Bazarr Provider Catalog",
                    "provider_id": "exampleservice", "name": "Exampleservice", "version": "1.0.0",
                    "trusted": True, "manifest": service_manifest("exampleservice"),
                    "resolved_commit": "b" * 40}})

    services = _get(client, f"/api/plugins/catalog?kind={SERVICE_KIND}").json["entries"]
    subtitles = _get(client, "/api/plugins/catalog?kind=subtitle_provider").json["entries"]

    assert [entry["provider_id"] for entry in services] == ["exampleservice"]
    assert subtitles == []


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
    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: {})
    keyed = {"X-API-KEY": API_KEY}

    refused = client.post("/api/plugins/installations", json={"manifest": provider_manifest_v1()}, headers=keyed)
    # A plugin of a new kind installs with the digest of the consent summary the person agreed to.
    digest = _get(client, "/api/plugins/catalog/consent?source=official&id=exampleservice&version=1.0.0").json["digest"]
    accepted = client.post("/api/plugins/installations",
                           json={"source": "official", "id": "exampleservice", "version": "1.0.0",
                                 "consent": digest}, headers=keyed)

    assert refused.status_code == 400
    assert accepted.status_code == 202
    assert queued == [("install_provider", {"source_id": "official", "provider_id": "exampleservice",
                                            "version": "1.0.0", "name": "Exampleservice",
                                            "entry_key": "official:plugins:exampleservice:1.0.0",
                                            "consent": digest})]


def test_the_provider_hub_install_route_queues_the_entry_key(client, monkeypatch):
    from provider_hub import jobs as hub_jobs

    queued = []
    monkeypatch.setattr(hub_jobs, "_enqueue", lambda label, func, kwargs: queued.append((func, kwargs)) or "job-1")

    accepted = client.post("/api/provider-hub/installations",
                           json={"source": "official", "provider_id": "catalogsubs", "version": "1.0.0"},
                           headers={"X-API-KEY": API_KEY})

    assert accepted.status_code == 202
    assert queued == [("install_provider", {"source_id": "official", "provider_id": "catalogsubs",
                                            "version": "1.0.0", "name": "Catalogsubs",
                                            "entry_key": "official:catalogsubs:1.0.0"})]


def test_the_subtitle_hub_install_route_does_not_install_another_kind(client, monkeypatch):
    from provider_hub import jobs as hub_jobs

    queued = []
    monkeypatch.setattr(hub_jobs, "_enqueue", lambda label, func, kwargs: queued.append((func, kwargs)) or "job-1")

    refused = client.post("/api/provider-hub/installations",
                          json={"source": "official", "provider_id": "exampleservice", "version": "1.0.0"},
                          headers={"X-API-KEY": API_KEY})

    assert refused.status_code == 400 and queued == []


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
    ("post", "/api/plugins/installations/exampleservice/repair",
     "/api/provider-hub/installations/exampleservice/repair", {"id": "exampleservice"}),
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
    ("repair", "post", "ProviderHubInstallationRepair", "PluginsInstallationRepair"),
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


@pytest.mark.parametrize("body", [[1], [], 0, False, ""])
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


def test_the_subtitle_hub_install_route_still_checks_the_closure_of_an_answer_that_names_no_kind(
        client, monkeypatch):
    from provider_hub import jobs as hub_jobs
    from provider_hub import service

    queued = []
    monkeypatch.setattr(hub_jobs, "_enqueue", lambda label, func, kwargs: queued.append((func, kwargs)) or "job-1")
    monkeypatch.setattr(service, "resolve_catalog_install", lambda *args, **kwargs: {
        "source": "official", "provider_id": "catalogsubs", "version": "1.0.0", "name": "Catalogsubs",
        "entry_key": "official:catalogsubs:1.0.0"})
    checked = []
    needs = {"consent": False}
    monkeypatch.setattr(service, "install_needs_consent",
                        lambda *args, **kwargs: checked.append(args) or needs["consent"])
    body = {"source": "official", "provider_id": "catalogsubs", "version": "1.0.0"}

    accepted = client.post("/api/provider-hub/installations", json=body, headers={"X-API-KEY": API_KEY})
    needs["consent"] = True
    refused = client.post("/api/provider-hub/installations", json=body, headers={"X-API-KEY": API_KEY})

    # The closure is checked either way, so a missing kind cannot let what needs consent through.
    assert len(checked) == 2
    assert accepted.status_code == 202 and refused.status_code == 400
    assert [func for func, _kwargs in queued] == ["install_provider"]


class _FakePool:
    def __init__(self, workers):
        self._workers = workers

    def status(self):
        return {"workers": self._workers}


def _bound_port(workers_status):
    from subtitles.tools.translate.runtime import workers

    return workers.PoolWorkers(_FakePool(workers_status), lambda plugin_id: None)


def test_an_installation_says_how_its_worker_is_doing_once_one_is_bound(client):
    from subtitles.tools.translate.runtime import workers

    workers.bind(_bound_port({"exampleservice": {
        "state": "backoff", "in_flight": 0, "retry_after": 12.5, "last_error": "the worker did not start",
        "starts": 3, "crashes": 2, "memory_bytes": 0, "recent_stderr": ["never in the listing"]}}))
    try:
        listed = {item["provider_id"]: item for item in _get(client, "/api/plugins/installations").json["data"]}
        single = _get(client, "/api/plugins/installations/exampleservice").json
    finally:
        workers.unbind()

    runtime = listed["exampleservice"]["runtime"]
    assert runtime == single["runtime"]
    assert runtime["phase"] == "backing_off" and runtime["retry_after"] == 12.5
    assert runtime["last_error"] == "the worker did not start"
    # The log is read on its own, and a plugin with no worker has no runtime at all.
    assert "recent_stderr" not in runtime
    assert "runtime" not in listed["providerhub"]


def test_no_runtime_is_reported_while_no_worker_is_bound(client):
    listed = _get(client, "/api/plugins/installations").json["data"]
    assert all("runtime" not in item for item in listed)


def test_the_log_lists_the_workers_recent_lines_with_secrets_masked(client):
    from subtitles.tools.translate.runtime import workers

    secret = "sk-" + "a1b2c3d4" * 4  # pragma: allowlist secret
    workers.bind(_bound_port({"exampleservice": {
        "state": "ready", "recent_stderr": ["started", f"Authorization: Bearer {secret}", "done"]}}))
    try:
        response = _get(client, "/api/plugins/installations/exampleservice/log")
    finally:
        workers.unbind()

    assert response.status_code == 200
    lines = response.json["lines"]
    assert lines[0] == "started" and lines[2] == "done"
    assert secret not in " ".join(lines)
    assert response.json["last_error"] is None


def test_the_log_of_a_plugin_with_no_worker_is_empty_and_an_unknown_plugin_is_not_found(client):
    empty = _get(client, "/api/plugins/installations/exampleservice/log")
    assert empty.status_code == 200 and empty.json["lines"] == []
    assert _get(client, "/api/plugins/installations/nothing/log").status_code == 404


def test_dismissing_a_review_notice_is_kept_on_the_installation(client, tmp_path):
    write_state(tmp_path / "state.json", installations={
        "exampleservice": {"provider_id": "exampleservice", "kind": SERVICE_KIND, "name": "Service",
                           "active_version": "1.0.0", "state": "active", "pending_restart": False,
                           "review_pending": True, "manifest": {"id": "exampleservice"}}})
    path = "/api/plugins/installations/exampleservice"
    assert _get(client, path).json["review_pending"] is True

    response = client.post(f"{path}/review/dismiss", headers={"X-API-KEY": API_KEY})

    assert response.status_code == 200 and response.json == {"review_pending": False}
    assert _get(client, path).json["review_pending"] is False
    missing = client.post("/api/plugins/installations/nothing/review/dismiss", headers={"X-API-KEY": API_KEY})
    assert missing.status_code == 404


@pytest.mark.parametrize("query, kept", [("", True), ("?keep_data=true", True), ("?keep_data=false", False),
                                          ("?keep_data=FALSE", False)])
def test_removing_a_plugin_keeps_its_data_unless_it_is_asked_to_delete_it(client, monkeypatch, query, kept):
    from provider_hub import jobs

    queued = []
    monkeypatch.setattr(jobs, "queue_uninstall", lambda plugin_id, name=None, keep_data=False: queued.append(
        (plugin_id, keep_data)) or "job-1")

    response = client.delete(f"/api/plugins/installations/exampleservice{query}", headers={"X-API-KEY": API_KEY})

    assert response.status_code == 202 and response.json == {"job_id": "job-1"}
    assert queued == [("exampleservice", kept)]


def test_removing_a_plugin_refuses_an_answer_that_is_neither_keep_nor_delete(client, monkeypatch):
    from provider_hub import jobs

    monkeypatch.setattr(jobs, "queue_uninstall", lambda *a, **k: pytest.fail("nothing is queued"))
    response = client.delete("/api/plugins/installations/exampleservice?keep_data=maybe",
                            headers={"X-API-KEY": API_KEY})
    assert response.status_code == 400


@pytest.mark.parametrize("query", ["", "?keep_data=false"])
def test_removing_a_plugin_needs_the_password_confirmation_whichever_answer_is_given_about_its_data(
        client, monkeypatch, query):
    from app.config import settings
    from provider_hub import jobs

    queued = []
    monkeypatch.setattr(jobs, "queue_uninstall", lambda *a, **k: queued.append((a, k)) or "job-1")
    monkeypatch.setattr(settings.auth, "type", "form")
    path = f"/api/plugins/installations/exampleservice{query}"

    keyed = client.delete(path, headers={"X-API-KEY": API_KEY})
    assert keyed.status_code == 403 and keyed.get_json()["code"] == "confirm_password"
    assert queued == []

    _prime(client)
    confirmed = client.delete(path, headers={"X-API-KEY": API_KEY})
    assert confirmed.status_code == 202 and len(queued) == 1


def test_a_removal_of_a_plugin_another_one_needs_is_refused_whatever_is_said_about_its_data(
        client, monkeypatch, tmp_path):
    from provider_hub import jobs

    monkeypatch.setattr(jobs, "queue_uninstall", lambda *a, **k: pytest.fail("nothing is queued"))

    def row(plugin_id, **manifest):
        return {"provider_id": plugin_id, "kind": SERVICE_KIND, "name": plugin_id, "active_version": "1.0.0",
                "state": "active", "pending_restart": False, "manifest": service_manifest(plugin_id, **manifest)}

    write_state(tmp_path / "state.json", installations={
        "exampleservice": row("exampleservice", provides=["example.v1"]),
        "dependent": row("dependent", extends={"id": "exampleservice", "capability": "example.v1"})})
    for query in ("", "?keep_data=false"):
        response = client.delete(f"/api/plugins/installations/exampleservice{query}", headers={"X-API-KEY": API_KEY})
        assert response.status_code == 400 and "dependent" in response.get_data(as_text=True)


def test_the_choice_about_a_plugins_data_travels_through_the_job_queue_to_the_removal(monkeypatch):
    from provider_hub import jobs, service

    enqueued = []
    monkeypatch.setattr(jobs, "_enqueue", lambda label, func, kwargs: enqueued.append((func, kwargs)) or "job-1")
    jobs.queue_uninstall("exampleservice", "Service", keep_data=False)
    jobs.queue_uninstall("exampleservice", "Service")
    assert enqueued[0] == ("uninstall_provider", {"provider_id": "exampleservice", "name": "Service",
                                                  "keep_data": False})
    # Not saying anything keeps, as the Subtitle Hub's own route always has.
    assert "keep_data" not in enqueued[1][1]

    seen = []
    monkeypatch.setattr(jobs.jobs_queue, "update_job_progress", lambda **kwargs: None)
    monkeypatch.setattr(service, "remove_installation", lambda provider_id, **kwargs: seen.append(kwargs) or True)
    jobs.uninstall_provider("exampleservice", "Service", keep_data=False, job_id=None)
    jobs.uninstall_provider("exampleservice", "Service", job_id=None)
    assert seen == [{"keep_data": False}, {"keep_data": True}]


def _service_row(plugin_id, enabled=True, **manifest):
    return {"provider_id": plugin_id, "kind": SERVICE_KIND, "name": plugin_id.title(), "active_version": "1.0.0",
            "state": "active", "pending_restart": False, "enabled": enabled,
            "manifest": service_manifest(plugin_id, **manifest)}


def test_the_listing_names_the_dependents_the_removal_and_the_switch_off_would_refuse_over(
        client, monkeypatch, tmp_path):
    from provider_hub import jobs, service

    monkeypatch.setattr(jobs, "queue_uninstall", lambda *a, **k: "job-1")
    need = {"extends": {"id": "exampleservice", "capability": "example.v1"}}
    write_state(tmp_path / "state.json", installations={
        "exampleservice": _service_row("exampleservice", provides=["example.v1"]),
        "dependent": _service_row("dependent", **need),
        "sleeper": _service_row("sleeper", enabled=False, **need)})

    listed = {item["provider_id"]: item for item in _get(client, "/api/plugins/installations").json["data"]}
    item = listed["exampleservice"]

    assert item["dependents"] == service.installed_dependents("exampleservice") == ["dependent", "sleeper"]
    assert item["enabled_dependents"] == service.installed_dependents("exampleservice", enabled_only=True)
    assert listed["dependent"]["dependents"] == [] and listed["dependent"]["enabled_dependents"] == []
    refusal = client.delete("/api/plugins/installations/exampleservice", headers={"X-API-KEY": API_KEY})
    assert refusal.status_code == 400
    assert all(name in refusal.get_data(as_text=True) for name in item["dependents"])


def test_a_capability_another_enabled_plugin_also_provides_leaves_nobody_depending_on_either(
        client, monkeypatch, tmp_path):
    from provider_hub import jobs

    monkeypatch.setattr(jobs, "queue_uninstall", lambda *a, **k: "job-1")
    write_state(tmp_path / "state.json", installations={
        "supplier_a": _service_row("supplier_a", provides=["shared.v1"]),
        "supplier_b": _service_row("supplier_b", provides=["shared.v1"]),
        "user": _service_row("user", requires=["shared.v1"])})
    listed = {item["provider_id"]: item for item in _get(client, "/api/plugins/installations").json["data"]}
    assert listed["supplier_a"]["dependents"] == [] and listed["supplier_b"]["dependents"] == []
    assert client.delete("/api/plugins/installations/supplier_a?keep_data=false",
                         headers={"X-API-KEY": API_KEY}).status_code == 202


def test_a_staged_change_reports_whether_it_needs_a_restart(client):
    """Both listings serve the same field: a staged provider waits for a restart, a staged plugin of a kind that
    attaches live does not."""
    from provider_hub.state import mutate_state

    def stage(state):
        state["installations"]["providerhub"]["pending_restart"] = True
        state["installations"]["exampleservice"]["pending_restart"] = True

    mutate_state(stage)
    plugins = {item["provider_id"]: item for item in _get(client, "/api/plugins/installations").json["data"]}
    assert plugins["providerhub"]["needs_restart"] is True
    assert plugins["exampleservice"]["needs_restart"] is False
    hub = {item["provider_id"]: item for item in _get(client, "/api/provider-hub/providers").json["data"]}
    assert hub["providerhub"]["needs_restart"] is True
    assert _get(client, "/api/plugins/installations/exampleservice").json["needs_restart"] is False


@pytest.fixture
def rollback_store(client, hub_store, tmp_path, monkeypatch):  # noqa: F811
    """Real retained releases and verified bundle files, isolated from the application's database."""
    from provider_hub import service, state

    root = tmp_path / "provider_hub"
    monkeypatch.setattr(state, "state_file", lambda: root / "state.json")
    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: {})

    def seed(plugin_id="exampleservice", *, previous=None, current=None, previous_supplied_by=None,
             previous_payloads=None, draining_previous=False, source_id="official"):
        factory = provider_manifest_v1 if plugin_id == "providerhub" else service_manifest
        previous = previous or factory(plugin_id, "1.0.0")
        current = current or factory(plugin_id, "2.0.0")
        source = (verified_official_source() if source_id == "official" else service.add_catalog_source(
            source_id, "https://github.com/example/plugin-catalog/blob/main/catalog.json"))
        row = {"provider_id": plugin_id, "name": plugin_id.title(), "kind": previous.get("kind", "subtitle_provider"),
               "state": "active", "pending_restart": False, "trusted": True, "origin": "catalog",
               "source_id": source_id, "source_bound": True, "enabled": True, "config": {"region": "eu"}}
        for key_prefix, manifest in zip("ab", (previous, current)):
            bundle = root / "bundles" / plugin_id / manifest["version"]
            bundle.mkdir(parents=True, exist_ok=True)
            filename = "provider.py" if plugin_id == "providerhub" else "service.py"
            content = b"class ExampleProvider: pass\n" if plugin_id == "providerhub" else b"class Service: pass\n"
            payloads = previous_payloads if manifest is previous and previous_payloads is not None else {filename: content}
            for filename, content in payloads.items():
                (bundle / filename).write_bytes(content)
            interpreter = root / "envs" / plugin_id / manifest["version"] / "bin" / "python"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("", encoding="utf-8")
            row.update(active_version=manifest["version"], active_path=str(bundle), python_path=str(interpreter),
                       manifest=manifest, supplied_by=previous_supplied_by if manifest is previous else {})
            row.pop("active_data_key", None)
            if "data_dir" in manifest.get("permissions", ()):
                from provider_hub.datadir import release_path

                key = key_prefix * 32
                data = release_path(root, plugin_id, key)
                data.mkdir(parents=True, exist_ok=True)
                (data / "settings").write_text(f"data for {manifest['version']}", encoding="utf-8")
                row["active_data_key"] = key
            if manifest is current and draining_previous:
                from provider_hub.releases import DRAIN_PREVIOUS
                row[DRAIN_PREVIOUS] = True
            document = state.load_state()
            document["installations"][plugin_id] = dict(row)
            document["catalog_sources"] = {source_id: source}
            state.save_state(document)
        return row

    return seed


def _rollback(client, plugin_id="exampleservice", **payload):
    path = f"/api/plugins/installations/{plugin_id}/rollback"
    if "confirmation_identity" not in payload:
        preview = _get(client, path).json
        payload["confirmation_identity"] = preview.get("confirmation_identity") if isinstance(preview, dict) else "missing"
        payload["confirmation_identity"] = payload["confirmation_identity"] or "unavailable"
    return client.post(path, json={"version": "1.0.0", **payload}, headers={"X-API-KEY": API_KEY})


@pytest.mark.parametrize("plugin_id", ["exampleservice", "providerhub"])
def test_rollback_requires_a_confirmation_identity_even_when_access_does_not_change(
        client, rollback_store, plugin_id):
    from provider_hub import service, state, state_store

    rollback_store(plugin_id)
    preview = _get(client, f"/api/plugins/installations/{plugin_id}/rollback").json
    assert preview["consent"] is None or preview["consent"]["needs_consent"] is False
    before = state.load_state()
    releases_before = state_store.list_releases(plugin_id)
    response = client.post(f"/api/plugins/installations/{plugin_id}/rollback", json={"version": "1.0.0"},
                           headers={"X-API-KEY": API_KEY})
    assert response.status_code == 400
    assert "confirmation_identity" in response.json
    assert state.load_state() == before and state_store.list_releases(plugin_id) == releases_before
    assert service._previous_release(plugin_id).version == "1.0.0"


def test_rollback_preview_needs_authentication_and_post_also_needs_password_confirmation(client, monkeypatch):
    from app.config import settings
    from api.plugins.plugins import PluginsInstallationRollback
    from provider_hub import service

    monkeypatch.setattr(settings.auth, "type", "form")
    monkeypatch.setattr(service, "rollback_release", lambda *a, **k: pytest.fail("the gate must refuse first"))
    path = "/api/plugins/installations/exampleservice/rollback"
    assert client.get(path).status_code in (401, 403)
    assert client.post(path, json={"version": "1.0.0"}).status_code in (401, 403)
    assert _get(client, path).status_code == 200
    refused = _rollback(client)
    assert refused.status_code == 403 and refused.json["code"] == "confirm_password"
    assert _decorator_names(PluginsInstallationRollback.post) == ["authenticate", "password_confirmation"]
    assert _decorator_names(PluginsInstallationRollback.get) == ["authenticate"]


@pytest.mark.parametrize("body", [None, {}, [], [1], 0, False, "", {"version": False}, {"version": " "},
                                  {"version": "1.0.0", "confirmation_identity": "reviewed", "consent": []},
                                  {"version": "1.0.0", "confirmation_identity": None},
                                  {"version": "1.0.0", "confirmation_identity": False},
                                  {"version": "1.0.0", "confirmation_identity": []},
                                  {"version": "1.0.0", "confirmation_identity": " "}])
def test_rollback_refuses_malformed_json_without_starting_code(client, monkeypatch, body):
    from provider_hub import service

    monkeypatch.setattr(service, "rollback_release", lambda *a, **k: pytest.fail("invalid requests cannot run code"))
    response = client.post("/api/plugins/installations/exampleservice/rollback", json=body,
                           headers={"X-API-KEY": API_KEY})
    assert response.status_code == 400


@pytest.mark.parametrize("plugin_id", ["missing", "ghost_plugin"])
def test_rollback_hides_missing_plugins_and_undefined_kinds(client, tmp_path, plugin_id):
    write_state(tmp_path / "state.json", installations={"ghost_plugin": _undefined_kind_installation()})
    assert _get(client, f"/api/plugins/installations/{plugin_id}/rollback").status_code == 404
    assert _rollback(client, plugin_id).status_code == 404


def test_a_plugin_without_a_previous_release_reports_why_rollback_is_unavailable(client):
    row = _get(client, "/api/plugins/installations/exampleservice").json
    preview = _get(client, "/api/plugins/installations/exampleservice/rollback").json

    assert row["rollback"]["previous_version"] is None
    assert "no previous release" in row["rollback"]["blocked_reason"]
    assert preview == {**row["rollback"], "consent": None, "confirmation_identity": None}
    assert _rollback(client).status_code == 409


@pytest.mark.parametrize("plugin_id,status", [("exampleservice", "activated"), ("providerhub", "waiting")])
def test_rollback_uses_the_kinds_existing_activation_policy_and_preserves_data(
        client, rollback_store, monkeypatch, tmp_path, plugin_id, status):
    from app.config import settings
    from provider_hub import service, state

    original = rollback_store(plugin_id)
    data = tmp_path / "provider_hub" / "data" / plugin_id / "kept.txt"
    data.parent.mkdir(parents=True)
    data.write_text("saved data", encoding="utf-8")
    monkeypatch.setattr(settings.auth, "type", "form")
    _prime(client)
    before = state.load_state()

    preview = _get(client, f"/api/plugins/installations/{plugin_id}/rollback")

    assert preview.status_code == 200
    assert preview.json["previous_version"] == "1.0.0" and preview.json["blocked_reason"] is None
    assert state.load_state() == before
    response = _rollback(client, plugin_id)
    assert response.status_code == 200 and response.json == {"status": status, "version": "1.0.0"}
    row = state.load_state()["installations"][plugin_id]
    assert row["config"] == original["config"] and data.read_text() == "saved data"
    assert row["active_version"] == ("1.0.0" if status == service.ACTIVATED else "2.0.0")
    assert row["pending_restart"] is (status == service.ACTIVATION_WAITING)
    if status == service.ACTIVATION_WAITING:
        assert row["staged_version"] == "1.0.0"


def test_the_rollback_listing_is_lightweight_but_preview_and_submit_verify_the_retained_bundle(
        client, rollback_store, monkeypatch):
    from provider_hub import service, state

    rollback_store()
    before = state.load_state()
    real_verify = service.verify_bundle_tree
    calls = []

    def verify(manifest, bundle):
        calls.append(manifest.version)
        return real_verify(manifest, bundle)

    monkeypatch.setattr(service, "verify_bundle_tree", verify)
    listed = _get(client, "/api/plugins/installations").json["data"]
    assert listed[0]["rollback"] == {"previous_version": "1.0.0", "blocked_reason": None}
    assert calls == []
    previous = service._previous_release("exampleservice")
    (Path(previous.bundle_path) / "service.py").write_text("changed", encoding="utf-8")
    preview = _get(client, "/api/plugins/installations/exampleservice/rollback")
    refused = _rollback(client)
    assert "no longer matches" in preview.json["blocked_reason"]
    assert refused.status_code == 400 and "no longer matches" in refused.json
    assert calls == ["1.0.0", "1.0.0", "1.0.0"] and state.load_state() == before


@pytest.mark.parametrize("reason", ["staged", "source", "revoked", "files", "dependent", "supplier"])
def test_rollback_preview_and_submit_reuse_the_service_refusals_without_mutating_state(
        client, rollback_store, monkeypatch, reason):
    from provider_hub import revocation, service, state

    previous = service_manifest("exampleservice", "1.0.0", **({"requires": ["helper.v1"]} if reason == "supplier" else {}))
    current = service_manifest("exampleservice", "2.0.0", **({"provides": ["example.v1"]} if reason == "dependent" else {}))
    rollback_store(previous=previous, current=current,
                   previous_supplied_by={"helper.v1": "removed-helper"} if reason == "supplier" else None)

    def change(document):
        row = document["installations"]["exampleservice"]
        if reason == "staged":
            row.update(staged_version="3.0.0", staged_manifest=service_manifest("exampleservice", "3.0.0"),
                       state="staged", pending_restart=True)
        if reason == "source":
            row["source_id"] = "another"
        if reason == "dependent":
            document["installations"]["dependent"] = _service_row(
                "dependent", extends={"id": "exampleservice", "capability": "example.v1"})

    state.mutate_state(change)
    if reason == "revoked":
        monkeypatch.setattr(service, "_revocations", lambda document: (
            revocation.Revocation(plugin_id="exampleservice", version="1.0.0", reason="withdrawn"),))
    if reason == "files":
        (Path(service._previous_release("exampleservice").bundle_path) / "service.py").unlink()
    before = state.load_state()
    releases_before = service._previous_release("exampleservice")
    preview = _get(client, "/api/plugins/installations/exampleservice/rollback")
    refused = _rollback(client)
    assert preview.status_code == 200 and preview.json["blocked_reason"]
    assert refused.status_code == 400 and refused.json == preview.json["blocked_reason"]
    assert state.load_state() == before and service._previous_release("exampleservice") == releases_before


def test_rollback_consent_is_for_the_retained_manifest_and_a_stale_digest_does_not_change_state(
        client, rollback_store):
    from provider_hub import service, state

    rollback_store(previous=service_manifest("exampleservice", "1.0.0", permissions=["network"]))
    before = state.load_state()
    preview = _get(client, "/api/plugins/installations/exampleservice/rollback").json
    assert preview["consent"] == service.rollback_consent("exampleservice")
    assert preview["consent"]["needs_consent"] is True
    missing = _rollback(client)
    stale = _rollback(client, consent="0" * len(preview["consent"]["digest"]))
    assert missing.status_code == 400 and stale.status_code == 409
    assert state.load_state() == before
    accepted = _rollback(client, consent=preview["consent"]["digest"])
    assert accepted.status_code == 200 and accepted.json == {"status": "activated", "version": "1.0.0"}


def test_rollback_never_selects_the_version_or_paths_supplied_by_the_caller(client, rollback_store):
    from provider_hub import state

    rollback_store()
    before = state.load_state()
    refused = _rollback(client, version="0.5.0", path="/tmp/untrusted", manifest={"version": "0.5.0"})
    assert refused.status_code == 409 and "previous release changed" in refused.json
    assert state.load_state() == before
    accepted = _rollback(client, path="/tmp/untrusted", manifest={"version": "0.5.0"})
    assert accepted.status_code == 200 and accepted.json["version"] == "1.0.0"


@pytest.mark.parametrize("plugin_id", ["exampleservice", "providerhub"])
@pytest.mark.parametrize("digest", ["outdated", ""])
def test_rollback_refuses_a_supplied_stale_digest_even_when_no_new_consent_is_required(
        client, rollback_store, plugin_id, digest):
    from provider_hub import state

    rollback_store(plugin_id)
    before = state.load_state()
    preview = _get(client, f"/api/plugins/installations/{plugin_id}/rollback").json
    assert preview["consent"] is None or preview["consent"]["needs_consent"] is False
    response = _rollback(client, plugin_id, consent=digest)
    assert response.status_code == 409 and "consent changed" in response.json
    assert state.load_state() == before


def test_rollback_consent_binds_the_bundle_identity_even_when_the_previous_version_stays_the_same(
        client, rollback_store):
    from provider_hub import state

    rollback_store()
    old_digest = _get(client, "/api/plugins/installations/exampleservice/rollback").json["consent"]["digest"]
    payloads = {"service.py": b"class RepackedService: pass\n"}
    rollback_store(previous=service_manifest("exampleservice", "1.0.0", file_payloads=payloads),
                   previous_payloads=payloads)
    before = state.load_state()
    response = _rollback(client, consent=old_digest)
    assert response.status_code == 409 and state.load_state() == before
    preview = _get(client, "/api/plugins/installations/exampleservice/rollback").json
    assert preview["previous_version"] == "1.0.0" and preview["consent"]["digest"] != old_digest
    assert _rollback(client, consent=preview["consent"]["digest"]).status_code == 200


@pytest.mark.parametrize("replacement", ["id", "python_path", "env_key"])
def test_rollback_confirmation_rejects_a_replaced_retained_identity_with_identical_version_and_bundle(
        client, rollback_store, replacement):
    import sqlalchemy as sa
    from provider_hub import releases, service, state, state_store

    rollback_store("providerhub")
    path = "/api/plugins/installations/providerhub/rollback"
    preview = _get(client, path).json
    identity = preview.get("confirmation_identity")
    assert isinstance(identity, str) and identity
    target = service._previous_release("providerhub")
    if replacement == "python_path":
        interpreter = Path(target.python_path).parents[2] / "replacement" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("", encoding="utf-8")
        value = str(interpreter)
    else:
        value = target.id + 100000 if replacement == "id" else "replacement-environment"
    state_store.change_releases(lambda connection: connection.execute(sa.update(releases.releases_table).where(
        releases.releases_table.c.id == target.id).values(**{replacement: value})))
    replaced = service._previous_release("providerhub")
    assert (replaced.version, replaced.bundle_path, replaced.bundle_digest) == (
        target.version, target.bundle_path, target.bundle_digest)
    before = state.load_state()
    releases_before = state_store.list_releases("providerhub")
    response = _rollback(client, "providerhub", confirmation_identity=identity)
    assert response.status_code == 409
    assert state.load_state() == before and state_store.list_releases("providerhub") == releases_before
    refreshed = _get(client, path).json
    assert refreshed["confirmation_identity"] != identity
    assert _rollback(client, "providerhub", confirmation_identity=refreshed["confirmation_identity"]).status_code == 200


@pytest.mark.parametrize("readd", [False, True])
def test_boot_discards_a_staged_rollback_after_its_catalog_source_is_removed(
        client, rollback_store, monkeypatch, tmp_path, service_kind, readd):
    from provider_hub import releases, service, state, state_store
    from provider_hub.kinds import ATTACH_ON_RESTART

    monkeypatch.setattr(service_kind, "attach_policy", ATTACH_ON_RESTART)
    original = rollback_store(source_id="custom-catalog")
    active = state_store.list_releases("exampleservice", states=(releases.ACTIVE,))[0]
    retained = service._previous_release("exampleservice")
    data = tmp_path / "provider_hub" / "data" / "exampleservice" / "saved.txt"
    data.parent.mkdir(parents=True)
    data.write_text("saved data", encoding="utf-8")
    for release in (active, retained):
        release_data = tmp_path / "provider_hub" / "data" / "exampleservice" / str(release.id)
        release_data.mkdir()
        (release_data / "saved.txt").write_text(release.version, encoding="utf-8")
    response = _rollback(client)
    assert response.status_code == 200 and response.json["status"] == "waiting"
    assert service.remove_catalog_source("custom-catalog")
    if readd:
        service.add_catalog_source("custom-catalog", "https://github.com/example/replacement/blob/main/catalog.json")
    assert service.activate_staged_installations() == []
    row = state.load_state()["installations"]["exampleservice"]
    assert row["active_version"] == "2.0.0" and row["source_id"] is None
    assert not row.get("staged_version") and not row["pending_restart"]
    assert row["config"] == original["config"] and data.read_text() == "saved data"
    assert state_store.list_releases("exampleservice", states=(releases.ACTIVE,))[0] == active
    assert service._previous_release("exampleservice").id == retained.id
    for release in (active, retained):
        assert Path(release.bundle_path).is_dir() and Path(release.python_path).exists()
        assert (data.parent / str(release.id) / "saved.txt").read_text() == release.version
    assert service.activate_staged_installations() == []


def test_rollback_refuses_history_from_a_removed_catalog_after_a_new_release_uses_its_readded_name(
        client, rollback_store, tmp_path):
    from provider_hub import releases, service, state, state_store

    original = rollback_store(source_id="custom-catalog")
    old_active = state_store.list_releases("exampleservice", states=(releases.ACTIVE,))[0]
    assert service.remove_catalog_source("custom-catalog")
    service.add_catalog_source("custom-catalog", "https://github.com/example/plugin-catalog/blob/main/catalog.json")
    root = tmp_path / "provider_hub"
    bundle = root / "bundles" / "exampleservice" / "3.0.0"
    bundle.mkdir(parents=True)
    (bundle / "service.py").write_bytes(b"class Service: pass\n")
    interpreter = root / "envs" / "exampleservice" / "3.0.0" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    manifest = service_manifest("exampleservice", "3.0.0")
    state.mutate_state(lambda document: document["installations"]["exampleservice"].update(
        source_id="custom-catalog", source_bound=True, origin="catalog", staged_version="3.0.0",
        staged_path=str(bundle), staged_python_path=str(interpreter), staged_manifest=manifest,
        state="staged", pending_restart=True))
    assert service.activate_staged_installations() == ["exampleservice"]
    retained = service._previous_release("exampleservice")
    assert retained.id == old_active.id and retained.version == "2.0.0"
    before = state.load_state()
    releases_before = state_store.list_releases("exampleservice")
    preview = _get(client, "/api/plugins/installations/exampleservice/rollback").json
    assert preview["previous_version"] == "2.0.0" and preview["blocked_reason"]
    response = _rollback(client, version="2.0.0")
    assert response.status_code == 400 and response.json == preview["blocked_reason"]
    assert state.load_state() == before and state_store.list_releases("exampleservice") == releases_before
    row = before["installations"]["exampleservice"]
    assert row["active_version"] == "3.0.0" and row["config"] == original["config"]
    assert Path(old_active.bundle_path).is_dir() and Path(old_active.python_path).exists()
    assert bundle.is_dir() and interpreter.exists()


@pytest.fixture
def community_install_job(client, rollback_store, tmp_path, monkeypatch):
    from provider_hub import jobs, service, state
    from provider_hub.kinds.subtitle_provider import SUBTITLE_PROVIDER_KIND

    provider_id, source_id = "providerhub", "fixture-catalog"
    url = "https://github.com/example/plugin-catalog/blob/main/catalog.json"
    rollback_store(plugin_id=provider_id, source_id=source_id)
    root = tmp_path / "provider_hub"
    queued = []
    monkeypatch.setattr(jobs, "_enqueue", lambda _label, func, args: queued.append((func, args)) or "install-job")
    monkeypatch.setattr(jobs, "_checkpoint", lambda _job_id: lambda _message: None)
    monkeypatch.setattr(service, "_fetch_bundle", lambda candidate, _deadline: (
        root / "bundles" / provider_id / candidate.version))
    monkeypatch.setattr(service.PluginEnvironment, "install", lambda _self, candidate, **_kwargs: (
        root / "envs" / provider_id / candidate.version))
    monkeypatch.setattr(SUBTITLE_PROVIDER_KIND, "smoke_test", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(SUBTITLE_PROVIDER_KIND, "on_activated", lambda *_args: None)

    def replace_catalog():
        assert service.remove_catalog_source(source_id)
        service.add_catalog_source(source_id, url)

    def publish(version):
        manifest = provider_manifest_v1(provider_id, version)
        key = f"{source_id}:{provider_id}:{version}"
        state.mutate_state(lambda document: document["catalog_entries"].update({key: {
            "source": source_id, "provider_id": provider_id, "version": version, "trusted": False, "manifest": manifest,
        }}))

    def queue(version):
        publish(version)
        response = client.post("/api/provider-hub/installations", headers={"X-API-KEY": API_KEY}, json={
            "source": source_id, "provider_id": provider_id, "version": version,
        })
        assert response.status_code == 202, response.json
        action, args = queued.pop()
        assert action == "install_provider"
        assert args["catalog_registration"] == state.load_state()["catalog_sources"][source_id]["registration"]
        return lambda: jobs.install_provider(**args)

    return provider_id, source_id, replace_catalog, publish, queue


@pytest.mark.parametrize("when", ["queued", "downloading"])
def test_an_authenticated_install_cannot_move_to_a_replacement_catalog_registration(
        community_install_job, monkeypatch, when):
    from provider_hub import jobs, releases, service, state, state_store

    provider_id, _, replace_catalog, publish, queue = community_install_job
    original, = state_store.list_releases(provider_id, states=(releases.RETIRED,))
    run = queue("1.0.0")
    fetch = service._fetch_bundle

    def replace():
        replace_catalog()
        publish("1.0.0")

    def fetch_after_replacement(*args):
        assert when == "downloading"
        replace()
        return fetch(*args)

    monkeypatch.setattr(service, "_fetch_bundle", fetch_after_replacement)
    monkeypatch.setattr(service.PluginEnvironment, "install", lambda *_args, **_kwargs: pytest.fail(
        "a changed registration must not build an environment"))
    if when == "queued":
        replace()
    with pytest.raises(jobs.JobFailed, match="catalog registration changed"):
        run()

    assert state_store.list_releases(provider_id, states=(releases.RETIRED,)) == [original]
    row = state.load_state()["installations"][provider_id]
    assert row["active_version"] == "2.0.0" and not row.get("staged_version")


def test_an_authenticated_reinstall_of_a_retained_version_can_later_roll_back(
        client, community_install_job):
    from provider_hub import releases, service, state_store

    provider_id, _, replace_catalog, _, queue = community_install_job
    original, = state_store.list_releases(provider_id, states=(releases.RETIRED,))
    replace_catalog()
    queue("1.0.0")()
    assert service.activate_staged_installations() == [provider_id]
    renewed, = state_store.list_releases(provider_id, states=(releases.ACTIVE,))
    assert renewed.id != original.id
    assert (renewed.version, renewed.bundle_path) == (original.version, original.bundle_path)
    assert renewed.catalog_registration != original.catalog_registration
    queue("2.0.0")()
    assert service.activate_staged_installations() == [provider_id]
    preview = _get(client, f"/api/plugins/installations/{provider_id}/rollback").json
    assert preview["previous_version"] == "1.0.0" and preview["blocked_reason"] is None

    response = _rollback(client, plugin_id=provider_id)
    assert response.status_code == 200 and response.json["status"] == "waiting", response.json
    assert _get(client, f"/api/plugins/installations/{provider_id}").json["staged_action"] == "rollback"
    assert _get(client, f"/api/provider-hub/providers/{provider_id}").json["staged_action"] == "rollback"
    assert service.activate_staged_installations() == [provider_id]
    restored, = state_store.list_releases(provider_id, states=(releases.ACTIVE,))
    assert (restored.id, restored.catalog_registration) == (renewed.id, renewed.catalog_registration)


def test_authenticated_rollback_smokes_a_private_snapshot_of_the_confirmed_release(
        client, rollback_store, monkeypatch):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import datadir, releases, service, state, state_store
    from provider_hub.kinds.subtitle_provider import SUBTITLE_PROVIDER_KIND

    plugin_id = "providerhub"
    previous, current = [provider_manifest_v2(plugin_id, version, permissions=["data_dir"])
                         for version in ("1.0.0", "2.0.0")]
    rollback_store(plugin_id=plugin_id, previous=previous, current=current)
    original, = state_store.list_releases(plugin_id, states=(releases.RETIRED,))
    retired_path = datadir.release_path(state.provider_hub_dir(), plugin_id, original.data_key)
    current_path = datadir.release_path(state.provider_hub_dir(), plugin_id, "b" * 32)
    smoke_paths = []

    def smoke(_manifest, _bundle, _interpreter, *, data_dir):
        assert data_dir != retired_path and data_dir != current_path
        assert (data_dir / "settings").read_text() == "data for 1.0.0"
        (data_dir / "settings").write_text("temporary smoke output")
        smoke_paths.append(data_dir)

    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: True)
    monkeypatch.setattr(SUBTITLE_PROVIDER_KIND, "smoke_test", smoke)
    monkeypatch.setattr(SUBTITLE_PROVIDER_KIND, "on_activated", lambda *_args: None)

    response = _rollback(client, plugin_id=plugin_id)
    assert response.status_code == 200 and response.json["status"] == "waiting", response.json
    assert smoke_paths == []
    assert service.activate_staged_installations() == [plugin_id]
    restored, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    assert (restored.id, restored.data_key) == (original.id, original.data_key)
    assert len(smoke_paths) == 1 and not smoke_paths[0].exists()
    assert (retired_path / "settings").read_text() == "data for 1.0.0"
    assert (current_path / "settings").read_text() == "data for 2.0.0"


def test_a_confirmed_rollback_activates_on_boot_and_preserves_active_and_retained_files(
        client, rollback_store, monkeypatch, tmp_path, service_kind):
    from provider_hub import releases, service, state, state_store
    from provider_hub.kinds import ATTACH_ON_RESTART

    monkeypatch.setattr(service_kind, "attach_policy", ATTACH_ON_RESTART)
    original = rollback_store(source_id="custom-catalog")
    retained = service._previous_release("exampleservice")
    active = state_store.list_releases("exampleservice", states=(releases.ACTIVE,))[0]
    data = tmp_path / "provider_hub" / "data" / "exampleservice" / "saved.txt"
    data.parent.mkdir(parents=True)
    data.write_text("saved data", encoding="utf-8")
    response = _rollback(client)
    assert response.status_code == 200 and response.json["status"] == "waiting"
    [listed] = _get(client, "/api/plugins/installations").json["data"]
    assert listed["staged_action"] == "rollback" and listed["needs_restart"]
    assert "rollback waiting for restart" in listed["rollback"]["blocked_reason"]
    assert service.activate_staged_installations() == ["exampleservice"]
    row = state.load_state()["installations"]["exampleservice"]
    assert row["active_version"] == "1.0.0" and not row["pending_restart"] and not row.get("staged_version")
    assert row["source_id"] == "custom-catalog" and row["config"] == original["config"]
    assert data.read_text() == "saved data"
    assert state_store.list_releases("exampleservice", states=(releases.ACTIVE,))[0].id == retained.id
    assert service._previous_release("exampleservice").id == active.id
    assert any(job["message"] == "Activated rollback 2.0.0 -> 1.0.0" for job in service.list_jobs())
    for release in (active, retained):
        assert Path(release.bundle_path).is_dir() and Path(release.python_path).exists()
    assert service.activate_staged_installations() == []


@pytest.mark.parametrize("outcome,status", [("failed", 400), ("skipped", 409)])
def test_rollback_does_not_report_unsuccessful_activation_as_success(client, monkeypatch, outcome, status):
    from provider_hub import service

    monkeypatch.setattr(service, "rollback_release", lambda *a, **k: service.Activation(
        "exampleservice", outcome, "1.0.0", "2.0.0", error="activation was refused"))
    response = _rollback(client)
    assert response.status_code == status
    assert isinstance(response.json, str)


@pytest.mark.parametrize("field,value", [("source_id", "another"), ("origin", "local"),
                                       ("manifest", service_manifest("exampleservice", "2.0.0", permissions=["network"]))])
def test_rollback_refuses_an_active_identity_changed_after_the_consent_check(
        client, rollback_store, monkeypatch, field, value):
    from provider_hub import service, state

    rollback_store()
    identity = _get(client, "/api/plugins/installations/exampleservice/rollback").json["confirmation_identity"]
    real_consent = service.rollback_consent

    def change_after_consent(*args, **kwargs):
        delta = real_consent(*args, **kwargs)
        state.mutate_state(lambda document: document["installations"]["exampleservice"].update({field: value}))
        return delta

    monkeypatch.setattr(service, "rollback_consent", change_after_consent)
    response = _rollback(client, confirmation_identity=identity)
    row = state.load_state()["installations"]["exampleservice"]
    assert response.status_code == 409
    assert row[field] == value and row["active_version"] == "2.0.0" and not row.get("staged_version")


@pytest.mark.parametrize("same_version", [False, True])
def test_rollback_cannot_activate_a_release_replaced_between_staging_and_activation(
        client, rollback_store, monkeypatch, same_version):
    from provider_hub import service, state

    rollback_store()
    real_activate = service.activate_release
    replacement = service_manifest("exampleservice", "1.0.0" if same_version else "3.0.0", permissions=["network"])

    def replace_before_activation(plugin_id, **kwargs):
        state.mutate_state(lambda document: document["installations"][plugin_id].update(
            staged_version=replacement["version"], staged_manifest=replacement))
        return real_activate(plugin_id, **kwargs)

    monkeypatch.setattr(service, "activate_release", replace_before_activation)
    monkeypatch.setattr(service, "_verified_staged_release",
                        lambda *a, **k: pytest.fail("the replaced release must not start"))
    response = _rollback(client)
    row = state.load_state()["installations"]["exampleservice"]
    assert response.status_code == 409
    assert row["active_version"] == "2.0.0" and row["staged_manifest"] == replacement


def test_rollback_does_not_abandon_a_different_release_replaced_during_validation(
        client, rollback_store, monkeypatch):
    from provider_hub import service, state

    rollback_store()
    replacement = service_manifest("exampleservice", "1.0.0", permissions=["network"])

    def replace_then_fail(*args):
        state.mutate_state(lambda document: document["installations"]["exampleservice"].update(
            staged_manifest=replacement))
        raise service.ProviderHubInstallError("the reviewed release could not start")

    monkeypatch.setattr(service, "_verified_staged_release", replace_then_fail)
    response = _rollback(client)
    row = state.load_state()["installations"]["exampleservice"]
    assert response.status_code == 400
    assert row["active_version"] == "2.0.0" and row["staged_manifest"] == replacement


def test_rollback_does_not_promote_across_a_source_change_during_activation(
        client, rollback_store, monkeypatch, service_kind):
    from provider_hub import service, state

    rollback_store()
    real_verify = service._verified_staged_release
    hooks = []

    def change_source(*args):
        manifest = real_verify(*args)
        state.mutate_state(lambda document: document["installations"]["exampleservice"].update(source_id="another"))
        return manifest

    monkeypatch.setattr(service, "_verified_staged_release", change_source)
    monkeypatch.setattr(service_kind, "on_activated", lambda *args: hooks.append(args))
    response = _rollback(client)
    row = state.load_state()["installations"]["exampleservice"]
    assert response.status_code == 409
    assert row["source_id"] == "another" and row["active_version"] == "2.0.0" and row["staged_version"] == "1.0.0"
    assert hooks == []


@pytest.mark.parametrize("retire_during_consent", [True, False])
def test_rollback_checks_the_retained_snapshot_and_accepts_an_unchanged_set_of_multiple_retired_releases(
        client, rollback_store, monkeypatch, retire_during_consent):
    from provider_hub import releases, service, state, state_store

    rollback_store(previous=service_manifest("exampleservice", "0.5.0"),
                   current=service_manifest("exampleservice", "1.0.0"))
    rollback_store(draining_previous=True)
    draining = state_store.list_releases("exampleservice", states=(releases.DRAINING,))[0]
    identity = _get(client, "/api/plugins/installations/exampleservice/rollback").json["confirmation_identity"]
    if retire_during_consent:
        real_consent = service.rollback_consent

        def retire_after_consent(*args, **kwargs):
            delta = real_consent(*args, **kwargs)
            assert state_store.transition_release(draining.id, releases.RETIRED)
            return delta

        monkeypatch.setattr(service, "rollback_consent", retire_after_consent)
    else:
        assert state_store.transition_release(draining.id, releases.RETIRED)
    response = _rollback(client, version="0.5.0" if retire_during_consent else "1.0.0",
                         **({"confirmation_identity": identity} if retire_during_consent else {}))
    row = state.load_state()["installations"]["exampleservice"]
    if retire_during_consent:
        assert response.status_code == 409
        assert row["active_version"] == "2.0.0" and not row.get("staged_version")
    else:
        assert response.status_code == 200 and row["active_version"] == "1.0.0"
