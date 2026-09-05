import logging
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("retired,replacement", [
    ("google/gemini-2.5-flash-preview-05-20", "google/gemini-2.5-flash"),
    ("google/gemini-2.5-flash-lite-preview-06-17", "google/gemini-2.5-flash-lite"),
    ("google/gemini-2.5-flash-lite-preview-09-2025", "google/gemini-2.5-flash-lite"),
    ("google/gemini-3-pro-preview", "google/gemini-3.1-pro-preview"),
    ("  google/gemini-2.5-flash-preview-05-20  ", "google/gemini-2.5-flash"),
])
def test_migrate_retired_openrouter_model_rewrites_retired_id(retired, replacement, caplog):
    from app import config

    settings = SimpleNamespace(translator=SimpleNamespace(openrouter_model=retired))

    with caplog.at_level(logging.WARNING):
        assert config.migrate_retired_openrouter_model(settings) is True

    assert settings.translator.openrouter_model == replacement
    assert any(
        record.levelno == logging.WARNING and retired.strip() in record.message and replacement in record.message
        for record in caplog.records
    )
    assert config.migrate_retired_openrouter_model(settings) is False


@pytest.mark.parametrize("model", ["google/gemini-2.5-flash-lite", "custom/model", "  custom/model  ", "", "  "])
def test_migrate_retired_openrouter_model_leaves_other_values_unchanged(model):
    from app import config

    settings = SimpleNamespace(translator=SimpleNamespace(openrouter_model=model))

    assert config.migrate_retired_openrouter_model(settings) is False
    assert settings.translator.openrouter_model == model
