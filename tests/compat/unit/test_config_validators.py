import pytest
import os
import tempfile
import yaml
from dynaconf import Dynaconf, Validator as OriginalValidator
from dynaconf.validator import ValidationError
from types import MappingProxyType


class Validator(OriginalValidator):
    # Match the custom Validator class from config.py
    default_messages = MappingProxyType(
        {
            "must_exist_true": "{name} is required",
            "must_exist_false": "{name} cannot exists",
            "condition": "{name} invalid for {function}({value})",
            "operations": "{name} must {operation} {op_value} but it is {value}",
            "combined": "combined validators failed {errors}",
        }
    )


def test_compat_endpoint_validators_require_secrets_when_enabled():
    """When enabled=True, all three secrets must exist and be >=32 chars."""
    with tempfile.TemporaryDirectory() as tmp_path:
        # Write a config with enabled=True but empty jwt_secret
        cfg_path = os.path.join(tmp_path, "config.yaml")
        config_data = {
            "compat_endpoint": {
                "enabled": True,
                "token": "x" * 32,
                "jwt_secret": "",  # empty -> must_exist enforcement
                "file_id_secret": "y" * 32,
            }
        }
        with open(cfg_path, "w") as f:
            yaml.dump(config_data, f)

        # Create Dynaconf instance and register validators
        settings = Dynaconf(
            settings_file=cfg_path,
            core_loaders=['YAML'],
            apply_default_on_none=True,
        )

        # Add the compat_endpoint validators
        validators_list = [
            Validator("compat_endpoint.enabled", default=False, cast=bool),
            Validator("compat_endpoint.token", default="", cast=str),
            Validator("compat_endpoint.jwt_secret", default="", cast=str),
            Validator("compat_endpoint.file_id_secret", default="", cast=str),
            Validator(
                "compat_endpoint.token",
                must_exist=True, is_type_of=str, len_min=32,
                when=Validator("compat_endpoint.enabled", eq=True),
            ),
            Validator(
                "compat_endpoint.jwt_secret",
                must_exist=True, is_type_of=str, len_min=32,
                when=Validator("compat_endpoint.enabled", eq=True),
            ),
            Validator(
                "compat_endpoint.file_id_secret",
                must_exist=True, is_type_of=str, len_min=32,
                when=Validator("compat_endpoint.enabled", eq=True),
            ),
            Validator("compat_endpoint.cache_ttl_seconds",
                      default=1800, cast=int, gte=60, lte=86400),
            Validator("compat_endpoint.cache_ttl_partial_seconds",
                      default=300, cast=int, gte=30, lte=3600),
            Validator("compat_endpoint.search_timeout_seconds",
                      default=20, cast=int, gte=5, lte=120),
            Validator("compat_endpoint.per_provider_timeout_seconds",
                      default=12, cast=int, gte=3, lte=60),
            Validator("compat_endpoint.file_id_ttl_seconds",
                      default=3600, cast=int, gte=300, lte=86400),
            Validator("compat_endpoint.stream_token_ttl_seconds",
                      default=300, cast=int, gte=60, lte=3600),
            Validator("compat_endpoint.jwt_ttl_seconds",
                      default=86400, cast=int, gte=3600, lte=604800),
        ]

        settings.validators.register(*validators_list)

        # Should raise ValidationError due to empty jwt_secret when enabled=True
        with pytest.raises(ValidationError):
            settings.validators.validate_all()


def test_compat_endpoint_defaults_when_disabled():
    """When enabled=False, validators should not enforce secret requirements."""
    with tempfile.TemporaryDirectory() as tmp_path:
        cfg_path = os.path.join(tmp_path, "config.yaml")
        config_data = {
            "compat_endpoint": {
                "enabled": False,
                # All secrets can be empty when disabled
                "token": "",
                "jwt_secret": "",
                "file_id_secret": "",
            }
        }
        with open(cfg_path, "w") as f:
            yaml.dump(config_data, f)

        settings = Dynaconf(
            settings_file=cfg_path,
            core_loaders=['YAML'],
            apply_default_on_none=True,
        )

        validators_list = [
            Validator("compat_endpoint.enabled", default=False, cast=bool),
            Validator("compat_endpoint.token", default="", cast=str),
            Validator("compat_endpoint.jwt_secret", default="", cast=str),
            Validator("compat_endpoint.file_id_secret", default="", cast=str),
            Validator(
                "compat_endpoint.token",
                must_exist=True, is_type_of=str, len_min=32,
                when=Validator("compat_endpoint.enabled", eq=True),
            ),
            Validator(
                "compat_endpoint.jwt_secret",
                must_exist=True, is_type_of=str, len_min=32,
                when=Validator("compat_endpoint.enabled", eq=True),
            ),
            Validator(
                "compat_endpoint.file_id_secret",
                must_exist=True, is_type_of=str, len_min=32,
                when=Validator("compat_endpoint.enabled", eq=True),
            ),
            Validator("compat_endpoint.cache_ttl_seconds",
                      default=1800, cast=int, gte=60, lte=86400),
            Validator("compat_endpoint.cache_ttl_partial_seconds",
                      default=300, cast=int, gte=30, lte=3600),
            Validator("compat_endpoint.search_timeout_seconds",
                      default=20, cast=int, gte=5, lte=120),
            Validator("compat_endpoint.per_provider_timeout_seconds",
                      default=12, cast=int, gte=3, lte=60),
            Validator("compat_endpoint.file_id_ttl_seconds",
                      default=3600, cast=int, gte=300, lte=86400),
            Validator("compat_endpoint.stream_token_ttl_seconds",
                      default=300, cast=int, gte=60, lte=3600),
            Validator("compat_endpoint.jwt_ttl_seconds",
                      default=86400, cast=int, gte=3600, lte=604800),
        ]

        settings.validators.register(*validators_list)

        # Should NOT raise ValidationError
        settings.validators.validate_all()
        assert settings.compat_endpoint.enabled is False
