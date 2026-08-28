# coding=utf-8
"""Two defects the v2.6.0 pre-release triage turned up.

1. ``apply_default_profile`` binds every unprofiled item id into a single IN
   clause. SQLite built with the legacy limit rejects a statement binding more
   than 999 variables, and the codebase already chunks at 900 elsewhere for
   exactly that reason. A library with more than that many unprofiled items
   would make the Apply button fail with nothing updated.

2. ``ensure_launcher`` sweeps every launcher directory this user owns on each
   install, and reads and writes the module-level path with no lock. Bazarr's
   jobs are threads in one process, so two syncs starting together can both
   install, and the second one's sweep can delete the directory the first is
   about to hand to a running alass.
"""
import os
import threading

import pytest

from sqlalchemy import select

from app.database import TableLanguagesProfiles, TableShows


# ------------------------------------------------- the unchunked IN clause

def _bound_in_sizes(statement):
    """How many values each IN clause in this statement binds."""
    sizes = []
    for criterion in getattr(statement, '_where_criteria', ()):
        for element in getattr(criterion, 'get_children', lambda **kw: ())(column_collections=False):
            expanding = getattr(element, 'expanding', False)
            value = getattr(element, 'value', None)
            if expanding and isinstance(value, (list, tuple)):
                sizes.append(len(value))
    return sizes


@pytest.fixture
def instance_with_many_unprofiled(schema_session, monkeypatch):
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    monkeypatch.setattr(service, '_known_profile_ids', lambda _s: {7})
    monkeypatch.setattr(service, '_excluded_profile_tags', lambda _kind: set())

    from arr_instances.media_defaults import merge_media_defaults_into_options

    # profileId carries a real foreign key, so the profile has to exist.
    schema_session.add(TableLanguagesProfiles(profileId=7, cutoff=None, originalFormat=0,
                                              items='[]', name='Default'))
    schema_session.flush()

    repo = ArrInstanceRepository(schema_session)
    inst = repo.create('sonarr', 'Sonarr')
    schema_session.flush()
    repo.update(inst.id, options=merge_media_defaults_into_options(
        None, {'default_profile': 7, 'default_enabled': True}))
    schema_session.flush()

    schema_session.add_all([
        TableShows(id=i, arr_instance_id=inst.id, sonarrSeriesId=1000 + i,
                   title=f'S{i}', path=f'/tv/{i}', profileId=None, tags='[]')
        for i in range(1, 1001)
    ])
    schema_session.commit()
    return service, inst.id, schema_session


def test_applying_a_default_profile_chunks_its_in_clause(instance_with_many_unprofiled,
                                                         monkeypatch):
    service, instance_id, session = instance_with_many_unprofiled

    widest = []
    real_execute = session.execute

    def _spy(statement, *a, **kw):
        widest.extend(_bound_in_sizes(statement))
        return real_execute(statement, *a, **kw)

    monkeypatch.setattr(session, 'execute', _spy)

    body, status = service.apply_default_profile(session, instance_id)

    assert status == 200, body
    assert widest, 'no IN clause was bound, the test is not looking at the right thing'
    assert max(widest) <= 900, (
        f'one statement bound {max(widest)} values into a single IN clause; SQLite '
        'built with the legacy limit rejects more than 999')


def test_applying_a_default_profile_still_updates_every_row(instance_with_many_unprofiled):
    service, instance_id, session = instance_with_many_unprofiled

    body, status = service.apply_default_profile(session, instance_id)

    assert status == 200
    assert body['updated'] == 1000
    remaining = session.execute(
        select(TableShows.id).where(TableShows.arr_instance_id == instance_id,
                                    TableShows.profileId.is_(None))).all()
    assert remaining == [], f'{len(remaining)} rows were left unprofiled'


# ------------------------------------------------------- the launcher sweep

def test_the_sweep_spares_the_launcher_in_use(tmp_path, monkeypatch):
    """A sweep that deletes the launcher a running alass is executing fails that
    sync and records a strike against the engine."""
    from subtitles.tools import alass_ffprobe_shim as shim

    parent = tmp_path / 'cache'
    parent.mkdir()
    live = parent / f'{shim.LAUNCHER_PREFIX}live'
    live.mkdir()
    (live / 'ffprobe').write_text('#!/bin/sh\n')
    stale = parent / f'{shim.LAUNCHER_PREFIX}stale'
    stale.mkdir()

    monkeypatch.setattr(shim, '_LAUNCHER_PATH', str(live / 'ffprobe'))

    shim._sweep(str(parent))

    assert live.exists(), 'the sweep deleted the launcher that is currently in use'
    assert not stale.exists(), 'the sweep should still reclaim earlier runs'


def test_concurrent_callers_install_one_launcher(tmp_path, monkeypatch):
    """Jobs are threads in one process, so two syncs starting together race on
    the module-level path and both install."""
    from subtitles.tools import alass_ffprobe_shim as shim

    monkeypatch.setattr(shim, '_LAUNCHER_PATH', None)
    monkeypatch.setattr(shim, '_writable_directories', lambda: [str(tmp_path)])

    installs = []
    barrier = threading.Barrier(4)

    def _install(directory, content):
        installs.append(directory)
        launcher = os.path.join(directory, 'ffprobe')
        with open(launcher, 'w') as handle:
            handle.write(content)
        return launcher

    monkeypatch.setattr(shim, '_install_launcher', _install)

    def _run():
        barrier.wait()
        shim.ensure_launcher()

    threads = [threading.Thread(target=_run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(installs) == 1, (
        f'{len(installs)} threads installed a launcher; only one should have, and '
        'the others must reuse it')
