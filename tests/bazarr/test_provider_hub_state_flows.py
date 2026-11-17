"""Plugin Hub service flows on the database state, on SQLite and PostgreSQL.

The rest of the Hub tests point the state at a plain file. These run the same
flows with the file override off, so what they read back is the stored row.
"""
import json
import os
import subprocess
import sys
import uuid

import pytest
import sqlalchemy as sa

import provider_hub_state_engines as engines
from provider_hub_state_engines import raw_row
from test_provider_hub import (
    _AdvertisementResponse,
    _git_advertisement,
    _FakeResponse,
    _manifest,
    _patch_local_install_env,
    _provider_zip,
)

hub_engine = engines.hub_engine
hub_store = engines.hub_store


@pytest.fixture(autouse=True)
def _legacy_catalog_host(monkeypatch):
    # These tests stub the legacy catalog only. They model a host that trusts no root, so the refresh
    # never asks for the signed files; the signed feed and the shipped root are covered in
    # test_provider_hub_signed_feed.py and test_provider_hub_signing.py.
    from provider_hub import signing_roots
    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", {})


@pytest.fixture
def hub(hub_store, hub_engine, tmp_path, monkeypatch):
    """The database state with the hub directory and state.json under tmp_path."""
    from provider_hub import state
    from secret_store import registry
    copy = tmp_path / "provider_hub" / "state.json"
    monkeypatch.setattr(state, "state_file", lambda: copy)
    saved = set(registry.plugin_secret_paths())
    registry.reset_plugin_secret_paths()
    state.load_state()
    try:
        yield hub_engine
    finally:
        registry.reset_plugin_secret_paths(saved)


def _seed(installations):
    from provider_hub.state import load_state, save_state
    state = load_state()
    state["installations"].update(installations)
    save_state(state)


def _stored(engine):
    return json.loads(raw_row(engine)[1])


def test_a_local_install_is_stored_and_registers_its_secret_paths(hub, tmp_path, monkeypatch):
    from provider_hub.service import stage_install_local
    from provider_hub.state import load_state
    from secret_store import registry
    content = b"class LocalProvider: pass\n"
    manifest = _manifest(provider_id="locallyinstalled", name="Locally Installed",
                         provider_content=content, dependencies={"requirements": []})
    package = _provider_zip(manifest, {"provider.py": content}, prefix="locallyinstalled/")
    _patch_local_install_env(monkeypatch, tmp_path)

    installation = stage_install_local(package)

    assert installation["state"] == "staged"
    stored = _stored(hub)
    assert stored["installations"]["locallyinstalled"]["origin"] == "local"
    assert "locallyinstalled.api_key" in stored["plugin_secret_paths"]
    assert registry.is_plugin_secret("locallyinstalled.api_key")
    assert load_state()["installations"]["locallyinstalled"]["trusted"] is False
    # The file copy the next start reads before the database is open.
    copy = json.loads((tmp_path / "provider_hub" / "state.json").read_text(encoding="utf-8"))
    assert copy == json.loads(raw_row(hub)[1])
    assert any(job["action"] == "install" for job in stored["jobs"])


def test_a_staged_install_activates_on_restart(hub, tmp_path, monkeypatch):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state
    provider_id = "stagehub"
    staged_path = tmp_path / "provider_hub" / "bundles" / provider_id / "1.1.0" / ("b" * 40)
    staged_python = tmp_path / "provider_hub" / "envs" / provider_id / "1.1.0" / "test" / "bin" / "python"
    _seed({provider_id: {
        "provider_id": provider_id, "name": "Stage Hub Provider", "active_version": "1.0.0",
        "staged_version": "1.1.0", "active_path": "/old/bundle", "python_path": "/old/python",
        "staged_path": str(staged_path), "staged_python_path": str(staged_python),
        "state": "staged", "pending_restart": True,
        "manifest": _manifest(provider_id=provider_id, version="1.1.0"),
    }})
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)
    before = raw_row(hub)[0]

    assert activate_staged_installations() == [provider_id]

    installation = load_state()["installations"][provider_id]
    assert installation["state"] == "active"
    assert installation["active_version"] == "1.1.0"
    assert installation["staged_path"] is None
    assert raw_row(hub)[0] > before
    assert any(job.get("action") == "activate" and job.get("state") == "completed"
               for job in _stored(hub)["jobs"])


def test_an_uninstall_stages_the_removal_and_the_restart_finishes_it(hub, monkeypatch):
    from provider_hub.service import activate_staged_installations, list_jobs, remove_installation
    from provider_hub.state import load_state
    monkeypatch.setattr("provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled",
                        lambda provider_id, enabled: True)
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    _seed({"examplehub": {
        "provider_id": "examplehub", "name": "Example", "active_version": "1.0.0",
        "state": "active", "pending_restart": False, "enabled": True,
        "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
    }})

    assert remove_installation("examplehub") is True

    assert list_jobs()[-1]["action"] == "uninstall"
    assert load_state()["installations"]["examplehub"]["pending_restart"] is True
    activate_staged_installations()
    assert "examplehub" not in load_state()["installations"]
    assert "examplehub" not in _stored(hub)["installations"]


def test_a_catalog_refresh_with_a_stubbed_fetch_records_its_summary(hub, monkeypatch):
    from provider_hub.service import list_jobs, refresh_catalog

    def fake_get(url, timeout, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([("f" * 40, "refs/heads/main")]))
        return _FakeResponse({"providers": []})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    before = raw_row(hub)[0]

    refresh_catalog()

    job = list_jobs()[-1]
    assert (job["action"], job["state"]) == ("refresh_catalog", "completed")
    assert _stored(hub)["catalog_sources"]["official"]["last_checked_at"]
    assert raw_row(hub)[0] > before


def test_an_enable_toggle_that_registers_paths_commits_once(hub, monkeypatch):
    """update_provider -> set_enabled -> the settings write -> the registry's persist."""
    from provider_hub.service import update_provider
    from provider_hub.state import load_state
    from secret_store import registry

    def enable_and_register(provider_id, enabled):
        # What the settings write does while the Hub state is being edited.
        registry._persist_plugin_secret_paths({"examplehub.token"})
        return True

    monkeypatch.setattr("provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled", enable_and_register)
    _seed({"examplehub": {
        "provider_id": "examplehub", "name": "Example", "active_version": "1.0.0",
        "state": "active", "pending_restart": False,
        "manifest": _manifest(provider_id="examplehub"),
    }})
    before = raw_row(hub)[0]

    update_provider("examplehub", enabled=True)

    state = load_state()
    assert state["installations"]["examplehub"]["enabled"] is True
    assert "examplehub.token" in state["plugin_secret_paths"]
    # The row commits first, then the settings write registers its path in a
    # write of its own.
    assert raw_row(hub)[0] == before + 2


def test_a_refused_toggle_puts_the_row_back(hub, monkeypatch):
    from provider_hub.service import ProviderHubSettingsError, update_provider
    from provider_hub.state import load_state
    from secret_store import registry

    def refuse(provider_id, enabled):
        registry._persist_plugin_secret_paths({"examplehub.token"})
        return False

    monkeypatch.setattr("provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled", refuse)
    _seed({"examplehub": {
        "provider_id": "examplehub", "name": "Example", "active_version": "1.0.0",
        "state": "active", "pending_restart": False, "manifest": _manifest(provider_id="examplehub"),
    }})

    with pytest.raises(ProviderHubSettingsError):
        update_provider("examplehub", enabled=True)

    # The toggle was refused after the commit, so the row is restored. A secret
    # path the settings write registered stays: paths only ever accumulate.
    row = load_state()["installations"]["examplehub"]
    assert "enabled" not in row


def test_the_decrypt_block_cache_follows_database_writes(hub):
    from provider_hub import service
    first = service._block_cache_key()
    _seed({"examplehub": {"provider_id": "examplehub", "state": "active", "active_version": "1.0.0",
                          "pending_restart": False, "manifest": _manifest(provider_id="examplehub")}})
    assert service._block_cache_key() != first


_BOOT_CHILD = '''\
import json
import os
import sys

ROOT = os.environ["BAZARR_REPO_ROOT"]
sys.path.insert(0, os.path.join(ROOT, "bazarr"))
sys.path.insert(0, os.path.join(ROOT, "custom_libs"))

from app.config import settings  # noqa: E402,F401
from provider_hub import state  # noqa: E402

print("RESULT " + json.dumps({
    "held": state.database_held(),
    "installation": state.load_state()["installations"]["bootplugin"],
    "paths": state.load_state()["plugin_secret_paths"],
}))
'''


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_the_boot_reads_the_copy_then_moves_to_the_database(backend, tmp_path):
    """Config import with a state.json from an older build.

    The credential paths are read from the file copy while the database is held
    back, the state is imported into the database once it is open, and the
    plaintext plugin credential is encrypted in the stored row and in the copy.
    """
    env = dict(os.environ)
    schema = None
    admin = None
    if backend == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.skip("BAZARR_PG_TEST_URL is required for real PostgreSQL coverage")
        schema = "hub_boot_" + uuid.uuid4().hex
        admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        env.update({"POSTGRES_ENABLED": "true", "POSTGRES_URL": url,
                    "PGOPTIONS": f"-csearch_path={schema}"})
    config_dir = tmp_path / "config"
    (config_dir / "provider_hub").mkdir(parents=True)
    manifest = _manifest(provider_id="bootplugin")
    (config_dir / "provider_hub" / "state.json").write_text(json.dumps({"installations": {"bootplugin": {
        "provider_id": "bootplugin", "name": "Boot Plugin", "active_version": "1.0.0", "state": "active",
        "pending_restart": False, "manifest": manifest, "config": {"api_key": "plaintext-credential"},
    }}}), encoding="utf-8")
    script = tmp_path / "boot_child.py"
    script.write_text(_BOOT_CHILD, encoding="utf-8")
    env.update({"BAZARR_REPO_ROOT": os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..")),
                "BAZARR_CONFIG_DIR": str(config_dir), "SZ_USER_AGENT": "test", "BAZARR_VERSION": "test"})
    env.pop("BAZARR_PROVIDER_HUB_STATE", None)
    try:
        proc = subprocess.run([sys.executable, str(script), "-c", str(config_dir)], env=env,
                              capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, f"child crashed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        result = json.loads(next(line for line in proc.stdout.splitlines()
                                 if line.startswith("RESULT "))[len("RESULT "):])
        assert result["held"] is False
        assert "bootplugin.api_key" in result["paths"]
        stored = result["installation"]["config"]["api_key"]
        assert stored.startswith("enc:v1:") and "plaintext-credential" not in json.dumps(result)
        copy = (config_dir / "provider_hub" / "state.json").read_text(encoding="utf-8")
        assert "plaintext-credential" not in copy
    finally:
        if admin is not None:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


_BOOT_CHILD_CORRUPT = _BOOT_CHILD.split("print(")[0] + '''print("RESULT " + json.dumps({
    "held": state.database_held(),
    "copies": [path.name for path in state.corrupt_copies()],
}))
'''


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("content", ["[]", "null", '{"jobs": []}', "{broken"])
def test_a_corrupt_state_file_does_not_stop_the_boot(backend, content, tmp_path):
    """Config import with a state.json that is not a state document.

    The credential migration reaches the file twice (the read, then the copy
    sync). The boot still completes, the file stays as it was, and a copy is
    kept, which pauses the environment cleanup.
    """
    env = dict(os.environ)
    schema = None
    admin = None
    if backend == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.skip("BAZARR_PG_TEST_URL is required for real PostgreSQL coverage")
        schema = "hub_boot_" + uuid.uuid4().hex
        admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        env.update({"POSTGRES_ENABLED": "true", "POSTGRES_URL": url,
                    "PGOPTIONS": f"-csearch_path={schema}"})
    config_dir = tmp_path / "config"
    (config_dir / "provider_hub").mkdir(parents=True)
    state_file = config_dir / "provider_hub" / "state.json"
    state_file.write_text(content, encoding="utf-8")
    script = tmp_path / "boot_child.py"
    script.write_text(_BOOT_CHILD_CORRUPT, encoding="utf-8")
    env.update({"BAZARR_REPO_ROOT": os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..")),
                "BAZARR_CONFIG_DIR": str(config_dir), "SZ_USER_AGENT": "test", "BAZARR_VERSION": "test"})
    env.pop("BAZARR_PROVIDER_HUB_STATE", None)
    try:
        proc = subprocess.run([sys.executable, str(script), "-c", str(config_dir)], env=env,
                              capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, f"the boot stopped\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        result = json.loads(next(line for line in proc.stdout.splitlines()
                                 if line.startswith("RESULT "))[len("RESULT "):])
        assert result["held"] is False
        assert len(result["copies"]) == 1 and result["copies"][0].endswith(".corrupt")
        assert state_file.read_text(encoding="utf-8") == content
    finally:
        if admin is not None:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


def _plaintext_installation(provider_id="credhub"):
    return {provider_id: {
        "provider_id": provider_id, "name": "Cred Hub", "active_version": "1.0.0", "state": "active",
        "pending_restart": False, "manifest": _manifest(provider_id=provider_id),
        "config": {"api_key": "plaintext-credential"},
    }}


@pytest.fixture
def migration_env(hub, monkeypatch):
    """The credential migration without touching the real settings store."""
    monkeypatch.setattr("secret_store.crypto.persist_master_key", lambda: None)
    return hub


def _copy_text(tmp_path):
    return (tmp_path / "provider_hub" / "state.json").read_text(encoding="utf-8")


def test_a_credential_migration_fails_when_the_file_copy_cannot_be_rewritten(migration_env, tmp_path, monkeypatch):
    from provider_hub import state
    from secret_store.registry import HubSecretMigrationError, migrate_installation_config_secrets
    _seed(_plaintext_installation())
    assert "plaintext-credential" in _copy_text(tmp_path)

    def broken(data, current):
        raise OSError("disk full")

    monkeypatch.setattr(state, "write_state_file", broken)
    with pytest.raises(HubSecretMigrationError):
        migrate_installation_config_secrets()

    # The database was encrypted; the file still holds the plaintext.
    assert _stored(migration_env)["installations"]["credhub"]["config"]["api_key"].startswith("enc:v1:")
    assert "plaintext-credential" in _copy_text(tmp_path)


def test_the_next_credential_migration_sanitizes_a_file_the_database_has_outgrown(migration_env, tmp_path, monkeypatch):
    from provider_hub import state
    from secret_store.registry import HubSecretMigrationError, migrate_installation_config_secrets
    _seed(_plaintext_installation())
    real = state.write_state_file
    monkeypatch.setattr(state, "write_state_file", lambda data, current: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(HubSecretMigrationError):
        migrate_installation_config_secrets()
    monkeypatch.setattr(state, "write_state_file", real)

    # A second start: nothing left to encrypt in the database, the file is repaired.
    migrate_installation_config_secrets()

    assert "plaintext-credential" not in _copy_text(tmp_path)
    assert json.loads(_copy_text(tmp_path)) == _stored(migration_env)


def test_a_successful_credential_migration_leaves_no_plaintext_in_either_place(migration_env, tmp_path):
    from secret_store.registry import migrate_installation_config_secrets
    _seed(_plaintext_installation())
    migrate_installation_config_secrets()
    assert "plaintext-credential" not in raw_row(migration_env)[1]
    assert "plaintext-credential" not in _copy_text(tmp_path)
