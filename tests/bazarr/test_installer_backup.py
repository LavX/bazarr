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

A backup that fails partway must not leave a folder that looks like a finished
one. The installer used to leave backup_<ts> behind with whatever it had copied
by then, a part of the config tree after a full disk, and restarted the old
services with that disk still full.

The migration guide's backup and restore commands are here too, run as a
reader would paste them. Both used `cp -a` into a name that could already
exist, which copies into the folder instead of replacing it: a second backup
on the same day went inside the first, and a restore after an interrupted one
swapped in the stale staging folder.
"""
import html
import os
import re
import signal
import stat
import subprocess
import time

import pytest

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', '..'))
GUIDE = os.path.join(ROOT, 'site', 'guides', 'migration.html')

FUNCTIONS = ('compose_services', 'compose_bazarr_service', 'compose_env', 'compose_config_mount',
             'config_postgres_value', 'url_query_value', 'url_query_count', 'url_decode', 'detect_database',
             'dump_postgres', 'discard_backup', 'restart_and_fail', 'backup_interrupted', 'do_backup')

STUBS = r'''
info() { printf 'INFO %s\n' "$*"; }; success() { printf 'SUCCESS %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
error() { :; }; fatal() { printf 'FATAL %s\n' "$*"; exit 1; }
run_with_spinner() { shift; "$@"; }
confirm() { return 1; }
sudo() { "$@"; }
# docker compose -f FILE <command> ...
docker() {
  shift 3
  case "$1" in
    config) cat "$FAKE_COMPOSE_CONFIG" ;;
    exec) printf 'EXEC %s\n' "$*" >> "$FAKE_LOG"
          [[ -z "${FAKE_DUMP_FAILS:-}" ]] || return 1
          printf 'PGDMP' ;;
    stop) printf '%s\n' "$*" >> "$FAKE_LOG"; [[ -z "${FAKE_STOP_FAILS:-}" ]] ;;
    # Also notes which backup folders are still there when the old services come back.
    start) local found; found=$(cd "$FAKE_INSTALL" && ls -d backup_* 2>/dev/null | tr '\n' ' ')
           printf 'start (backup folders: %s)\n' "${found:-none}" >> "$FAKE_LOG" ;;
    *) printf '%s\n' "$*" >> "$FAKE_LOG" ;;
  esac
}
PG_BACKUP_DOCS=https://example.invalid/docs
'''

# The config copy is `sudo cp -a SOURCE BACKUP/config`, and the sudo stub runs the cp
# function below. Each writes part of the tree first, the way a copy stopped by a full
# disk or a kill does.
FAILING_CONFIG_COPY = r'''
cp() {
  if [[ "$3" == */config ]]; then mkdir -p "$3/db"; printf half > "$3/db/bazarr.db"; return 1; fi
  command cp "$@"
}
'''
KILLED_CONFIG_COPY = r'''
cp() {
  if [[ "$3" == */config ]]; then mkdir -p "$3/db"; printf half > "$3/db/bazarr.db"; kill -KILL $$; fi
  command cp "$@"
}
'''

# A catchable signal partway through the config copy, which leaves the folder the
# interrupted run has to remove. The copy waits on a step the signal lands in: bash runs
# the pending trap as soon as the wait it interrupted returns, still inside do_backup's
# call. SIGNAL is the name the test substitutes.
INTERRUPTED_CONFIG_COPY = r'''
cp() {
  if [[ "$3" == */config ]]; then
    mkdir -p "$3/db"; printf half > "$3/db/bazarr.db"
    ( sleep 5 ) >/dev/null 2>&1 & local waiter=$!
    ( sleep 1; kill -SIGNAL $$ ) >/dev/null 2>&1 &
    wait "$waiter"
  fi
  command cp "$@"
}
'''


def _functions(*names):
    with open(os.path.join(ROOT, 'site', 'install.sh')) as handle:
        script = handle.read()
    bodies = []
    for name in names or FUNCTIONS:
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

    def run(config_text, succeed=True, stubs='', status=None, after='', **fake):
        rendered.write_text(config_text, encoding='utf-8')
        program = (_functions() + STUBS + stubs + 'do_backup "$1"\n' + after +
                   'printf "CONFIG_DIR=%s\\nDB_ENGINE=%s\\n" "$CONFIG_DIR" "$DB_ENGINE"\n')
        env = dict(os.environ, FAKE_COMPOSE_CONFIG=str(rendered), FAKE_LOG=str(log),
                   FAKE_INSTALL=str(install_dir))
        env.update({f'FAKE_{name.upper()}': value for name, value in fake.items()})
        result = subprocess.run(['bash', '-c', program, 'bash', str(install_dir)], capture_output=True,
                                text=True, timeout=60, env=env)
        backups = [path for path in install_dir.iterdir() if path.name.startswith('backup_')]
        docker_log = log.read_text(encoding='utf-8') if log.exists() else ''
        if not succeed:
            assert result.returncode != 0, f'do_backup went ahead:\n{result.stdout}\n{result.stderr}'
            if status is not None:
                assert result.returncode == status, (
                    f'do_backup ended with status {result.returncode}, not {status}:\n'
                    f'{result.stdout}\n{result.stderr}')
            return result.stdout, backups, docker_log
        assert result.returncode == 0, f'do_backup failed:\n{result.stdout}\n{result.stderr}'
        [backup] = backups
        return result.stdout, backup, docker_log

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


def _with_environment(rendered, **variables):
    lines = ''.join(f'      {name}: "{value}"\n' for name, value in variables.items())
    return rendered.replace('      PUID: "1000"\n', '      PUID: "1000"\n' + lines)


# What config.yaml holds when the database was set up through the environment alone.
DEFAULT_POSTGRES_YAML = "postgresql:\n  enabled: false\n  host: localhost\n  port: 5432\n  database: ''\n"


def test_an_in_stack_postgres_configured_only_by_its_url_is_dumped(install, tmp_path):
    """config.yaml always holds host localhost, and it used to beat the URL's host, so a stack
    set up by POSTGRES_URL alone was taken for an outside server and left without a dump."""
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, DEFAULT_POSTGRES_YAML)
    rendered = _with_environment(_rendered_config(custom, postgres_service=True), POSTGRES_ENABLED='true',
                                 POSTGRES_URL='postgresql://bazarr:not-a-secret@db:5432/bazarr?sslmode=disable')

    output, backup, docker_log = run(rendered)

    assert 'DB_ENGINE=postgres' in output
    assert (backup / 'bazarr_postgres.dump').read_text(encoding='utf-8') == 'PGDMP'
    [dumped] = [line for line in docker_log.splitlines() if line.startswith('EXEC ')]
    assert dumped.startswith('EXEC exec -T db ') and dumped.endswith(' bazarr')


def test_an_empty_database_variable_keeps_the_config_yaml_database_out(install, tmp_path):
    """Bazarr+ connects with no database name then, not config.yaml's, so dumping that one
    would back up the wrong database."""
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'postgresql:\n  enabled: true\n  host: db\n  database: bazarr\n')
    rendered = _with_environment(_rendered_config(custom, postgres_service=True), POSTGRES_DATABASE='')

    # Declining to go on without a database backup ends the run here.
    output, backups, docker_log = run(rendered, succeed=False, stubs='confirm() { exit 3; }\n')

    assert 'WARN Bazarr+ uses a PostgreSQL database outside this compose stack' in output
    assert backups == []
    assert 'EXEC' not in docker_log


@pytest.mark.parametrize('query_host, in_stack', [
    ('host=db', True),
    ('hostaddr=10.0.0.5', False),
])
def test_host_and_database_given_only_in_the_url_query_beat_config_yaml(install, tmp_path,
                                                                        query_host, in_stack):
    """libpq takes host, hostaddr and dbname from the URL's query string, which the
    installer's authority and path parsing did not cover: config.yaml's host then decided
    alone, so its localhost turned an in-stack database into an outside one, and its db made
    the installer dump a server the URL never named."""
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'postgresql:\n  enabled: true\n  host: db\n  database: other\n')
    rendered = _with_environment(_rendered_config(custom, postgres_service=True),
                                 POSTGRES_ENABLED='true',
                                 POSTGRES_URL=f'postgresql:///?{query_host}&dbname=bazarr')

    if in_stack:
        output, backup, docker_log = run(rendered)
        assert 'DB_ENGINE=postgres' in output
        assert (backup / 'bazarr_postgres.dump').read_text(encoding='utf-8') == 'PGDMP'
        [dumped] = [line for line in docker_log.splitlines() if line.startswith('EXEC ')]
        assert dumped.startswith('EXEC exec -T db ') and dumped.endswith(' bazarr')
    else:
        output, backups, docker_log = run(rendered, succeed=False, stubs='confirm() { exit 3; }\n')
        assert 'WARN Bazarr+ uses a PostgreSQL database outside this compose stack' in output
        assert backups == []
        assert 'EXEC' not in docker_log


def test_a_query_host_beats_the_authority_host(install, tmp_path):
    """libpq reads the host or hostaddr a URL's query string names over the authority's,
    so the application connects to the query's host. The classification must read the
    URL the same way round, or a URL that moves the connection into the stack keeps
    being taken for an outside database and is never dumped."""
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'postgresql:\n  enabled: true\n  host: localhost\n  database: bazarr\n')
    rendered = _with_environment(_rendered_config(custom, postgres_service=True),
                                 POSTGRES_ENABLED='true',
                                 POSTGRES_URL='postgresql://outside.example/bazarr?host=db')

    output, backup, docker_log = run(rendered)

    assert 'DB_ENGINE=postgres' in output
    [dumped] = [line for line in docker_log.splitlines() if line.startswith('EXEC ')]
    assert dumped.startswith('EXEC exec -T db ')


def test_a_query_dbname_beats_the_path_database(install, tmp_path):
    """The dbname a URL's query names wins over the database in its path for the
    application's own connection, so the installer dumps that database, not the
    path's: a dump of the wrong database is no backup at all."""
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'postgresql:\n  enabled: true\n  host: localhost\n  database: bazarr\n')
    rendered = _with_environment(_rendered_config(custom, postgres_service=True),
                                 POSTGRES_ENABLED='true',
                                 POSTGRES_URL='postgresql://db/other?dbname=bazarr')

    output, backup, docker_log = run(rendered)

    assert 'DB_ENGINE=postgres' in output
    [dumped] = [line for line in docker_log.splitlines() if line.startswith('EXEC ')]
    assert dumped.startswith('EXEC exec -T db ') and dumped.endswith(' bazarr')


@pytest.mark.parametrize('url', [
    'postgresql://db/bazarr?dbname=',
    'postgresql://db/bazarr?host=',
    'postgresql://external.example/bazarr?host=&host=db&dbname=bazarr',
])
def test_a_query_pair_set_to_nothing_is_discarded_like_the_application_discards_it(
        install, tmp_path, url):
    """The application's URL parser drops a query pair whose value is empty, the way
    parse_qsl does, so the authority or the path decides. The installer read a blank
    value as present: ?dbname= blanked the path's database, ?host= blanked the
    authority's host, and ?host=&host=db counted as an ambiguous failover list, and
    all three left the stack's own database without a dump."""
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, DEFAULT_POSTGRES_YAML)
    rendered = _with_environment(_rendered_config(custom, postgres_service=True), POSTGRES_ENABLED='true',
                                 POSTGRES_URL=url)

    output, backup, docker_log = run(rendered)

    assert 'DB_ENGINE=postgres' in output
    assert (backup / 'bazarr_postgres.dump').read_text(encoding='utf-8') == 'PGDMP'
    [dumped] = [line for line in docker_log.splitlines() if line.startswith('EXEC ')]
    assert dumped.startswith('EXEC exec -T db ') and dumped.endswith(' bazarr')


@pytest.mark.parametrize('query', [
    'host=external&host=db',
    'hostaddr=external&hostaddr=db',
    'host=db&hostaddr=10.0.0.5',
])
def test_a_query_naming_a_host_more_than_once_is_an_outside_database(install, tmp_path, query):
    """The application's own engine reads a host named twice as an ordered failover
    list it may connect to either member of, and hostaddr as the address it connects
    through when a query names both, so no single service can be named to dump. The
    classification takes such a URL for an outside database rather than dumping a
    database the application may never connect to, which would upgrade the stack
    with no backup of the database it uses."""
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'postgresql:\n  enabled: true\n  host: localhost\n  database: bazarr\n')
    rendered = _with_environment(_rendered_config(custom, postgres_service=True),
                                 POSTGRES_ENABLED='true',
                                 POSTGRES_URL=f'postgresql://outside.example/bazarr?{query}&dbname=bazarr')

    output, backups, docker_log = run(rendered, succeed=False, stubs='confirm() { exit 3; }\n')

    assert 'WARN Bazarr+ uses a PostgreSQL database outside this compose stack' in output
    assert backups == []
    assert 'EXEC' not in docker_log


@pytest.mark.parametrize('encoded, decoded', [
    ('bazarr%20blue', 'bazarr blue'),
    ('bazarr+blue', 'bazarr blue'),
    ('bazarr%2Fblue', 'bazarr/blue'),
    ('bazarr%zzblue', 'bazarr%zzblue'),
])
def test_a_percent_encoded_query_value_is_decoded_like_the_application_decodes_it(
        install, tmp_path, encoded, decoded):
    """The application's own URL parser decodes a query value's percent escapes and its
    plus signs before the name reaches libpq, so the decoded name is what the installer
    dumps, not the encoding libpq would refuse. A percent that does not introduce two
    hexadecimal digits stays as written, the way the parser leaves it."""
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'postgresql:\n  enabled: true\n  host: localhost\n  database: bazarr\n')
    rendered = _with_environment(_rendered_config(custom, postgres_service=True),
                                 POSTGRES_ENABLED='true',
                                 POSTGRES_URL=f'postgresql:///?host=db&dbname={encoded}')

    output, backup, docker_log = run(rendered)

    assert 'DB_ENGINE=postgres' in output
    [dumped] = [line for line in docker_log.splitlines() if line.startswith('EXEC ')]
    assert dumped.startswith('EXEC exec -T db ') and dumped.endswith(f' {decoded}')


def test_a_url_naming_a_service_is_an_outside_database_even_when_config_yaml_names_one(install, tmp_path):
    """The service file the query names lives inside the container, where this installer
    cannot read it, so nothing says the database is the compose service db that config.yaml
    happens to name. It used to be dumped anyway."""
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'postgresql:\n  enabled: true\n  host: db\n  database: bazarr\n')
    rendered = _with_environment(_rendered_config(custom, postgres_service=True),
                                 POSTGRES_ENABLED='true',
                                 POSTGRES_URL='postgresql:///?service=bazarr')

    output, backups, docker_log = run(rendered, succeed=False, stubs='confirm() { exit 3; }\n')

    assert 'WARN Bazarr+ uses a PostgreSQL database outside this compose stack' in output
    assert backups == []
    assert 'EXEC' not in docker_log


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


@pytest.mark.parametrize('leftover_config_dir', [False, True])
def test_a_named_config_volume_stops_before_anything_is_stopped_or_copied(install, leftover_config_dir):
    # With /config in a named volume there is no host directory to copy. The backup used to
    # fall back to ./config: it warned and went on when that was missing, and copied a stale
    # ./config left next to the compose file when it was not, either way reporting success.
    install_dir, run = install
    if leftover_config_dir:
        _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    rendered = _rendered_config('/unused').replace(
        '      - type: bind\n        source: /unused\n        target: /config\n        bind: {}\n',
        '      - type: volume\n        source: bazarr-config\n        target: /config\n        volume: {}\n')
    assert 'type: volume\n        source: bazarr-config\n        target: /config' in rendered

    output, backups, docker_log = run(rendered, succeed=False)

    assert 'FATAL' in output and 'bazarr-config' in output
    assert 'SUCCESS' not in output
    assert backups == []
    assert docker_log == ''


def _backup_names(backups):
    return sorted(path.name for path in backups)


def test_a_finished_backup_carries_its_final_name_and_stays_private(install):
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')

    output, backup, _ = run(_rendered_config(install_dir / 'config'))

    assert re.fullmatch(r'backup_\d{8}_\d{6}', backup.name)
    assert f'to {backup}\n' in output
    # 0700: it holds .env and config.yaml, and a database dump when there is one.
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700


@pytest.mark.parametrize('failing', ['docker-compose.yml', '.env'])
def test_a_failed_copy_before_anything_is_stopped_leaves_no_backup(install, failing):
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    stubs = ('cp() { [[ "$2" == */%s ]] && return 1; command cp "$@"; }\n' % failing)

    output, backups, docker_log = run(_rendered_config(install_dir / 'config'), succeed=False,
                                      stubs=stubs)

    assert f'FATAL Could not back up {failing}. Nothing was changed.' in output
    assert 'INFO Removed the incomplete backup' in output
    assert backups == []
    assert docker_log == ''


def test_a_failed_config_copy_leaves_no_backup_and_starts_the_old_services(install):
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')

    output, backups, docker_log = run(_rendered_config(install_dir / 'config'), succeed=False,
                                      stubs=FAILING_CONFIG_COPY)

    assert 'FATAL Backing up ./config failed.' in output
    assert 'INFO Removed the incomplete backup' in output
    assert 'SUCCESS' not in output
    assert backups == []
    # Removed before the old services start, so a disk the copy filled is free again first.
    assert docker_log.splitlines() == ['stop', 'start (backup folders: none)']
    assert (install_dir / 'config' / 'db' / 'bazarr.db').read_text(encoding='utf-8') == 'sqlite-bytes'


def test_a_failed_database_dump_leaves_no_backup_and_starts_the_old_services(install, tmp_path):
    install_dir, run = install
    custom = tmp_path / 'srv' / 'bazarr'
    _config_tree(custom, 'postgresql:\n  enabled: true\n  host: db\n  database: bazarr\n')

    output, backups, docker_log = run(_rendered_config(custom, postgres_service=True), succeed=False,
                                      dump_fails='1')

    assert 'FATAL Dumping the PostgreSQL database failed' in output
    assert 'INFO Removed the incomplete backup' in output
    assert backups == []
    stopped, dumped, started = docker_log.splitlines()
    assert stopped == 'stop bazarr' and dumped.startswith('EXEC exec -T db ')
    assert started == 'start (backup folders: none)'


def test_services_that_do_not_stop_leave_no_backup(install):
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')

    output, backups, docker_log = run(_rendered_config(install_dir / 'config'), succeed=False,
                                      stop_fails='1')

    assert 'FATAL Could not stop the services, so ./config was not backed up.' in output
    assert backups == []
    assert docker_log.splitlines() == ['stop', 'start (backup folders: none)']


def test_a_backup_that_cannot_take_its_final_name_is_removed(install):
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')

    output, backups, docker_log = run(_rendered_config(install_dir / 'config'), succeed=False,
                                      stubs='mv() { return 1; }\n')

    assert 'FATAL Could not rename' in output and 'SUCCESS' not in output
    assert backups == []
    assert docker_log.splitlines() == ['stop', 'start (backup folders: none)']


def test_a_backup_folder_that_appears_just_before_the_rename_is_not_moved_into(install):
    # The final name is checked right before the rename, and can still appear in between. A
    # plain `mv` then puts this backup inside that folder and reports success.
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    stubs = ('mv() { local target="${!#}"; mkdir -p "$target"; printf keep > "$target/sentinel"; '
             'command mv "$@"; }\n')

    output, backups, docker_log = run(_rendered_config(install_dir / 'config'), succeed=False,
                                      stubs=stubs)

    assert 'FATAL Could not rename' in output and 'SUCCESS' not in output
    [other] = backups
    assert re.fullmatch(r'backup_\d{8}_\d{6}', other.name)
    assert _tree(other) == {'sentinel': b'keep'}
    assert docker_log.splitlines() == ['stop', f'start (backup folders: {other.name} )']


def test_an_incomplete_backup_that_cannot_be_removed_is_named_in_a_warning(install):
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    stubs = FAILING_CONFIG_COPY + 'rm() { [[ "$1" == -rf ]] && return 1; command rm "$@"; }\n'

    output, backups, docker_log = run(_rendered_config(install_dir / 'config'), succeed=False,
                                      stubs=stubs)

    [left] = backups
    assert left.name.endswith('.partial')
    assert f'WARN Could not remove the incomplete backup {left}' in output
    assert 'FATAL Backing up ./config failed.' in output
    assert 'start (backup folders: ' in docker_log


def test_a_run_killed_mid_copy_leaves_only_a_folder_marked_incomplete(install):
    # Nothing in the script runs after a kill, so what is left has to say so by its name.
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')

    _, backups, _ = run(_rendered_config(install_dir / 'config'), succeed=False,
                        stubs=KILLED_CONFIG_COPY)

    [left] = backups
    assert re.fullmatch(r'backup_\d{8}_\d{6}\.\d+\.partial', left.name)


@pytest.mark.parametrize('signal', ['INT', 'TERM', 'HUP'])
def test_a_backup_interrupted_mid_copy_discards_the_partial_and_starts_the_old_services(
        install, signal):
    # A catchable signal during the backup is handled like a failed step: the .partial
    # folder goes before the old services start again, and the run ends with the
    # script's interrupted status, not with a half-made backup left under its name.
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')

    output, backups, docker_log = run(_rendered_config(install_dir / 'config'), succeed=False,
                                      stubs=INTERRUPTED_CONFIG_COPY.replace('SIGNAL', signal),
                                      status=130)

    assert 'INFO Removed the incomplete backup' in output
    assert backups == []
    assert docker_log.splitlines() == ['stop', 'start (backup folders: none)']
    assert (install_dir / 'config' / 'db' / 'bazarr.db').read_text(encoding='utf-8') == 'sqlite-bytes'


def test_the_signal_trap_ends_with_do_backup(install):
    # The trap is the backup's own: once do_backup returns, the script handles signals
    # the way it did before, so the steps after the backup keep their own handling.
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')

    output, backup, _ = run(_rendered_config(install_dir / 'config'),
                            after='trap -p INT; trap -p TERM; trap -p HUP\n')

    assert 'trap --' not in output


def test_a_backup_folder_with_the_final_name_that_already_exists_is_left_alone(install):
    # Two runs in the same second still share the final name, and the folder that is
    # there is not this run's, so it is neither written into nor removed, and nothing
    # is stopped. A leftover partial no longer blocks a run: the new tests below pin
    # that, because the partial name is the run's own now.
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    existing = install_dir / 'backup_20260929_120000'
    existing.mkdir()
    (existing / 'sentinel').write_text('keep me', encoding='utf-8')

    output, backups, docker_log = run(_rendered_config(install_dir / 'config'), succeed=False,
                                      stubs="date() { printf '20260929_120000\\n'; }\n")

    assert 'FATAL' in output and 'SUCCESS' not in output
    assert 'Removed' not in output
    assert _backup_names(backups) == [existing.name]
    assert [entry.name for entry in existing.iterdir()] == ['sentinel']
    assert (existing / 'sentinel').read_text(encoding='utf-8') == 'keep me'
    assert docker_log == ''


# --- Each run owns its partial folder and its spinner ---

# The same second two runs share, so with the clock standing still both compute the
# same backup_<ts> names.
SAME_SECOND = r'''
date() { printf '20260929_120000\n'; }
'''

# A copy that stops mid-step, holding its partial while a second run starts: the
# first run blocks in its docker-compose.yml copy, its partial already created.
BLOCKED_FIRST_COPY = r'''
cp() {
  if [[ "$2" == */docker-compose.yml ]]; then
    printf 'building' > "$FAKE_FIRST_RUN_BLOCKED"
    sleep 30
  fi
  command cp "$@"
}
'''


def test_two_runs_in_the_same_second_each_own_their_partial(install, tmp_path):
    # Two runs in the same second named the same partial folder, so the second run's
    # mkdir refused it and the second run backed nothing up beside the first, even
    # though the first run's folder was never the second run's to be blocked by. The
    # name carries the run's pid now: both build beside each other, the final name
    # goes to whoever finishes first, and neither run's cleanup meets the other's
    # folder.
    install_dir, _ = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    rendered = tmp_path / 'rendered.yml'
    rendered.write_text(_rendered_config(install_dir / 'config'), encoding='utf-8')
    log = tmp_path / 'docker.log'
    blocked = tmp_path / 'first-run-blocked'
    env = dict(os.environ, FAKE_COMPOSE_CONFIG=str(rendered), FAKE_LOG=str(log),
               FAKE_INSTALL=str(install_dir), FAKE_FIRST_RUN_BLOCKED=str(blocked))
    program = _functions() + STUBS + SAME_SECOND + BLOCKED_FIRST_COPY + 'do_backup "$1"\n'
    first = subprocess.Popen(['bash', '-c', program, 'bash', str(install_dir)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             text=True, env=env)
    try:
        for _ in range(400):
            if blocked.exists():
                break
            time.sleep(0.05)
        else:
            pytest.fail('the first run never reached its docker-compose.yml copy')
        assert list(install_dir.glob('backup_*.partial')), \
            'the first run had not created its partial folder yet'
        second = subprocess.run(['bash', '-c', _functions() + STUBS + SAME_SECOND + 'do_backup "$1"\n',
                                 'bash', str(install_dir)], capture_output=True, text=True,
                                timeout=60, env=env)
        assert second.returncode == 0, (
            "the second run was refused beside the first run's partial:\n"
            f'{second.stdout}\n{second.stderr}')
        assert 'Backed up docker-compose.yml, .env, ./config and the SQLite database inside it' \
            in second.stdout
        # The first run's partial is still there, still mid-build and still the first
        # run's: the second run neither removed it nor moved anything into it.
        [first_partial] = list(install_dir.glob('backup_*.partial'))
        assert re.fullmatch(r'backup_20260929_120000\.\d+\.partial', first_partial.name)
        assert (install_dir / 'backup_20260929_120000' / 'config' / 'db' / 'bazarr.db').is_file()
        # The second run stopped the services for its own copy and took the final name;
        # the first run's partial was never in its way.
        assert log.read_text(encoding='utf-8') == 'stop\n'
    finally:
        first.kill()
        first.wait()
    # A run killed outright leaves only its own partial, named for what it is.
    assert first_partial.exists() and not any(first_partial.iterdir())


# The first run blocks in its docker-compose.yml copy until the test releases it, so
# a second run can finish and take the final name while the first is mid-backup.
BARRIER_FIRST_COPY = r'''
cp() {
  if [[ "$2" == */docker-compose.yml ]]; then
    printf blocked > "$FAKE_FIRST_RUN_BLOCKED"
    while [[ ! -e "$FAKE_RELEASE" ]]; do sleep 0.2; done
  fi
  command cp "$@"
}
'''


def test_a_run_that_resumes_after_another_took_its_final_name_is_refused(install, tmp_path):
    # The final-name refusal was only ever proven for a folder that was there before
    # the run started. A run that resumes after a concurrent run has taken the final
    # name in the meantime has to be refused too, its own partial is the only thing
    # that goes, and the winner is left exactly as the other run wrote it.
    install_dir, _ = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    rendered = tmp_path / 'rendered.yml'
    rendered.write_text(_rendered_config(install_dir / 'config'), encoding='utf-8')
    log = tmp_path / 'docker.log'
    blocked = tmp_path / 'first-run-blocked'
    release = tmp_path / 'release'
    env = dict(os.environ, FAKE_COMPOSE_CONFIG=str(rendered), FAKE_LOG=str(log),
               FAKE_INSTALL=str(install_dir), FAKE_FIRST_RUN_BLOCKED=str(blocked),
               FAKE_RELEASE=str(release))
    program = _functions() + STUBS + SAME_SECOND + BARRIER_FIRST_COPY + 'do_backup "$1"\n'
    first = subprocess.Popen(['bash', '-c', program, 'bash', str(install_dir)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             env=env)
    try:
        for _ in range(400):
            if blocked.exists():
                break
            time.sleep(0.05)
        else:
            pytest.fail('the first run never reached its docker-compose.yml copy')
        [first_partial] = list(install_dir.glob('backup_*.partial'))
        assert first_partial.name == f'backup_20260929_120000.{first.pid}.partial'
        # The second run finishes beside the blocked one and takes the final name.
        second = subprocess.run(['bash', '-c', _functions() + STUBS + SAME_SECOND + 'do_backup "$1"\n',
                                 'bash', str(install_dir)], capture_output=True, text=True,
                                timeout=60, env=env)
        assert second.returncode == 0, (
            "the second run was refused beside the first run's partial:\n"
            f'{second.stdout}\n{second.stderr}')
        winning = install_dir / 'backup_20260929_120000'
        won = _tree(winning)
        assert set(won) == {'.env', 'docker-compose.yml', 'config', 'config/config',
                            'config/config/config.yaml', 'config/db',
                            'config/db/bazarr.db'}
        # The first run resumes into a world where its final name is taken.
        release.write_text('')
        first_out, _ = first.communicate(timeout=60)
        assert first.returncode != 0, first_out
        assert 'FATAL Could not rename' in first_out and 'SUCCESS' not in first_out
        assert 'INFO Removed the incomplete backup' in first_out
        assert _tree(winning) == won, 'the refused run changed the winning backup'
        assert not first_partial.exists(), "the refused run's own partial was left behind"
        backups = [path.name for path in install_dir.iterdir() if path.name.startswith('backup_')]
        assert backups == [winning.name]
        assert log.read_text(encoding='utf-8').splitlines() == \
            ['stop', 'stop', 'start (backup folders: backup_20260929_120000 )']
    finally:
        release.write_text('')
        if first.poll() is None:
            first.kill()
            first.wait()


# The compose child of each run, blocked on a barrier the test releases: the child
# records the pid of the run that is waiting on it. The real run_with_spinner runs
# here, not the stub, so the pid it holds is the run's own child to kill.
BLOCKED_STOP = r'''
docker() {
  shift 3
  case "$1" in
    config) cat "$FAKE_COMPOSE_CONFIG" ;;
    stop) printf '%s\n' "$BASHPID" > "$FAKE_CHILD_PID"
          while [[ ! -e "$FAKE_RELEASE" ]]; do sleep 0.2; done
          printf '%s\n' "$*" >> "$FAKE_LOG" ;;
    start) local found; found=$(cd "$FAKE_INSTALL" && ls -d backup_* 2>/dev/null | tr '\n' ' ')
           printf 'start (backup folders: %s)\n' "${found:-none}" >> "$FAKE_LOG" ;;
    *) printf '%s\n' "$*" >> "$FAKE_LOG" ;;
  esac
}
'''


def test_an_interrupted_run_kills_only_its_own_compose_child(install, tmp_path):
    # Two runs share the launching shell's process group, and the compose commands
    # run_with_spinner runs sat in it with no way to be told apart: the interruption
    # handling could not aim at the run's own child, and the command substitution it
    # sat in held a trapped signal back until the command finished. The command is
    # this run's own child, held by pid, and the wait it sits in lets the trap
    # through at once, so the interrupted run ends promptly, its own child goes with
    # it, and the other run's child is still going.
    install_dir, _ = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    rendered = tmp_path / 'rendered.yml'
    rendered.write_text(_rendered_config(install_dir / 'config'), encoding='utf-8')
    log = tmp_path / 'docker.log'
    release = tmp_path / 'release'
    program = (_functions() + STUBS + 'error() { printf "ERROR %s\\n" "$*"; }\n' +
               _functions('spinner_start', 'spinner_stop', 'run_with_spinner') +
               BLOCKED_STOP + SAME_SECOND + 'do_backup "$1"\n')
    runs, children, outputs = {}, {}, {}
    try:
        for name in ('first', 'second'):
            child_file = tmp_path / f'{name}.child'
            out_file = tmp_path / f'{name}.out'
            with open(out_file, 'w') as handle:
                runs[name] = subprocess.Popen(
                    ['bash', '-c', program, 'bash', str(install_dir)], stdout=handle,
                    stderr=subprocess.STDOUT, text=True,
                    env=dict(os.environ, FAKE_COMPOSE_CONFIG=str(rendered), FAKE_LOG=str(log),
                             FAKE_INSTALL=str(install_dir), FAKE_RELEASE=str(release),
                             FAKE_CHILD_PID=str(child_file)))
            for _ in range(400):
                if child_file.exists():
                    break
                assert runs[name].poll() is None, (
                    f'the {name} run ended before its compose child:\n'
                    f'{out_file.read_text(encoding="utf-8")}')
                time.sleep(0.05)
            else:
                pytest.fail(f'the {name} run never reached its compose child')
            children[name] = int(child_file.read_text(encoding='utf-8').strip())
            outputs[name] = out_file
        # Each run's compose child is its own.
        assert children['first'] != children['second']
        for pid in children.values():
            os.kill(pid, 0)
        # Interrupting the first run ends it with the interrupted status while its
        # step is still going: its own child goes with it, the other run's does not.
        runs['first'].send_signal(signal.SIGINT)
        assert runs['first'].wait(timeout=10) == 130, (
            'the interrupted run did not end promptly:\n'
            f'{outputs["first"].read_text(encoding="utf-8")}')
        for _ in range(400):
            try:
                os.kill(children['first'], 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail('the interrupted run left its own compose child running')
        os.kill(children['second'], 0)
        first_out = outputs['first'].read_text(encoding='utf-8')
        assert ('ERROR Interrupted. Nothing was upgraded or reinstalled, '
                'and the old services were started again.') in first_out
        [second_partial] = list(install_dir.glob('backup_*.partial'))
        assert second_partial.name == f'backup_20260929_120000.{runs["second"].pid}.partial'
        assert log.read_text(encoding='utf-8').splitlines() == \
            [f'start (backup folders: {second_partial.name} )']
        # The second run finishes once its stop is released, beside the first run's
        # interruption, and its backup is complete.
        release.write_text('')
        assert runs['second'].wait(timeout=60) == 0, outputs['second'].read_text(encoding='utf-8')
        second_out = outputs['second'].read_text(encoding='utf-8')
        assert 'Backed up docker-compose.yml, .env, ./config and the SQLite database inside it' \
            in second_out
        assert log.read_text(encoding='utf-8').splitlines() == \
            [f'start (backup folders: {second_partial.name} )', 'stop']
        final = install_dir / 'backup_20260929_120000'
        assert set(_tree(final)) == {'.env', 'docker-compose.yml', 'config', 'config/config',
                                     'config/config/config.yaml', 'config/db',
                                     'config/db/bazarr.db'}
        assert not list(install_dir.glob('backup_*.partial'))
    finally:
        release.write_text('')
        for process in runs.values():
            if process.poll() is None:
                process.kill()
                process.wait()


# The compose child of an interrupted run with a TERM handler that takes its time, the
# shape a command that keeps stopping things for a while after its TERM has: the child
# records its pid, and the start command checks that pid, so the log says whether the
# child was still alive when the old services came back.
SLOW_TERM_STOP = r'''
docker() {
  shift 3
  case "$1" in
    config) cat "$FAKE_COMPOSE_CONFIG" ;;
    stop) printf '%s\n' "$BASHPID" > "$FAKE_SLOW_CHILD"
          trap 'sleep 0.5; exit 0' TERM
          while [[ ! -e "$FAKE_RELEASE" ]]; do sleep 0.2; done
          printf '%s\n' "$*" >> "$FAKE_LOG" ;;
    start) if kill -0 "$(cat "$FAKE_SLOW_CHILD")" 2>/dev/null; then
             printf 'start (slow child: alive)\n' >> "$FAKE_LOG"
           else
             printf 'start (slow child: gone)\n' >> "$FAKE_LOG"
           fi ;;
    *) printf '%s\n' "$*" >> "$FAKE_LOG" ;;
  esac
}
'''


def test_an_interrupted_run_reaps_the_compose_child_before_the_services_restart(
        install, tmp_path):
    # A command that handles TERM slowly can still be stopping things while the old
    # services start again, and a stop still pending in it can undo that restart: the
    # recovery starts only once the child is gone, which the start command observes,
    # and the run does not outlive its own child.
    install_dir, _ = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    rendered = tmp_path / 'rendered.yml'
    rendered.write_text(_rendered_config(install_dir / 'config'), encoding='utf-8')
    log = tmp_path / 'docker.log'
    release = tmp_path / 'release'
    slow_child = tmp_path / 'slow-child'
    out_file = tmp_path / 'run.out'
    program = (_functions() + STUBS + 'error() { printf "ERROR %s\\n" "$*"; }\n' +
               _functions('spinner_start', 'spinner_stop', 'run_with_spinner') +
               SLOW_TERM_STOP + 'do_backup "$1"\n')
    with open(out_file, 'w') as handle:
        run = subprocess.Popen(
            ['bash', '-c', program, 'bash', str(install_dir)],
            stdout=handle, stderr=subprocess.STDOUT, text=True,
            env=dict(os.environ, FAKE_COMPOSE_CONFIG=str(rendered), FAKE_LOG=str(log),
                     FAKE_INSTALL=str(install_dir), FAKE_RELEASE=str(release),
                     FAKE_SLOW_CHILD=str(slow_child)))
    try:
        for _ in range(400):
            if slow_child.exists():
                break
            assert run.poll() is None, out_file.read_text(encoding='utf-8')
            time.sleep(0.05)
        else:
            pytest.fail('the run never reached its compose child')
        child = int(slow_child.read_text(encoding='utf-8').strip())
        os.kill(child, 0)
        run.send_signal(signal.SIGINT)
        assert run.wait(timeout=10) == 130, out_file.read_text(encoding='utf-8')
        # The start command ran only after the child was gone, and the child's death
        # is observed from here as well.
        assert log.read_text(encoding='utf-8').splitlines() == ['start (slow child: gone)']
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        first_out = out_file.read_text(encoding='utf-8')
        assert ('ERROR Interrupted. Nothing was upgraded or reinstalled, '
                'and the old services were started again.') in first_out
    finally:
        release.write_text('')
        if run.poll() is None:
            run.kill()
            run.wait()


# The step an interrupted run is sitting in when the signal lands, still before its
# own partial exists: the compose read at the top of do_backup.
INTERRUPTED_COMPOSE_READ = r'''
docker() {
  case "$4" in
    config) ( sleep 1 ) >/dev/null 2>&1 & local waiter=$!
             ( sleep 0.3; kill -INT $$ ) >/dev/null 2>&1 &
             wait "$waiter" ;;
    start) local found; found=$(cd "$FAKE_INSTALL" && ls -d backup_* 2>/dev/null | tr '\n' ' ')
           printf 'start (backup folders: %s)\n' "${found:-none}" >> "$FAKE_LOG" ;;
    *) printf '%s\n' "$*" >> "$FAKE_LOG" ;;
  esac
}
'''


def test_an_interrupted_run_discards_only_its_own_partial(install):
    # A run interrupted before its mkdir still names a partial, and with two runs in
    # one second that name used to be the other run's live folder: the interrupted
    # run's cleanup removed it and then started the services the other run had
    # stopped for its own backup. The name is the run's own now, so the other run's
    # folder is left exactly as it was, while the interrupted run still starts the
    # old services again and ends with the interrupted status.
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    others = install_dir / 'backup_20260929_120000.partial'
    (others / 'config').mkdir(parents=True)
    (others / 'config' / 'half').write_text('half a copy', encoding='utf-8')

    output, backups, docker_log = run(_rendered_config(install_dir / 'config'), succeed=False,
                                      stubs=SAME_SECOND + INTERRUPTED_COMPOSE_READ +
                                            'error() { printf "ERROR %s\\n" "$*"; }\n',
                                      status=130)

    assert 'Removed the incomplete backup' not in output
    assert 'ERROR Interrupted. Nothing was upgraded or reinstalled' in output
    assert 'FATAL' not in output and 'SUCCESS' not in output
    assert _tree(others) == {'config': None, 'config/half': b'half a copy'}
    assert [path.name for path in backups] == ['backup_20260929_120000.partial']
    assert docker_log == 'start (backup folders: backup_20260929_120000.partial )\n'


def test_discard_backup_leaves_a_partial_that_is_not_this_runs_alone(install):
    # discard_backup's guard used to be shape-only: any backup_*.partial folder it
    # was handed went, whichever run had built it. The pid in the name is the owner
    # now, so a folder another run named is warned about and left for that run's own
    # cleanup, and only this run's own partial is ever removed.
    install_dir, _ = install
    others = install_dir / 'backup_20260929_120000.999999.partial'
    others.mkdir()
    (others / 'sentinel').write_text('keep me', encoding='utf-8')

    program = _functions() + STUBS + 'discard_backup "$1"\n'
    result = subprocess.run(['bash', '-c', program, 'bash', str(others)], capture_output=True,
                            text=True, timeout=60, env=dict(os.environ))

    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'
    assert 'WARN The incomplete backup' in result.stdout
    assert 'INFO Removed' not in result.stdout
    assert _tree(others) == {'sentinel': b'keep me'}


# The stubs the spinner functions need: the colors are main's, and tput has nothing
# to hide here.
SPINNER_STUBS = r'''
tput() { :; }
info() { :; }; success() { :; }; warn() { :; }; error() { :; }
RST=''; BLD=''; RED=''; GRN=''; YLW=''; BLU=''; CYN=''; DIM=''
'''


def test_the_spinner_runs_in_a_process_group_of_its_own(tmp_path):
    # The spinner is one run's own job, but it used to share the run's process group,
    # which two runs can also share with each other: one shell that started them both,
    # with no job control, puts them all in one group, and a signal for that group
    # then landed on both runs' spinners at once. The spinner runs in a group of its
    # own now, and the run's own spinner_stop, which knows its pid, is still what
    # stops it.
    program = _functions('spinner_start', 'spinner_stop') + SPINNER_STUBS + r'''
SPINNER_PID=""
spinner_start "step"
spinner=$SPINNER_PID
kill -0 "$spinner" && echo SPINNER_ALIVE || echo SPINNER_DEAD
echo "SPINPGID $(ps -o pgid= -p "$spinner" | tr -d ' ')"
echo "SHELLPGID $(ps -o pgid= -p $$ | tr -d ' ')"
spinner_stop ok "step"
sleep 0.3
kill -0 "$spinner" 2>/dev/null && echo AFTER_ALIVE || echo AFTER_DEAD
'''
    result = subprocess.run(['bash', '-c', program], capture_output=True, text=True,
                            timeout=60, cwd=tmp_path)

    assert 'SPINNER_ALIVE' in result.stdout, result.stdout + result.stderr
    spin_pgid = re.search(r'SPINPGID (\d+)', result.stdout).group(1)
    shell_pgid = re.search(r'SHELLPGID (\d+)', result.stdout).group(1)
    assert spin_pgid != shell_pgid, "the spinner shares the run's process group"
    assert 'AFTER_DEAD' in result.stdout, 'spinner_stop no longer stops the spinner'


# --- The migration guide's backup and restore commands ---

GUIDE_STUBS = r'''
docker() { printf '%s\n' "$*" >> "$FAKE_LOG"; [ -z "${FAKE_DOWN_FAILS:-}" ]; }
# The real date, at a fixed moment, so the guide's own format decides the folder name.
date() { command date -d "$FAKE_NOW" "$@"; }
'''


def _guide_snippet(block_id):
    with open(GUIDE, encoding='utf-8') as handle:
        page = handle.read()
    found = re.search(rf'<pre id="{block_id}"><code>(.*?)</code></pre>', page, re.S)
    assert found, f'<pre id="{block_id}"><code> is gone from the migration guide'
    assert '<' not in found.group(1), 'markup inside the commands would not reach the reader'
    return html.unescape(found.group(1))


def _tree(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in sorted(root.rglob('*'))}


@pytest.fixture
def stack(tmp_path):
    """A compose directory next to its ./config, and a runner for one guide snippet."""
    stack_dir = tmp_path / 'stack'
    stack_dir.mkdir()
    _config_tree(stack_dir / 'config', 'general:\n  port: 6767\n')
    log = tmp_path / 'docker.log'

    def run(snippet, stubs='', now='2026-09-24 14:30:15', **fake):
        if log.exists():
            log.unlink()
        env = dict(os.environ, FAKE_LOG=str(log), FAKE_NOW=now)
        env.update({f'FAKE_{name.upper()}': value for name, value in fake.items()})
        result = subprocess.run(['bash', '-c', GUIDE_STUBS + stubs + snippet], cwd=stack_dir,
                                capture_output=True, text=True, timeout=60, env=env)
        docker_log = log.read_text(encoding='utf-8') if log.exists() else ''
        return result.stdout + result.stderr, docker_log

    return stack_dir, run


def _restore_snippet(backup_name):
    snippet, count = re.subn(r'^BACKUP=\S+', f'BACKUP=./{backup_name}', _guide_snippet('migration-restore-code'),
                             count=1, flags=re.M)
    assert count == 1, 'the restore commands no longer start by setting BACKUP'
    return snippet


def test_the_guide_backup_run_twice_on_one_day_makes_two_separate_backups(stack):
    stack_dir, run = stack
    config = _tree(stack_dir / 'config')
    snippet = _guide_snippet('migration-backup-code')

    first, _ = run(snippet, now='2026-09-24 14:30:15')
    second, docker_log = run(snippet, now='2026-09-24 18:02:09')

    backups = sorted(path.name for path in stack_dir.iterdir() if path.name.startswith('config-backup-'))
    assert backups == ['config-backup-20260924-143015', 'config-backup-20260924-180209']
    for name in backups:
        assert _tree(stack_dir / name) == config, f'{name} is not a copy of ./config'
    assert 'Backup: ./config-backup-20260924-143015' in first
    assert 'Backup: ./config-backup-20260924-180209' in second
    assert docker_log == 'compose down\n'


@pytest.mark.parametrize('suffix', ['', '.partial'])
def test_the_guide_backup_refuses_a_folder_that_is_already_there(stack, suffix):
    stack_dir, run = stack
    existing = stack_dir / f'config-backup-20260924-143015{suffix}'
    existing.mkdir()
    (existing / 'sentinel').write_text('keep me', encoding='utf-8')

    output, docker_log = run(_guide_snippet('migration-backup-code'))

    assert docker_log == '', 'Bazarr was stopped for a backup that could not be taken'
    assert _tree(existing) == {'sentinel': b'keep me'}
    assert sorted(path.name for path in stack_dir.iterdir()) == sorted(['config', existing.name])
    assert 'already exists' in output and f'./{existing.name}' in output


def test_the_guide_backup_copies_nothing_when_bazarr_does_not_stop(stack):
    stack_dir, run = stack

    output, docker_log = run(_guide_snippet('migration-backup-code'), down_fails='1')

    assert docker_log == 'compose down\n'
    assert sorted(path.name for path in stack_dir.iterdir()) == ['config']
    assert 'Backup:' not in output


def test_a_guide_backup_copy_that_fails_leaves_only_a_folder_marked_incomplete(stack):
    stack_dir, run = stack
    stubs = 'cp() { mkdir -p "$3/db"; return 1; }\n'

    output, _ = run(_guide_snippet('migration-backup-code'), stubs=stubs)

    assert sorted(path.name for path in stack_dir.iterdir()) == \
        ['config', 'config-backup-20260924-143015.partial']
    assert 'Backup:' not in output


@pytest.mark.parametrize('backup_name', ['config-backup-20260924-143015',
                                         # The name the guide gave before it added the time.
                                         'config-backup-20260924'])
def test_the_guide_restore_swaps_the_backup_in(stack, backup_name):
    stack_dir, run = stack
    _config_tree(stack_dir / backup_name, 'general:\n  port: 6768\n')
    before = _tree(stack_dir / 'config')
    backup = _tree(stack_dir / backup_name)
    moved = 'config-bazarr-plus-20260924-143015'

    output, docker_log = run(_restore_snippet(backup_name))

    assert _tree(stack_dir / 'config') == backup
    assert _tree(stack_dir / moved) == before
    assert _tree(stack_dir / backup_name) == backup
    assert not (stack_dir / 'config-restore').exists()
    assert f'the config you had is at ./{moved}' in output


def test_the_guide_restore_refuses_a_leftover_config_restore(stack):
    # A leftover ./config-restore is what an interrupted restore leaves. `cp -a` would
    # copy the new backup inside it, so the commands refuse, delete nothing, and tell
    # the reader to move it away.
    stack_dir, run = stack
    backup_name = 'config-backup-20260924-143015'
    _config_tree(stack_dir / backup_name, 'general:\n  port: 6768\n')
    _config_tree(stack_dir / 'config-restore', 'general:\n  port: 1111\n')
    before = {name: _tree(stack_dir / name) for name in ('config', backup_name, 'config-restore')}

    output, docker_log = run(_restore_snippet(backup_name))

    assert {name: _tree(stack_dir / name) for name in before} == before
    assert sorted(path.name for path in stack_dir.iterdir()) == sorted(before)
    assert docker_log == '', 'Bazarr+ was stopped for a restore that could not run'
    assert './config-restore is already there' in output and 'Move it away' in output


def test_the_guide_restore_moves_aside_without_touching_an_earlier_revert(stack):
    # ./config-bazarr-plus is where an older version of these commands put the config it
    # moved aside. The fresh name never aims at it: it neither blocks the restore nor is
    # written into.
    stack_dir, run = stack
    backup_name = 'config-backup-20260924-143015'
    _config_tree(stack_dir / backup_name, 'general:\n  port: 6768\n')
    _config_tree(stack_dir / 'config-bazarr-plus', 'general:\n  port: 1111\n')
    earlier = _tree(stack_dir / 'config-bazarr-plus')
    before = _tree(stack_dir / 'config')
    moved = 'config-bazarr-plus-20260924-143015'

    output, docker_log = run(_restore_snippet(backup_name))

    assert _tree(stack_dir / 'config-bazarr-plus') == earlier
    assert _tree(stack_dir / moved) == before
    assert _tree(stack_dir / 'config') == _tree(stack_dir / backup_name)
    assert not (stack_dir / 'config-restore').exists()
    assert f'the config you had is at ./{moved}' in output


def test_a_second_revert_never_meets_the_folder_the_first_one_moved(stack):
    # The name of the moved-aside config carries the time of the restore, so running the
    # revert again needs nothing moved out of the way first.
    stack_dir, run = stack
    original = _tree(stack_dir / 'config')
    _config_tree(stack_dir / 'config-backup-20260924-143015', 'general:\n  port: 6768\n')
    _config_tree(stack_dir / 'config-backup-20260925-090102', 'general:\n  port: 6769\n')
    first_backup = _tree(stack_dir / 'config-backup-20260924-143015')

    first, _ = run(_restore_snippet('config-backup-20260924-143015'), now='2026-09-24 14:30:15')
    second, _ = run(_restore_snippet('config-backup-20260925-090102'), now='2026-09-25 09:01:02')

    assert 'the config you had is at ./config-bazarr-plus-20260924-143015' in first
    assert 'the config you had is at ./config-bazarr-plus-20260925-090102' in second
    assert _tree(stack_dir / 'config-bazarr-plus-20260924-143015') == original
    assert _tree(stack_dir / 'config-bazarr-plus-20260925-090102') == first_backup
    assert _tree(stack_dir / 'config') == _tree(stack_dir / 'config-backup-20260925-090102')


@pytest.mark.parametrize('backup_name', ['config-backup-20260924-143015.partial',
                                         # What tab completion types.
                                         'config-backup-20260924-143015.partial/'])
def test_the_guide_restore_refuses_a_copy_that_did_not_finish(stack, backup_name):
    # `ls -d ./config-backup-*` lists the .partial folder a failed backup leaves, next to the
    # real ones.
    stack_dir, run = stack
    unfinished = stack_dir / backup_name.rstrip('/')
    _config_tree(unfinished, 'general:\n  port: 6768\n')
    before = {name: _tree(stack_dir / name) for name in ('config', unfinished.name)}

    output, docker_log = run(_restore_snippet(backup_name))

    assert {name: _tree(stack_dir / name) for name in before} == before
    assert sorted(path.name for path in stack_dir.iterdir()) == sorted(before)
    assert docker_log == '', 'Bazarr+ was stopped for a restore that could not run'
    assert 'did not finish' in output
