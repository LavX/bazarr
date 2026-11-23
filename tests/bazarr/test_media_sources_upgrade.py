"""Source upgrades and opt-in history statistics on SQLite and PostgreSQL."""
import json
import os
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from media_source_helpers import add_item, add_source, video
import test_media_sources_no_change as legacy
import test_media_sources_pipeline as pipeline
import test_media_sources_schedule as schedules
from test_media_sources_no_change import HEADERS

arr_library = legacy.arr_library
client = legacy.client
everything_uses_the_test_database = legacy.everything_uses_the_test_database
real_scheduler = schedules.real_scheduler
world = pipeline.world


@pytest.fixture(autouse=True)
def upgrade_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, 'upgrade_subs', True)
    monkeypatch.setattr(settings.general, 'upgrade_manual', False)
    monkeypatch.setattr(settings.general, 'upgrade_translated', False)
    monkeypatch.setattr(settings.general, 'days_to_upgrade_subs', 7)
    monkeypatch.setattr(settings.general, 'ai_translated_score_penalty', 0)


def _history(db, source, item, media, subtitle, **extra):
    from media_sources import repository

    values = dict(item_id=item, source_id=source, item_kind='movie', item_title=media.stem, action=1,
                  description='downloaded', score=60, score_out_of=120, video_path=str(media),
                  subtitles_path=str(subtitle), language='en', provider='fake')
    return repository.insert_history(db, **{**values, **extra})


def _item(db, source, library, media, **extra):
    subtitle = media.with_suffix('.en.srt')
    subtitle.write_bytes(b'Old subtitle')
    values = dict(subtitles=str([['en', str(subtitle), subtitle.stat().st_size]]), missing_subtitles='[]')
    item = add_item(db, source, library, media, **{**values, **extra})
    return item, subtitle


def test_schedule_registers_for_either_arr_or_an_enabled_source(source_db, tmp_path, real_scheduler, monkeypatch):
    from app.config import settings
    from media_sources import repository
    from subtitles import upgrade

    scheduler, jobs = real_scheduler
    source_id, _, _ = add_source(source_db, tmp_path)
    monkeypatch.setattr(settings.general, 'use_sonarr', False)
    monkeypatch.setattr(settings.general, 'use_radarr', False)
    scheduler.update_configurable_tasks()
    assert jobs['upgrade_subtitles'] is upgrade.upgrade_subtitles

    actions = [Mock(), Mock(), Mock()]
    for name, action in zip(('source', 'episodes', 'movies'), actions):
        monkeypatch.setattr(upgrade, f'upgrade_{name}_subtitles', action)
    jobs['upgrade_subtitles'](wait_for_completion=True)
    actions[0].assert_called_once_with(wait_for_completion=True)
    actions[1].assert_not_called()
    actions[2].assert_not_called()

    repository.update_source(source_id, source_db, enabled=0)
    scheduler.update_configurable_tasks()
    assert 'upgrade_subtitles' not in jobs
    monkeypatch.setattr(settings.general, 'use_radarr', True)
    scheduler.update_configurable_tasks()
    assert jobs['upgrade_subtitles'] is upgrade.upgrade_subtitles
    jobs['upgrade_subtitles'](wait_for_completion=True)
    actions[0].assert_called_once()
    actions[2].assert_called_once_with(wait_for_completion=True)


def test_candidates_and_item_routes_share_current_source_history(client, arr_library, tmp_path, monkeypatch):
    from app.config import settings
    from media_sources import repository
    from media_sources.history import get_upgradable_source_subtitles

    source, library, root = add_source(arr_library, tmp_path, profile_id=1)
    disabled, disabled_library, disabled_root = add_source(arr_library, tmp_path, name='Disabled', folder='Disabled',
                                                           profile_id=1)
    repository.update_source(disabled, arr_library, enabled=0)
    _, profileless_library, profileless_root = add_source(
        arr_library, tmp_path, name='Profileless', folder='Profileless')
    _, library_off, library_off_root = add_source(arr_library, tmp_path, name='Library off', folder='Library off',
                                                  profile_id=1)
    repository.update_library(library_off, arr_library, enabled=0)
    media = video(root / 'Eligible.mkv')
    item, sub = _item(arr_library, source, library, media)
    expected = _history(arr_library, source, item, media, sub)
    history_ids = {}
    for title, item_values, history_values in (
            ('Monitored', {'monitored': 0}, {}), ('Server missing', {'server_missing': 1}, {}),
            ('Shadowed', {'arr_shadow': 1}, {}), ('Ambiguous', {'identity_state': 'ambiguous'}, {}),
            ('Expired', {}, {'timestamp': datetime.now() - timedelta(days=8)}), ('Near max', {}, {'score': 117}),
            ('Manual', {}, {'action': 2}), ('Translated', {}, {'action': 6}), ('Upload', {}, {'action': 4}),
            ('Unwanted', {}, {'language': 'hu'}), ('Missing file', {}, {}), ('Replaced', {}, {})):
        path = video(root / f'{title}.mkv')
        other, subtitle = _item(arr_library, source, library, path, **item_values)
        history_ids[title] = _history(arr_library, source, other, path, subtitle, **history_values)
        if title == 'Missing file':
            subtitle.unlink()
        if title == 'Replaced':
            # A manual user replacement ends the old download even with manual upgrades off.
            history_ids[title] = _history(arr_library, source, other, path, subtitle, action=2)
    for library_id, other_root in ((disabled_library, disabled_root), (profileless_library, profileless_root),
                                   (library_off, library_off_root)):
        owner = repository.get_library(library_id).source_id
        path = video(other_root / 'Film.mkv')
        other, subtitle = _item(arr_library, owner, library_id, path)
        _history(arr_library, owner, other, path, subtitle)

    assert get_upgradable_source_subtitles() == {expected: None}
    detail = client.get(f'/api/sources/items/{item}', headers=HEADERS).get_json()
    assert detail['upgradable'] is True and detail['lowest_subtitle_score'] == 50
    page = client.get('/api/sources/items?scores=1', headers=HEADERS).get_json()['data']
    assert [row['id'] for row in page if row['upgradable']] == [item]
    assert next(row for row in page if row['id'] == item)['lowest_subtitle_score'] == 50
    initial = client.get('/api/sources/items', headers=HEADERS).get_json()['data']
    assert 'upgradable' not in initial[0]
    monkeypatch.setattr(settings.general, 'upgrade_manual', True)
    monkeypatch.setattr(settings.general, 'upgrade_translated', True)
    assert get_upgradable_source_subtitles() == {
        row_id: None for row_id in (expected, history_ids['Manual'], history_ids['Translated'], history_ids['Replaced'])
    }
    assert get_upgradable_source_subtitles([expected, history_ids['Unwanted']]) == {expected: None}


@pytest.mark.parametrize(('initial_variant', 'alias_folder'), [
    ('en', None), ('en:hi', None), ('en', 'relative'), ('en:hi', 'absolute'),
])
def test_scheduled_job_replaces_source_file_and_preserves_original_history(
        world, monkeypatch, initial_variant, alias_folder):
    from app.config import settings
    from media_sources import repository
    from media_sources.context import resolve_item
    from media_sources.history import get_upgradable_source_subtitles
    from media_sources.indexer import index_item
    from subtitles import upgrade

    if alias_folder is None:
        context = pipeline._movie(world)
        folder = Path(context.path).parent
    else:
        actual = video(world.tmp_path / 'recordings' / 'Canonical.mkv')
        alias = world.root / 'Film.mkv'
        alias.symlink_to(actual)
        item_id = add_item(world.db, world.source_id, world.library_id, alias, title='Film')
        context = resolve_item(item_id)
        if alias_folder == 'relative':
            folder = world.root / 'Subs'
            folder.mkdir()
            monkeypatch.setattr(settings.general, 'subfolder', 'relative')
            monkeypatch.setattr(settings.general, 'subfolder_custom', 'Subs')
        else:
            folder = pipeline._custom_folder(world, monkeypatch, world.tmp_path)
    target = folder / Path(context.path).with_suffix('.en.srt').name
    original = target if initial_variant == 'en' else target.with_suffix('.hi.srt')
    original.write_bytes(b'Old subtitle')
    index_item(context.item_id)
    history_id = _history(world.db, world.source_id, context.item_id, Path(context.path), original,
                          language=initial_variant)
    monkeypatch.setattr(settings.general, 'use_sonarr', False)
    monkeypatch.setattr(settings.general, 'use_radarr', False)
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    asked = pipeline._download(monkeypatch)
    assert get_upgradable_source_subtitles() == {history_id: None}

    upgrade.upgrade_source_subtitles(job_id=10)

    assert asked['min_score'] == 61
    assert record_rows(world.db) == [(history_id, 1, None), (history_id + 1, 3, history_id)]
    assert target.read_bytes() == pipeline.CONTENT
    assert original == target or not original.exists()
    assert [event.subtitle_path for event in world.published] == [
        str(path) for path in dict.fromkeys((target, original))
    ]
    assert get_upgradable_source_subtitles() == {history_id + 1: history_id}
    assert repository.get_item(context.item_id).missing_subtitles == '[]'


def test_concurrent_source_upgrade_skips_the_same_completed_history_entry(world, monkeypatch):
    from media_sources.indexer import index_item
    from subtitles import download, upgrade

    source = pipeline._movie(world)
    media = Path(source.path)
    destination = media.with_suffix('.en.srt')
    destination.write_bytes(b'Old subtitle')
    index_item(source.item_id)
    first = _history(world.db, world.source_id, source.item_id, media, destination)
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    asked = pipeline._download(monkeypatch)
    choose = download.download_best_subtitles
    attempts = []

    def complete_another_search(videos, languages, min_score, **kwargs):
        attempts.append(min_score)
        monkeypatch.setattr(download, 'download_best_subtitles', choose)
        upgrade.upgrade_source_subtitles(job_id=11)
        return choose(videos, languages, min_score, **kwargs)

    monkeypatch.setattr(download, 'download_best_subtitles', complete_another_search)
    upgrade.upgrade_source_subtitles(job_id=10)

    assert attempts == [61] and asked['min_score'] == 61
    assert record_rows(world.db) == [(first, 1, None), (first + 1, 3, first)]
    assert destination.read_bytes() == pipeline.CONTENT
    assert [event.subtitle_path for event in world.published] == [str(destination)]


def record_rows(db):
    from media_sources import repository

    table = repository.History.c
    return db.execute(sa.select(table.id, table.action, table.upgradedFromId).order_by(table.id)).all()


def _change_after_ownership_preparation(monkeypatch, change):
    from media_sources.indexer import SourcePublicationOwnerIndex

    publication = SourcePublicationOwnerIndex.publication

    @contextmanager
    def changed_publication(self, source, prepare=None, **kwargs):
        changed = False

        def changed_prepare():
            nonlocal changed
            prepared = prepare() if prepare is not None else None
            if not changed:
                change(source)
                changed = True
            return prepared

        with publication(self, source, changed_prepare, **kwargs) as guarded:
            yield guarded

    monkeypatch.setattr(SourcePublicationOwnerIndex, 'publication', changed_publication)


@pytest.mark.parametrize(('failure_step', 'same_name'), [
    ('remove old', False), ('publish new', False), ('commit guard', False), ('commit guard', True),
    ('insert history', False), ('insert history', True),
])
def test_failed_source_replacement_leaves_upgrade_retryable(world, monkeypatch, failure_step, same_name):
    from media_sources import repository
    from media_sources.history import get_upgradable_source_subtitles
    from media_sources.indexer import index_item
    from subtitles import download, upgrade

    from subtitles.tools.subsync_engines import engine_output_path

    context = pipeline._movie(world)
    media = Path(context.path)
    destination = media.with_suffix('.en.srt')
    original = destination if same_name else media.with_suffix('.en.hi.srt')
    original.write_bytes(b'Old subtitle')
    siblings = {engine_output_path(original, 'ffsubsync'): b'Generated original',
                engine_output_path(destination, 'autosubsync'): b'Generated destination'}
    for sibling, content in siblings.items():
        sibling.write_bytes(content)
    index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original,
                     language='en' if same_name else 'en:hi')
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    pipeline._download(monkeypatch)
    remove = download.os.remove
    replace = download.os.replace
    failures = []
    guard_backups = []

    def fail_original_removal(path, *args, **kwargs):
        if Path(path) == original:
            failures.append('remove old')
            raise PermissionError('Cannot remove the old sidecar')
        return remove(path, *args, **kwargs)

    def fail_destination_publication(source, target, *args, **kwargs):
        if Path(target) == destination:
            failures.append('publish new')
            raise PermissionError('Cannot publish the new sidecar')
        return replace(source, target, *args, **kwargs)

    def fail_guard_commit(connection):
        if destination.exists() and destination.read_bytes() == pipeline.CONTENT and not failures:
            failures.append('commit guard')
            guard_backups.extend(path.read_bytes() for path in media.parent.glob('.bazarr-replaced-*'))
            guard_backups.extend(path.read_bytes() for path in media.parent.glob('.bazarr-sync-obsolete-*'))
            assert all(not path.exists() for path in siblings)
            assert not world.published
            raise sa.exc.OperationalError('COMMIT', None, OSError('Lost the connection during commit'))

    def fail_history_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith(f'insert into {repository.History.name.lower()}'):
            failures.append('insert history')
            raise sa.exc.OperationalError(statement, None, OSError('Unable to record the upgrade'))

    with monkeypatch.context() as failure:
        if failure_step == 'remove old':
            failure.setattr(download.os, 'remove', fail_original_removal)
            # A failed retirement must never make recovery depend on deleting the new file.
            real_unlink = download.os.unlink

            def fail_destination_cleanup(path, *args, **kwargs):
                if Path(path) == destination:
                    raise PermissionError('Cannot remove the new sidecar')
                return real_unlink(path, *args, **kwargs)

            failure.setattr(download.os, 'unlink', fail_destination_cleanup)
        elif failure_step == 'publish new':
            failure.setattr(download.os, 'replace', fail_destination_publication)
        engine = world.db.get_bind()
        if failure_step == 'commit guard':
            sa.event.listen(engine, 'commit', fail_guard_commit)
        elif failure_step == 'insert history':
            sa.event.listen(engine, 'before_cursor_execute', fail_history_insert)
        try:
            upgrade.upgrade_source_subtitles(job_id=10)
        finally:
            if failure_step == 'commit guard':
                sa.event.remove(engine, 'commit', fail_guard_commit)
            elif failure_step == 'insert history':
                sa.event.remove(engine, 'before_cursor_execute', fail_history_insert)

    assert failures == [failure_step]
    if failure_step == 'commit guard':
        assert sorted(guard_backups) == sorted([b'Old subtitle', *siblings.values()])
    assert record_rows(world.db) == [(first, 1, None)]
    assert original.read_bytes() == b'Old subtitle'
    if not same_name:
        assert not destination.exists()
    assert {path: path.read_bytes() for path in siblings} == siblings
    assert not list(media.parent.glob('.bazarr-replaced-*'))
    assert not list(media.parent.glob('.bazarr-sync-obsolete-*'))
    assert not world.published
    assert get_upgradable_source_subtitles() == {first: None}

    upgrade.upgrade_source_subtitles(job_id=11)
    original_row, replacement_row = record_rows(world.db)
    assert original_row == (first, 1, None)
    assert replacement_row[0] > first
    assert replacement_row[1:] == (3, first)
    assert destination.read_bytes() == pipeline.CONTENT
    if not same_name:
        assert not original.exists()
    assert all(not path.exists() for path in siblings)
    assert sorted(path.read_bytes() for path in media.parent.glob('.bazarr-sync-obsolete-*')) == sorted(siblings.values())


def test_postgres_shared_upgrade_blocks_normal_arr_insert_through_publication(world, backend, monkeypatch):
    if backend != 'postgresql':
        pytest.skip('PostgreSQL is needed to observe a concurrent Arr insert')

    from app.config import settings
    from app.database import TableArrInstances, TableMovies
    from media_sources.indexer import index_item
    from radarr.sync import movies
    from subtitles import download, upgrade
    from subtitles.tools.subsync_engines import engine_output_path

    context = pipeline._movie(world)
    media = Path(context.path)
    folder = pipeline._custom_folder(world, monkeypatch, world.tmp_path)
    original = folder / media.with_suffix('.en.hi.srt').name
    destination = folder / media.with_suffix('.en.srt').name
    arr_path = video(world.tmp_path / 'arr' / media.name)
    siblings = (engine_output_path(original, 'ffsubsync'), engine_output_path(destination, 'ffsubsync'))
    original.write_bytes(b'Old subtitle')
    for sibling in siblings:
        sibling.write_bytes(b'Old generated subtitle')
    index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original, language='en:hi')
    world.db.execute(sa.insert(TableArrInstances).values(
        id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1))
    monkeypatch.setattr(settings.general, 'use_radarr', True)
    monkeypatch.setattr(movies, 'database', world.db)
    monkeypatch.setattr(movies, 'store_subtitles_movie', Mock())
    monkeypatch.setattr(movies, 'event_stream', Mock())
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    pipeline._download(monkeypatch)

    start_insert, insert_ready = Event(), Event()
    writer_pids, blockers = [], []
    engine = world.db.get_bind()
    replace = download.os.replace

    def insert_movie():
        try:
            assert start_insert.wait(10)
            connection = world.db.connection()
            connection.exec_driver_sql("SET statement_timeout = '10s'")
            writer_pids.append(connection.exec_driver_sql('SELECT pg_backend_pid()').scalar_one())
            insert_ready.set()
            movies.add_movie(dict(id=73, arr_instance_id=42, radarrId=9000, title='Film',
                                  path=str(arr_path), tmdbId='73'))
        finally:
            world.db.remove()

    def observe_generated_quarantine(staged, target):
        if Path(staged) == siblings[0] and not start_insert.is_set():
            start_insert.set()
            assert insert_ready.wait(10)
            with engine.connect() as observer:
                deadline = monotonic() + 5
                while not writer.done():
                    pids = observer.scalar(sa.text('SELECT pg_blocking_pids(:pid)'), {'pid': writer_pids[0]})
                    if pids:
                        blockers.extend(pids)
                        break
                    assert monotonic() < deadline, 'Arr insert did not reach the ownership lock'
                    sleep(0.01)
        return replace(staged, target)

    monkeypatch.setattr(download.os, 'replace', observe_generated_quarantine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(insert_movie)
        try:
            upgrade.upgrade_source_subtitles(job_id=10)
            assert start_insert.is_set(), 'upgrade never reached generated subtitle cleanup'
        finally:
            start_insert.set()
        writer.result(timeout=10)

    assert blockers, 'Ordinary Arr insert completed before the subtitle publication transaction exited'
    assert world.db.scalar(sa.select(TableMovies.path)) == str(arr_path)
    assert record_rows(world.db) == [(first, 1, None), (first + 1, 3, first)]
    assert destination.read_bytes() == pipeline.CONTENT
    assert not original.exists()
    assert all(not sibling.exists() for sibling in siblings)


@pytest.mark.parametrize('folder_mode', ['absolute', 'relative'])
def test_postgres_shared_upgrade_rechecks_a_concurrent_symlink_owner(world, backend, monkeypatch, folder_mode):
    if backend != 'postgresql':
        pytest.skip('PostgreSQL is needed to observe the Arr transaction holding its registry lock')

    from app.config import settings
    from app.database import TableArrInstances, TableMovies
    from media_sources.history import get_upgradable_source_subtitles
    from media_sources.indexer import index_item
    from subtitles import upgrade
    from subtitles.tools.subsync_engines import engine_output_path
    from utilities.helper import get_target_folder

    context = pipeline._movie(world)
    media = Path(context.path)
    folder = world.root.parent / 'shared'
    folder.mkdir()
    monkeypatch.setattr(settings.general, 'subfolder', folder_mode)
    monkeypatch.setattr(settings.general, 'subfolder_custom',
                        str(folder) if folder_mode == 'absolute' else '../shared')
    # The remote and mapped aliases both differ from the canonical media's basename.
    other_media = video(folder.parent / 'arr' / media.name)
    alias = other_media.with_name('Alias.mkv')
    alias.symlink_to(other_media.name)
    arr_path = '/movies/Alias.mkv'
    destination = folder / media.with_suffix('.en.srt').name
    original = Path(get_target_folder(str(media), create=False)) / media.with_suffix('.en.hi.srt').name
    generated = engine_output_path(original, 'ffsubsync')
    original.write_bytes(b'Old subtitle')
    generated.write_bytes(b'Old generated subtitle')
    index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original, language='en:hi')
    assert get_upgradable_source_subtitles() == {first: None}
    world.db.execute(sa.insert(TableArrInstances).values(
        id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1,
        path_mappings=json.dumps([['/movies', str(alias.parent)]])))
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    pipeline._download(monkeypatch)

    inserted, lock_conflict = Event(), Event()
    engine = world.db.get_bind()

    def insert_owner():
        with engine.connect().execution_options(isolation_level='READ COMMITTED') as connection:
            with connection.begin():
                connection.execute(sa.insert(TableMovies).values(
                    id=73, arr_instance_id=42, radarrId=9000, title='Film', path=arr_path, tmdbId='73'))
                inserted.set()
                assert lock_conflict.wait(10)

    def observe_lock_failure(error):
        if 'IN SHARE MODE NOWAIT' in (error.statement or '') and error.original_exception.pgcode == '55P03':
            lock_conflict.set()

    sa.event.listen(engine, 'handle_error', observe_lock_failure)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(insert_owner)
            try:
                assert inserted.wait(10)
                upgrade.upgrade_source_subtitles(job_id=10)
                assert lock_conflict.is_set()
            finally:
                lock_conflict.set()
            writer.result(timeout=10)
    finally:
        sa.event.remove(engine, 'handle_error', observe_lock_failure)

    assert world.db.scalar(sa.select(TableMovies.path)) == arr_path
    assert record_rows(world.db) == [(first, 1, None)]
    assert original.read_bytes() == b'Old subtitle'
    assert generated.read_bytes() == b'Old generated subtitle'
    assert not destination.exists()
    assert not world.published


def test_source_upgrade_leaves_another_videos_generated_name_alone(world, monkeypatch):
    from app.database import TableArrInstances, TableMovies
    from media_sources.indexer import index_item
    from subtitles import upgrade
    from subtitles.tools.subsync_engines import engine_output_path

    context = pipeline._movie(world)
    media = Path(context.path)
    folder = pipeline._custom_folder(world, monkeypatch, world.tmp_path)
    original = folder / media.with_suffix('.en.srt').name
    original.write_bytes(b'Old subtitle')
    generated = engine_output_path(original, 'ffsubsync')
    generated.write_bytes(b'Another movie subtitle')
    unrelated = video(world.tmp_path / 'arr' / 'Other Film.mkv')
    alias = unrelated.with_name(generated.with_suffix('.mkv').name)
    alias.symlink_to(unrelated.name)
    world.db.execute(sa.insert(TableArrInstances).values(
        id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1))
    world.db.execute(sa.insert(TableMovies).values(
        id=73, arr_instance_id=42, radarrId=9000, title='Other', path=str(alias), tmdbId='73'))
    index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original)
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    pipeline._download(monkeypatch)

    upgrade.upgrade_source_subtitles(job_id=10)

    assert record_rows(world.db) == [(first, 1, None), (first + 1, 3, first)]
    assert original.read_bytes() == pipeline.CONTENT
    assert generated.read_bytes() == b'Another movie subtitle'
    assert not list(folder.glob('.bazarr-sync-obsolete-*'))


@pytest.mark.parametrize('change', [
    'destination owner', 'previous owner', 'generated owner', 'symlink retarget', 'unrelated video',
    'registered destination alias', 'registered previous alias', 'registered generated alias',
    'registered unrelated alias', 'registered directory alias', 'registered alias loop',
    'previous edit', 'generated edit',
])
def test_source_upgrade_rechecks_ownership_and_versions_at_publication(world, monkeypatch, change):
    from app.config import settings
    from app.database import TableArrInstances, TableMovies
    from media_sources import indexer
    from subtitles import upgrade
    from subtitles.tools.subsync_engines import engine_output_path

    context = pipeline._movie(world)
    media = Path(context.path)
    directory_alias = change == 'registered directory alias'
    if directory_alias:
        folder = media.parent / '../subtitles'
        folder.mkdir()
        monkeypatch.setattr(settings.general, 'subfolder', 'relative')
        monkeypatch.setattr(settings.general, 'subfolder_custom', '../subtitles')
    else:
        folder = pipeline._custom_folder(world, monkeypatch, world.tmp_path)
    destination = folder / media.with_suffix('.en.srt').name
    same_name = change not in ('previous owner', 'registered previous alias')
    original = destination if same_name else folder / media.with_suffix('.en.hi.srt').name
    generated = engine_output_path(original, 'ffsubsync')
    original.write_bytes(b'Old subtitle')
    generated.write_bytes(b'Old generated subtitle')
    indexer.index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original,
                     language='en' if same_name else 'en:hi')
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    pipeline._download(monkeypatch)

    alias = media.with_name('Alias.mkv')
    if change == 'symlink retarget':
        unrelated = video(world.tmp_path / 'Other' / 'Unrelated.mkv')
        competing = video(unrelated.with_name(destination.with_suffix('.mkv').name))
        alias.symlink_to(unrelated)
    elif change.startswith('registered '):
        unrelated = video(world.tmp_path / 'Arr' / 'Unrelated.mkv')
        if directory_alias:
            filename = destination.with_suffix('.mkv').name
            # Both directories lead to the same canonical video. Retargeting
            # changes the alias name's claim in the relative subtitle folder.
            previous = world.tmp_path / 'Separate' / 'Movies'
            previous.mkdir(parents=True)
            competing = unrelated.parent
            for directory in (previous, competing):
                (directory / filename).symlink_to(unrelated)
            alias = previous.with_name('Alias')
            alias.symlink_to(previous, target_is_directory=True)
            mapped_folder = alias
        else:
            alias = unrelated.with_name('Alias.mkv')
            claim = {'registered destination alias': destination, 'registered previous alias': original,
                     'registered generated alias': generated, 'registered unrelated alias': Path('Another.srt')}
            competing = (alias.name if change == 'registered alias loop'
                         else video(unrelated.with_name(claim[change].with_suffix('.mkv').name)))
            alias.symlink_to(unrelated.name)
            mapped_folder = alias.parent
            filename = alias.name
        world.db.execute(sa.insert(TableArrInstances).values(
            id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1,
            path_mappings=json.dumps([['/movies', str(mapped_folder)]])))
        world.db.execute(sa.insert(TableMovies).values(
            id=73, arr_instance_id=42, radarrId=9000, title='Other', path=f'/movies/{filename}', tmdbId='73'))

    publish = indexer.source_item_publication
    reached = []

    def apply_change(source):
        reached.append(source.item_id)
        if change == 'destination owner':
            video(media.with_name(destination.with_suffix('.mkv').name))
        elif change == 'previous owner':
            video(media.with_name(original.with_suffix('.mkv').name))
        elif change == 'generated owner':
            video(media.with_name(generated.with_suffix('.mkv').name))
        elif change == 'symlink retarget' or change.startswith('registered '):
            alias.unlink()
            alias.symlink_to(competing, target_is_directory=directory_alias)
        elif change == 'unrelated video':
            video(media.with_name('Unrelated.mkv'))
        elif change == 'previous edit':
            original.write_bytes(b'Edited original subtitle')
        else:
            generated.write_bytes(b'Edited generated subtitle')

    @contextmanager
    def change_after_prepare(source, **kwargs):
        with publish(source, **kwargs) as guarded:
            apply_change(source)
            yield guarded

    if change == 'symlink retarget' or change.startswith('registered '):
        _change_after_ownership_preparation(monkeypatch, apply_change)
    else:
        monkeypatch.setattr(indexer, 'source_item_publication', change_after_prepare)
    upgrade.upgrade_source_subtitles(job_id=10)

    assert reached == [context.item_id]
    if change in ('unrelated video', 'registered unrelated alias'):
        assert record_rows(world.db) == [(first, 1, None), (first + 1, 3, first)]
        assert destination.read_bytes() == pipeline.CONTENT
        assert not generated.exists()
        return
    assert record_rows(world.db) == [(first, 1, None)]
    assert original.read_bytes() == (b'Edited original subtitle' if change == 'previous edit' else b'Old subtitle')
    assert generated.read_bytes() == (b'Edited generated subtitle' if change == 'generated edit'
                                      else b'Old generated subtitle')
    if not same_name:
        assert not destination.exists()
    assert not world.published
    assert not list(folder.glob('.bazarr-sync-obsolete-*'))


@pytest.mark.parametrize(('registry', 'change'), [
    ('source', 'regular video becomes alias'),
    ('global Radarr mapping', 'regular video becomes alias'),
    ('global Radarr mapping', 'regular video becomes alias within timestamp tick'),
    ('source', 'relative subtitle folder retarget'),
    ('global Radarr mapping', 'relative subtitle folder retarget through mapped symlink parent'),
])
@pytest.mark.parametrize('outcome', ['competing', 'unrelated', 'rollback'])
def test_source_upgrade_rechecks_registered_filesystem_claims_after_writer_admission(
        world, monkeypatch, registry, change, outcome):
    from app import config
    from app.config import settings
    from app.database import TableArrInstances, TableMovies
    from media_sources import indexer, repository
    from subtitles import upgrade
    from subtitles.tools.subsync_engines import engine_output_path
    from utilities.path_mappings import path_mappings

    context = pipeline._movie(world, 'Film.mkv')
    media = Path(context.path)
    relative = change.startswith('relative subtitle folder retarget')
    mapped_parent = relative and registry == 'global Radarr mapping'
    coarse_timestamps = change.endswith('within timestamp tick')
    competing = outcome == 'competing'
    if relative:
        folder = media.parent / 'Subs'
        folder.mkdir()
        monkeypatch.setattr(settings.general, 'subfolder', 'relative')
        monkeypatch.setattr(settings.general, 'subfolder_custom', 'Subs')
    else:
        folder = pipeline._custom_folder(world, monkeypatch, world.tmp_path)
    original = folder / media.with_suffix('.en.srt').name
    generated = engine_output_path(original, 'ffsubsync')
    original.write_bytes(b'Old subtitle')
    generated.write_bytes(b'Old generated subtitle')

    other_source, other_library, other_root = add_source(
        world.db, world.tmp_path, name='Other', folder='Other')
    mapped_root = other_root
    if relative:
        registered = other_root / 'Film.en.mkv'
        if mapped_parent:
            canonical = video(other_root / 'Canonical.mkv')
            registered.symlink_to(canonical.name)
            aliases = world.tmp_path / 'Aliases'
            aliases.mkdir()
            nested = world.tmp_path / 'Nested'
            nested.mkdir()
            (aliases / 'jump').symlink_to(nested, target_is_directory=True)
            mapped_root = aliases / 'jump' / '..' / other_root.name
            assert (mapped_root / registered.name).samefile(canonical)
        else:
            video(registered)
        old_folder = world.tmp_path / 'Previous subtitles'
        old_folder.mkdir()
        alias = other_root / 'Subs'
        alias.symlink_to(old_folder, target_is_directory=True)
        target = folder if competing else world.tmp_path / 'Unrelated subtitles'
        target.mkdir(exist_ok=True)
    else:
        registered = video(other_root / 'Alias.mkv')
        alias = registered
        target = video(other_root / ('Film.en.mkv' if competing else 'Unrelated.mkv'))
        if coarse_timestamps:
            directory_stat = other_root.stat()

            def stat_with_coarse_timestamps(path, *args, **kwargs):
                if os.fspath(path) == str(other_root):
                    return directory_stat
                return os.stat(path, *args, **kwargs)

            monkeypatch.setattr(indexer, 'os', Mock(wraps=os, path=os.path, stat=stat_with_coarse_timestamps))
    if registry == 'source':
        registered_id = add_item(world.db, other_source, other_library, registered)
    else:
        monkeypatch.setattr(settings.general, 'use_radarr', True)
        monkeypatch.setattr(settings.general, 'path_mappings_movie', [])
        monkeypatch.setattr(path_mappings, 'path_mapping_series', [])
        monkeypatch.setattr(path_mappings, 'path_mapping_movies', [])
        monkeypatch.setattr(config, 'write_config', lambda: True)
        monkeypatch.setattr(config, 'validate_log_regex', lambda: None)
        monkeypatch.setattr(settings.validators, 'validate', lambda: None)
        # Connections saves global Radarr mappings as repeated form values.
        config.save_settings([('settings-general-path_mappings_movie', [f'/movies,{mapped_root}'])])
        expected_mapping = [['/movies', str(mapped_root)]]
        assert settings.general.path_mappings_movie == expected_mapping
        assert path_mappings.path_mapping_movies == expected_mapping
        world.db.execute(sa.insert(TableArrInstances).values(
            id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1,
            path_mappings=None))
        stored_path = f'/movies/{registered.name}'
        world.db.execute(sa.insert(TableMovies).values(
            id=73, arr_instance_id=42, radarrId=9000, title='Other', path=stored_path, tmdbId='73'))
        mapped_path = Path(path_mappings.path_replace_instance(stored_path, 42, 'movie'))
        assert mapped_path == mapped_root / registered.name
        assert mapped_path.samefile(registered)
    registered_realpath = registered.resolve()
    indexer.index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original)
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    pipeline._download(monkeypatch)
    reached, rollbacks = [], []
    publish = indexer.source_item_publication

    @contextmanager
    def change_after_admission(source, **kwargs):
        with publish(source, **kwargs) as guarded:
            current, connection = guarded
            assert current is not None and connection.in_transaction() and kwargs['shared']
            reached.append(source.item_id)
            before = indexer.os.stat(other_root)
            alias.unlink()
            alias.symlink_to(target, target_is_directory=relative)
            if not relative:
                os.utime(other_root, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                after = indexer.os.stat(other_root)
                assert (after.st_mtime_ns == before.st_mtime_ns) == coarse_timestamps
                if coarse_timestamps:
                    assert after.st_ctime_ns == before.st_ctime_ns
            yield guarded

    def discard_at_commit(_connection):
        if not rollbacks and original.read_bytes() == pipeline.CONTENT:
            assert not generated.exists()
            rollbacks.append(True)
            raise sa.exc.OperationalError('COMMIT', None, OSError('Lost the connection during commit'))

    monkeypatch.setattr(indexer, 'source_item_publication', change_after_admission)
    engine = world.db.get_bind()
    if outcome == 'rollback':
        sa.event.listen(engine, 'commit', discard_at_commit)
    try:
        upgrade.upgrade_source_subtitles(job_id=10)
    finally:
        if outcome == 'rollback':
            sa.event.remove(engine, 'commit', discard_at_commit)

    assert reached == [context.item_id]
    assert rollbacks == ([True] if outcome == 'rollback' else [])
    if relative:
        assert registered.resolve() == registered_realpath
    if registry == 'source':
        assert repository.get_item(registered_id).path == str(registered)
    else:
        assert world.db.scalar(sa.select(TableMovies.path).where(TableMovies.id == 73)) == stored_path
        assert world.db.scalar(sa.select(TableArrInstances.path_mappings).where(TableArrInstances.id == 42)) is None
    if outcome != 'unrelated':
        assert record_rows(world.db) == [(first, 1, None)]
        assert original.read_bytes() == b'Old subtitle'
        assert generated.read_bytes() == b'Old generated subtitle'
        assert not world.published
        assert not list(folder.glob('.bazarr-sync-obsolete-*'))
    else:
        assert record_rows(world.db) == [(first, 1, None), (first + 1, 3, first)]
        assert original.read_bytes() == pipeline.CONTENT
        assert not generated.exists()
        assert [event.subtitle_path for event in world.published] == [str(original)]


def test_source_upgrade_rejects_subtitle_folder_change_after_saver_selected_destination(world, monkeypatch):
    from app.config import settings
    from media_sources.indexer import index_item
    from subtitles import download, upgrade
    from subtitles.tools.subsync_engines import engine_output_path

    context = pipeline._movie(world)
    media = Path(context.path)
    original = media.with_suffix('.en.srt')
    generated = engine_output_path(original, 'ffsubsync')
    original.write_bytes(b'Old subtitle')
    generated.write_bytes(b'Old generated subtitle')
    index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original)
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    pipeline._download(monkeypatch)
    new_folder = world.tmp_path / 'new-subtitle-folder'
    new_folder.mkdir()
    save = download.save_subtitles
    reached = []

    def change_before_saving(*args, **kwargs):
        reached.append(kwargs['directory'])
        monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
        monkeypatch.setattr(settings.general, 'subfolder_custom', str(new_folder))
        return save(*args, **kwargs)

    monkeypatch.setattr(download, 'save_subtitles', change_before_saving)
    upgrade.upgrade_source_subtitles(job_id=10)

    assert reached == [None]
    assert record_rows(world.db) == [(first, 1, None)]
    assert original.read_bytes() == b'Old subtitle'
    assert generated.read_bytes() == b'Old generated subtitle'
    assert not list(new_folder.iterdir())
    assert not world.published


@pytest.mark.parametrize('change', [
    'subtitle', 'monitoring', 'other destination', 'arr owner', 'arr owner after prepare',
    'shared Arr owner after prepare', 'mapped shared Arr owner after prepare', 'mapped shared Arr owner at old stem',
    'history score', 'history superseded',
])
def test_upgrade_preserves_files_when_eligibility_changes_during_download(world, monkeypatch, change):
    from app.config import settings
    from app.database import TableArrInstances, TableMovies
    from media_sources import indexer, repository
    from media_sources.indexer import index_item
    from subtitles import download, upgrade

    old_stem_only = change == 'mapped shared Arr owner at old stem'
    context = (pipeline._movie(world, name='Tűz.2001.1080p.BluRay.x264-GRP.mkv') if old_stem_only
               else pipeline._movie(world))
    media = Path(context.path)
    shared = 'shared Arr' in change
    mapped = change.startswith('mapped')
    folder = pipeline._custom_folder(world, monkeypatch, world.tmp_path) if shared else media.parent
    destination = folder / media.with_suffix('.en.srt').name
    same_destination = mapped and not old_stem_only
    original = destination if same_destination else folder / media.with_suffix('.en.hi.srt').name
    arr_name = (unicodedata.normalize('NFD', original.with_suffix('.mkv').name.upper()) if old_stem_only
                else media.name)
    arr_path = video(world.tmp_path / 'arr' / arr_name) if shared else media
    unrelated = world.tmp_path / 'arr' / 'Unrelated.mkv'
    stored_arr_path = '/remote/Arr Movie.mkv' if mapped else str(arr_path)
    mappings = [[stored_arr_path, str(arr_path)]] if mapped else []
    original.write_bytes(b'Old subtitle')
    index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original,
                     language='en' if same_destination else 'en:hi')
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    unrelated_resolutions = []

    def claim():
        monkeypatch.setattr(settings.general, 'use_radarr', True)
        world.db.execute(sa.insert(TableArrInstances).values(
            id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1,
            path_mappings=json.dumps(mappings)))
        world.db.execute(sa.insert(TableMovies).values(
            id=73, arr_instance_id=42, radarrId=9000, title='Film', path=stored_arr_path, tmdbId='73'))
        if old_stem_only:
            world.db.execute(sa.insert(TableMovies).values(
                id=74, arr_instance_id=42, radarrId=9001, title='Unrelated', path=str(unrelated), tmdbId='74'))

    if change == 'arr owner after prepare' or shared:
        publication = indexer.source_item_publication

        @contextmanager
        def claim_at_registry_lock(source, **kwargs):
            claim()
            realpath = os.path.realpath

            def watch_realpath(path, **options):
                if path == str(unrelated):
                    unrelated_resolutions.append(path)
                return realpath(path, **options)

            with monkeypatch.context() as watch:
                if old_stem_only:
                    watch.setattr(os.path, 'realpath', watch_realpath)
                with publication(source, **kwargs) as guarded:
                    yield guarded

        monkeypatch.setattr(indexer, 'source_item_publication', claim_at_registry_lock)

    def provider(videos, **kwargs):
        if change == 'subtitle':
            original.write_bytes(b'Manual edit')
        elif change == 'monitoring':
            repository.update_item(context.item_id, world.db, monitored=0)
        elif change == 'other destination':
            destination.write_bytes(b'Manual replacement')
        elif change == 'arr owner':
            claim()
        elif change == 'history score':
            history = repository.History
            world.db.execute(history.update().where(history.c.id == first).values(score=90))
        elif change == 'history superseded':
            _history(world.db, world.source_id, context.item_id, media, original, language='en:hi', action=2)
        return {next(iter(videos)): [pipeline.FakeSubtitle()]}

    download_attempt = Mock(side_effect=provider)
    monkeypatch.setattr(download, 'download_best_subtitles', download_attempt)
    upgrade.upgrade_source_subtitles(job_id=10)
    download_attempt.assert_called_once()
    if shared:
        assert world.db.scalar(sa.select(TableMovies.path).where(TableMovies.id == 73)) == stored_arr_path
    assert not unrelated_resolutions
    expected_history = [(first, 1, None)]
    if change == 'history superseded':
        expected_history.append((first + 1, 2, None))
    assert record_rows(world.db) == expected_history
    assert original.read_bytes() == (b'Manual edit' if change == 'subtitle' else b'Old subtitle')
    assert not world.published
    if change == 'other destination':
        assert destination.read_bytes() == b'Manual replacement'
    elif destination != original:
        assert not destination.exists()


def test_statistics_opt_in_includes_source_acquisitions_filters_and_retained_rows(client, arr_library, tmp_path):
    from app.database import TableHistoryMovie
    from media_sources import repository
    from subliminal_patch.score import MAX_SCORES

    now = datetime.now() - timedelta(hours=12)
    arr_library.execute(sa.update(TableHistoryMovie).values(timestamp=now))
    stats = '/api/history/stats?timeFrame=week'
    metrics = '/api/history/metrics?timeFrame=week'
    providers = '/api/providers?history=true'
    languages = '/api/system/languages?history=true'
    requests = [stats, metrics, providers, languages]
    plain = {url: client.get(url, headers=HEADERS).data for url in requests}
    for url in requests:
        assert client.get(url + '&sources=1', headers=HEADERS).data == plain[url]

    source, library, root = add_source(arr_library, tmp_path, profile_id=1)
    media = video(root / 'Film.mkv')
    item, subtitle = _item(arr_library, source, library, media)
    for kind, action, score in [('movie', 1, MAX_SCORES['movie'] // 2),
                                ('episode', 3, MAX_SCORES['episode'] // 2), ('sports_event', 4, 100)]:
        _history(arr_library, source, item, media, subtitle, item_kind=kind, action=action, score=score,
                 score_out_of=None, language='hu:hi', provider='source-provider', timestamp=now)
    repository.insert_blacklist(arr_library, item_id=item, source_id=source, item_kind='movie',
                                provider='source-provider', subs_id='release-1', language='hu:hi', timestamp=now)
    repository.delete_source(source, arr_library)

    for url in requests:
        assert client.get(url, headers=HEADERS).data == plain[url]
    assert client.get(stats + '&sources=false', headers=HEADERS).data == plain[stats]
    chart = client.get(stats + '&sources=1&language=hu', headers=HEADERS).get_json()
    assert chart['sources'] == [dict(row, count=2 if row['date'] == now.date().isoformat() else 0)
                                for row in chart['series']]
    plain_chart = client.get(stats, headers=HEADERS).get_json()
    combined_chart = client.get(stats + '&sources=1', headers=HEADERS).get_json()
    assert {key: combined_chart[key] for key in plain_chart} == plain_chart

    combined = client.get(metrics + '&sources=1', headers=HEADERS).get_json()
    assert combined['totals']['downloads'] == 3
    assert combined['totals']['movies'] == 1
    assert combined['totals']['sources'] == 2
    assert combined['totals']['peakCount'] == 3
    filtered_url = metrics + '&sources=1&provider=source-provider&language=hu'
    filtered = client.get(filtered_url, headers=HEADERS).get_json()
    assert filtered['byProvider'] == [{'provider': 'source-provider', 'count': 2, 'avgScorePct': 50.0}]
    assert filtered['providerReliability'] == [
        {'provider': 'source-provider', 'downloads': 2, 'blacklisted': 1, 'ratePct': 50.0}]
    assert filtered['scoreHistogram'][5]['count'] == 2
    assert client.get(filtered_url + '&action=3', headers=HEADERS).get_json()['totals']['downloads'] == 1
    assert 'source-provider' in [row['name'] for row in
                                 client.get(providers + '&sources=1', headers=HEADERS).get_json()['data']]
    assert 'hu' in [row['code2'] for row in client.get(languages + '&sources=1', headers=HEADERS).get_json()]
