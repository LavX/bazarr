# coding=utf-8
"""An update is offered only to an installation of the same kind, and only for a kind the install path stages."""
import json

import pytest

from plugin_platform_helpers import service_manifest
from test_provider_hub import _manifest

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


def _translator_manifest(version):
    return service_manifest(PLUGIN, version, kind="translator", kind_api="bazarr.translator.v1",
                            settings_version=1, settings={"fields": []})


def _entry(manifest):
    return {"source": "official", "source_name": "Official", "provider_id": PLUGIN, "name": "Swap Hub",
            "version": manifest["version"], "trusted": True, "manifest": manifest}


def _write_state(tmp_path, monkeypatch, installed_manifest, installed_kind, *entries):
    row = {
        "provider_id": PLUGIN, "name": "Swap Hub", "active_version": "1.0.0", "active_path": "/old/bundle",
        "python_path": "/old/python", "state": "active", "pending_restart": False, "source_bound": True,
        "source_id": "official", "manifest": installed_manifest,
    }
    if installed_kind:
        row["kind"] = installed_kind
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "catalog_entries": {f"official:{PLUGIN}:{e['version']}": e for e in entries},
        "installations": {PLUGIN: row}, "jobs": [],
    }), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))


def _fail_if_staged(monkeypatch):
    monkeypatch.setattr("provider_hub.service.stage_install",
                        lambda *args, **kwargs: pytest.fail("nothing is staged for this installation"))


def test_a_translator_release_is_offered_to_a_translator_installation_and_staged_by_its_kind(tmp_path, monkeypatch):
    from provider_hub.service import _latest_catalog_manifest, apply_update, check_updates
    from provider_hub.state import load_state

    _write_state(tmp_path, monkeypatch, _translator_manifest("1.0.0"), "translator",
                 _entry(_translator_manifest("2.0.0")))
    state = load_state()
    state["catalog_sources"]["official"] = {"id": "official", "trusted": True}
    from provider_hub.state import save_state
    save_state(state)
    staged = []
    monkeypatch.setattr("provider_hub.service.stage_install",
                        lambda manifest, **kwargs: staged.append((manifest["version"], kwargs)) or
                        {"state": "staged", "staged_version": manifest["version"]})

    assert _latest_catalog_manifest(
        load_state(), PLUGIN, "1.0.0", "official", installed_kind="translator")["version"] == "2.0.0"
    assert check_updates()["details"]["updates_available"] == 1
    provider = apply_update(PLUGIN)

    assert provider["staged_version"] == "2.0.0"
    assert staged == [("2.0.0", {"checkpoint": None, "source_id": "official"})]


def test_a_candidate_of_another_kind_is_not_offered_to_a_subtitle_installation(tmp_path, monkeypatch):
    from provider_hub.service import _latest_catalog_manifest, apply_update, check_updates
    from provider_hub.state import load_state

    _write_state(tmp_path, monkeypatch, _manifest(provider_id=PLUGIN, version="1.0.0"), None,
                 _entry(_translator_manifest("2.0.0")))
    _fail_if_staged(monkeypatch)

    assert _latest_catalog_manifest(load_state(), PLUGIN, "1.0.0", "official") is None
    assert check_updates()["details"]["updates_available"] == 0
    assert apply_update(PLUGIN)["state"] == "active"


def test_a_subtitle_update_is_still_selected_and_staged(tmp_path, monkeypatch):
    from provider_hub.service import _latest_catalog_manifest, apply_update
    from provider_hub.state import load_state

    update = _manifest(provider_id=PLUGIN, version="1.1.0")
    _write_state(tmp_path, monkeypatch, _manifest(provider_id=PLUGIN, version="1.0.0"), None,
                 _entry(update), _entry(_translator_manifest("1.2.0")))
    monkeypatch.setattr("provider_hub.service._fetch_bundle", lambda manifest, deadline=None: tmp_path / "bundle")

    class FakeEnvironment:
        def __init__(self, root):
            self.root = root

        def install(self, validated, timeout=None):
            # Recording a row refuses an environment whose interpreter is not there.
            (tmp_path / "env" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "env" / "bin" / "python").write_text("", encoding="utf-8")
            return tmp_path / "env"

    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service.python_executable", lambda env_path: tmp_path / "python")
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    assert _latest_catalog_manifest(load_state(), PLUGIN, "1.0.0", "official")["version"] == "1.1.0"
    provider = apply_update(PLUGIN)

    assert provider["state"] == "staged"
    assert provider["pending_restart"] is True
    assert provider["staged_version"] == "1.1.0"


def test_a_manifest_the_install_path_refuses_is_a_refused_update(tmp_path, monkeypatch):
    from provider_hub.manifest import ManifestValidationError
    from provider_hub.service import apply_update
    from provider_hub.state import load_state

    _write_state(tmp_path, monkeypatch, _manifest(provider_id=PLUGIN, version="1.0.0"), None,
                 _entry(_manifest(provider_id=PLUGIN, version="1.1.0")))

    def refuse(*args, **kwargs):
        raise ManifestValidationError("not a manifest this install path accepts")

    monkeypatch.setattr("provider_hub.service.stage_install", refuse)

    provider = apply_update(PLUGIN)

    assert provider["state"] == "active"
    assert provider["last_error"] == "not a manifest this install path accepts"
    assert load_state()["installations"][PLUGIN]["last_error"] == "not a manifest this install path accepts"
    job = next(item for item in load_state()["jobs"] if item["target_id"] == PLUGIN)
    assert job["state"] == "failed"
    assert job["error"] == "not a manifest this install path accepts"
