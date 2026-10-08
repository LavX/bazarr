# coding=utf-8
"""A build has to say which build it is.

System Status on a candidate of the development line reported "Bazarr Version:
latest", because a local image bake passed no version and the Dockerfile's own
default was exactly that word. The owner could not tell which tree the shared
test box was running, and neither "latest" nor the last released package
constant identifies a running build.

The rule this file guards: every image bake carries the line version, the
source commit and the build date, stamped at build time and shown in System
Status. The two workflows that bake the image already pass all three args,
so the only bake that changes behaviour is an unstamped local one, which now
fails loudly instead of shipping an image nobody can identify.

The repository does not track a ``BUILD`` file. A source checkout reads an
empty stamp and shows no build identity, which is correct: a checkout is not
a build.
"""
import os
import re

DOCKERFILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'Dockerfile')

WORKFLOWS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    '.github', 'workflows')


def test_the_build_stamp_sits_beside_the_package_info():
    from utilities.build import build_stamp_path
    from utilities.package import package_info_path

    stamp = build_stamp_path()
    assert os.path.basename(stamp) == 'BUILD'
    assert os.path.dirname(stamp) == os.path.dirname(package_info_path()), (
        f'{stamp} is not beside the package_info at {package_info_path()}. '
        'In the shipped image the Dockerfile writes both into /app/bazarr, '
        'so a derivation that looks anywhere else never finds the stamp.')


def test_a_written_stamp_parses_into_commit_and_date(tmp_path):
    from utilities.build import read_build_stamp

    stamp = tmp_path / 'BUILD'
    stamp.write_text('commit=5c8d31197\ndate=2026-09-30\n')
    assert read_build_stamp(path=str(stamp)) == {
        'commit': '5c8d31197', 'date': '2026-09-30'}


def test_a_source_checkout_carries_no_build_identity():
    """Nothing writes a BUILD file into a checkout, so the default path reads
    empty and the page shows no build row. That is the honest answer."""
    from utilities.build import read_build_stamp

    assert read_build_stamp() == {}


def test_an_absent_stamp_is_an_empty_identity(tmp_path):
    from utilities.build import read_build_stamp

    assert read_build_stamp(path=str(tmp_path / 'absent')) == {}


def test_an_unreadable_stamp_is_an_empty_identity(tmp_path):
    """The status page reads this on every load, so a damaged stamp must not
    break the page it is only decorating."""
    import os as os_mod

    from utilities.build import read_build_stamp

    stamp = tmp_path / 'BUILD'
    stamp.write_text('commit=5c8d31197\n')
    os_mod.chmod(stamp, 0o000)
    try:
        assert read_build_stamp(path=str(stamp)) == {}
    finally:
        os_mod.chmod(stamp, 0o644)


def test_an_undecodable_stamp_is_an_empty_identity(tmp_path):
    from utilities.build import read_build_stamp

    stamp = tmp_path / 'BUILD'
    stamp.write_bytes(b'commit=\xff\xfe\x00garbage\n')
    assert read_build_stamp(path=str(stamp)) == {}


def _status_payload(monkeypatch, stamp):
    """Call SystemStatus.get with the world it reads fixed: the arr probes,
    the media server sweep and the stamp reader, so only the payload wiring
    is under test."""
    from types import SimpleNamespace

    from api.system import status

    monkeypatch.setenv('BAZARR_VERSION', '2.7.1')
    monkeypatch.setattr(status, 'get_sonarr_info', SimpleNamespace(version=lambda: 'sonarr'))
    monkeypatch.setattr(status, 'get_radarr_info', SimpleNamespace(version=lambda: 'radarr'))
    monkeypatch.setattr(status, 'get_sportarr_info', SimpleNamespace(version=lambda: 'sportarr'))
    monkeypatch.setattr(status, 'media_server_statuses', lambda: [])
    monkeypatch.setattr(status, 'read_build_stamp', lambda path=None: stamp)

    # authenticate wraps the handler; call the function it wrapped.
    handler = status.SystemStatus.get
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler(status.SystemStatus())['data']


def test_a_stamped_build_reaches_the_status_payload(monkeypatch):
    data = _status_payload(monkeypatch, {'commit': '5c8d31197', 'date': '2026-09-30'})
    assert data['build_commit'] == '5c8d31197'
    assert data['build_date'] == '2026-09-30'
    assert data['bazarr_version'] == '2.7.1'


def test_an_unstamped_build_reports_empty_identity_fields(monkeypatch):
    """The keys are always present, empty strings without a stamp, matching
    how package_version always exists."""
    data = _status_payload(monkeypatch, {})
    assert data['build_commit'] == ''
    assert data['build_date'] == ''
    assert data['bazarr_version'] == '2.7.1'


def test_the_version_arg_has_no_default():
    """The Dockerfile itself used to default BAZARR_VERSION to latest, so a
    local bake that passed nothing shipped an identity nobody could pin to a
    tree. With no default, an unstamped bake fails instead."""
    text = open(DOCKERFILE).read()
    assert 'ARG BAZARR_VERSION=latest' not in text, (
        'the Dockerfile still defaults BAZARR_VERSION, so a bake that passes '
        'nothing ships "latest" again')
    assert re.search(r'^ARG BAZARR_VERSION$', text, re.MULTILINE), (
        'the version arg is missing from the Dockerfile')


def test_the_bake_requires_the_version_the_commit_and_the_date():
    """The two workflows that build the image pass all three args, so the
    guard only bites a bake that would otherwise ship an unnamed image."""
    text = open(DOCKERFILE).read()
    guard = text.find('test -n "${BAZARR_VERSION}"')
    version_write = text.find('echo "${BAZARR_VERSION}" > /app/bazarr/VERSION')
    assert guard != -1, 'the guard RUN is missing from the Dockerfile'
    assert version_write != -1, 'the VERSION write is missing from the Dockerfile'
    assert guard < version_write, 'the guard must run before the VERSION write'
    for arg in ('BAZARR_VERSION', 'VCS_REF', 'BUILD_DATE'):
        assert f'test -n "${{{arg}}}"' in text, (
            f'the guard does not require {arg}')


def test_the_bake_writes_the_stamp_after_the_version():
    text = open(DOCKERFILE).read()
    version_write = text.find('echo "${BAZARR_VERSION}" > /app/bazarr/VERSION')
    stamp_write = text.find('printf \'commit=%s\\ndate=%s\\n\'')
    assert stamp_write != -1, 'the BUILD write is missing from the Dockerfile'
    assert version_write != -1
    assert version_write < stamp_write, (
        'the BUILD stamp is not written after the VERSION it belongs to')


def _block_from(text, start_marker):
    """The lines of one workflow block: from a start marker to the next
    named step, so the assertions below cannot match unrelated content."""
    start = text.find(start_marker)
    if start == -1:
        return ''
    end = text.find('- name:', start + len(start_marker))
    return text[start:end if end != -1 else len(text)]


def test_every_bake_of_the_root_dockerfile_passes_the_identity_args():
    """Three workflows bake the root Dockerfile: the release build, the
    manual build and the e2e lane. The Dockerfile guard fails any bake that
    passes nothing, so each baker must pass all three identity args. And a
    repository's last-updated timestamp is not the date of the bake, so no
    build-args block may stamp itself with github.event.repository.updated_at."""
    for name in ('build-docker.yml', 'build-docker-manual.yml'):
        block = _block_from(open(os.path.join(WORKFLOWS, name)).read(),
                            'build-args:')
        assert block, f'{name} has no build-args block to guard'
        for arg in ('BAZARR_VERSION=', 'BUILD_DATE=', 'VCS_REF='):
            assert arg in block, (
                f'{name} does not pass {arg}, so its bake would fail '
                'the Dockerfile guard')
        assert 'github.event.repository.updated_at' not in block, (
            f'{name} stamps the build date with the repository\'s '
            'last-updated timestamp, which is not the date of the bake')

    e2e_step = _block_from(open(os.path.join(WORKFLOWS, 'e2e.yml')).read(),
                           '- name: Build image')
    assert e2e_step, 'e2e.yml has no Build image step to guard'
    for arg in ('--build-arg BAZARR_VERSION=', '--build-arg VCS_REF=',
                '--build-arg BUILD_DATE='):
        assert arg in e2e_step, (
            f'the e2e bake does not pass {arg}, so the image build fails '
            'the Dockerfile guard and the whole lane dies there')
