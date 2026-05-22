# coding=utf-8
import base64
import json
import os
import subprocess
from pathlib import Path

import pytest
from subzero.language import Language
from subliminal.video import Movie


def _sha256(value):
    import hashlib
    return hashlib.sha256(value).hexdigest()


def _bundle_sha256(file_payloads):
    import hashlib
    digest = hashlib.sha256()
    for relative_path, content in sorted(file_payloads.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest(**overrides):
    provider_content = overrides.pop("provider_content", b"class ExampleProvider: pass\n")
    file_payloads = overrides.pop("file_payloads", {"provider.py": provider_content})
    files = overrides.pop("files", {path: _sha256(content) for path, content in file_payloads.items()})
    bundle_digest = overrides.pop("bundle_sha256", _bundle_sha256(file_payloads))
    manifest = {
        "schema_version": 1,
        "provider_id": "examplehub",
        "name": "Example Hub Provider",
        "version": "1.0.0",
        "api_version": "bazarr.provider-hub.v1",
        "entry_module": "provider",
        "entry_class": "ExampleProvider",
        "config_schema": {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "secret": True},
            },
        },
        "secret_fields": ["api_key"],
        "supported_media": ["movie", "episode"],
        "languages": ["eng", "spa"],
        "files": files,
        "bundle_sha256": bundle_digest,
        "source": {
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": "b" * 40,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": False,
        },
        "dependencies": {
            "python": ">=3.12",
            "requirements": [
                {
                    "name": "cloudscraper",
                    "version": "1.2.58",
                    "hashes": ["sha256:" + ("c" * 64)],
                },
            ],
        },
    }
    manifest.update(overrides)
    return manifest


class _FakeResponse:
    def __init__(self, payload=None, content=b""):
        self.payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_manifest_accepts_valid_github_bundle():
    from provider_hub.manifest import validate_manifest

    validated = validate_manifest(_manifest(), built_in_provider_ids={"opensubtitles"})

    assert validated.provider_id == "examplehub"
    assert validated.trusted is False
    assert validated.files == {"provider.py": _sha256(b"class ExampleProvider: pass\n")}
    assert validated.source_path == ""
    assert validated.dependency_requirements[0].pip_line == (
        "cloudscraper==1.2.58 --hash=sha256:" + ("c" * 64)
    )


def test_manifest_accepts_safe_github_source_path():
    from provider_hub.manifest import validate_manifest

    manifest = _manifest(
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": "b" * 40,
            "path": "providers/smoke",
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": False,
        }
    )

    validated = validate_manifest(manifest, built_in_provider_ids=set())

    assert validated.source_path == "providers/smoke"


@pytest.mark.parametrize("bad_source_path", ["../smoke", "/providers/smoke", "providers/../smoke", "providers\\smoke"])
def test_manifest_rejects_unsafe_github_source_path(bad_source_path):
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": "b" * 40,
            "path": bad_source_path,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": False,
        }
    )

    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest, built_in_provider_ids=set())


@pytest.mark.parametrize(
    "bad_path",
    [
        "../provider.py",
        "/tmp/provider.py",
        "provider.txt",
        "pkg/../provider.py",
        "pkg/native.so",
    ],
)
def test_manifest_rejects_unsafe_declared_files(bad_path):
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(files={bad_path: "a" * 64})

    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest, built_in_provider_ids=set())


def test_manifest_rejects_secret_field_missing_from_config_schema():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(secret_fields=["missing_secret"])

    with pytest.raises(ManifestValidationError, match="secret field"):
        validate_manifest(manifest, built_in_provider_ids=set())


def test_manifest_rejects_entry_module_not_declared():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(entry_module="missing")

    with pytest.raises(ManifestValidationError, match="entry_module"):
        validate_manifest(manifest, built_in_provider_ids=set())


def test_manifest_rejects_built_in_provider_shadowing():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(provider_id="opensubtitles")

    with pytest.raises(ManifestValidationError, match="built-in"):
        validate_manifest(manifest, built_in_provider_ids={"opensubtitles"})


@pytest.mark.parametrize(
    "requirement",
    [
        {"name": "cloudscraper", "version": ">=1.2.58", "hashes": ["sha256:" + ("c" * 64)]},
        {"name": "cloudscraper", "version": "1.2.58", "hashes": []},
        {"name": "git+https://github.com/x/y", "version": "1.0.0", "hashes": ["sha256:" + ("c" * 64)]},
        {"name": "pkg; os_name=='posix'", "version": "1.0.0", "hashes": ["sha256:" + ("c" * 64)]},
    ],
)
def test_manifest_rejects_unlocked_or_unsafe_dependencies(requirement):
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(dependencies={"requirements": [requirement]})

    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest, built_in_provider_ids=set())


def test_worker_protocol_round_trips_language_video_and_download_payload():
    from provider_hub.protocol import (
        candidate_from_worker,
        language_to_payload,
        video_to_payload,
        worker_download_to_content,
    )

    language = Language("eng", hi=True)
    movie = Movie(
        "/media/example.mkv",
        "Example Movie",
        year=2024,
        source="Web",
        release_group="GROUP",
        resolution="1080p",
        video_codec="H.264",
        audio_codec="AAC",
        imdb_id="tt1234567",
    )
    movie.hashes["opensubtitles"] = "abc123"
    movie.radarrId = 12

    candidate = candidate_from_worker(
        provider_name="examplehub",
        payload={
            "provider": "upstream",
            "id": "sub-1",
            "language": language_to_payload(language),
            "release_info": "Example.Movie.2024.1080p-GROUP",
            "filename": "example.srt",
            "matches": ["title", "year", "hash"],
            "score": 360,
            "score_without_hash": 300,
            "score_out_of": 360,
            "hash_verifiable": True,
            "hearing_impaired_verifiable": True,
            "display": {"download_count": 7, "ratings": 4.5},
            "provider_payload": {"provider": "upstream", "schema": 1, "data": {"file_id": "sub-1"}},
        },
    )

    assert language_to_payload(language)["hi"] is True
    assert video_to_payload(movie)["hashes"]["opensubtitles"] == "abc123"
    assert video_to_payload(movie)["media_ids"]["radarrId"] == 12
    assert candidate.provider_name == "examplehub"
    assert candidate.source_provider == "upstream"
    assert candidate.id == "upstream:sub-1"
    assert candidate.matches == {"title", "year", "hash"}
    assert candidate.provider_payload["data"]["file_id"] == "sub-1"

    content = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
    worker_download_to_content(
        candidate,
        {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_sha256": _sha256(content),
            "content_type": "application/x-subrip",
            "empty": False,
        },
    )
    assert candidate.content == content


def test_venv_installer_uses_isolated_hash_checked_pip(monkeypatch, tmp_path):
    from provider_hub.venv import PluginEnvironment
    from provider_hub.manifest import validate_manifest

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    env = PluginEnvironment(tmp_path)
    env.install(validate_manifest(_manifest(), built_in_provider_ids=set()))

    pip_calls = [
        cmd
        for cmd, _kwargs in calls
        if len(cmd) > 3 and cmd[1:4] == ["-m", "pip", "install"]
    ]
    assert pip_calls, calls
    install_cmd = " ".join(pip_calls[-1])
    assert "--require-hashes" in install_cmd
    assert "--only-binary=:all:" in install_cmd
    assert "--no-warn-script-location" in install_cmd
    assert "-r" in install_cmd
    assert "/usr/local" not in install_cmd
    assert "custom_libs" not in install_cmd
    requirements_files = list(tmp_path.glob("envs/examplehub/1.0.0/*/requirements.txt"))
    assert len(requirements_files) == 1
    requirements_text = requirements_files[0].read_text(encoding="utf-8")
    assert "cloudscraper==1.2.58" in requirements_text
    assert "--hash=sha256:" + ("c" * 64) in requirements_text


def test_active_provider_hub_installation_registers_proxy(tmp_path, monkeypatch):
    from provider_hub.registry import register_active_provider_classes
    from subliminal_patch.extensions import provider_registry

    provider_id = "examplehub"
    if provider_id in provider_registry:
        del provider_registry[provider_id]

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Example Hub Provider",
                        "active_version": "1.0.0",
                        "state": "active",
                        "pending_restart": False,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    register_active_provider_classes()

    assert provider_id in provider_registry.names()
    provider_cls = provider_registry[provider_id]
    assert provider_cls.provider_name == provider_id
    assert provider_cls.languages


def test_get_providers_registers_active_provider_hub_installation(tmp_path, monkeypatch):
    from app import get_providers
    from subliminal_patch.extensions import provider_registry

    provider_id = "autohub"
    if provider_id in provider_registry:
        del provider_registry[provider_id]

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Auto Hub Provider",
                        "active_version": "1.0.0",
                        "state": "active",
                        "pending_restart": False,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", [provider_id], raising=False)

    assert get_providers.get_providers() == [provider_id]


def test_provider_hub_database_tables_are_registered():
    from app.database import (
        TableProviderHubCatalogEntry,
        TableProviderHubCatalogSource,
        TableProviderHubConfig,
        TableProviderHubInstallation,
        TableProviderHubInstallEvent,
        TableProviderHubJob,
        TableProviderHubSecret,
        metadata,
    )

    expected = {
        "provider_hub_catalog_sources": TableProviderHubCatalogSource,
        "provider_hub_catalog_entries": TableProviderHubCatalogEntry,
        "provider_hub_installations": TableProviderHubInstallation,
        "provider_hub_config": TableProviderHubConfig,
        "provider_hub_secrets": TableProviderHubSecret,
        "provider_hub_jobs": TableProviderHubJob,
        "provider_hub_install_events": TableProviderHubInstallEvent,
    }

    for table_name, model in expected.items():
        assert table_name in metadata.tables
        assert model.__tablename__ == table_name


def test_provider_hub_api_namespace_is_registered():
    from api import api_ns_list
    from api.provider_hub import api_ns_provider_hub

    assert any(api_ns_provider_hub in group for group in api_ns_list)


def test_provider_hub_scheduler_task_is_registered_on_scheduler():
    from app.scheduler import Scheduler
    from unittest.mock import MagicMock

    instance = Scheduler.__new__(Scheduler)
    instance.aps_scheduler = MagicMock()

    Scheduler._Scheduler__provider_hub_update_task(instance)

    _, args, kwargs = instance.aps_scheduler.add_job.mock_calls[0]
    assert args[1] == "interval"
    assert kwargs["id"] == "provider_hub_update_check"
    assert kwargs["name"] == "Check Provider Hub Updates"
    assert kwargs["replace_existing"] is True


def test_provider_hub_update_task_refreshes_catalog_before_checking_updates(monkeypatch):
    from provider_hub import tasks

    calls = []
    monkeypatch.setattr(tasks, "refresh_catalog", lambda: calls.append("refresh"))
    monkeypatch.setattr(tasks, "check_updates", lambda: calls.append("check") or {"state": "completed"})

    assert tasks.provider_hub_check_updates() == {"state": "completed"}
    assert calls == ["refresh", "check"]


def test_provider_hub_updates_check_endpoint_refreshes_catalog_before_check(monkeypatch):
    from api.provider_hub.provider_hub import ProviderHubUpdatesCheck
    from api.provider_hub import provider_hub

    calls = []
    monkeypatch.setattr(provider_hub.service, "refresh_catalog", lambda: calls.append("refresh"))
    monkeypatch.setattr(
        provider_hub.service,
        "check_updates",
        lambda: calls.append("check") or {"state": "completed"},
    )

    assert ProviderHubUpdatesCheck.post.__wrapped__(ProviderHubUpdatesCheck()) == {"state": "completed"}
    assert calls == ["refresh", "check"]


def test_apply_update_without_available_manifest_does_not_stage(tmp_path, monkeypatch):
    from provider_hub.service import apply_update
    from provider_hub.state import load_state

    provider_id = "activehub"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Active Hub",
                        "active_version": "1.0.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "state": "active",
                        "pending_restart": False,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                },
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    provider = apply_update(provider_id)

    assert provider["state"] == "active"
    assert provider["pending_restart"] is False
    assert provider.get("staged_version") is None
    assert "No update manifest" in provider["last_error"]
    assert load_state()["installations"][provider_id]["state"] == "active"


def test_apply_update_uses_latest_catalog_manifest(tmp_path, monkeypatch):
    from provider_hub.service import apply_update
    from provider_hub.state import load_state

    provider_id = "activehub"
    state_file = tmp_path / "state.json"
    update_manifest = _manifest(provider_id=provider_id, version="1.1.0")
    state_file.write_text(
        json.dumps(
            {
                "catalog_entries": {
                    f"official:{provider_id}:1.1.0": {
                        "source": "official",
                        "source_name": "Official",
                        "provider_id": provider_id,
                        "name": "Active Hub",
                        "version": "1.1.0",
                        "trusted": True,
                        "manifest": update_manifest,
                    }
                },
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Active Hub",
                        "active_version": "1.0.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "state": "active",
                        "pending_restart": False,
                        "manifest": _manifest(provider_id=provider_id, version="1.0.0"),
                    }
                },
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr("provider_hub.service._fetch_bundle", lambda manifest: tmp_path / "bundle")
    class FakeEnvironment:
        def __init__(self, root):
            self.root = root

        def install(self, validated):
            return tmp_path / "env"

    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service.python_executable", lambda env_path: tmp_path / "python")
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    provider = apply_update(provider_id)

    assert provider["state"] == "staged"
    assert provider["pending_restart"] is True
    assert provider["staged_version"] == "1.1.0"
    installation = load_state()["installations"][provider_id]
    assert installation["staged_manifest"] == update_manifest


def test_provider_hub_restart_activation_promotes_staged_install(tmp_path, monkeypatch):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    provider_id = "stagehub"
    staged_path = tmp_path / "provider_hub" / "bundles" / provider_id / "1.1.0" / ("b" * 40)
    staged_python_path = tmp_path / "provider_hub" / "envs" / provider_id / "1.1.0" / "test" / "bin" / "python"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Stage Hub Provider",
                        "active_version": "1.0.0",
                        "staged_version": "1.1.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "staged_path": str(staged_path),
                        "staged_python_path": str(staged_python_path),
                        "state": "staged",
                        "pending_restart": True,
                        "manifest": _manifest(provider_id=provider_id, version="1.1.0"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    assert activate_staged_installations() == [provider_id]

    installation = load_state()["installations"][provider_id]
    assert installation["state"] == "active"
    assert installation["active_version"] == "1.1.0"
    assert installation["active_path"] == str(staged_path)
    assert installation["python_path"] == str(staged_python_path)
    assert installation["staged_path"] is None
    assert installation["staged_python_path"] is None
    assert installation["pending_restart"] is False


def test_provider_hub_restart_activation_keeps_active_on_staged_failure(tmp_path, monkeypatch):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    provider_id = "stagehub"
    old_manifest = _manifest(provider_id=provider_id, version="1.0.0")
    staged_manifest = _manifest(provider_id=provider_id, version="1.1.0")
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Stage Hub Provider",
                        "active_version": "1.0.0",
                        "staged_version": "1.1.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "staged_path": "/missing/bundle",
                        "staged_python_path": "/missing/python",
                        "staged_manifest": staged_manifest,
                        "state": "staged",
                        "pending_restart": True,
                        "manifest": old_manifest,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert activate_staged_installations() == []

    installation = load_state()["installations"][provider_id]
    assert installation["state"] == "active"
    assert installation["active_version"] == "1.0.0"
    assert installation["active_path"] == "/old/bundle"
    assert installation["python_path"] == "/old/python"
    assert installation["staged_version"] is None
    assert installation["staged_manifest"] is None
    assert installation["manifest"] == old_manifest
    assert installation["pending_restart"] is False
    assert installation["last_error"]


def test_provider_hub_restart_activation_finalizes_removed_installation(tmp_path, monkeypatch):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    provider_id = "removedhub"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Removed Hub Provider",
                        "active_version": "1.0.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "state": "removed",
                        "pending_restart": True,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert activate_staged_installations() == []
    assert provider_id not in load_state()["installations"]


def test_provider_hub_uninstall_discards_staged_install_without_restart(tmp_path, monkeypatch):
    from provider_hub.service import remove_installation
    from provider_hub.state import load_state

    provider_id = "stagedonlyhub"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Staged Only Hub Provider",
                        "active_version": None,
                        "staged_version": "1.0.0",
                        "staged_path": "/new/bundle",
                        "staged_python_path": "/new/python",
                        "state": "staged",
                        "pending_restart": True,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert remove_installation(provider_id) is True
    assert provider_id not in load_state()["installations"]


def test_provider_hub_uninstall_stages_active_removal_and_discards_staged_update(tmp_path, monkeypatch):
    from provider_hub.service import remove_installation
    from provider_hub.state import load_state

    provider_id = "updatedhub"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Updated Hub Provider",
                        "active_version": "1.0.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "staged_version": "1.1.0",
                        "staged_path": "/new/bundle",
                        "staged_python_path": "/new/python",
                        "staged_manifest": _manifest(provider_id=provider_id, version="1.1.0"),
                        "state": "staged",
                        "pending_restart": True,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert remove_installation(provider_id) is True
    installation = load_state()["installations"][provider_id]
    assert installation["state"] == "removed"
    assert installation["pending_restart"] is True
    assert installation["active_version"] == "1.0.0"
    assert installation["staged_version"] is None
    assert installation["staged_path"] is None
    assert installation["staged_python_path"] is None
    assert installation["staged_manifest"] is None


def test_provider_hub_config_redacts_secret_and_preserves_placeholder(tmp_path, monkeypatch):
    from provider_hub.service import SECRET_PLACEHOLDER, runtime_provider_configs, update_provider
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(dependencies={"requirements": []}),
            "config": {},
        }
    }
    save_state(state)

    redacted = update_provider(
        "examplehub",
        enabled=True,
        config={"api_key": "real-secret", "region": "eu"},
    )

    assert redacted["enabled"] is True
    assert redacted["config"]["api_key"] == SECRET_PLACEHOLDER
    assert redacted["config"]["region"] == "eu"
    assert "real-secret" not in json.dumps(redacted)
    assert runtime_provider_configs()["examplehub"]["api_key"] == "real-secret"

    redacted = update_provider(
        "examplehub",
        config={"api_key": SECRET_PLACEHOLDER, "region": "us"},
    )

    assert redacted["config"]["api_key"] == SECRET_PLACEHOLDER
    assert redacted["config"]["region"] == "us"
    assert runtime_provider_configs()["examplehub"] == {
        "api_key": "real-secret",
        "region": "us",
    }

    update_provider("examplehub", config={"api_key": "new-secret"})

    assert runtime_provider_configs()["examplehub"]["api_key"] == "new-secret"


def test_get_providers_auth_includes_active_provider_hub_config(tmp_path, monkeypatch):
    from app.get_providers import get_providers_auth
    from provider_hub.service import update_provider
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(dependencies={"requirements": []}),
            "config": {},
        }
    }
    save_state(state)
    update_provider("examplehub", config={"api_key": "runtime-secret", "region": "eu"})

    assert get_providers_auth()["examplehub"]["api_key"] == "runtime-secret"
    assert get_providers_auth()["examplehub"]["region"] == "eu"


def test_provider_hub_connection_test_runs_worker_health(tmp_path, monkeypatch):
    from provider_hub.state import load_state, save_state

    requests = []

    class FakeWorkerClient:
        def __init__(self, command, cwd=None, env=None):
            self.command = command
            self.cwd = cwd
            self.env = env

        def request(self, op, payload=None, timeout=30):
            requests.append((op, payload, timeout, self.cwd, self.env))
            return type("Result", (), {"payload": {"initialized": True}, "events": []})()

        def stop(self):
            requests.append(("stop", None, None, self.cwd, self.env))

    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    python_path = tmp_path / "venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda _manifest, _path: None)
    monkeypatch.setattr("provider_hub.service.ProviderWorkerClient", FakeWorkerClient)

    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "active_path": str(bundle_path),
            "python_path": str(python_path),
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(dependencies={"requirements": []}),
            "config": {"region": "eu"},
            "last_error": "old failure",
        }
    }
    save_state(state)

    from provider_hub.service import test_provider_connection

    result = test_provider_connection("examplehub")

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["message"] == "Worker health check passed"
    assert requests[0][0] == "health"
    assert requests[0][2] == 10
    assert str(requests[0][3]) == str(bundle_path)
    assert json.loads(requests[0][4]["BAZARR_PROVIDER_HUB_MANIFEST"])["provider_id"] == "examplehub"
    assert load_state()["installations"]["examplehub"]["last_error"] is None


def test_provider_hub_connection_test_ignores_runtime_pycache(tmp_path, monkeypatch):
    from provider_hub.state import load_state, save_state

    class FakeWorkerClient:
        def __init__(self, command, cwd=None, env=None):
            self.command = command
            self.cwd = cwd
            self.env = env

        def request(self, op, payload=None, timeout=30):
            return type("Result", (), {"payload": {"initialized": True}, "events": []})()

        def stop(self):
            return None

    provider_content = b"class ExampleProvider: pass\n"
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    (bundle_path / "provider.py").write_bytes(provider_content)
    pycache_path = bundle_path / "__pycache__"
    pycache_path.mkdir()
    (pycache_path / "provider.cpython-314.pyc").write_bytes(b"runtime bytecode")

    python_path = tmp_path / "venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    monkeypatch.setattr("provider_hub.service.ProviderWorkerClient", FakeWorkerClient)

    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "active_path": str(bundle_path),
            "python_path": str(python_path),
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(
                provider_content=provider_content,
                dependencies={"requirements": []},
            ),
            "config": {},
        }
    }
    save_state(state)

    from provider_hub.service import test_provider_connection

    result = test_provider_connection("examplehub")

    assert result["ok"] is True
    assert not pycache_path.exists()


def test_stage_install_failure_preserves_active_install_and_records_last_error(tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, stage_install
    from provider_hub.state import load_state

    provider_id = "stablehub"
    state_file = tmp_path / "provider_hub" / "state.json"
    old_installation = {
        "provider_id": provider_id,
        "name": "Stable Hub Provider",
        "active_version": "1.0.0",
        "staged_version": None,
        "active_path": "/old/bundle",
        "python_path": "/old/python",
        "staged_path": None,
        "staged_python_path": None,
        "state": "active",
        "pending_restart": False,
        "manifest": _manifest(provider_id=provider_id, name="Stable Hub Provider"),
        "last_error": None,
    }
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"installations": {provider_id: old_installation}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    def fake_get(url, timeout):
        return _FakeResponse(content=b"tampered\n")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)

    with pytest.raises(ProviderHubInstallError, match="SHA256 mismatch"):
        stage_install(
            _manifest(
                provider_id=provider_id,
                name="Stable Hub Provider",
                version="1.1.0",
                dependencies={"requirements": []},
            )
        )

    installation = load_state()["installations"][provider_id]
    assert installation["state"] == "active"
    assert installation["active_version"] == "1.0.0"
    assert installation["active_path"] == "/old/bundle"
    assert installation["python_path"] == "/old/python"
    assert installation["staged_version"] is None
    assert installation["staged_path"] is None
    assert installation["staged_python_path"] is None
    assert "SHA256 mismatch" in installation["last_error"]


def test_custom_github_catalog_source_is_stored_as_untrusted(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, list_catalog

    state_file = tmp_path / "state.json"
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    source = add_catalog_source(
        name="community",
        url="https://github.com/example/providers/blob/main/catalog.json",
        trusted=True,
    )

    assert source["trusted"] is False
    assert source["type"] == "github"
    assert source["url"] == "https://github.com/example/providers/blob/main/catalog.json"
    sources = {item["name"]: item for item in list_catalog()["sources"]}
    assert sources["community"]["trusted"] is False


def test_existing_state_normalizes_custom_source_trust(tmp_path, monkeypatch):
    from provider_hub.service import list_catalog

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "catalog_sources": {
                    "community": {
                        "id": "community",
                        "name": "community",
                        "type": "github",
                        "url": "https://github.com/example/providers/blob/main/catalog.json",
                        "enabled": True,
                        "trusted": True,
                    }
                },
                "catalog_entries": {
                    "community:examplehub:1.0.0": {
                        "source": "community",
                        "provider_id": "examplehub",
                        "version": "1.0.0",
                        "trusted": True,
                        "manifest": _manifest(),
                    }
                },
                "installations": {},
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    catalog = list_catalog()
    sources = {item["id"]: item for item in catalog["sources"]}

    assert sources["community"]["trusted"] is False
    assert catalog["entries"][0]["trusted"] is False


def test_official_catalog_source_is_reseeded_when_missing(tmp_path, monkeypatch):
    from provider_hub.state import OFFICIAL_CATALOG_SOURCE_ID
    from provider_hub.service import list_catalog

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    sources = {item["id"]: item for item in list_catalog()["sources"]}

    assert sources[OFFICIAL_CATALOG_SOURCE_ID]["trusted"] is True


def test_official_catalog_source_cannot_be_overwritten_or_deleted(tmp_path, monkeypatch):
    from provider_hub.service import CatalogSourceError, add_catalog_source, remove_catalog_source

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))

    with pytest.raises(CatalogSourceError, match="reserved"):
        add_catalog_source(
            name="official",
            url="https://github.com/example/providers/blob/main/catalog.json",
            trusted=True,
        )

    assert remove_catalog_source("official") is False


def test_empty_state_seeds_official_trusted_catalog_source(tmp_path, monkeypatch):
    from provider_hub.state import OFFICIAL_CATALOG_SOURCE_ID, OFFICIAL_CATALOG_URL
    from provider_hub.service import list_catalog

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))

    sources = {item["id"]: item for item in list_catalog()["sources"]}

    assert sources[OFFICIAL_CATALOG_SOURCE_ID]["url"] == OFFICIAL_CATALOG_URL
    assert sources[OFFICIAL_CATALOG_SOURCE_ID]["trusted"] is True


def test_list_catalog_auto_refreshes_unchecked_sources(tmp_path, monkeypatch):
    from provider_hub.service import list_catalog

    def fake_get(url, timeout):
        if "api.github.com" in url:
            return _FakeResponse({"sha": "d" * 40})
        return _FakeResponse(
            {
                "providers": [
                    {
                        "manifest": _manifest(
                            provider_id="autocataloghub",
                            dependencies={"requirements": []},
                        )
                    }
                ]
            }
        )

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))

    catalog = list_catalog(auto_refresh=True)

    assert catalog["sources"][0]["last_checked_at"]
    assert catalog["sources"][0]["last_error"] is None
    assert catalog["entries"][0]["provider_id"] == "autocataloghub"
    assert catalog["entries"][0]["trusted"] is True


def test_custom_catalog_source_rejects_non_github_url(tmp_path, monkeypatch):
    from provider_hub.service import CatalogSourceError, add_catalog_source

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))

    with pytest.raises(CatalogSourceError):
        add_catalog_source(name="bad", url="https://example.com/catalog.json")


def test_catalog_refresh_fetches_github_catalog_entries(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, list_catalog, refresh_catalog

    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        if "api.github.com" in url:
            return _FakeResponse({"sha": "d" * 40})
        if "LavX/bazarr-provider-catalog" in url:
            return _FakeResponse({"providers": []})
        return _FakeResponse({"providers": [{"manifest": _manifest(provider_id="cataloghub")} ]})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source("community", "https://github.com/example/providers/blob/main/catalog.json")
    result = refresh_catalog()

    assert result["sources"] == 2
    assert result["entries"] == 1
    catalog = list_catalog()
    assert catalog["entries"][0]["provider_id"] == "cataloghub"
    assert catalog["entries"][0]["trusted"] is False
    assert any(call[0].endswith("/repos/example/providers/commits/main") for call in calls)
    assert (
        "https://raw.githubusercontent.com/example/providers/" + ("d" * 40) + "/catalog.json",
        30,
    ) in calls
    assert catalog["entries"][0]["manifest"]["source"]["commit"] == "d" * 40


def test_catalog_refresh_prunes_stale_entries_for_successful_source(tmp_path, monkeypatch):
    from provider_hub.service import list_catalog, refresh_catalog

    def fake_get(url, timeout):
        if "api.github.com" in url:
            return _FakeResponse({"sha": "d" * 40})
        if "LavX/bazarr-provider-catalog" in url:
            return _FakeResponse({"providers": []})
        return _FakeResponse({"providers": [{"manifest": _manifest(provider_id="freshhub")} ]})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "catalog_sources": {
                    "community": {
                        "id": "community",
                        "name": "community",
                        "type": "github",
                        "url": "https://github.com/example/providers/blob/main/catalog.json",
                        "enabled": True,
                        "trusted": False,
                    }
                },
                "catalog_entries": {
                    "community:stalehub:1.0.0": {
                        "source": "community",
                        "provider_id": "stalehub",
                        "version": "1.0.0",
                        "trusted": False,
                        "manifest": _manifest(provider_id="stalehub"),
                    }
                },
                "installations": {},
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    refresh_catalog()

    provider_ids = {entry["provider_id"] for entry in list_catalog()["entries"]}
    assert "freshhub" in provider_ids
    assert "stalehub" not in provider_ids


def test_stage_install_fetches_bundle_builds_env_and_records_staged_paths(tmp_path, monkeypatch):
    from provider_hub.service import stage_install
    from provider_hub.state import load_state

    provider_content = b"class ExampleProvider: pass\n"
    commit = "e" * 40
    manifest = _manifest(
        files={"provider.py": _sha256(provider_content)},
        dependencies={"requirements": []},
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": commit,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    state_file = tmp_path / "provider_hub" / "state.json"
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return _FakeResponse(content=provider_content)

    env_calls = []

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated):
            env_calls.append((self.root, validated.provider_id, validated.version))
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    installation = stage_install(manifest)

    bundle_path = tmp_path / "provider_hub" / "bundles" / "examplehub" / "1.0.0" / commit
    python_path = tmp_path / "provider_hub" / "envs" / "examplehub" / "1.0.0" / "test"
    python_path = python_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert calls == [
        (
            "https://raw.githubusercontent.com/owner/repo/" + commit + "/provider.py",
            30,
        )
    ]
    assert env_calls == [(tmp_path / "provider_hub", "examplehub", "1.0.0")]
    assert (bundle_path / "provider.py").read_bytes() == provider_content
    assert json.loads((bundle_path / "provider.json").read_text(encoding="utf-8"))["provider_id"] == "examplehub"
    assert installation["staged_path"] == str(bundle_path)
    assert installation["staged_python_path"] == str(python_path)
    assert installation["active_path"] is None
    assert installation["python_path"] is None
    assert installation["trusted"] is False

    stored = load_state()["installations"]["examplehub"]
    assert stored["staged_path"] == str(bundle_path)
    assert stored["staged_python_path"] == str(python_path)


def test_stage_install_fetches_bundle_from_manifest_source_path(tmp_path, monkeypatch):
    from provider_hub.service import stage_install

    provider_content = b"class ExampleProvider: pass\n"
    commit = "e" * 40
    manifest = _manifest(
        file_payloads={"provider.py": provider_content},
        dependencies={"requirements": []},
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": commit,
            "path": "providers/smoke",
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))

    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    stage_install(manifest)

    assert calls == [
        (
            "https://raw.githubusercontent.com/owner/repo/" + commit + "/providers/smoke/provider.py",
            30,
        )
    ]


def test_stage_install_trust_comes_from_catalog_entry_only(tmp_path, monkeypatch):
    from provider_hub.service import stage_install
    from provider_hub.state import official_catalog_source

    provider_content = b"class ExampleProvider: pass\n"
    commit = "e" * 40
    catalog_manifest = _manifest(
        file_payloads={"provider.py": provider_content},
        dependencies={"requirements": []},
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": commit,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    install_manifest = json.loads(json.dumps(catalog_manifest))
    install_manifest["source"].pop("trusted")
    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "catalog_sources": {"official": official_catalog_source()},
                "catalog_entries": {
                    "official:examplehub:1.0.0": {
                        "source": "official",
                        "provider_id": "examplehub",
                        "version": "1.0.0",
                        "trusted": True,
                        "manifest": catalog_manifest,
                    }
                },
                "installations": {},
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    def fake_get(url, timeout):
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    installation = stage_install(install_manifest)

    assert installation["trusted"] is True

    tampered_manifest = json.loads(json.dumps(install_manifest))
    tampered_manifest["entry_class"] = "DifferentProvider"

    installation = stage_install(tampered_manifest)

    assert installation["trusted"] is False


def test_stage_install_smoke_failure_records_failed_install(tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, stage_install
    from provider_hub.state import load_state

    provider_content = b"class ExampleProvider: pass\n"
    manifest = _manifest(
        file_payloads={"provider.py": provider_content},
        dependencies={"requirements": []},
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))

    def fake_get(url, timeout):
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(
        "provider_hub.service._smoke_validate_worker",
        lambda manifest, bundle_path, python_path: (_ for _ in ()).throw(RuntimeError("worker broke")),
    )

    with pytest.raises(ProviderHubInstallError, match="worker broke"):
        stage_install(manifest)

    installation = load_state()["installations"]["examplehub"]
    assert installation["state"] == "failed"
    assert installation["pending_restart"] is False
    assert "worker broke" in installation["last_error"]


def test_bundle_tree_verification_rejects_symlink_and_hash_mismatch(tmp_path):
    from provider_hub.bundle import BundleValidationError, verify_bundle_tree
    from provider_hub.manifest import validate_manifest

    manifest = validate_manifest(_manifest(), built_in_provider_ids=set())
    provider_file = tmp_path / "provider.py"
    provider_file.write_bytes(b"class ExampleProvider: pass\n")

    verify_bundle_tree(manifest, tmp_path)

    provider_file.write_bytes(b"tampered\n")
    with pytest.raises(BundleValidationError, match="SHA256"):
        verify_bundle_tree(manifest, tmp_path)

    provider_file.write_bytes(b"class ExampleProvider: pass\n")
    provider_file.unlink()
    provider_file.symlink_to(tmp_path / "target.py")
    with pytest.raises(BundleValidationError, match="symlink"):
        verify_bundle_tree(manifest, tmp_path)


def test_bundle_tree_verification_rejects_bundle_hash_mismatch(tmp_path):
    from provider_hub.bundle import BundleValidationError, verify_bundle_tree
    from provider_hub.manifest import validate_manifest

    provider_file = tmp_path / "provider.py"
    provider_file.write_bytes(b"class ExampleProvider: pass\n")
    manifest = validate_manifest(_manifest(bundle_sha256="a" * 64), built_in_provider_ids=set())

    with pytest.raises(BundleValidationError, match="bundle SHA256"):
        verify_bundle_tree(manifest, tmp_path)


def test_hub_proxy_provider_search_and_download_uses_worker_payload():
    from provider_hub.manifest import validate_manifest
    from provider_hub.registry import _make_provider_class
    from provider_hub.protocol import language_to_payload

    class FakeWorker:
        def __init__(self):
            self.requests = []

        def request(self, op, payload, timeout):
            self.requests.append((op, payload, timeout))
            if op == "search":
                return type(
                    "Result",
                    (),
                    {
                        "payload": {
                            "candidates": [
                                {
                                    "provider": "fake",
                                    "id": "sub-1",
                                    "language": language_to_payload(Language("eng")),
                                    "release_info": "Example.Movie.2024.1080p-GROUP",
                                    "filename": "example.srt",
                                    "matches": ["title", "year"],
                                    "provider_payload": {
                                        "provider": "fake",
                                        "schema": 1,
                                        "data": {"file_id": "sub-1"},
                                    },
                                }
                            ]
                        },
                        "events": [],
                    },
                )()
            content = b"hello"
            return type(
                "Result",
                (),
                {
                    "payload": {
                        "content_b64": base64.b64encode(content).decode("ascii"),
                        "content_sha256": _sha256(content),
                        "empty": False,
                    },
                    "events": [],
                },
            )()

        def stop(self):
            return None

    worker = FakeWorker()
    manifest = validate_manifest(_manifest(provider_id="proxyhub"), built_in_provider_ids=set())
    provider_cls = _make_provider_class(manifest, worker_client=worker)
    provider_config = {"profile_name": "smoke-profile", "api_token": "secret-token"}
    provider = provider_cls(timeout=9, **provider_config)
    movie = Movie("/media/example.mkv", "Example Movie", year=2024)

    subtitles = provider.list_subtitles(movie, {Language("eng")})
    assert len(subtitles) == 1
    assert subtitles[0].provider_name == "proxyhub"

    provider.download_subtitle(subtitles[0])
    assert subtitles[0].content == b"hello"
    assert worker.requests[0][0] == "search"
    assert worker.requests[0][2] == 9
    assert worker.requests[0][1]["config"] == provider_config
    assert worker.requests[1][0] == "download"
    assert worker.requests[1][1]["config"] == provider_config


def test_worker_runner_executes_simple_bundle(tmp_path):
    import sys

    from provider_hub.worker import ProviderWorkerClient, worker_command

    provider_file = tmp_path / "provider.py"
    provider_file.write_text(
        """
import base64


class ExampleProvider:
    def search(self, video, languages, config):
        if config.get("profile_name") != "smoke-profile" or config.get("api_token") != "secret-token":
            raise ValueError("missing smoke config")
        return [{
            "provider": "example",
            "id": "sub-1",
            "language": languages[0],
            "release_info": video.get("title"),
            "matches": ["title"],
            "provider_payload": {
                "provider": "example",
                "schema": 1,
                "profile_name": config["profile_name"],
                "data": {"id": "sub-1"},
            },
        }]

    def download(self, provider_payload, language, config):
        if config.get("profile_name") != "smoke-profile" or config.get("api_token") != "secret-token":
            raise ValueError("missing smoke config")
        if provider_payload.get("profile_name") != config["profile_name"]:
            raise ValueError("profile mismatch")
        content = b"hello from worker"
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_sha256": "94bbc6037685e2186909083aa02abe58fbec222f6e2d73bb3e9e59d5b24a3d25",
            "empty": False,
        }
""",
        encoding="utf-8",
    )
    manifest = _manifest(
        files={"provider.py": _sha256(provider_file.read_bytes())},
        dependencies={"requirements": []},
    )
    runner = Path(__file__).parents[2] / "bazarr" / "provider_hub" / "worker_runner.py"
    client = ProviderWorkerClient(
        worker_command(sys.executable, runner),
        cwd=tmp_path,
        env={
            "BAZARR_PROVIDER_HUB_BUNDLE": str(tmp_path),
            "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps(manifest),
        },
    )

    try:
        search = client.request(
            "search",
            {
                "video": {"title": "Example Movie"},
                "languages": [{"alpha3": "eng", "hi": False, "forced": False}],
                "config": {"profile_name": "smoke-profile", "api_token": "secret-token"},
            },
            timeout=3,
        )
        assert search.payload["candidates"][0]["id"] == "sub-1"

        download = client.request(
            "download",
            {
                "provider_payload": search.payload["candidates"][0]["provider_payload"],
                "language": {"alpha3": "eng"},
                "config": {"profile_name": "smoke-profile", "api_token": "secret-token"},
            },
            timeout=3,
        )
        assert download.payload["empty"] is False
    finally:
        client.stop()


def test_worker_command_disables_bytecode_in_isolated_mode(tmp_path):
    from provider_hub.worker import worker_command

    runner = tmp_path / "runner.py"

    assert worker_command("/venv/bin/python", runner) == [
        "/venv/bin/python",
        "-I",
        "-B",
        str(runner),
    ]


def test_add_catalog_source_accepts_optional_dev_ref(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    source = add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
        dev_ref="feat/foo",
    )
    assert source["dev_ref"] == "feat/foo"


def test_add_catalog_source_rejects_invalid_dev_ref(tmp_path, monkeypatch):
    import pytest
    from provider_hub.service import CatalogSourceError, add_catalog_source

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    with pytest.raises(CatalogSourceError):
        add_catalog_source(
            "bad",
            "https://github.com/example/providers/blob/main/catalog.json",
            dev_ref="this is not a ref",
        )


def test_update_catalog_source_sets_dev_ref(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, update_catalog_source

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
    )
    updated = update_catalog_source("community", dev_ref="feat/test")
    assert updated["dev_ref"] == "feat/test"
    cleared = update_catalog_source("community", dev_ref=None)
    assert cleared["dev_ref"] is None


def test_update_catalog_source_rejects_invalid_dev_ref(tmp_path, monkeypatch):
    import pytest
    from provider_hub.service import (
        CatalogSourceError,
        add_catalog_source,
        update_catalog_source,
    )

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
    )
    with pytest.raises(CatalogSourceError):
        update_catalog_source("community", dev_ref="bad ref with space")


def test_update_catalog_source_returns_none_for_unknown(tmp_path, monkeypatch):
    from provider_hub.service import update_catalog_source

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert update_catalog_source("missing", dev_ref="feat/x") is None


def test_refresh_catalog_uses_dev_ref_when_set(tmp_path, monkeypatch):
    from provider_hub.service import (
        add_catalog_source,
        refresh_catalog,
        update_catalog_source,
    )

    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        if "api.github.com" in url:
            return _FakeResponse({"sha": "f" * 40})
        return _FakeResponse({"providers": []})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
    )
    update_catalog_source("community", dev_ref="feat/test-branch")
    refresh_catalog()

    assert any(
        url.endswith("/repos/example/providers/commits/feat/test-branch")
        for url in calls
    ), f"calls were: {calls}"


def test_patch_catalog_source_updates_dev_ref(tmp_path, monkeypatch):
    from provider_hub.service import (
        add_catalog_source,
        get_catalog_source,
        refresh_catalog,
        update_catalog_source,
    )

    def fake_get(url, timeout):
        if "api.github.com" in url:
            return _FakeResponse({"sha": "9" * 40})
        return _FakeResponse({"providers": []})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
    )

    # Mirrors the PATCH endpoint logic: validate + update, then refresh.
    update_catalog_source("community", dev_ref="feat/branch")
    refresh_catalog()

    source = get_catalog_source("community")
    assert source is not None
    assert source["dev_ref"] == "feat/branch"
    assert source["last_checked_at"] is not None


def _empty_state_file(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    return state_file


def test_record_job_writes_lifecycle_pending_running_completed(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, record_job

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    observed = []
    with record_job(
        "stage_update",
        target_kind="provider",
        target_id="examplehub",
        target_name="Example",
        from_version="1.0.0",
        to_version="1.1.0",
    ) as job:
        observed.append(list_jobs()[-1]["state"])
        job.update(message="staged")

    jobs = list_jobs()
    assert len(jobs) == 1
    last = jobs[-1]
    assert observed == ["running"]
    assert last["state"] == "completed"
    assert last["action"] == "stage_update"
    assert last["target_id"] == "examplehub"
    assert last["from_version"] == "1.0.0"
    assert last["to_version"] == "1.1.0"
    assert last["message"] == "staged"
    assert last["duration_ms"] is not None
    assert last["started_at"] is not None
    assert last["completed_at"] is not None
    assert last["error"] is None


def test_record_job_marks_failed_with_exception(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, record_job

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    with pytest.raises(ValueError):
        with record_job("install", target_kind="provider", target_id="examplehub") as job:
            job.update(message="kicking off")
            raise ValueError("boom")

    last = list_jobs()[-1]
    assert last["state"] == "failed"
    assert last["error"] == "boom"
    assert last["duration_ms"] is not None
    assert last["message"] == "kicking off"


def test_record_job_trims_history_to_limit(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.service import list_jobs, record_job

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    monkeypatch.setattr(service, "_JOB_LOG_LIMIT", 3)

    for index in range(5):
        with record_job("check_updates", target_kind="system") as job:
            job.update(message=f"check {index}")

    jobs = list_jobs()
    assert len(jobs) == 3
    assert [job["message"] for job in jobs] == ["check 2", "check 3", "check 4"]


def test_check_updates_emits_lifecycle_job(tmp_path, monkeypatch):
    from provider_hub.service import check_updates, list_jobs

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    job = check_updates()

    assert job["state"] == "completed"
    assert job["action"] == "check_updates"
    assert job["duration_ms"] is not None
    assert "No new updates available" in job["message"]
    assert job["id"] == list_jobs()[-1]["id"]


def test_check_updates_reports_available_updates(tmp_path, monkeypatch):
    from provider_hub.service import check_updates
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
    }
    state["catalog_entries"]["official:examplehub:1.1.0"] = {
        "provider_id": "examplehub",
        "version": "1.1.0",
        "source": "official",
        "manifest": {"provider_id": "examplehub", "version": "1.1.0"},
    }
    save_state(state)

    job = check_updates()

    assert job["state"] == "completed"
    assert job["details"]["updates_available"] == 1
    assert "1.0.0 -> 1.1.0" in job["message"]


def test_add_catalog_source_records_activity_job(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, list_jobs

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
        dev_ref="feat/foo",
    )

    job = list_jobs()[-1]
    assert job["action"] == "add_source"
    assert job["state"] == "completed"
    assert job["target_kind"] == "source"
    assert job["target_id"] == "community"
    assert job["details"]["dev_ref"] == "feat/foo"
    assert "Added catalog source" in job["message"]


def test_update_catalog_source_records_version_diff(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, list_jobs, update_catalog_source

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
        dev_ref="feat/old",
    )
    update_catalog_source("community", dev_ref="feat/new")

    job = list_jobs()[-1]
    assert job["action"] == "update_source"
    assert job["state"] == "completed"
    assert job["from_version"] == "feat/old"
    assert job["to_version"] == "feat/new"


def test_remove_catalog_source_records_failed_when_missing(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, remove_catalog_source

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    assert remove_catalog_source("ghost") is False
    job = list_jobs()[-1]
    assert job["action"] == "remove_source"
    assert job["state"] == "completed"
    assert "not found" in job["message"]


def test_refresh_catalog_records_summary(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, refresh_catalog

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "catalog_sources": {
                    "official": {
                        "id": "official",
                        "name": "Official Bazarr Provider Catalog",
                        "type": "github",
                        "url": "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json",
                        "enabled": True,
                        "official": True,
                        "trusted": True,
                        "dev_ref": None,
                        "last_checked_at": None,
                        "last_error": None,
                    },
                },
                "catalog_entries": {},
                "installations": {},
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    def fake_get(url, timeout):
        if "api.github.com" in url:
            return _FakeResponse({"sha": "f" * 40})
        return _FakeResponse({"providers": []})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)

    refresh_catalog()
    job = list_jobs()[-1]
    assert job["action"] == "refresh_catalog"
    assert job["state"] == "completed"
    assert job["details"]["sources_total"] == 1
    assert job["details"]["sources_ok"] == 1
    assert "Refreshed 1 source(s)" in job["message"]


def test_remove_installation_records_uninstall_job(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, remove_installation
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
    }
    save_state(state)

    assert remove_installation("examplehub") is True
    job = list_jobs()[-1]
    assert job["action"] == "uninstall"
    assert job["state"] == "completed"
    assert job["target_id"] == "examplehub"
    assert job["from_version"] == "1.0.0"
    assert "Staged removal" in job["message"]


def test_stage_install_records_install_job_on_success(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, stage_install

    provider_content = b"class ExampleProvider: pass\n"
    commit = "a" * 40
    manifest = _manifest(
        files={"provider.py": _sha256(provider_content)},
        dependencies={"requirements": []},
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": commit,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))

    def fake_get(url, timeout):
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(
        "provider_hub.service._smoke_validate_worker",
        lambda manifest, bundle_path, python_path: None,
    )

    stage_install(manifest)

    job = list_jobs()[-1]
    assert job["action"] == "install"
    assert job["state"] == "completed"
    assert job["target_id"] == "examplehub"
    assert job["to_version"] == "1.0.0"
    assert job["from_version"] is None
    assert "Staged install" in job["message"]


def test_stage_install_records_failed_job_on_smoke_error(tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, list_jobs, stage_install

    provider_content = b"class ExampleProvider: pass\n"
    manifest = _manifest(
        file_payloads={"provider.py": provider_content},
        dependencies={"requirements": []},
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))

    def fake_get(url, timeout):
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(
        "provider_hub.service._smoke_validate_worker",
        lambda manifest, bundle_path, python_path: (_ for _ in ()).throw(RuntimeError("worker broke")),
    )

    with pytest.raises(ProviderHubInstallError):
        stage_install(manifest)

    job = list_jobs()[-1]
    assert job["action"] == "install"
    assert job["state"] == "failed"
    assert "worker broke" in job["error"]


def test_list_providers_overlays_enabled_from_bazarr_enabled_providers(
    tmp_path, monkeypatch
):
    from provider_hub import service
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "enabled": False,  # stale stored value
        "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
    }
    save_state(state)

    monkeypatch.setattr(
        service, "_bazarr_enabled_providers", lambda: ["examplehub", "other"]
    )

    providers = service.list_providers()
    assert any(
        p["provider_id"] == "examplehub" and p["enabled"] is True for p in providers
    )

    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["other"])
    providers = service.list_providers()
    assert any(
        p["provider_id"] == "examplehub" and p["enabled"] is False for p in providers
    )


def test_update_provider_syncs_enabled_providers(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "enabled": True,
        "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
    }
    save_state(state)

    calls = []
    monkeypatch.setattr(
        service,
        "_set_bazarr_provider_enabled",
        lambda pid, enabled: calls.append((pid, enabled)) or True,
    )

    service.update_provider("examplehub", enabled=False)
    service.update_provider("examplehub", enabled=True)
    assert calls == [("examplehub", False), ("examplehub", True)]


def test_remove_installation_clears_enabled_providers(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "enabled": True,
        "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
    }
    save_state(state)

    calls = []
    monkeypatch.setattr(
        service,
        "_set_bazarr_provider_enabled",
        lambda pid, enabled: calls.append((pid, enabled)) or True,
    )

    service.remove_installation("examplehub")
    assert calls == [("examplehub", False)]


def test_manifest_rejects_unsafe_version_path_components():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    for bad in ("../escape", "/abs", "a/b", "a\\b", "..", "."):
        with pytest.raises(ManifestValidationError):
            validate_manifest(_manifest(version=bad), built_in_provider_ids=set())
