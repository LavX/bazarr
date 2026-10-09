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
import stat
import subprocess

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
    assert re.fullmatch(r'backup_\d{8}_\d{6}\.partial', left.name)


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


@pytest.mark.parametrize('suffix', ['', '.partial'])
def test_a_backup_folder_that_already_exists_is_left_alone(install, suffix):
    # Two runs in the same second get the same name. The folder that is there is not this
    # run's, so it is neither written into nor removed, and nothing is stopped.
    install_dir, run = install
    _config_tree(install_dir / 'config', 'general:\n  port: 6767\n')
    existing = install_dir / f'backup_20260929_120000{suffix}'
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
