import os
import tempfile

import pytest
import yaml
from dynaconf import Dynaconf
from dynaconf.validator import ValidationError

# Pull the real validators list from the application config module.
# This is the source of truth: the same Validator objects registered
# into the production `settings` singleton at startup.
from bazarr.app.config import validators as app_validators


def _compat_validators():
    """Return only the compat_endpoint validators from the real config list."""
    return [
        v for v in app_validators
        if any("compat_endpoint" in name for name in v.names)
    ]


def _make_settings(config_data: dict) -> Dynaconf:
    """Fresh, isolated Dynaconf instance loaded from a temp YAML file and
    pre-registered with the real compat_endpoint validators from config.py.
    """
    tmp_dir = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp_dir, "config.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(config_data, f)
    s = Dynaconf(settings_file=cfg_path, core_loaders=["YAML"], apply_default_on_none=True)
    s.validators.register(*_compat_validators())
    return s


def test_defaults_when_no_config_present():
    """Empty config: validators apply defaults (enabled=False, empty secrets)
    and do not raise."""
    tmp_dir = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp_dir, "config.yaml")
    open(cfg_path, "w").close()

    s = Dynaconf(settings_file=cfg_path, core_loaders=["YAML"], apply_default_on_none=True)
    s.validators.register(*_compat_validators())

    s.validators.validate_all()
    assert s.compat_endpoint.enabled is False


def test_save_with_enabled_true_and_empty_secrets_is_allowed():
    """The save-time path must accept enabled=True with empty secrets.
    Secret length is enforced at blueprint registration (boot_hmac_selftest),
    NOT at save time -- saving must not reject a first-time enable just
    because secrets haven't been auto-generated yet."""
    s = _make_settings({
        "compat_endpoint": {
            "enabled": True,
            "token": "",
            "jwt_secret": "",
            "file_id_secret": "",
        }
    })
    s.validators.validate_all()  # must NOT raise
    assert s.compat_endpoint.enabled is True


def test_ttl_bounds_enforced():
    """Numeric TTL validators still fire (gte/lte) regardless of enabled."""
    s = _make_settings({
        "compat_endpoint": {
            "cache_ttl_seconds": 5,  # below gte=60
        }
    })
    with pytest.raises(ValidationError):
        s.validators.validate_all()


def test_ttl_bounds_accept_valid_value():
    s = _make_settings({
        "compat_endpoint": {
            "cache_ttl_seconds": 1800,
            "search_timeout_seconds": 25,
        }
    })
    s.validators.validate_all()
    assert s.compat_endpoint.cache_ttl_seconds == 1800
