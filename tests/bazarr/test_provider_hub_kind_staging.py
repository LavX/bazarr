# coding=utf-8
"""Staging uses the kind a manifest belongs to, for catalog installs and local packages alike."""
import io
import zipfile

import pytest

from plugin_platform_helpers import provider_manifest_v2, service_manifest, verified_official_source, write_state

PLUGIN = "swaphub"


@pytest.fixture(autouse=True)
def translator_kind():
    from provider_hub import kinds
    from provider_hub.kinds.translator import register_translator_kind

    before = kinds._KINDS.pop("translator", None)
    register_translator_kind()
    try:
        yield
    finally:
        kinds._KINDS.pop("translator", None)
        if before is not None:
            kinds._KINDS["translator"] = before


def _translator(version="1.0.0"):
    return service_manifest(PLUGIN, version, kind="translator", kind_api="bazarr.translator.v1",
                            settings_version=1, settings={"fields": []})


def _official_entry(manifest, catalog_list="plugins"):
    return {"source": "official", "source_name": "Official", "provider_id": manifest.get("id") or manifest["provider_id"],
            "name": manifest["name"], "version": manifest["version"], "trusted": True, "manifest": manifest,
            "catalog_list": catalog_list}


@pytest.fixture
def staged_world(tmp_path, monkeypatch):
    """The install path with the network, the environment build and the worker replaced."""
    from types import SimpleNamespace

    calls = SimpleNamespace(smoke=[], worker=[], installed=[])

    class FakeEnvironment:
        def __init__(self, root):
            self.root = root

        def install(self, validated, timeout=None):
            (tmp_path / "env" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "env" / "bin" / "python").write_text("", encoding="utf-8")
            return tmp_path / "env"

    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: {})
    monkeypatch.setattr("provider_hub.service._fetch_bundle", lambda manifest, deadline=None: tmp_path / "bundle")
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service.python_executable", lambda env_path: tmp_path / "python")
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker",
                        lambda *args, **kwargs: calls.worker.append(args[0].kind))

    from provider_hub.kinds.translator import TranslatorKind

    def translator_smoke(self, validated, bundle_path, python_path, timeout=None):
        calls.smoke.append((validated.kind, timeout))

    def translator_installed(self, plugin_id, missing_required=()):
        calls.installed.append((plugin_id, tuple(missing_required)))
        return True

    monkeypatch.setattr(TranslatorKind, "smoke_test", translator_smoke)
    monkeypatch.setattr(TranslatorKind, "on_installed", translator_installed)
    state_file = write_state(
        tmp_path / "state.json",
        catalog_sources={"official": verified_official_source()},
        catalog_entries={f"official:plugins:{PLUGIN}:1.0.0": _official_entry(_translator())},
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    return calls


def test_a_translator_entry_is_staged_by_its_own_kind(staged_world):
    from provider_hub.service import install_catalog_entry, install_consent
    from provider_hub.state import load_state

    digest = install_consent("official", PLUGIN, "1.0.0", catalog_list="plugins")["digest"]

    row = install_catalog_entry("official", PLUGIN, "1.0.0", consent_digest=digest)

    assert row["state"] == "staged" and row["kind"] == "translator"
    assert load_state()["installations"][PLUGIN]["staged_manifest"]["kind"] == "translator"
    # The translator kind was smoke tested and told about the install, and the provider worker check never ran.
    assert staged_world.smoke == [("translator", None)]
    assert staged_world.worker == []
    assert staged_world.installed == [(PLUGIN, ())]


def test_a_translator_install_on_a_time_budget_hands_the_kind_what_is_left(staged_world):
    from provider_hub.service import stage_install

    stage_install(_translator(), install_timeout=60.0, source_id="official", catalog_list="plugins")

    kind, timeout = staged_world.smoke[0]
    assert kind == "translator" and 0 < timeout <= 10.0


def test_a_translator_manifest_is_refused_when_the_kind_is_not_defined(staged_world):
    from provider_hub import kinds
    from provider_hub.manifest import ManifestValidationError
    from provider_hub.service import stage_install

    kinds._KINDS.pop("translator")

    with pytest.raises(ManifestValidationError, match="does not install this kind of plugin"):
        stage_install(_translator(), source_id="official", catalog_list="plugins")


def test_a_manifest_whose_kind_does_not_match_its_contract_is_refused(staged_world):
    from provider_hub.manifest import ManifestValidationError
    from provider_hub.service import stage_install

    manifest = _translator()
    manifest["kind"] = "subtitle_provider"

    with pytest.raises(ManifestValidationError):
        stage_install(manifest, source_id="official", catalog_list="plugins")


def test_a_subtitle_provider_is_still_staged_by_the_worker_check(staged_world, tmp_path):
    from provider_hub.service import stage_install

    manifest = provider_manifest_v2("subhub")
    row = stage_install(manifest, source_id=None, catalog_list="providers")

    assert row["state"] == "staged" and row["kind"] == "subtitle_provider"
    assert staged_world.worker == ["subtitle_provider"]
    assert staged_world.smoke == []


def test_a_local_package_of_a_translator_is_refused_with_its_reason(staged_world, tmp_path):
    from provider_hub.service import ProviderHubInstallError, stage_install_local

    manifest = _translator()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        import json
        package.writestr("provider.json", json.dumps(manifest))
        package.writestr("service.py", "class Service: pass\n")

    with pytest.raises(ProviderHubInstallError, match="only be a subtitle provider"):
        stage_install_local(buffer.getvalue())

    assert staged_world.smoke == [] and staged_world.worker == []


def test_every_kind_is_told_about_a_first_install_with_the_missing_fields(monkeypatch, tmp_path):
    from provider_hub import kinds
    from provider_hub.kinds import Kind, register_kind, unregister_kind

    seen = []

    class RecordingKind(Kind):
        name = "recording"
        kind_apis = frozenset({"bazarr.recording.v1"})

        def on_installed(self, plugin_id, missing_required=()):
            seen.append((plugin_id, tuple(missing_required)))
            return True

        def validate_manifest(self, manifest, trusted):
            from provider_hub.manifest import validate_plugin_bundle

            return validate_plugin_bundle(manifest)

    register_kind(RecordingKind())
    try:
        from provider_hub import service

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
        manifest = service_manifest("recorded", "1.0.0", kind="recording", kind_api="bazarr.recording.v1")
        monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(
            tmp_path / "state.json", catalog_sources={"official": verified_official_source()},
            catalog_entries={"official:plugins:recorded:1.0.0": _official_entry(manifest)})))

        digest = service.install_consent("official", "recorded", "1.0.0", catalog_list="plugins")["digest"]
        service.install_catalog_entry("official", "recorded", "1.0.0", consent_digest=digest)
    finally:
        unregister_kind("recording")
    assert seen == [("recorded", ())]
    assert kinds.kind_for_api("bazarr.recording.v1") is None


def test_the_translator_kinds_own_install_hook_accepts_the_missing_fields(monkeypatch):
    from types import SimpleNamespace

    from provider_hub.kinds import translator
    from provider_hub.kinds.translator import TranslatorKind

    created = []
    monkeypatch.setattr(translator, "_store", lambda: SimpleNamespace(ensure_instance=created.append))

    assert TranslatorKind().on_installed(PLUGIN, missing_required=("api_key",)) is True
    assert created == [PLUGIN]
