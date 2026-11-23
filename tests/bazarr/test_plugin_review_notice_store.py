"""Review notices and retained plugin data persist on SQLite and PostgreSQL."""
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import provider_hub_state_engines as engines
from plugin_platform_helpers import provider_manifest_v2, service_manifest, verified_official_source
from provider_hub_state_engines import raw_row
from test_provider_hub import _provider_zip

hub_engine = engines.hub_engine
hub_store = engines.hub_store


def _installation(provider_id, **extra):
    return {"provider_id": provider_id, "name": provider_id, "active_version": "1.0.0", "state": "active",
            "pending_restart": False, "manifest": {"name": provider_id}, **extra}


def test_dismissal_clears_the_flag_in_the_stored_document_and_leaves_the_others(hub_store, hub_engine):
    from provider_hub import service
    from provider_hub.state import load_state, mutate_state

    mutate_state(lambda state: state["installations"].update(
        flagged=_installation("flagged", review_pending=True), other=_installation("other", review_pending=True)))
    revision = raw_row(hub_engine)[0]

    assert service.dismiss_review_notice("flagged") is True

    installations = load_state()["installations"]
    assert installations["flagged"]["review_pending"] is False
    assert installations["other"]["review_pending"] is True
    assert raw_row(hub_engine)[0] == revision + 1


def test_dismissing_the_notice_of_a_plugin_that_is_not_there_writes_nothing_it_did_not_have(hub_store, hub_engine):
    from provider_hub import service
    from provider_hub.state import load_state

    assert service.dismiss_review_notice("ghost") is False
    assert "ghost" not in load_state()["installations"]


class _Kind:
    keeps_data = True

    def __init__(self):
        self.discarded = []
        self.reclaimed = []
        self.fail_discard = False

    def discard_kept_data(self, plugin_id):
        if self.fail_discard:
            raise RuntimeError("cannot clear")
        self.discarded.append(plugin_id)

    def kept_data_reclaimed(self, plugin_id):
        self.reclaimed.append(plugin_id)


def _removal(service, kind_hooks, plugin_id, keep, **row):
    from provider_hub.state import mutate_state

    installation = {"provider_id": plugin_id, "kind": "translator", "source_id": "official", **row}
    mutate_state(lambda state: service._record_kept_data(state, plugin_id, installation, kind_hooks, keep))


def _stage_row(plugin_id, **row):
    from provider_hub.state import mutate_state

    mutate_state(lambda state: state["installations"].update(
        {plugin_id: {"provider_id": plugin_id, "state": "staged", "kind": "translator", "staged_version": "1.0.0",
                     **row}}))


@pytest.mark.parametrize(("plugin_kind", "incoming_kind", "source", "reclaims"), [
    pytest.param("subtitle_provider", "subtitle_provider", "official", True, id="provider-same-owner"),
    pytest.param("translator", "translator", "official", True, id="translator-same-owner"),
    pytest.param("service", "service", "official", True, id="service-same-owner"),
    pytest.param("subtitle_provider", "subtitle_provider", "community", False, id="provider-other-source"),
    pytest.param("subtitle_provider", "subtitle_provider", None, False, id="provider-upload"),
    pytest.param("translator", "subtitle_provider", None, False, id="translator-provider-upload"),
    pytest.param("service", "subtitle_provider", None, False, id="service-provider-upload"),
    pytest.param("subtitle_provider", "service", "official", False, id="provider-to-service"),
    pytest.param("translator", "service", "official", False, id="translator-to-service"),
    pytest.param("service", "translator", "official", False, id="service-to-translator"),
])
def test_kept_data_goes_back_only_to_the_same_plugin_from_the_same_source_and_kind(
        hub_store, hub_engine, monkeypatch, tmp_path, plugin_kind, incoming_kind, source, reclaims):
    from compat import service as compat_service
    from provider_hub import catalog_feed, consent, datadir, detach, kinds, leases, registry, service
    from provider_hub.kinds import subtitle_provider, translator
    from provider_hub.kinds.service import SERVICE_KIND_API, ServiceKind
    from provider_hub.state import load_state, mutate_state
    from subtitles.tools.translate.runtime import worker_binding

    # Keep the actual lifecycle hooks; isolate their settings, runtime and worker dependencies.
    monkeypatch.setattr(subtitle_provider, "set_bazarr_provider_enabled", lambda plugin_id, enabled: True)
    monkeypatch.setattr(subtitle_provider, "bazarr_enabled_providers", lambda: ["engine"])
    monkeypatch.setattr(subtitle_provider, "reset_own_settings_section", lambda plugin_id: True)
    monkeypatch.setattr(registry, "unregister_provider_class", lambda plugin_id: SimpleNamespace(removed=False))
    monkeypatch.setattr(registry, "revoke_host_calls_of", lambda plugin_id: None)
    monkeypatch.setattr(registry, "retire_provider_instances", lambda plugin_id, grace: [])
    monkeypatch.setattr(compat_service, "reset_compat_pool", lambda: None)
    monkeypatch.setattr(leases, "detach_service", lambda plugin_id: [])
    monkeypatch.setattr(translator, "_store", lambda: SimpleNamespace(
        release_plugin=lambda plugin_id: SimpleNamespace(notices=[]), delete_plugin_data=lambda plugin_id: None,
        ensure_instance=lambda plugin_id: None))
    for name in ("plugin_changed", "plugin_kept", "plugin_installed", "discard_folders"):
        monkeypatch.setattr(worker_binding, name, lambda plugin_id: None)
    monkeypatch.setattr(worker_binding, "plugin_detached", lambda plugin_id, grace: [])
    monkeypatch.setattr(worker_binding, "sweep_removed_plugins", lambda: None)
    monkeypatch.setattr(detach, "snapshot_workers", lambda plugin_id: [])
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda: True)
    hooks = {
        "subtitle_provider": subtitle_provider.SubtitleProviderKind(),
        "translator": translator.TranslatorKind(), "service": ServiceKind(),
    }
    observed_hooks = ("on_removed", "after_removed", "removal_finished", "discard_kept_data", "kept_data_reclaimed")
    for name, hook in hooks.items():
        for method in observed_hooks:
            monkeypatch.setattr(hook, method, Mock(wraps=getattr(hook, method)))
        monkeypatch.setitem(kinds._KINDS, name, hook)
    kind = hooks[plugin_kind]
    monkeypatch.setattr(service, "provider_hub_dir", lambda: tmp_path)
    monkeypatch.setattr(catalog_feed, "counter_file", lambda: tmp_path / "plugins" / catalog_feed.COUNTER_FILE_NAME)
    manifest = {"schema_version": 2, "kind": plugin_kind, "permissions": ["data_dir"], "version": "1.0.0"}
    active = datadir.release_path(tmp_path, "engine", "a" * 32)
    active.mkdir(parents=True)
    (active / "saved.txt").write_text("saved state")
    retained = datadir.plugin_root(tmp_path, "engine") / "kept"
    marker = retained / "saved.txt"
    owner = {
        "source_id": "official", "origin": "catalog", "kind": plugin_kind, "config": {"region": "eu"},
    }
    mutate_state(lambda state: state["installations"].update(engine=_installation(
        "engine", kind=plugin_kind, source_id="official", origin="catalog", manifest=manifest,
        active_data_key=active.name, config={"region": "eu"})))

    assert service.remove_installation("engine", keep_data=True) is True
    assert detach.wait_idle(timeout=5)

    stored = json.loads(raw_row(hub_engine)[1])
    assert "engine" not in stored["installations"]
    assert stored["kept_data"]["engine"] == owner
    kind.on_removed.assert_called_once_with("engine")
    kind.after_removed.assert_called_once_with("engine", keep_data=True)
    kind.removal_finished.assert_called_once_with("engine")
    assert not active.exists()
    assert marker.read_text() == "saved state"

    env_path = tmp_path / "envs" / "fixture"
    interpreter = service.python_executable(env_path)
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(service.PluginEnvironment, "install", lambda *args, **kwargs: env_path)
    monkeypatch.setattr(service, "_smoke_validate_worker", lambda *args, **kwargs: None)
    loaded_entries = []

    def load_translator_fixture(self, validated, bundle, interpreter, data, timeout):
        manifest = validated.raw
        entry = f"{manifest['entry_module']}:{manifest['entry_class']}"
        assert validated.provider_id == "engine" and entry == "translator_fixture:FixtureTranslator"
        check = subprocess.run([sys.executable, "-I", "-B", "-c", """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from bazarr_plugin_sdk.runner import load_plugin
plugin, _ = load_plugin(sys.argv[3], [sys.argv[2]])
module = sys.modules[type(plugin).__module__]
assert Path(module.__file__).resolve().parent == Path(sys.argv[2]).resolve()
assert plugin.describe() == {"backend_types": ["fixture"]}
""", str(worker_binding._sdk_root()), str(bundle), entry], capture_output=True, text=True, timeout=10)
        assert check.returncode == 0, check.stderr
        loaded_entries.append(entry)

    monkeypatch.setattr(worker_binding.TranslatorWorkers, "smoke", load_translator_fixture)
    monkeypatch.setattr(consent, "sandbox_status", lambda: {})

    version = "2.0.0"
    if incoming_kind == "subtitle_provider":
        files = {"provider.py": b"class ExampleProvider: pass\n"}
        incoming = provider_manifest_v2("engine", version, permissions=["data_dir"], file_payloads=files)
    else:
        if incoming_kind == "translator":
            files = {"translator_fixture.py": b"""from bazarr_plugin_sdk.translator.v1 import TranslatorPlugin

class FixtureTranslator(TranslatorPlugin):
    def describe(self):
        return {"backend_types": ["fixture"]}

    async def translate(self, request, ctx):
        return {"lines": request["lines"]}
"""}
            contract = {"kind_api": translator.TRANSLATOR_KIND_API, "settings_version": 1, "settings": {"fields": []},
                        "entry_module": "translator_fixture", "entry_class": "FixtureTranslator"}
        else:
            files = {"service.py": b"class Service: pass\n"}
            contract = {"kind_api": SERVICE_KIND_API, "provides": ["engine.v1"]}
        incoming = service_manifest("engine", version, kind=incoming_kind, permissions=["data_dir"],
                                    file_payloads=files, **contract)

    if source is None:
        result = service.stage_install_local(_provider_zip(incoming, files))
    else:
        # Seed the cached catalog entry as a refresh leaves it and serve its bundle files offline.
        trusted = source == "official"
        catalog_source = verified_official_source() if trusted else {
            "id": source, "name": "Community", "type": "github", "enabled": True, "trusted": False,
        }
        source_name = catalog_source["name"]
        entry = {"source": source, "source_name": source_name, "provider_id": "engine", "name": incoming["name"],
                 "version": version, "trusted": trusted, "manifest": incoming, "catalog_list": "plugins"}

        def seed_catalog(state):
            state["catalog_sources"][source] = catalog_source
            state["catalog_entries"][f"{source_name}:plugins:engine:{version}"] = entry

        mutate_state(seed_catalog)
        monkeypatch.setattr(service, "_fetch_github_bundle_file",
                            lambda validated, path, **kwargs: files[path])
        digest = service.install_consent(source, "engine", version, catalog_list="plugins")["digest"]
        result = service.install_catalog_entry(source, "engine", version, consent_digest=digest)

    staged = load_state()["installations"]["engine"]
    assert result["state"] == staged["state"] == "staged"
    assert staged["staged_version"] == version
    assert staged["kind"] == incoming_kind
    assert loaded_entries == (["translator_fixture:FixtureTranslator"] if incoming_kind == "translator" else [])
    assert staged["origin"] == ("local" if source is None else "catalog")
    assert staged["source_id"] == source
    staged_dir = datadir.release_path(tmp_path, "engine", staged["staged_data_key"])
    assert staged_dir != active
    staged_marker = staged_dir / marker.name
    stored = json.loads(raw_row(hub_engine)[1])
    saved_config = stored["installations"]["engine"]["config"]
    if reclaims:
        kind.discard_kept_data.assert_not_called()
        kind.kept_data_reclaimed.assert_called_once_with("engine")
        assert stored["kept_data"]["engine"] == {**owner, "config": None}
        assert saved_config == owner["config"]
        assert marker.read_text() == "saved state"
        assert staged_marker.read_text() == "saved state"
    else:
        kind.discard_kept_data.assert_called_once_with("engine")
        kind.kept_data_reclaimed.assert_not_called()
        assert "engine" not in stored["kept_data"]
        assert saved_config == {}
        assert not retained.exists()
        assert staged_dir.is_dir() and not staged_marker.exists()
    for name, hook in hooks.items():
        if name != plugin_kind:
            for method in observed_hooks:
                getattr(hook, method).assert_not_called()


def test_a_plugins_saved_settings_come_back_with_its_kept_data_once(hub_store, hub_engine, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state

    kind = _Kind()
    monkeypatch.setattr(service, "get_kind", lambda name: kind)
    _removal(service, kind, "engine", True, config={"base_url": "https://x.test", "api_key": "enc:abc"})
    _stage_row("engine")

    assert service.claim_kept_data("engine", "official", "translator") is True

    state = load_state()
    assert state["installations"]["engine"]["config"] == {"base_url": "https://x.test", "api_key": "enc:abc"}
    # The copy of the settings in the record is dropped once they are back on the row.
    assert state["kept_data"]["engine"]["config"] is None


def test_a_claim_that_cannot_clear_what_was_kept_refuses_the_install_and_keeps_the_record(
        hub_store, hub_engine, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state

    kind = _Kind()
    kind.fail_discard = True
    monkeypatch.setattr(service, "get_kind", lambda name: kind)
    _removal(service, kind, "engine", True)

    with pytest.raises(service.ProviderHubInstallError, match="could not be cleared"):
        service.claim_kept_data("engine", "community", "translator")

    assert "engine" in load_state()["kept_data"]


def test_a_removal_that_deletes_the_data_forgets_what_was_kept_and_a_kind_with_nothing_to_keep_records_nothing(
        hub_store, hub_engine, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state

    kind = _Kind()
    monkeypatch.setattr(service, "get_kind", lambda name: kind)
    _removal(service, kind, "engine", True)
    _removal(service, kind, "engine", False)
    assert "engine" not in load_state()["kept_data"]

    kind.keeps_data = False
    _removal(service, kind, "service", True, kind="service")
    assert "service" not in load_state()["kept_data"]
    # Nothing was kept for it, so a first install has nothing to discard either.
    assert service.claim_kept_data("service", "official", "service") is False
    assert kind.discarded == []
