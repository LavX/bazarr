"""Source upgrades and opt-in history statistics on SQLite and PostgreSQL."""
import json
from datetime import datetime, timedelta
from pathlib import Path
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


@pytest.mark.parametrize('initial_variant', ['en', 'en:hi'])
def test_scheduled_job_replaces_source_file_and_preserves_original_history(world, monkeypatch, initial_variant):
    from app.config import settings
    from media_sources import repository
    from media_sources.history import get_upgradable_source_subtitles
    from media_sources.indexer import index_item
    from subtitles import upgrade

    context = pipeline._movie(world)
    target = Path(context.path).with_suffix('.en.srt')
    original = target if initial_variant == 'en' else Path(context.path).with_suffix('.en.hi.srt')
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


def record_rows(db):
    from media_sources import repository

    table = repository.History.c
    return db.execute(sa.select(table.id, table.action, table.upgradedFromId).order_by(table.id)).all()


@pytest.mark.parametrize('failure_step', ['remove old', 'publish new'])
def test_failed_source_replacement_leaves_upgrade_retryable(world, monkeypatch, failure_step):
    from media_sources.history import get_upgradable_source_subtitles
    from media_sources.indexer import index_item
    from subtitles import download, upgrade

    context = pipeline._movie(world)
    media = Path(context.path)
    original = media.with_suffix('.en.hi.srt')
    destination = media.with_suffix('.en.srt')
    original.write_bytes(b'Old subtitle')
    index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original, language='en:hi')
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())
    pipeline._download(monkeypatch)
    remove = download.os.remove
    replace = download.os.replace
    failures = []

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
        else:
            failure.setattr(download.os, 'replace', fail_destination_publication)
        upgrade.upgrade_source_subtitles(job_id=10)

    assert failures == [failure_step]
    assert record_rows(world.db) == [(first, 1, None)]
    assert original.read_bytes() == b'Old subtitle'
    assert not destination.exists()
    assert not list(media.parent.glob('.bazarr-replaced-*'))
    assert not world.published
    assert get_upgradable_source_subtitles() == {first: None}

    upgrade.upgrade_source_subtitles(job_id=11)
    assert record_rows(world.db) == [(first, 1, None), (first + 1, 3, first)]
    assert destination.read_bytes() == pipeline.CONTENT
    assert not original.exists()


@pytest.mark.parametrize('change', [
    'subtitle', 'monitoring', 'other destination', 'arr owner', 'arr owner after prepare',
    'shared Arr owner after prepare', 'mapped shared Arr owner after prepare', 'history score', 'history superseded',
])
def test_upgrade_preserves_files_when_eligibility_changes_during_download(world, monkeypatch, change):
    from app.config import settings
    from app.database import TableArrInstances, TableMovies
    from media_sources import indexer, repository
    from media_sources.indexer import index_item
    from subtitles import download, upgrade

    context = pipeline._movie(world)
    media = Path(context.path)
    shared = 'shared Arr' in change
    mapped = change == 'mapped shared Arr owner after prepare'
    folder = pipeline._custom_folder(world, monkeypatch, world.tmp_path) if shared else media.parent
    destination = folder / media.with_suffix('.en.srt').name
    original = destination if mapped else folder / media.with_suffix('.en.hi.srt').name
    arr_path = video(world.tmp_path / 'arr' / media.name) if shared else media
    stored_arr_path = '/remote/Arr Movie.mkv' if mapped else str(arr_path)
    mappings = [[stored_arr_path, str(arr_path)]] if mapped else []
    original.write_bytes(b'Old subtitle')
    index_item(context.item_id)
    first = _history(world.db, world.source_id, context.item_id, media, original,
                     language='en' if mapped else 'en:hi')
    monkeypatch.setattr(upgrade, 'get_providers', lambda: ['fake'])
    monkeypatch.setattr(upgrade, 'jobs_queue', Mock())

    def claim():
        monkeypatch.setattr(settings.general, 'use_radarr', True)
        world.db.execute(sa.insert(TableArrInstances).values(
            id=42, kind='radarr', name='Cinema', stable_key='cinema', port=9800, enabled=1,
            path_mappings=json.dumps(mappings)))
        world.db.execute(sa.insert(TableMovies).values(
            id=73, arr_instance_id=42, radarrId=9000, title='Film', path=stored_arr_path, tmdbId='73'))

    if change == 'arr owner after prepare' or shared:
        publication = indexer.source_item_publication

        def claim_at_registry_lock(source, **kwargs):
            claim()
            return publication(source, **kwargs)

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
        assert world.db.scalar(sa.select(TableMovies.path)) == stored_arr_path
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
