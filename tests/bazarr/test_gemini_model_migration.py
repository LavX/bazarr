import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml


def test_retired_default_moves_to_the_current_default(caplog):
    from app import config

    settings = SimpleNamespace(translator=SimpleNamespace(gemini_model="gemini-2.0-flash"))

    with caplog.at_level(logging.WARNING):
        assert config.migrate_retired_gemini_model(settings) is True

    assert settings.translator.gemini_model == "gemini-3.6-flash"
    assert any(record.levelno == logging.WARNING and "gemini-2.0-flash" in record.message
               and "gemini-3.6-flash" in record.message for record in caplog.records)
    assert config.migrate_retired_gemini_model(settings) is False


@pytest.mark.parametrize("model", [
    "gemini-3.6-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-3.1-flash-lite",
    "my-tuned-model",
    "",
])
def test_other_gemini_models_are_left_alone(model):
    from app import config

    settings = SimpleNamespace(translator=SimpleNamespace(gemini_model=model))

    assert config.migrate_retired_gemini_model(settings) is False
    assert settings.translator.gemini_model == model


@pytest.mark.parametrize("configured, expected", [
    (None, "gemini-3.6-flash"),
    ("gemini-2.0-flash", "gemini-3.6-flash"),
    ("gemini-2.5-flash", "gemini-2.5-flash"),
    ("gemini-2.0-flash-lite", "gemini-2.0-flash-lite"),
    ("my-tuned-model", "my-tuned-model"),
])
def test_startup_default_and_persisted_migration(tmp_path, configured, expected):
    config_dir = tmp_path / "install"
    (config_dir / "config").mkdir(parents=True)
    config_file = config_dir / "config" / "config.yaml"
    translator = {"translator_type": "gemini"}
    if configured is not None:
        translator["gemini_model"] = configured
    config_file.write_text(yaml.safe_dump({"translator": translator}), encoding="utf-8")
    repo = Path(__file__).resolve().parents[2]
    script = '''
import json
import os
import sys
sys.path[:0] = [os.path.join(sys.argv[1], 'custom_libs'), os.path.join(sys.argv[1], 'bazarr')]
os.environ['NO_CLI'] = 'true'
from app.get_args import args
args.config_dir = sys.argv[2]
args.no_tasks = True
from app.config import settings
print(json.dumps({'model': settings.translator.gemini_model}))
'''
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    first_contents = None
    for _startup in range(2):
        process = subprocess.run([sys.executable, "-c", script, str(repo), str(config_dir)],
                                 cwd=repo, env=env, capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stderr
        assert json.loads(process.stdout) == {"model": expected}
        persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert persisted["translator"]["gemini_model"] == expected
        if first_contents is None:
            first_contents = config_file.read_bytes()
        else:
            assert config_file.read_bytes() == first_contents
