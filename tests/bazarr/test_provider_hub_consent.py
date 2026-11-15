# coding=utf-8
"""The consent summary an install or update shows, its digest, and the refusal of a request that no longer matches."""
import pytest
from flask import Flask

from plugin_platform_helpers import service_manifest, verified_official_source, write_state

API_KEY = "plugin-consent-test-key"  # pragma: allowlist secret
KEYED = {"X-API-KEY": API_KEY}
PARENT_CAPABILITY = "demo_parent.backend.v1"
SANDBOX = {"workers_sandboxed": True, "network": "enforced", "filesystem": "declared"}


def _plugin(plugin_id, version="1.0.0", **fields):
    return service_manifest(plugin_id, version, **fields)


def _entry(manifest, source="official"):
    return {"source": source, "source_name": source, "provider_id": manifest["id"], "name": manifest["name"],
            "version": manifest["version"], "trusted": source == "official", "manifest": manifest,
            "catalog_list": "plugins"}


def _installed(manifest, **fields):
    row = {"provider_id": manifest["id"], "kind": manifest["kind"], "name": manifest["name"],
           "active_version": manifest["version"], "state": "active", "pending_restart": False,
           "source_id": "official", "source_bound": True, "manifest": manifest}
    row.update(fields)
    return {manifest["id"]: row}


@pytest.fixture(autouse=True)
def sandbox(monkeypatch):
    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: dict(SANDBOX))


@pytest.fixture
def world(tmp_path, monkeypatch, service_kind):
    def build(manifests, installations=None, sources=None):
        entries = {f"official:plugins:{m['id']}:{m['version']}": _entry(m) for m in manifests}
        monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(
            tmp_path / "state.json",
            catalog_sources=sources or {"official": verified_official_source(last_checked_at="2026-01-01T00:00:00Z")},
            catalog_entries=entries, installations=installations or {})))

    return build


def _summary(plugin_id="addon", version="1.0.0"):
    from provider_hub.service import install_consent

    return install_consent("official", plugin_id, version, catalog_list="plugins")


def _pair():
    parent = _plugin("parent", provides=[PARENT_CAPABILITY], permissions=["network"],
                     endpoints=["https://api.example.org:443"])
    addon = _plugin("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY},
                    permissions=["media_read", "network"], endpoints=["https://other.example.org:443"])
    return parent, addon


# --- the summary -------------------------------------------------------------------------------


def test_the_summary_names_the_plugin_its_closure_and_what_each_asks_for(world):
    parent, addon = _pair()
    world([parent, addon])

    summary = _summary()

    assert summary["plugin"] == {"id": "addon", "name": "Addon", "version": "1.0.0", "kind": "test_service"}
    assert [(m["id"], m["reason"]) for m in summary["members"]] == [("parent", "extends"), ("addon", "root")]
    assert summary["destinations"] == ["https://api.example.org:443", "https://other.example.org:443"]
    # Marks follow the host's sandbox: the network is enforced, a media read is only declared.
    assert summary["permissions"] == [{"name": "media_read", "mark": "declared"},
                                      {"name": "network", "mark": "enforced"}]
    assert summary["members"][1]["source"] == {
        "id": "official", "name": "Official Bazarr Provider Catalog", "official": True, "trusted": True,
        "signature": None}
    assert summary["digest"].startswith("sha256:")
    assert summary["external_requirements"] == [] and summary["already_installed"] == []


def test_an_installed_dependency_is_listed_as_already_there_and_not_as_a_member(world):
    parent, addon = _pair()
    world([parent, addon], _installed(parent))

    summary = _summary()

    assert [m["id"] for m in summary["members"]] == ["addon"]
    assert summary["already_installed"] == [{"id": "parent", "version": "1.0.0", "capability": PARENT_CAPABILITY}]


def test_capabilities_no_plugin_supplies_are_listed_for_the_person(world):
    world([_plugin("addon", requires=["host.ffmpeg.v1", "llm.openai.v1"])])

    assert _summary()["external_requirements"] == ["host.ffmpeg.v1", "llm.openai.v1"]


def test_without_an_enforcing_sandbox_every_permission_is_declared(world, monkeypatch):
    parent, addon = _pair()
    world([parent, addon])
    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: {"workers_sandboxed": False})

    assert {item["mark"] for item in _summary()["permissions"]} == {"declared"}


def test_a_binary_a_member_runs_is_listed():
    from provider_hub.closure import Closure, Member
    from provider_hub.consent import install_summary
    from provider_hub.plugin_manifest import parse_plugin_manifest
    from plugin_platform_helpers import archive_runtime

    manifest = _plugin("native", **archive_runtime("daemon"))
    closure = Closure(members=[Member(_entry(manifest), parse_plugin_manifest(manifest), "root")])

    summary = install_summary(closure, lambda source_id: {"id": source_id})

    assert summary["binaries"] == [{"artifact": "archive", "process": "daemon"}]


# --- the digest ----------------------------------------------------------------------------------


def test_the_digest_is_stable_and_ignores_the_sandbox_marks(world, monkeypatch):
    parent, addon = _pair()
    world([parent, addon])
    first = _summary()["digest"]
    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: {"workers_sandboxed": False})

    assert _summary()["digest"] == first


@pytest.mark.parametrize("change", [
    lambda parent, addon: addon.update(permissions=["media_read", "network", "data_dir"]),
    lambda parent, addon: addon.update(endpoints=["https://elsewhere.example.org:443"]),
    lambda parent, addon: parent.update(bundle_sha256="c" * 64),
    lambda parent, addon: addon.update(requires=["host.ffmpeg.v1"]),
])
def test_the_digest_changes_when_anything_shown_changes(world, change):
    parent, addon = _pair()
    world([parent, addon])
    before = _summary()["digest"]
    change(parent, addon)
    world([parent, addon])

    assert _summary()["digest"] != before


def test_the_digest_changes_when_a_dependency_gets_installed_in_between(world):
    parent, addon = _pair()
    world([parent, addon])
    before = _summary()["digest"]
    world([parent, addon], _installed(parent))

    assert _summary()["digest"] != before


# --- the install request -------------------------------------------------------------------------


def test_an_install_with_a_stale_digest_is_refused_before_anything_is_staged(world, monkeypatch):
    from provider_hub import service

    parent, addon = _pair()
    world([parent, addon])
    stale = _summary()["digest"]
    addon["permissions"] = ["media_read", "network", "data_dir"]
    world([parent, addon])
    staged = []
    monkeypatch.setattr(service, "stage_install", lambda manifest, **kwargs: staged.append(manifest["id"]))

    with pytest.raises(service.ProviderHubConsentError) as error:
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=stale)

    assert error.value.changed and "changed since it was shown" in str(error.value)
    assert staged == []
    service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=_summary()["digest"])
    assert staged == ["parent", "addon"]


def test_a_community_entry_has_no_consent_summary(world):
    from provider_hub.service import ProviderHubInstallError

    manifest = _plugin("addon")
    community = {"id": "community", "name": "community", "type": "github", "enabled": True, "trusted": False,
                 "url": "https://github.com/example/providers/blob/main/catalog.json"}
    world([], sources={"community": community})
    from provider_hub.state import load_state, save_state
    state = load_state()
    state["catalog_entries"]["community:plugins:addon:1.0.0"] = _entry(manifest, "community")
    save_state(state)

    with pytest.raises(ProviderHubInstallError, match="Only the official catalog"):
        from provider_hub.service import install_consent
        install_consent("community", "addon", "1.0.0", catalog_list="plugins")


@pytest.fixture
def client(monkeypatch, tmp_path, service_kind):
    import requests

    def no_network(session, method, url, **kwargs):
        if method.lower() == "get" and url.endswith("announcements.json"):
            response = requests.Response()
            response.status_code = 200
            response._content = b"[]"
            return response
        raise AssertionError("Unexpected network access during consent API tests")

    monkeypatch.setattr(requests.Session, "request", no_network)
    monkeypatch.setattr("app.check_update._fetch_repo_releases", lambda *args, **kwargs: [])
    from api import api_bp
    from app.config import settings

    monkeypatch.setattr(settings.auth, "apikey", API_KEY)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "plugin-consent-test-secret"  # pragma: allowlist secret
    app.register_blueprint(api_bp)
    return app.test_client()


def test_the_consent_endpoint_serves_the_summary_and_the_install_checks_its_digest(client, world, monkeypatch):
    from provider_hub import jobs as hub_jobs

    parent, addon = _pair()
    world([parent, addon])
    queued = []
    monkeypatch.setattr(hub_jobs, "_enqueue", lambda label, func, kwargs: queued.append((func, kwargs)) or "job-1")
    body = {"source": "official", "id": "addon", "version": "1.0.0"}

    summary = client.get("/api/plugins/catalog/consent?source=official&id=addon&version=1.0.0", headers=KEYED)
    missing = client.post("/api/plugins/installations", json=body, headers=KEYED)
    wrong = client.post("/api/plugins/installations", json={**body, "consent": "sha256:" + "0" * 64}, headers=KEYED)
    accepted = client.post("/api/plugins/installations", json={**body, "consent": summary.json["digest"]},
                           headers=KEYED)

    assert summary.status_code == 200 and [m["id"] for m in summary.json["members"]] == ["parent", "addon"]
    assert missing.status_code == 400 and "needs your consent" in missing.get_data(as_text=True)
    assert wrong.status_code == 409 and "changed since it was shown" in wrong.get_data(as_text=True)
    assert accepted.status_code == 202
    assert [kwargs["consent"] for _func, kwargs in queued] == [summary.json["digest"]]


def test_the_consent_endpoint_refuses_an_entry_that_cannot_be_installed(client, world):
    world([_plugin("addon", extends={"id": "gone", "capability": "gone.v1"})])

    refused = client.get("/api/plugins/catalog/consent?source=official&id=addon&version=1.0.0", headers=KEYED)

    assert refused.status_code == 400 and "extends gone" in refused.get_data(as_text=True)


def test_removing_a_plugin_another_depends_on_is_refused_by_the_route(client, world):
    parent, addon = _pair()
    world([], {**_installed(parent), **_installed(addon)})

    refused = client.delete("/api/plugins/installations/parent", headers=KEYED)

    assert refused.status_code == 400 and "addon depends on it" in refused.get_data(as_text=True)


# --- updates ask again only for what is new ---------------------------------------------------------


def _update_world(world, old, new, installed_extra=(), extra_manifests=()):
    installations = _installed(old)
    for manifest in installed_extra:
        installations.update(_installed(manifest))
    world([new, *extra_manifests], installations)


def _delta():
    from provider_hub.service import update_consent

    return update_consent("addon")


def test_an_update_that_adds_nothing_asks_for_nothing(world):
    old = _plugin("addon", "1.0.0", permissions=["network"], endpoints=["https://api.example.org:443"])
    new = _plugin("addon", "1.1.0", permissions=["network"], endpoints=["https://api.example.org:443"],
                  dependencies={"requirements": [{"name": "newpkg", "version": "1.0", "hashes": ["sha256:" + "a" * 64]}]})
    _update_world(world, old, new)

    assert _delta()["needs_consent"] is False


@pytest.mark.parametrize("fields,section", [
    ({"endpoints": ["https://api.example.org:443", "https://new.example.org:443"]}, "destinations"),
    ({"endpoints": ["https://api.example.org:443"], "permissions": ["network", "media_read"]}, "permissions"),
    ({"endpoints": ["https://api.example.org:443"], "permissions": ["network"],
      "settings_version": 1, "settings": {"fields": [{"key": "address", "type": "text", "destination": True}]}},
     "destination_fields"),
])
def test_an_update_that_adds_a_destination_or_a_permission_asks_again(world, fields, section):
    old = _plugin("addon", "1.0.0", permissions=["network"], endpoints=["https://api.example.org:443"])
    world_fields = {"permissions": ["network"], **fields}
    new = _plugin("addon", "1.1.0", **world_fields)
    _update_world(world, old, new)

    delta = _delta()

    assert delta["needs_consent"] is True and delta["added"][section]


def test_an_update_that_joins_a_plugin_to_the_closure_asks_again(world):
    old = _plugin("addon", "1.0.0")
    helper = _plugin("helper", provides=["translit.v1"])
    new = _plugin("addon", "1.1.0", requires=["translit.v1"])
    _update_world(world, old, new, extra_manifests=[helper])

    delta = _delta()

    assert delta["needs_consent"] is True and delta["added"]["plugins"] == ["helper"]


def test_an_update_that_needs_consent_is_not_staged_without_its_digest_and_is_with_it(world, monkeypatch):
    from provider_hub import service

    old = _plugin("addon", "1.0.0")
    new = _plugin("addon", "1.1.0", permissions=["network"], endpoints=["https://new.example.org:443"])
    _update_world(world, old, new)
    staged = []
    monkeypatch.setattr(service, "stage_install",
                        lambda manifest, **kwargs: staged.append(manifest["version"]) or {"state": "staged"})

    refused = service.apply_update("addon")
    assert staged == [] and refused["last_error"].startswith("This update needs your consent first")
    assert "https://new.example.org:443" in refused["last_error"]

    service.apply_update("addon", consent_digest=service.update_consent("addon")["digest"])
    assert staged == ["1.1.0"]


def test_the_update_check_lists_what_needs_consent_and_the_route_serves_the_delta(client, world, monkeypatch):
    from provider_hub import service

    old = _plugin("addon", "1.0.0")
    new = _plugin("addon", "1.1.0", permissions=["network"], endpoints=["https://new.example.org:443"])
    _update_world(world, old, new)

    details = service.check_updates()["details"]
    delta = client.get("/api/plugins/updates/consent?id=addon", headers=KEYED)

    assert details["updates_available"] == 1 and details["needs_consent"] == ["addon"]
    assert delta.status_code == 200 and delta.json["added"]["permissions"] == [
        {"name": "network", "mark": "enforced"}]
    assert client.get("/api/plugins/updates/consent?id=ghost", headers=KEYED).status_code == 404
