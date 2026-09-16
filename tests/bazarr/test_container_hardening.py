# coding=utf-8
"""The container must not hand the application tree to the runtime user.

The entrypoint used to ``chown /app/bazarr`` to PUID:PGID before dropping
privileges, so every process running as that user, including each Provider Hub
worker, could rewrite the ``.py`` files the host imports on the next restart.
That is what made the worker subprocess a fault boundary and not a security
boundary. The tree is root-owned now, and the one runtime writer under it, the
binaries downloader, writes under the configuration directory instead.

These assertions are about files nobody imports (the entrypoint, the reference
compose, the install guide), which is exactly why they rot silently: a later
edit that reintroduces the chown, or drops ``read_only`` from the compose users
copy, breaks nothing that any other test runs.
"""
import hashlib
import logging
import os
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _entrypoint():
    with open(os.path.join(ROOT, 'docker', 'entrypoint.sh')) as handle:
        return handle.read()


def _compose():
    with open(os.path.join(ROOT, 'docker-compose.yml')) as handle:
        return yaml.safe_load(handle)


def _readme_compose():
    """The compose block under README's "Docker Compose Setup" heading.

    The single most copied configuration in the repository, and for a while the
    only one still shipping without any of this.
    """
    with open(os.path.join(ROOT, 'README.md')) as handle:
        readme = handle.read()
    block = re.search(r'### Docker Compose Setup.*?```yaml\n(.*?)```', readme, re.S)
    assert block, 'the README no longer has a Docker Compose Setup block to check'
    return yaml.safe_load(block.group(1))


def _installer_compose():
    """Run install.sh's generator and parse what it writes.

    The one-liner installer writes the compose file most new installs actually
    run, so asserting on the reference file alone would leave it unguarded. The
    function is extracted rather than sourced because install.sh ends in
    `main "$@"`, and sourcing it whole would run the installer.
    """
    with open(os.path.join(ROOT, 'site', 'install.sh')) as handle:
        script = handle.read()
    body = re.search(r'^generate_compose\(\) \{.*?^\}', script, re.S | re.M)
    assert body, 'generate_compose() is gone from install.sh, or no longer starts at column 0'

    # bazarr_port, movies, tv, translator, translator_port, flaresolverr
    program = body.group(0) + '\ngenerate_compose 6767 /srv/movies /srv/tv n 8765 y\n'
    result = subprocess.run(['bash', '-c', program], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f'generate_compose failed: {result.stderr}'
    return yaml.safe_load(result.stdout)


def _guide():
    with open(os.path.join(ROOT, 'site', 'guides', 'getting-started.html')) as handle:
        return handle.read()


def _dockerfile():
    with open(os.path.join(ROOT, 'Dockerfile')) as handle:
        return handle.read()


def _assert_hardened(service, where):
    assert service.get('read_only') is True, f'read_only: true is missing from {where}'

    tmpfs = service.get('tmpfs', [])
    mounted = [entry.split(':', 1)[0] for entry in tmpfs]
    assert '/tmp' in mounted, (
        f'{where} has read_only without a tmpfs for /tmp, which breaks subtitle bundle '
        f'downloads and every worker temporary directory: {tmpfs}')
    assert any(entry.startswith('/tmp:') and 'size=' in entry for entry in tmpfs), (
        f'the /tmp tmpfs in {where} has no size= cap, so it may grow to half of host RAM '
        f'and is charged to the host rather than the container: {tmpfs}')

    assert service.get('cap_drop') == ['ALL'], f'cap_drop: [ALL] is missing from {where}'
    assert service.get('cap_add') == ['CHOWN', 'SETUID', 'SETGID'], (
        f'{where} adds back a different capability set than the startup needs: '
        f'{service.get("cap_add")}')
    assert 'no-new-privileges:true' in service.get('security_opt', []), (
        f'no-new-privileges:true is missing from {where}')


def test_the_entrypoint_never_gives_the_application_tree_to_the_runtime_user():
    offenders = [line.strip() for line in _entrypoint().splitlines()
                 if 'chown' in line and '/app' in line]
    assert not offenders, (
        'The entrypoint still chowns the application tree, so anything running as '
        f'PUID can rewrite the code the host imports: {offenders}')


def test_the_entrypoint_drops_privileges_by_number():
    """usermod writes /etc/passwd, which ``read_only: true`` makes read-only.

    Dropping to the numeric uid:gid works either way; relying on usermod having
    succeeded would silently leave the process running as uid 1000 on a hardened
    host.
    """
    assert 'gosu "$PUID:$PGID"' in _entrypoint(), (
        'The entrypoint must hand gosu the numeric uid:gid, not a user name whose '
        'uid was only remapped if /etc was writable.')


def test_the_reference_compose_hardens_the_bazarr_service():
    _assert_hardened(_compose()['services']['bazarr'], 'the reference docker-compose.yml')


def test_the_readme_compose_hardens_the_bazarr_service():
    _assert_hardened(_readme_compose()['services']['bazarr'], "the README's compose block")


def test_the_installer_writes_a_hardened_compose():
    _assert_hardened(_installer_compose()['services']['bazarr'],
                     'the compose file install.sh generates')


def test_only_the_three_capabilities_the_startup_needs_are_added_back():
    """Nothing boots under a bare ``cap_drop: [ALL]``.

    The entrypoint starts as root: it needs CHOWN to hand /config to PUID:PGID
    and SETUID/SETGID to become that user, and gosu fails outright without the
    last two, which would leave the container dead rather than insecure. Every
    other default capability is gone, and the dropped-privilege process that
    runs Bazarr and the Provider Hub workers holds none at all.

    _assert_hardened already pins the set everywhere; this says why it is that
    set, so a later reader does not "tidy" it back to a bare cap_drop.
    """
    assert _compose()['services']['bazarr'].get('cap_add') == ['CHOWN', 'SETUID', 'SETGID']


def test_the_image_refuses_to_build_an_application_tree_it_does_not_own():
    """The Dockerfile assertion is the only thing standing between a stray
    COPY --chown and a writable /app, and nothing else would notice."""
    dockerfile = _dockerfile()
    assert 'find /app' in dockerfile and '! -user root' in dockerfile and '-perm /go=w' in dockerfile, (
        'the Dockerfile no longer asserts that /app is root-owned and not group/other '
        'writable, so the ownership half of this fix has nothing holding it in place')
    assert '/app/bazarr/bin' not in dockerfile, (
        'the image creates the old binaries directory inside the application tree again')


@pytest.mark.parametrize('flag', [
    'read_only: true',
    'cap_drop:',
    'no-new-privileges:true',
    'tmpfs:',
    '--read-only',
    '--cap-drop ALL',
    '--cap-add CHOWN --cap-add SETUID --cap-add SETGID',
    'cap_add:',
    '--security-opt no-new-privileges:true',
    '--tmpfs /tmp',
])
def test_the_install_guide_documents_the_same_hardening(flag):
    assert flag in _guide(), (
        f'The Docker install guide no longer shows {flag!r}, so the configuration users '
        'copy is weaker than the one the image is built for.')


def test_downloaded_binaries_land_outside_the_application_tree(monkeypatch):
    from app.get_args import args
    from utilities import binaries

    monkeypatch.setattr(args, 'config_dir', '/config')
    resolved = binaries.binaries_dir()

    assert not resolved.startswith(ROOT + os.sep), (
        f'The binaries downloader still writes into the application tree ({resolved}), '
        'which is root-owned and read-only in the image.')
    assert resolved.startswith(os.path.realpath('/config') + os.sep)




# --- where the binaries downloader writes -----------------------------------
#
# These drive the real functions against tmp_path. The point is the behaviour,
# not the arithmetic: asserting that legacy_binaries_dir() equals the expression
# inside legacy_binaries_dir() would pass no matter what the downloader does.


@pytest.fixture
def unreachable_args(monkeypatch):
    """Make `from app.get_args import args` raise, the way a worker sees it.

    The sports analysis worker installs an import hook that refuses `app`
    imports outright. A None entry in sys.modules gives the same answer.
    """
    monkeypatch.setitem(sys.modules, 'app.get_args', None)


def test_binaries_land_under_the_config_directory(monkeypatch, tmp_path):
    from app.get_args import args
    from utilities import binaries

    monkeypatch.setattr(args, 'config_dir', str(tmp_path))
    assert binaries.binaries_dir() == os.path.realpath(tmp_path / 'managed' / 'bin')


def test_the_config_directory_can_come_from_the_environment(monkeypatch, tmp_path, unreachable_args):
    """A process that cannot import the application still has BAZARR_CONFIG_DIR."""
    from utilities import binaries

    monkeypatch.setenv('BAZARR_CONFIG_DIR', str(tmp_path))
    assert binaries.binaries_dir() == os.path.realpath(tmp_path / 'managed' / 'bin')


def test_without_any_config_directory_the_legacy_location_is_used(monkeypatch, unreachable_args):
    """The sports analysis worker: no `app` import, and an environment scrubbed
    down to PATH. Neither source resolves, so the old location is all there is."""
    from utilities import binaries

    monkeypatch.delenv('BAZARR_CONFIG_DIR', raising=False)
    assert binaries.binaries_dir() == binaries.legacy_binaries_dir()


def _stub_binary(monkeypatch, tmp_path, payload):
    """Point the downloader at a fake catalogue entry under tmp_path.

    Returns the list that records download URLs. Nothing here touches the
    network or the real application tree.
    """
    from app.get_args import args
    from utilities import binaries

    monkeypatch.setattr(args, 'config_dir', str(tmp_path / 'config'))
    monkeypatch.setattr(binaries, 'legacy_binaries_dir',
                        lambda: os.path.realpath(tmp_path / 'app' / 'bin'))
    monkeypatch.setattr(binaries.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(binaries.platform, 'machine', lambda: 'x86_64')
    # Nothing on PATH, so the download path is the one under test.
    monkeypatch.setattr(binaries, 'which', lambda name: None)

    monkeypatch.setattr(binaries, 'get_binaries_from_json', lambda: [{
        'system': 'Linux', 'machine': 'x86_64', 'directory': 'ffmpeg',
        'name': 'ffmpeg', 'checksum': hashlib.md5(payload).hexdigest(),
        'url': 'https://example.invalid/ffmpeg',
    }])

    requested = []

    def fake_get(url, *args_, **kwargs):
        requested.append(url)
        return SimpleNamespace(content=payload)

    monkeypatch.setattr(binaries.requests, 'get', fake_get)
    return requested


def test_a_matching_binary_in_the_legacy_location_is_used_instead_of_downloading(monkeypatch, tmp_path):
    """The upgrade case. Re-fetching ffmpeg because it moved would be a
    regression every existing install pays for on its next start."""
    from utilities import binaries

    payload = b'#!/bin/sh\nexit 0\n'
    requested = _stub_binary(monkeypatch, tmp_path, payload)

    legacy_exe = tmp_path / 'app' / 'bin' / 'Linux' / 'x86_64' / 'ffmpeg' / 'ffmpeg'
    legacy_exe.parent.mkdir(parents=True)
    legacy_exe.write_bytes(payload)

    resolved = binaries.get_binary('ffmpeg')

    assert resolved == str(legacy_exe.resolve()), 'the existing binary was not reused'
    assert requested == [], f'it downloaded anyway: {requested}'
    assert not (tmp_path / 'config' / 'managed').exists(), (
        'it wrote to the new location even though the old copy already matched')


def test_a_downloaded_binary_is_only_ever_written_under_the_config_directory(monkeypatch, tmp_path):
    from utilities import binaries

    payload = b'#!/bin/sh\nexit 1\n'
    requested = _stub_binary(monkeypatch, tmp_path, payload)

    resolved = binaries.get_binary('ffmpeg')

    expected = tmp_path / 'config' / 'managed' / 'bin' / 'Linux' / 'x86_64' / 'ffmpeg' / 'ffmpeg'
    assert resolved == str(expected.resolve())
    assert expected.read_bytes() == payload
    assert requested == ['https://example.invalid/ffmpeg']
    assert not (tmp_path / 'app' / 'bin').exists(), (
        'the downloader created its directory inside the application tree, which is '
        'root-owned and read-only in the image')
    assert os.access(expected, os.X_OK), 'the downloaded binary was not made executable'


def test_a_stale_legacy_binary_does_not_win_over_a_fresh_download(monkeypatch, tmp_path):
    """Checksum, not location: a legacy copy that no longer matches is ignored,
    and the download still lands in the new place."""
    from utilities import binaries

    payload = b'#!/bin/sh\nexit 2\n'
    requested = _stub_binary(monkeypatch, tmp_path, payload)

    legacy_exe = tmp_path / 'app' / 'bin' / 'Linux' / 'x86_64' / 'ffmpeg' / 'ffmpeg'
    legacy_exe.parent.mkdir(parents=True)
    legacy_exe.write_bytes(b'an older build\n')

    resolved = binaries.get_binary('ffmpeg')

    assert requested == ['https://example.invalid/ffmpeg']
    assert resolved.startswith(str(tmp_path / 'config' / 'managed' / 'bin'))
    assert legacy_exe.read_bytes() == b'an older build\n', 'the old tree was written to'


# --- the update installer must not run against a tree it does not own --------
#
# main.py called apply_update() unconditionally, outside the no_update gate. A
# /config carried over from a source install can still hold that install's
# queued release, and /config outlives the install that wrote it.
#
# Measured both ways before writing the gate. Against a tree the process cannot
# write, apply_update() logs "unable to unzip release" with a PermissionError
# and its finally: clause does remove the archive, so that is one noisy boot
# rather than a loop. Against a WRITABLE tree, which is exactly the development
# container the guide documents (source bind-mounted over /app/bazarr,
# read_only dropped), it unpacks the stale release and then update_cleaner
# deletes the files the archive did not contain. That is the one that matters.


@pytest.fixture
def staged_release(monkeypatch, tmp_path):
    """A config directory holding a queued release, and a writable app tree.

    ``check_update.__file__`` is repointed into tmp_path for every test that
    uses this fixture, not just the ones that call the real extractor. That is
    deliberate: apply_update() derives the directory it unpacks over from that
    attribute, so a test which forgets to set it does not fail, it rewrites the
    checkout it is running from. Pinning it here makes that unreachable.
    """
    import zipfile

    from app.check_update import args

    config_dir = tmp_path / 'config'
    app_root = tmp_path / 'app' / 'bazarr'
    (app_root / 'bazarr' / 'app').mkdir(parents=True)
    (app_root / 'bazarr.py').write_text('the installed copy\n')
    (app_root / 'keep_me.txt').write_text('not in the archive\n')

    bazarr_zip = config_dir / 'update' / 'bazarr.zip'
    bazarr_zip.parent.mkdir(parents=True)
    with zipfile.ZipFile(bazarr_zip, 'w') as archive:
        # Real release zips record the root directory. Without that entry the
        # extractor's zip_root_directory heuristic picks the first file instead,
        # every member is skipped, and a test written on such a zip proves
        # nothing at all.
        archive.writestr('bazarr-9.9.9/', '')
        archive.writestr('bazarr-9.9.9/bazarr.py', 'from the archive\n')

    monkeypatch.setattr(args, 'config_dir', str(config_dir))
    monkeypatch.setattr(check_update_module(), '__file__',
                        str(app_root / 'bazarr' / 'app' / 'check_update.py'))
    return SimpleNamespace(config_dir=config_dir, app_root=app_root, zip=bazarr_zip)


def check_update_module():
    from app import check_update

    return check_update


def test_an_externally_managed_install_never_unpacks_a_release(monkeypatch, staged_release):
    from app.check_update import args

    check_update = check_update_module()
    monkeypatch.setattr(args, 'no_update', True)
    called = []
    monkeypatch.setattr(check_update, 'apply_update', lambda: called.append(True))

    check_update.install_downloaded_update()

    assert called == [], 'apply_update ran on an install that is updated from the outside'


def test_the_stale_archive_is_discarded_rather_than_left_to_be_retried(staged_release, monkeypatch, caplog):
    from app.check_update import args

    check_update = check_update_module()
    monkeypatch.setattr(args, 'no_update', True)

    with caplog.at_level(logging.INFO):
        check_update.install_downloaded_update()

    assert not staged_release.zip.exists(), (
        'the archive survived, so the next boot has the same decision to make again')
    assert any('discarded a leftover release archive' in record.message for record in caplog.records), (
        f'nothing said why the archive went away: {[r.message for r in caplog.records]}')


def test_discarding_says_nothing_when_there_is_no_archive(monkeypatch, tmp_path, caplog):
    from app.check_update import args

    check_update = check_update_module()
    monkeypatch.setattr(args, 'config_dir', str(tmp_path))
    monkeypatch.setattr(args, 'no_update', True)

    with caplog.at_level(logging.INFO):
        check_update.install_downloaded_update()

    assert caplog.records == [], 'the ordinary case logged something'


def test_an_unmanaged_install_still_installs_what_it_downloaded(monkeypatch, staged_release):
    """The gate must not take the in-app updater away from source installs."""
    from app.check_update import args

    check_update = check_update_module()
    monkeypatch.setattr(args, 'no_update', False)
    called = []
    monkeypatch.setattr(check_update, 'apply_update', lambda: called.append(True))

    check_update.install_downloaded_update()

    assert called == [True], 'a self-updating install stopped installing its own downloads'


def test_a_writable_application_tree_survives_a_stale_release(monkeypatch, staged_release, caplog):
    """The real bite, through the real extractor rather than a mock.

    Before the gate this overwrote bazarr.py and then deleted keep_me.txt,
    because update_cleaner removes whatever the archive does not contain. On a
    development container that tree is the operator's own checkout.
    """
    from app.check_update import args

    check_update = check_update_module()
    monkeypatch.setattr(args, 'no_update', True)

    with caplog.at_level(logging.DEBUG):
        check_update.install_downloaded_update()

    assert (staged_release.app_root / 'bazarr.py').read_text() == 'the installed copy\n', (
        'a queued release overwrote the application tree on an externally managed install')
    assert (staged_release.app_root / 'keep_me.txt').exists(), (
        'the upgrade leftover cleaner deleted a file that was never ours to delete')
    assert not any('unable to unzip' in record.message for record in caplog.records)
