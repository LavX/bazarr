# coding=utf-8
"""Boot reconcile and environment repair resolve each installation's kind.

A translator installation is validated and checked by its own kind, not by the subtitle provider
validator. A subtitle installation with a manifest that does not validate is still refused on both paths.
"""
import os
import sys
from pathlib import Path

import pytest

from plugin_platform_helpers import service_manifest
from provider_hub import env_registry
from provider_hub.state import load_state, save_state

PLUGIN = "lingarr"
SOURCE = b"class Service: pass\n"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from provider_hub import kinds
    from provider_hub.kinds.translator import register_translator_kind

    root = tmp_path / "provider_hub"
    root.mkdir()
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(root / "state.json"))
    before = kinds._KINDS.pop("translator", None)
    register_translator_kind()
    try:
        yield root
    finally:
        kinds._KINDS.pop("translator", None)
        if before is not None:
            kinds._KINDS["translator"] = before


def _env(root, name):
    env = root / "shared_envs" / name
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("", encoding="utf-8")
    (env / "pyvenv.cfg").write_text(
        f"home = /usr/bin\nversion = {sys.version_info.major}.{sys.version_info.minor}.0\n", encoding="utf-8")
    return env


def _translator_manifest(plugin_id=PLUGIN):
    return service_manifest(plugin_id, "1.0.0", kind="translator", kind_api="bazarr.translator.v1",
                            settings_version=1, settings={"fields": []})


def _translator_row(root, env, plugin_id=PLUGIN, **extra):
    bundle = Path(os.environ["BAZARR_PROVIDER_HUB_STATE"]).parent / "bundles" / plugin_id / "1.0.0" / "c"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "service.py").write_bytes(SOURCE)
    row = {
        "provider_id": plugin_id, "name": plugin_id.title(), "kind": "translator", "active_version": "1.0.0",
        "active_path": str(bundle), "python_path": str(env / "bin" / "python"), "state": "active",
        "pending_restart": False, "manifest": _translator_manifest(plugin_id), "last_error": None,
    }
    row.update(extra)
    return row


def _subtitle_row_with_invalid_manifest(root, env):
    bundle = Path(os.environ["BAZARR_PROVIDER_HUB_STATE"]).parent / "bundles" / "badhub" / "1.0.0" / "c"
    bundle.mkdir(parents=True, exist_ok=True)
    return {
        "provider_id": "badhub", "name": "Bad Hub", "active_version": "1.0.0", "active_path": str(bundle),
        "python_path": str(env / "bin" / "python"), "state": "active", "pending_restart": False,
        "manifest": {"schema_version": 1, "provider_id": "badhub"}, "last_error": None,
    }


def _save(*rows):
    state = load_state()
    state["installations"] = {row["provider_id"]: row for row in rows}
    save_state(state)


class _BuildsEnvironment:
    def __init__(self, root):
        self.root = Path(root)

    def install(self, validated, timeout=None, *, rebuild=False):
        return _env(self.root, "rebuilt")


def test_a_translator_installation_is_adopted_by_boot_reconcile_and_checked_by_its_kind(hub, monkeypatch):
    from provider_hub import service
    from provider_hub.kinds import get_kind

    first, second = _env(hub, "first"), _env(hub, "second")
    _save(_translator_row(hub, first), _translator_row(hub, second, plugin_id="lingarr_two"))
    probed = []
    monkeypatch.setattr(get_kind("translator"), "smoke_test",
                        lambda validated, bundle, python: probed.append((validated.provider_id, python)),
                        raising=False)
    monkeypatch.setattr(service, "_smoke_validate_worker",
                        lambda *args, **kwargs: pytest.fail("a translator is not started as a subtitle provider"))

    summary = service.reconcile_plugin_environments()

    state = load_state()
    assert summary["adopted"] == ["lingarr"]
    assert summary["repointed"] == ["lingarr_two"]
    assert summary["needs_repair"] == []
    assert probed == [("lingarr_two", first / "bin" / "python")]
    assert state["installations"]["lingarr_two"]["python_path"] == str(first / "bin" / "python")
    assert not state["installations"]["lingarr"].get("needs_repair")


def test_a_translator_installation_can_be_repaired(hub, monkeypatch):
    from provider_hub import service
    from provider_hub.kinds import get_kind

    old = _env(hub, "old")
    _save(_translator_row(hub, old, needs_repair=True, last_error=env_registry.NEEDS_REPAIR_MESSAGE))
    monkeypatch.setattr(service, "PluginEnvironment", _BuildsEnvironment)
    # The real registration and retire steps run; they are watched, not replaced.
    from provider_hub import registry

    registered, retired = [], []
    real_register, real_retire = registry.register_active_provider_classes, registry.retire_provider_instances
    monkeypatch.setattr(registry, "register_active_provider_classes",
                        lambda installations=None: registered.append(installations) or real_register(installations))
    monkeypatch.setattr(registry, "retire_provider_instances",
                        lambda provider_id: retired.append(provider_id) or real_retire(provider_id))
    probed = []
    monkeypatch.setattr(get_kind("translator"), "smoke_test",
                        lambda validated, bundle, python: probed.append(python), raising=False)
    monkeypatch.setattr(service, "_smoke_validate_worker",
                        lambda *args, **kwargs: pytest.fail("a translator is not started as a subtitle provider"))

    service.repair_installation_environment(PLUGIN)

    new = hub / "shared_envs" / "rebuilt"
    row = load_state()["installations"][PLUGIN]
    assert probed == [new / "bin" / "python"]
    assert row["python_path"] == str(new / "bin" / "python")
    assert "needs_repair" not in row
    assert row["last_error"] is None
    # A translator has no subtitle provider class to register and no pooled workers to retire.
    assert registered == [] and retired == []


def test_a_subtitle_installation_with_an_invalid_manifest_is_still_refused_by_boot_reconcile(hub):
    from provider_hub import service

    env = _env(hub, "bad")
    _save(_subtitle_row_with_invalid_manifest(hub, env))

    summary = service.reconcile_plugin_environments()

    state = load_state()
    assert summary["adopted"] == [] and summary["repointed"] == []
    assert state.get("plugin_envs", {}) == {}


def test_a_subtitle_installation_with_an_invalid_manifest_is_still_refused_by_repair(hub, monkeypatch):
    from provider_hub import service
    from provider_hub.manifest import ManifestValidationError

    env = _env(hub, "bad")
    _save(_subtitle_row_with_invalid_manifest(hub, env))
    monkeypatch.setattr(service, "PluginEnvironment",
                        lambda root: pytest.fail("nothing is built for a manifest that does not validate"))

    with pytest.raises(ManifestValidationError):
        service.repair_installation_environment("badhub")

    assert load_state()["installations"]["badhub"]["python_path"] == str(env / "bin" / "python")
