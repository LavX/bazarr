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
    assert "cloudscraper==1.2.58" in install_cmd
    assert "/usr/local" not in install_cmd
    assert "custom_libs" not in install_cmd


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
    provider = provider_cls(timeout=9)
    movie = Movie("/media/example.mkv", "Example Movie", year=2024)

    subtitles = provider.list_subtitles(movie, {Language("eng")})
    assert len(subtitles) == 1
    assert subtitles[0].provider_name == "proxyhub"

    provider.download_subtitle(subtitles[0])
    assert subtitles[0].content == b"hello"
    assert worker.requests[0][0] == "search"
    assert worker.requests[0][2] == 9
    assert worker.requests[1][0] == "download"


def test_worker_runner_executes_simple_bundle(tmp_path):
    import sys

    from provider_hub.worker import ProviderWorkerClient, worker_command

    provider_file = tmp_path / "provider.py"
    provider_file.write_text(
        """
import base64


class ExampleProvider:
    def search(self, video, languages, config):
        return [{
            "provider": "example",
            "id": "sub-1",
            "language": languages[0],
            "release_info": video.get("title"),
            "matches": ["title"],
            "provider_payload": {"provider": "example", "schema": 1, "data": {"id": "sub-1"}},
        }]

    def download(self, provider_payload, language, config):
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
                "config": {},
            },
            timeout=3,
        )
        assert search.payload["candidates"][0]["id"] == "sub-1"

        download = client.request(
            "download",
            {
                "provider_payload": search.payload["candidates"][0]["provider_payload"],
                "language": {"alpha3": "eng"},
                "config": {},
            },
            timeout=3,
        )
        assert download.payload["empty"] is False
    finally:
        client.stop()
