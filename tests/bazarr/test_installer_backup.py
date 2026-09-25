# coding=utf-8
"""The installer's upgrade backup has to follow the config directory the stack mounts.

The reference compose file mounts ``${CONFIG_PATH:-./config}`` at /config, and
the backup used to look for ./config only. With CONFIG_PATH set, it copied the
compose file and .env and nothing else, and it read the database settings from
a config.yaml that did not exist, so a PostgreSQL database in the stack was
taken for SQLite and never dumped. Both upgrade and reinstall went ahead on the
strength of that backup.

The functions run in bash as install.sh defines them, pulled out by name
because the script ends in `main "$@"` and sourcing it would run the installer.
Docker and sudo are shell stubs: `docker compose config` answers with the long
form Docker itself renders, which is where the mount source is read from.
"""
import os
import re
import subprocess

import pytest

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', '..'))

FUNCTIONS = ('compose_services', 'compose_bazarr_service', 'compose_env', 'compose_config_source',
             'config_postgres_value', 'detect_database', 'dump_postgres', 'restart_and_fail',
             'do_backup')

STUBS = r'''
info() { :; }; success() { printf 'SUCCESS %s\n' "$*"; }; warn() { printf 'WARN %s\n' "$*"; }
error() { :; }; fatal() { printf 'FATAL %s\n' "$*"; exit 1; }
run_with_spinner() { shift; "$@"; }
confirm() { return 1; }
sudo() { "$@"; }
# docker compose -f FILE <command> ...
docker() {
  shift 3
  case "$1" in
    config) cat "$FAKE_COMPOSE_CONFIG" ;;
    exec) printf 'EXEC %s\n' "$*" >> "$FAKE_LOG"; printf 'PGDMP' ;;
    *) printf '%s\n' "$*" >> "$FAKE_LOG" ;;
  esac
}
PG_BACKUP_DOCS=https://example.invalid/docs
'''


def _functions():
    with open(os.path.join(ROOT, 'site', 'install.sh')) as handle:
        script = handle.read()
    bodies = []
    for name in FUNCTIONS:
        body = re.search(rf'^{name}\(\) \{{.*?^\}}', script, re.S | re.M)
        assert body, f'{name}() is gone from install.sh, or no longer starts at column 0'
        bodies.append(body.group(0))
    return '\n'.join(bodies)


def _rendered_config(config_source, postgres_service=False):
    """What `docker compose config` prints for the stack, trimmed to what is read."""
    rendered = (
        'name: bazarr\n'
        'services:\n'
        '  bazarr:\n'
        '    environment:\n'
        '      PUID: "1000"\n'
        '    image: ghcr.io/lavx/bazarr:latest\n'
        '    volumes:\n'
        '      - type: bind\n'
        '        source: /srv/movies\n'
        '        target: /movies\n'
        '        bind: {}\n'
        '      - type: bind\n'
        f'        source: {config_source}\n'
        '        target: /config\n'
        '        bind: {}\n'
    )
    if postgres_service:
        rendered += (
            '  db:\n'
            '    image: postgres:16\n'
            '    volumes:\n'
            '      - type: volume\n'
            '        source: pgdata\n'
            '        target: /var/lib/postgresql/data\n'
            '        volume: {}\n'
        )
    return rendered


@pytest.fixture
def install(tmp_path):
    """An existing install directory, and a runner for do_backup against it."""
    install_dir = tmp_path / 'install'
    install_dir.mkdir()
    (install_dir / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
    (install_dir / '.env').write_text('PUID=1000\n', encoding='utf-8')
    rendered = tmp_path / 'rendered.yml'
    log = tmp_path / 'docker.log'

    def run(config_text):
        rendered.write_text(config_text, encoding='utf-8')
        program = (_functions() + STUBS + 'do_backup "$1"\n'
                   'printf "CONFIG_DIR=%s\\nDB_ENGINE=%s\\n" "$CONFIG_DIR" "$DB_ENGINE"\n')
        result = subprocess.run(['bash', '-c', program, 'bash', str(install_dir)], capture_output=True,
                                text=True, timeout=60,
                                env=dict(os.environ, FAKE_COMPOSE_CONFIG=str(rendered), FAKE_LOG=str(log)))
        assert result.returncode == 0, f'do_backup failed:\n{result.stdout}\n{result.stderr}'
        [backup] = [path for path in install_dir.iterdir() if path.name.startswith('backup_')]
        return result.stdout, backup, log.read_text(encoding='utf-8') if log.exists() else ''

    return install_dir, run


def _config_tree(root, config_yaml):
    (root / 'config').mkdir(parents=True)
    (root / 'db').mkdir()
    (root / 'config' / 'config.yaml').write_text(config_yaml, encoding='utf-8')
    (root / 'db' / 'bazarr.db').write_text('sqlite-bytes', encoding='utf-8')


def test_a_custom_config_path_is_what_gets_backed_up(install, tmp_path):
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'general:\n  port: 6767\n')

    output, backup, _ = run(_rendered_config(custom))

    assert f'CONFIG_DIR={custom}' in output
    assert (backup / 'config' / 'config' / 'config.yaml').read_text(encoding='utf-8') == \
        'general:\n  port: 6767\n'
    assert (backup / 'config' / 'db' / 'bazarr.db').is_file()
    assert f'Backed up docker-compose.yml, .env, {custom} and the SQLite database inside it' in output
    assert 'WARN' not in output


def test_an_in_stack_postgres_configured_only_in_a_custom_config_path_is_dumped(install, tmp_path):
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'postgresql:\n  enabled: true\n  host: db\n  database: bazarr\n')

    output, backup, docker_log = run(_rendered_config(custom, postgres_service=True))

    assert 'DB_ENGINE=postgres' in output
    assert (backup / 'bazarr_postgres.dump').read_text(encoding='utf-8') == 'PGDMP'
    assert 'EXEC exec -T db' in docker_log
    assert (backup / 'config' / 'config' / 'config.yaml').is_file()


def test_the_default_config_directory_is_still_backed_up(install):
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')

    output, backup, _ = run(_rendered_config(install_dir / 'config'))

    assert (backup / 'config' / 'db' / 'bazarr.db').is_file()
    assert 'Backed up docker-compose.yml, .env, ./config and the SQLite database inside it' in output


def test_without_a_config_bind_mount_the_backup_falls_back_to_the_config_directory(install):
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    rendered = _rendered_config('/unused').replace(
        '      - type: bind\n        source: /unused\n        target: /config\n        bind: {}\n', '')
    assert 'target: /config' not in rendered

    output, backup, _ = run(rendered)

    assert f'CONFIG_DIR={install_dir}/config' in output
    assert (backup / 'config' / 'db' / 'bazarr.db').is_file()
