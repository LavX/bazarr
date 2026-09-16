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
import os

import pytest
import yaml

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _entrypoint():
    with open(os.path.join(ROOT, 'docker', 'entrypoint.sh')) as handle:
        return handle.read()


def _compose():
    with open(os.path.join(ROOT, 'docker-compose.yml')) as handle:
        return yaml.safe_load(handle)


def _guide():
    with open(os.path.join(ROOT, 'site', 'guides', 'getting-started.html')) as handle:
        return handle.read()


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
    service = _compose()['services']['bazarr']
    assert service.get('read_only') is True, 'read_only: true is missing from the reference compose'
    assert '/tmp' in service.get('tmpfs', []), (
        'read_only without a tmpfs for /tmp breaks everything that writes a temporary file')
    assert service.get('cap_drop') == ['ALL'], 'cap_drop: [ALL] is missing from the reference compose'
    assert 'no-new-privileges:true' in service.get('security_opt', []), (
        'no-new-privileges:true is missing from the reference compose')


def test_only_the_three_capabilities_the_startup_needs_are_added_back():
    """Nothing boots under a bare ``cap_drop: [ALL]``.

    The entrypoint starts as root: it needs CHOWN to hand /config to PUID:PGID
    and SETUID/SETGID to become that user, and gosu fails outright without the
    last two, which would leave the container dead rather than insecure. Every
    other default capability is gone, and the dropped-privilege process that
    runs Bazarr and the Provider Hub workers holds none at all.
    """
    assert _compose()['services']['bazarr'].get('cap_add') == ['CHOWN', 'SETUID', 'SETGID'], (
        'The reference compose adds back a different capability set than the startup needs.')


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


def test_the_legacy_binaries_location_is_still_read():
    """An upgrade must not re-download ffmpeg, unrar and alass from scratch."""
    from utilities import binaries

    legacy = binaries.legacy_binaries_dir()
    assert legacy == os.path.realpath(os.path.join(ROOT, 'bin'))
