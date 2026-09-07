import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


@pytest.mark.parametrize('configured, expected', [
    (None, 'google/gemini-2.5-flash-lite'),
    ('google/gemini-2.5-flash-preview-05-20', 'google/gemini-2.5-flash'),
    ('google/gemini-2.5-flash-lite-preview-06-17', 'google/gemini-2.5-flash-lite'),
    ('google/gemini-2.5-flash-lite-preview-09-2025', 'google/gemini-2.5-flash-lite'),
    ('google/gemini-3-pro-preview', 'google/gemini-3.1-pro-preview'),
    ('google/gemini-2.5-flash', 'google/gemini-2.5-flash'),
    ('google/gemini-2.5-flash-lite', 'google/gemini-2.5-flash-lite'),
    ('google/gemini-3.1-pro-preview', 'google/gemini-3.1-pro-preview'),
    ('x-ai/grok-4-fast', 'x-ai/grok-4-fast'),
    ('x-ai/grok-4.20-beta', 'x-ai/grok-4.20-beta'),
    ('custom/my-model', 'custom/my-model'),
    ('google/gemini-2.5-flash-preview-custom', 'google/gemini-2.5-flash-preview-custom'),
])
def test_startup_persists_only_known_model_replacements(tmp_path, configured, expected):
    config_dir = tmp_path / 'install'
    (config_dir / 'config').mkdir(parents=True)
    config_file = config_dir / 'config' / 'config.yaml'
    translator = {'openrouter_temperature': 0.42, 'translator_type': 'openrouter'}
    if configured is not None:
        translator['openrouter_model'] = configured
    config_file.write_text(yaml.safe_dump({'translator': translator}), encoding='utf-8')
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
print(json.dumps({'model': settings.translator.openrouter_model,
                  'temperature': settings.translator.openrouter_temperature}))
'''
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'XDG_CACHE_HOME': str(tmp_path / 'cache')}
    first_contents = None
    for _startup in range(2):
        process = subprocess.run([sys.executable, '-c', script, str(repo), str(config_dir)],
                                 cwd=repo, env=env, capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stderr
        assert json.loads(process.stdout) == {'model': expected, 'temperature': 0.42}
        persisted = yaml.safe_load(config_file.read_text(encoding='utf-8'))
        assert persisted['translator']['openrouter_model'] == expected
        assert persisted['translator']['openrouter_temperature'] == 0.42
        if first_contents is None:
            first_contents = config_file.read_bytes()
        else:
            assert config_file.read_bytes() == first_contents
