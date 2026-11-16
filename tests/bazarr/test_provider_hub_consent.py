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
        "development": False, "signature": None}
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

    assert delta["needs_consent"] is True
    # What joins is shown in full, not as a name.
    assert [(item["id"], item["version"], item["permissions"], item["destinations"], item["binaries"])
            for item in delta["added"]["plugins"]] == [("helper", "1.0.0", [], [], [])]


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


# --- a closure with a plugin that is not a subtitle provider ----------------------------------------------


def _subtitle_with_service():
    from plugin_platform_helpers import provider_manifest_v2

    helper = _plugin("helper", provides=["translit.v1"])
    subtitle = provider_manifest_v2("closuresub")
    subtitle["requires"] = ["translit.v1"]
    return helper, subtitle


def test_a_provider_that_pulls_in_another_kind_needs_consent_to_install(world, monkeypatch):
    from provider_hub import service

    helper, subtitle = _subtitle_with_service()
    world([helper, subtitle])
    staged = []
    monkeypatch.setattr(service, "stage_install", lambda manifest, **kwargs: staged.append(manifest.get("id")))

    summary = _summary("closuresub")
    assert [member["kind"] for member in summary["members"]] == ["test_service", "subtitle_provider"]
    assert service.install_needs_consent("official", "closuresub", "1.0.0", catalog_list="plugins") is True
    with pytest.raises(service.ProviderHubConsentError, match="needs your consent"):
        service.check_install_consent(None, "official", "closuresub", "1.0.0", catalog_list="plugins")
    # The install job refuses it as well, however it was queued.
    with pytest.raises(service.ProviderHubConsentError, match="needs your consent"):
        service.install_catalog_entry("official", "closuresub", "1.0.0")
    assert staged == []
    service.install_catalog_entry("official", "closuresub", "1.0.0", consent_digest=summary["digest"])
    assert staged == ["helper", "closuresub"]


def test_the_subtitle_hub_route_refuses_a_provider_whose_closure_has_another_kind(client, world, monkeypatch):
    from provider_hub import jobs as hub_jobs

    helper, subtitle = _subtitle_with_service()
    world([helper, subtitle])
    queued = []
    monkeypatch.setattr(hub_jobs, "_enqueue", lambda label, func, kwargs: queued.append(func) or "job-1")

    refused = client.post("/api/provider-hub/installations",
                          json={"source": "official", "provider_id": "closuresub", "version": "1.0.0"},
                          headers=KEYED)
    over_plugins = client.post("/api/plugins/installations",
                               json={"source": "official", "id": "closuresub", "version": "1.0.0"}, headers=KEYED)

    assert refused.status_code == 400 and "subtitle providers only" in refused.get_data(as_text=True)
    assert over_plugins.status_code == 400 and "needs your consent" in over_plugins.get_data(as_text=True)
    assert queued == []


def test_a_plain_subtitle_provider_still_installs_without_consent(world, monkeypatch):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import service

    plain = provider_manifest_v2("plainsub")
    world([plain])
    staged = []
    monkeypatch.setattr(service, "stage_install", lambda manifest, **kwargs: staged.append(manifest.get("id")))

    assert service.install_needs_consent("official", "plainsub", "1.0.0", catalog_list="plugins") is False
    service.install_catalog_entry("official", "plainsub", "1.0.0")
    assert staged == ["plainsub"]


def test_an_empty_closure_is_refused_with_its_reason_and_never_summarised(world, monkeypatch):
    from provider_hub import service
    from provider_hub.closure import Closure
    from provider_hub.consent import install_summary

    world([_plugin("addon")])
    monkeypatch.setattr(service, "_closure_of", lambda *args, **kwargs: Closure(unresolved=["nothing resolves"]))

    with pytest.raises(service.ProviderHubInstallError, match="nothing resolves"):
        _summary()
    with pytest.raises(ValueError):
        install_summary(Closure(), lambda source_id: {})


# --- updates stage what the new release needs, and say why a block is a block --------------------------------


def test_an_update_stages_the_plugins_the_new_release_needs_before_the_plugin(world, monkeypatch):
    from provider_hub import service

    old = _plugin("addon", "1.0.0")
    helper = _plugin("helper", provides=["translit.v1"])
    new = _plugin("addon", "1.1.0", requires=["translit.v1"])
    _update_world(world, old, new, extra_manifests=[helper])
    staged = []
    monkeypatch.setattr(service, "stage_install",
                        lambda manifest, **kwargs: staged.append(manifest["id"]) or {"state": "staged"})

    refused = service.apply_update("addon")
    assert staged == [] and "needs your consent" in refused["last_error"]
    service.apply_update("addon", consent_digest=service.update_consent("addon")["digest"])

    assert staged == ["helper", "addon"]


def test_an_update_of_a_provider_that_would_pull_in_another_kind_asks_for_consent(world, monkeypatch):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import service

    helper = _plugin("helper", provides=["translit.v1"])
    old = provider_manifest_v2("closuresub")
    new = provider_manifest_v2("closuresub", "1.1.0")
    new["requires"] = ["translit.v1"]
    world([new, helper], {**_installed(old, kind="subtitle_provider")})
    staged = []
    monkeypatch.setattr(service, "stage_install",
                        lambda manifest, **kwargs: staged.append(manifest["id"]) or {"state": "staged"})

    refused = service.apply_update("closuresub")

    assert staged == [] and "needs your consent" in refused["last_error"]


def test_an_update_blocked_by_an_expired_catalog_says_so(world, monkeypatch):
    from provider_hub import service

    old = _plugin("addon", "1.0.0")
    expired = verified_official_source(
        last_checked_at="2026-01-01T00:00:00Z", catalog_signature="verified", catalog_channel="main",
        catalog_signed_version=1, catalog_expires_at="2020-01-01T00:00:00Z")
    world([_plugin("addon", "1.1.0")], _installed(old), sources={"official": expired})

    refused = service.apply_update("addon")

    assert "signed main catalog expired" in refused["last_error"]


# --- a dependency changed between the consent and the update --------------------------------------------


def _helper_update_world(world):
    old = _plugin("addon", "1.0.0")
    helper = _plugin("helper", provides=["translit.v1"], permissions=["network"],
                     endpoints=["https://helper.example.org:443"])
    new = _plugin("addon", "1.1.0", requires=["translit.v1"])
    _update_world(world, old, new, extra_manifests=[helper])
    return helper


def test_the_update_consent_shows_each_joining_plugin_in_full(world):
    _helper_update_world(world)

    [joining] = _delta()["added"]["plugins"]

    assert (joining["id"], joining["version"], joining["kind"]) == ("helper", "1.0.0", "test_service")
    assert joining["permissions"] == [{"name": "network", "mark": "enforced"}]
    assert joining["destinations"] == ["https://helper.example.org:443"]


@pytest.mark.parametrize("change", [
    lambda helper: helper.update(permissions=["network", "media_read"]),
    lambda helper: helper.update(endpoints=["https://other.example.org:443"]),
    lambda helper: helper.update(bundle_sha256="c" * 64),
    lambda helper: helper.update(version="1.0.1"),
])
def test_an_update_whose_dependency_changed_after_the_consent_is_not_staged(world, monkeypatch, change):
    from provider_hub import service

    helper = _helper_update_world(world)
    digest = service.update_consent("addon")["digest"]
    change(helper)
    _update_world(world, _plugin("addon", "1.0.0"), _plugin("addon", "1.1.0", requires=["translit.v1"]),
                  extra_manifests=[helper])
    staged = []
    monkeypatch.setattr(service, "stage_install",
                        lambda manifest, **kwargs: staged.append(manifest["id"]) or {"state": "staged"})

    refused = service.apply_update("addon", consent_digest=digest)

    assert staged == [] and "needs your consent" in refused["last_error"]


# --- a root that fails to stage takes its freshly staged dependencies with it -------------------------------


@pytest.fixture
def staging(tmp_path, monkeypatch):
    from provider_hub import service

    class FakeEnvironment:
        def __init__(self, root):
            pass

        def install(self, validated, timeout=None):
            (tmp_path / "env" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "env" / "bin" / "python").write_text("", encoding="utf-8")
            return tmp_path / "env"

    def fetch(manifest, deadline=None):
        if manifest.provider_id == "addon":
            raise service.ProviderHubInstallError("the download failed")
        return tmp_path / "bundle"

    monkeypatch.setattr(service, "_fetch_bundle", fetch)
    monkeypatch.setattr(service, "PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(service, "python_executable", lambda env_path: tmp_path / "python")


def test_a_failed_install_discards_the_dependencies_it_staged_in_the_same_call(world, staging):
    from provider_hub import service
    from provider_hub.state import load_state

    parent, addon = _pair()
    world([parent, addon])
    digest = _summary()["digest"]

    with pytest.raises(service.ProviderHubInstallError, match="download failed"):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    installations = load_state()["installations"]
    assert "parent" not in installations
    assert installations["addon"]["state"] == "failed"


def test_a_failed_update_discards_the_dependencies_it_staged_in_the_same_call(world, staging):
    from provider_hub import service
    from provider_hub.state import load_state

    old = _plugin("addon", "1.0.0")
    helper = _plugin("helper", provides=["translit.v1"])
    new = _plugin("addon", "1.1.0", requires=["translit.v1"])
    _update_world(world, old, new, extra_manifests=[helper])

    service.apply_update("addon", consent_digest=service.update_consent("addon")["digest"])

    installations = load_state()["installations"]
    assert "helper" not in installations
    assert installations["addon"]["active_version"] == "1.0.0" and installations["addon"]["staged_version"] is None


def test_a_second_member_that_fails_takes_the_first_with_it_and_leaves_no_row_of_its_own(
        world, staging, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state

    helper = _plugin("helper", provides=["translit.v1"])
    parent = _plugin("parent", provides=[PARENT_CAPABILITY], requires=["translit.v1"])
    addon = _plugin("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    world([helper, parent, addon])
    digest = _summary()["digest"]
    real_fetch = service._fetch_bundle

    def fetch(manifest, deadline=None):
        if manifest.provider_id == "parent":
            raise service.ProviderHubInstallError("the parent download failed")
        return real_fetch(manifest, deadline)

    monkeypatch.setattr(service, "_fetch_bundle", fetch)

    with pytest.raises(service.ProviderHubInstallError, match="parent download failed"):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    assert set(load_state()["installations"]) == set()


def test_a_failed_update_discards_a_dependency_even_when_the_active_release_already_needed_it(world, staging):
    from provider_hub import service
    from provider_hub.state import load_state

    old = _plugin("addon", "1.0.0", requires=["translit.v1"])
    helper = _plugin("helper", provides=["translit.v1"])
    new = _plugin("addon", "1.1.0", requires=["translit.v1"])
    _update_world(world, old, new, extra_manifests=[helper])

    service.apply_update("addon", consent_digest=service.update_consent("addon")["digest"])

    installations = load_state()["installations"]
    assert "helper" not in installations
    assert installations["addon"]["staged_version"] is None


def test_a_failed_install_keeps_a_dependency_another_plugin_adopted_in_the_meantime(world, staging, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, mutate_state

    helper = _plugin("helper", provides=["translit.v1"])
    parent = _plugin("parent", provides=[PARENT_CAPABILITY], requires=["translit.v1"])
    addon = _plugin("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    world([helper, parent, addon])
    digest = _summary()["digest"]
    other = _plugin("other", requires=["translit.v1"])
    real_fetch = service._fetch_bundle

    def fetch(manifest, deadline=None):
        if manifest.provider_id == "addon":
            # While the root is being staged, another install lands and relies on the helper.
            mutate_state(lambda state: state["installations"].update(_installed(other)))
        return real_fetch(manifest, deadline)

    monkeypatch.setattr(service, "_fetch_bundle", fetch)

    with pytest.raises(service.ProviderHubInstallError, match="download failed"):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    # The parent had no outside dependent and went; the helper had one and stays. The failed root keeps its
    # failed row, as it always did.
    installations = load_state()["installations"]
    assert set(installations) == {"helper", "other", "addon"} and installations["addon"]["state"] == "failed"


def test_a_root_that_stays_staged_keeps_its_dependencies(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import load_state

    class FakeEnvironment:
        def __init__(self, root):
            pass

        def install(self, validated, timeout=None):
            (tmp_path / "env" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "env" / "bin" / "python").write_text("", encoding="utf-8")
            return tmp_path / "env"

    monkeypatch.setattr(service, "_fetch_bundle", lambda manifest, deadline=None: tmp_path / "bundle")
    monkeypatch.setattr(service, "PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(service, "python_executable", lambda env_path: tmp_path / "python")
    parent, addon = _pair()
    entries = {f"official:plugins:{m['id']}:{m['version']}": _entry(m) for m in (parent, addon)}
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(
        tmp_path / "state.json",
        catalog_sources={"official": verified_official_source(last_checked_at="2026-01-01T00:00:00Z")},
        catalog_entries=entries)))
    digest = _summary()["digest"]
    real = service_kind.on_installed

    def on_installed(plugin_id, missing_required=()):
        # The root is staged, and only its switch cannot be saved.
        return False if plugin_id == "addon" else real(plugin_id, missing_required)

    monkeypatch.setattr(service_kind, "on_installed", on_installed)

    with pytest.raises(service.ProviderHubSettingsError):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    installations = load_state()["installations"]
    assert set(installations) == {"parent", "addon"} and installations["addon"]["state"] == "staged"


def test_a_failed_install_keeps_an_adopted_intermediate_member_and_what_it_depends_on(world, staging, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, mutate_state

    helper = _plugin("helper", provides=["translit.v1"])
    parent = _plugin("parent", provides=[PARENT_CAPABILITY], requires=["translit.v1"])
    addon = _plugin("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    world([helper, parent, addon])
    digest = _summary()["digest"]
    other = _plugin("other", requires=[PARENT_CAPABILITY])
    real_fetch = service._fetch_bundle

    def fetch(manifest, deadline=None):
        if manifest.provider_id == "addon":
            # Another install lands and relies on the parent while the root is being staged.
            mutate_state(lambda state: state["installations"].update(_installed(other)))
        return real_fetch(manifest, deadline)

    monkeypatch.setattr(service, "_fetch_bundle", fetch)

    with pytest.raises(service.ProviderHubInstallError, match="download failed"):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    # The parent was adopted, so it stays, and the helper it needs stays with it.
    assert set(load_state()["installations"]) == {"helper", "parent", "other", "addon"}


def test_a_member_that_cannot_be_enabled_is_discarded_and_named(world, staging, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import load_state

    helper = _plugin("helper", provides=["translit.v1"])
    parent = _plugin("parent", provides=[PARENT_CAPABILITY], requires=["translit.v1"])
    addon = _plugin("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    world([helper, parent, addon])
    digest = _summary()["digest"]
    real = service_kind.on_installed
    monkeypatch.setattr(service_kind, "on_installed",
                        lambda plugin_id, missing_required=(): False if plugin_id == "parent"
                        else real(plugin_id, missing_required))

    with pytest.raises(service.ProviderHubInstallError, match="Parent, which this install needs, could not be enabled, so nothing was installed"):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    # Neither the member that could not be enabled nor the one staged before it is left behind.
    assert load_state()["installations"] == {}


def test_a_helper_that_was_only_a_failed_row_is_discarded_with_a_root_that_fails(world, staging, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, mutate_state

    parent, addon = _pair()
    world([parent, addon])
    digest = _summary()["digest"]
    # An earlier attempt to install the parent failed and left a row that records only that.
    mutate_state(lambda state: state["installations"].update({"parent": {
        "provider_id": "parent", "kind": "test_service", "name": "Parent", "state": "failed", "active_version": None,
        "staged_version": None, "last_error": "an earlier download failed", "manifest": parent}}))

    with pytest.raises(service.ProviderHubInstallError, match="download failed"):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    installations = load_state()["installations"]
    assert "parent" not in installations and installations["addon"]["state"] == "failed"


def test_a_later_member_that_was_never_reached_keeps_its_old_failed_row(world, staging, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, mutate_state

    helper = _plugin("helper", provides=["translit.v1"])
    parent = _plugin("parent", provides=[PARENT_CAPABILITY], requires=["translit.v1"])
    addon = _plugin("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    world([helper, parent, addon])
    digest = _summary()["digest"]
    old_row = {"provider_id": "parent", "kind": "test_service", "name": "Parent", "state": "failed",
               "active_version": None, "staged_version": None, "last_error": "an earlier download failed",
               "manifest": parent}
    mutate_state(lambda state: state["installations"].update({"parent": dict(old_row)}))
    real_fetch = service._fetch_bundle

    def fetch(manifest, deadline=None):
        if manifest.provider_id == "helper":
            raise service.ProviderHubInstallError("the helper download failed")
        return real_fetch(manifest, deadline)

    monkeypatch.setattr(service, "_fetch_bundle", fetch)

    with pytest.raises(service.ProviderHubInstallError, match="helper download failed"):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    # The helper failed first, so the parent was never reached and its old record stays as it was.
    installations = load_state()["installations"]
    assert installations["parent"] == old_row and "helper" not in installations


def test_a_failed_install_leaves_a_provider_the_person_had_on_switched_on(world, staging, monkeypatch):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub.kinds import subtitle_provider
    from provider_hub import service

    parent = provider_manifest_v2("subparent")
    parent["provides"] = [PARENT_CAPABILITY]
    addon = _plugin("addon", extends={"id": "subparent", "capability": PARENT_CAPABILITY})
    world([parent, addon])
    digest = _summary()["digest"]
    enabled = ["subparent"]
    calls = []

    def set_enabled(provider_id, value):
        calls.append((provider_id, value))
        if value and provider_id not in enabled:
            enabled.append(provider_id)
        if not value and provider_id in enabled:
            enabled.remove(provider_id)
        return True

    monkeypatch.setattr(subtitle_provider, "bazarr_enabled_providers", lambda: list(enabled))
    monkeypatch.setattr(subtitle_provider, "set_bazarr_provider_enabled", set_enabled)
    # The provider's worker check has no bundle to run in this test.
    monkeypatch.setattr(service, "_smoke_validate_worker", lambda *args, **kwargs: None)

    with pytest.raises(service.ProviderHubInstallError, match="download failed"):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    # Discarding the provider switched it off, and it was put back: it was on before this install.
    assert ("subparent", False) in calls and calls[-1] == ("subparent", True)
    assert enabled == ["subparent"]


def test_a_provider_that_was_off_stays_off_after_a_failed_install(world, staging, monkeypatch):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub.kinds import subtitle_provider
    from provider_hub import service

    parent = provider_manifest_v2("subparent")
    parent["provides"] = [PARENT_CAPABILITY]
    addon = _plugin("addon", extends={"id": "subparent", "capability": PARENT_CAPABILITY})
    world([parent, addon])
    digest = _summary()["digest"]
    enabled = []

    def set_enabled(provider_id, value):
        if value and provider_id not in enabled:
            enabled.append(provider_id)
        if not value and provider_id in enabled:
            enabled.remove(provider_id)
        return True

    monkeypatch.setattr(subtitle_provider, "bazarr_enabled_providers", lambda: list(enabled))
    monkeypatch.setattr(subtitle_provider, "set_bazarr_provider_enabled", set_enabled)
    # The provider's worker check has no bundle to run in this test.
    monkeypatch.setattr(service, "_smoke_validate_worker", lambda *args, **kwargs: None)

    with pytest.raises(service.ProviderHubInstallError, match="download failed"):
        service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    assert enabled == []
