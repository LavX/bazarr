import sys
from types import SimpleNamespace

import pytest
import yaml
from dynaconf import Dynaconf
from dynaconf.validator import ValidationError


@pytest.fixture
def saved_config(tmp_path, monkeypatch):
    from app import config
    from sqlalchemy import update
    from app.database import System
    snapshot = config.settings.as_dict()
    path = tmp_path / "config.yaml"
    # Bound to its own file, exactly as the live object is: a settings object
    # with nowhere to reload from cannot show what a failed save falls back to.
    settings = Dynaconf(settings_file=str(path), core_loaders=["YAML"], validators=config.validators)
    settings.update(snapshot)
    settings.validators.validate()
    monkeypatch.setattr(config, "settings", settings)
    from media_servers import dispatcher
    monkeypatch.setattr(dispatcher, "_configuration", dispatcher.NativeConfiguration(settings))
    monkeypatch.setattr(config, "config_yaml_file", str(path))
    monkeypatch.setattr(config, "_active_provider_hub_provider_ids", lambda: set())
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: None), update=update, System=System))
    return config, path


@pytest.mark.parametrize("server", ["emby", "silo"])
def test_retired_numeric_key_remains_encrypted_after_unrelated_save(saved_config, server):
    from secret_store.migration import decrypt_settings_in_place
    config, path = saved_config
    config.settings[server].apikey = '000123'
    rows = [{"local_path": "/movies", "remote_path": "/media", "library_id": "0007"}]
    config.settings[server].path_mappings = rows
    config.settings[server].verify_ssl = False
    config.save_settings([("settings-general-page_size", ["50"])])
    on_disk = yaml.safe_load(path.read_text())
    assert on_disk[server]["apikey"].startswith("enc:v1:")
    assert "000123" not in path.read_text()
    rebooted = Dynaconf(settings_file=str(path), core_loaders=["YAML"])
    decrypt_settings_in_place(rebooted)
    assert rebooted[server].apikey == "000123"
    assert rebooted[server].path_mappings == rows
    assert rebooted[server].verify_ssl is False
    assert rebooted.general.page_size == 50
    assert server not in config.get_settings()


@pytest.mark.parametrize("value", ["not-json", "{}", '[{"local_path":"/movies"}]',
                                  '[{"local_path":"/movies","remote_path":"/media","library_id":7}]'])
@pytest.mark.parametrize("server", ["emby", "silo"])
def test_invalid_mapping_batch_is_rejected_before_any_setting_changes(saved_config, value, server):
    config, path = saved_config
    config.write_config()
    before_bytes = path.read_bytes()
    before_page_size = config.settings.general.page_size
    with pytest.raises(ValidationError):
        config.save_settings([("settings-general-page_size", ["123"]), (f"settings-{server}-path_mappings", [value])])
    assert path.read_bytes() == before_bytes
    assert config.settings.general.page_size == before_page_size


@pytest.mark.parametrize("server", ["emby", "silo"])
def test_native_defaults_are_disabled_and_safe(server):
    from app import config
    settings = Dynaconf(core_loaders=["YAML"], validators=[v for v in config.validators
                        if any(name.startswith(f"{server}.") or name == f"general.use_{server}" for name in v.names)])
    settings.validators.validate()
    assert settings.general[f"use_{server}"] is False
    assert settings[server].url == ""
    assert settings[server].apikey == ""
    assert settings[server].verify_ssl is True
    assert settings[server].path_mappings == []


def test_master_switch_changes_only_affected_kind(saved_config):
    from media_servers.dispatcher import get_native_configuration
    from test_media_server_dispatcher import IDS, native_settings, native_snapshots
    config, _path = saved_config
    native = get_native_configuration()
    config.settings.general.use_emby = True
    config.settings.general.use_silo = True
    native.publish_masters(native_settings())
    for snapshot in native_snapshots(native_settings()):
        native.publish(snapshot)
    emby_before = native.read(IDS['emby'])
    silo_before = native.read(IDS['silo'])
    config.save_settings([('settings-general-use_silo', ['false'])])
    assert native.read(IDS['emby']) == emby_before
    assert native.read(IDS['silo'])[0] != silo_before[0]
    assert native.read(IDS['silo'])[1].enabled is False


@pytest.mark.parametrize('failure', ['validation', 'write', 'move'])
def test_failed_master_save_keeps_last_usable_revision(saved_config, monkeypatch, failure):
    from media_servers.dispatcher import get_native_configuration
    from test_media_server_dispatcher import IDS, native_settings, native_snapshots
    config, path = saved_config
    native = get_native_configuration()
    for snapshot in native_snapshots(native_settings()):
        native.publish(snapshot)
    config.save_settings([('settings-general-use_silo', ['true'])])
    before = native.read(IDS['silo'])
    before_disk = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('synthetic-sensitive-remote-text')
    if failure != 'validation':
        monkeypatch.setattr(config, failure, fail)
    value = 'invalid' if failure == 'validation' else 'false'
    with pytest.raises(ValidationError):
        config.save_settings([('settings-general-use_silo', [value])])
    assert native.read(IDS['silo']) == before
    native.ensure_current(IDS['silo'], before[0])
    assert config.settings.general.use_silo is True
    assert path.read_bytes() == before_disk


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_import_only_scalar_secrets_hidden_and_edits_rejected(saved_config, kind):
    config, _path = saved_config
    config.settings[kind].apikey = 'synthetic-private-import-key'
    assert kind not in config.get_settings()
    before = config.settings.general.page_size
    with pytest.raises(ValidationError):
        config.save_settings([('settings-general-page_size', ['999']), (f'settings-{kind}-url', ['http://other'])])
    assert config.settings.general.page_size == before


def test_failed_startup_write_keeps_generated_key_pending_until_persisted(saved_config, monkeypatch):
    from secret_store import persist_master_key
    config, path = saved_config
    config.settings.general.secrets_encryption_key = ''
    original_write = config.write
    def fail(*args, **kwargs):
        raise OSError('synthetic failure')
    monkeypatch.setattr(config, 'write', fail)
    assert config.write_config() is False
    generated = config.settings.general.secrets_encryption_key
    assert generated
    with pytest.raises(ValueError, match='Unable to persist secrets encryption key'):
        persist_master_key()
    assert not path.exists()
    monkeypatch.setattr(config, 'write', original_write)
    persist_master_key()
    assert yaml.safe_load(path.read_text())['general']['secrets_encryption_key'] == generated
    monkeypatch.setattr(config, 'write_config', lambda: pytest.fail('durable key was persisted again'))
    persist_master_key()


def test_successful_ordinary_save_marks_generated_master_key_durable(saved_config, monkeypatch):
    from secret_store import persist_master_key
    config, path = saved_config
    config.settings.general.secrets_encryption_key = ''
    assert config.write_config() is True and path.exists()
    monkeypatch.setattr(config, 'write_config', lambda: pytest.fail('ordinary save already persisted this key'))
    persist_master_key()


@pytest.mark.parametrize('failure', ['write', 'move'])
def test_a_save_that_never_reached_disk_leaves_nothing_applied(saved_config, monkeypatch, failure):
    """A master switch travels with whatever else the settings page submitted.

    Refusing the request while the co-saved values stay on the live settings
    object leaves the process running a configuration that is not on disk, and
    reverting silently at the next restart.
    """
    from media_servers.dispatcher import get_native_configuration
    from test_media_server_dispatcher import IDS, native_settings, native_snapshots
    config, path = saved_config
    native = get_native_configuration()
    for snapshot in native_snapshots(native_settings()):
        native.publish(snapshot)
    config.save_settings([('settings-general-use_silo', ['true']),
                          ('settings-general-page_size', ['25'])])
    before_disk = path.read_bytes()
    before = native.read(IDS['silo'])

    def fail(*args, **kwargs):
        raise OSError('synthetic-sensitive-remote-text')

    monkeypatch.setattr(config, failure, fail)
    with pytest.raises(ValidationError):
        config.save_settings([('settings-general-use_silo', ['false']),
                              ('settings-general-page_size', ['250'])])

    assert path.read_bytes() == before_disk
    assert config.settings.general.page_size == 25
    assert config.settings.general.use_silo is True
    assert native.read(IDS['silo']) == before
    native.ensure_current(IDS['silo'], before[0])


def test_a_failed_save_keeps_credentials_readable(saved_config, monkeypatch):
    """settings.reload() pulls the on-disk ciphertext back in, so the rollback
    has to decrypt again or every secret stays an ``enc:v1:`` string until the
    process restarts.

    The co-submitted page size is what makes the key assertion mean anything:
    without a rollback nothing touches the apikey either, so the plaintext set
    below survives trivially and the test cannot tell "reloaded and decrypted
    again" from "never reloaded".
    """
    config, _path = saved_config
    config.settings.auth.apikey = 'synthetic-rollback-key'
    config.save_settings([('settings-general-use_emby', ['false']),
                          ('settings-general-page_size', ['25'])])

    def fail(*args, **kwargs):
        raise OSError('synthetic-sensitive-remote-text')

    monkeypatch.setattr(config, 'write', fail)
    with pytest.raises(ValidationError):
        config.save_settings([('settings-general-use_emby', ['true']),
                              ('settings-general-page_size', ['250'])])
    assert config.settings.general.page_size == 25, 'the rollback did not reload'
    assert config.settings.auth.apikey == 'synthetic-rollback-key'


@pytest.mark.parametrize('failure', ['write', 'move'])
def test_an_ordinary_save_that_never_reached_disk_is_refused(saved_config, monkeypatch, failure):
    """Every save, not only the ones carrying a media-server master switch.

    A settings request applies each submitted value to the live object before
    config.yaml is written. A write that fails and is ignored answers success
    to the frontend and leaves the process running a configuration that is on
    no disk and reverts silently at the next restart.
    """
    config, path = saved_config
    config.save_settings([('settings-general-page_size', ['25'])])
    before_disk = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError('synthetic-sensitive-remote-text')

    monkeypatch.setattr(config, failure, fail)
    with pytest.raises(ValidationError):
        config.save_settings([('settings-general-page_size', ['250'])])
    assert path.read_bytes() == before_disk
    assert config.settings.general.page_size == 25
