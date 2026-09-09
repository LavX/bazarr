# coding=utf-8
"""Batch and mass operations on sports events.

The batch endpoint dropped anything that was not an episode, a movie or a
series before it reached the collector, so selecting sports rows and choosing
Sync, Translate, a subtitle mod, Scan Disk, Search Missing or Upgrade did
nothing at all and reported nothing wrong. Everything below is the sports half
of machinery that already existed for the other two media types.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def test_the_endpoint_accepts_sports_items():
    import inspect

    from api.subtitles import batch

    source = inspect.getsource(batch)
    assert "'sports', 'sportsLeague'" in source
    assert "'sportsEventId', 'sportsLeagueId'" in source


@pytest.fixture
def sports_library(schema_session, monkeypatch, tmp_path):
    from app.database import TableArrInstances, TableSportsEvents, TableSportsLeagues
    from subtitles import mass_operations
    from utilities import path_mappings

    library = tmp_path / "sports"
    library.mkdir()
    (library / "race.mkv").write_bytes(b"video")
    (library / "race.hu.srt").write_text("1\n")
    (library / "race.en.combined-hu.srt").write_text("1\n")

    schema_session.add(TableArrInstances(id=42, kind='sportarr', stable_key='sportarr-1',
                                         name='Sportarr', enabled=1, port=1867, api_key='k'))
    schema_session.add(TableSportsLeagues(id=51, arr_instance_id=42, sportarrLeagueId=7,
                                          title='Formula 1', path='/remote/sports'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=61, arr_instance_id=42, league_id=51, sportarrEventId=9, file_id=71,
        path='/remote/sports/race.mkv', title='Race',
        subtitles=("[['hu', '/remote/sports/race.hu.srt'], "
                   "['en:combined-hu', '/remote/sports/race.en.combined-hu.srt']]")))
    schema_session.commit()

    monkeypatch.setattr(mass_operations, 'database', schema_session)
    monkeypatch.setattr(path_mappings, '_sports_instance_mapping',
                        lambda _id: [['/remote/sports', str(library)]])
    return library


def test_a_selected_event_collects_its_subtitles(sports_library):
    from subtitles.mass_operations import _collect_sports

    items, skipped = _collect_sports(event_ids=[61], sports_instance={61: {42}})

    # The combined artifact is not a sync source: syncing a composed file has
    # no meaning and it would overwrite a build output.
    assert [item['srt_lang'] for item in items] == ['hu']
    assert skipped == 1
    item = items[0]
    assert item['srt_path'] == str(sports_library / "race.hu.srt")
    assert item['video_path'] == str(sports_library / "race.mkv")
    assert item['sports_event_id'] == 61
    assert item['arr_instance_id'] == 42
    # A sports translation carries no native media metadata, and
    # translate_subtitles_file refuses one that does.
    assert item['metadata'] is None
    assert item['radarr_id'] is None and item['sonarr_episode_id'] is None


def test_another_owners_selection_collects_nothing(sports_library):
    from subtitles.mass_operations import _collect_sports

    items, _ = _collect_sports(event_ids=[61], sports_instance={61: {43}})
    assert items == []


def test_a_league_selection_expands_to_its_events(sports_library):
    from subtitles.mass_operations import _sports_event_ids_for_leagues

    assert _sports_event_ids_for_leagues([51], {51: {42}}) == {61: {42}}
    # And stays inside the owner that was asked for.
    assert _sports_event_ids_for_leagues([51], {51: {43}}) == {}


def test_a_missing_file_is_skipped_not_queued(sports_library):
    from subtitles.mass_operations import _collect_sports

    (sports_library / "race.hu.srt").unlink()
    items, skipped = _collect_sports(event_ids=[61], sports_instance={61: {42}})
    assert items == []
    assert skipped == 2


def test_syncing_publishes_under_the_owned_boundary():
    """A batch item collected before a resync replaced the recording must not
    write over the new file, which is what the guard pins."""
    from subtitles.mass_operations import _process_subtitle_item

    item = {
        'video_path': '/mapped/race.mkv', 'srt_path': '/mapped/race.hu.srt',
        'srt_lang': 'hu', 'forced': False, 'hi': False,
        'sonarr_series_id': None, 'sonarr_episode_id': None, 'radarr_id': None,
        'sports_event_id': 61, 'arr_instance_id': 42,
        'max_offset_seconds': '60', 'no_fix_framerate': True, 'gss': True,
    }
    guard = SimpleNamespace()
    validate = SimpleNamespace()
    context = SimpleNamespace()

    class _Operation:
        def __enter__(self):
            return context, validate, guard, '/guarded/race.mkv'

        def __exit__(self, *exc):
            return False

    with patch('sportarr.subtitles.sports_manual_operation', return_value=_Operation()), \
         patch('subtitles.mass_operations.sync_subtitles') as sync:
        _process_subtitle_item(item, 'sync', {}, job_id=1)

    kwargs = sync.call_args.kwargs
    assert kwargs['context'] is context
    assert kwargs['validate'] is validate
    assert kwargs['publication_guard'] is guard
    # The guarded path, not the collector's: the operation resolves the file
    # that is on disk right now.
    assert kwargs['video_path'] == '/guarded/race.mkv'
    assert kwargs['arr_instance_id'] == 42


def test_episodes_and_movies_still_sync_without_a_guard():
    from subtitles.mass_operations import _process_subtitle_item

    item = {
        'video_path': '/v.mkv', 'srt_path': '/v.en.srt', 'srt_lang': 'en',
        'forced': False, 'hi': False, 'sonarr_series_id': 10,
        'sonarr_episode_id': 1, 'radarr_id': None, 'arr_instance_id': 3,
        'max_offset_seconds': '60', 'no_fix_framerate': True, 'gss': True,
    }
    with patch('subtitles.mass_operations.sync_subtitles') as sync:
        _process_subtitle_item(item, 'sync', {}, job_id=1)
    assert 'publication_guard' not in sync.call_args.kwargs


def test_translating_binds_its_own_operation_per_item():
    """The guard pins one source, one destination and one file signature, so a
    batch-wide operation would authorise writes it never inspected."""
    from subtitles.mass_operations import _process_subtitle_item

    item = {
        'video_path': '/mapped/race.mkv', 'srt_path': '/mapped/race.hu.srt',
        'srt_lang': 'hu', 'forced': False, 'hi': False,
        'sonarr_series_id': None, 'sonarr_episode_id': None, 'radarr_id': None,
        'sports_event_id': 61, 'arr_instance_id': 42, 'metadata': None,
    }
    operation = MagicMock()
    with patch('sportarr.profile_hooks.manual_translation_operation',
               return_value=operation) as bind, \
         patch('subtitles.tools.translate.main.translate_subtitles_file') as translate:
        assert _process_subtitle_item(item, 'translate', {'to_lang': 'en'}, job_id=1) is True

    assert bind.call_args.args[:4] == (61, 42, '/mapped/race.hu.srt', 'en')
    kwargs = translate.call_args.kwargs
    assert kwargs['media_type'] == 'sports'
    assert kwargs['sports_operation'] is operation
    assert kwargs['metadata'] is None


def test_a_target_the_profile_does_not_want_skips_the_item_not_the_batch():
    from subtitles.mass_operations import _process_subtitle_item

    item = {
        'video_path': '/mapped/race.mkv', 'srt_path': '/mapped/race.hu.srt',
        'srt_lang': 'hu', 'forced': False, 'hi': False,
        'sonarr_series_id': None, 'sonarr_episode_id': None, 'radarr_id': None,
        'sports_event_id': 61, 'arr_instance_id': 42, 'metadata': None,
    }
    with patch('sportarr.profile_hooks.manual_translation_operation',
               side_effect=ValueError('not wanted')), \
         patch('subtitles.tools.translate.main.translate_subtitles_file') as translate:
        assert _process_subtitle_item(item, 'translate', {'to_lang': 'de'}, job_id=1) is False
    translate.assert_not_called()


def test_scan_disk_reindexes_an_event_and_a_whole_league(sports_library):
    from subtitles.mass_operations import _scan_sports

    with patch('subtitles.indexer.sports.store_subtitles_sports') as store:
        assert _scan_sports({'type': 'sports', 'sportsEventId': 61,
                             'arr_instance_id': 42}) is True
        assert store.call_args.args == (61, 42)

    with patch('subtitles.indexer.sports.store_subtitles_sports') as store:
        assert _scan_sports({'type': 'sportsLeague', 'sportsLeagueId': 51,
                             'arr_instance_id': 42}) is True
        assert store.call_args.args == (61, 42)


def test_an_unowned_sports_selection_is_skipped(sports_library):
    """A sports path mapping is always per instance with no global fallback, so
    acting without an owner cannot resolve a file at all."""
    from subtitles.mass_operations import _scan_sports, _search_sports

    assert _scan_sports({'type': 'sports', 'sportsEventId': 61}) is False
    assert _search_sports({'type': 'sports', 'sportsEventId': 61}, job_id=1) is False


def test_search_missing_runs_per_event_and_per_league():
    from subtitles.mass_operations import _search_sports

    with patch('sportarr.automatic.search_event') as search:
        assert _search_sports({'type': 'sports', 'sportsEventId': 61,
                               'arr_instance_id': 42}, job_id=7) is True
        assert search.call_args.args == (61, 42)

    with patch('sportarr.workflows.sports_download_subtitles') as download:
        assert _search_sports({'type': 'sportsLeague', 'sportsLeagueId': 51,
                               'arr_instance_id': 42}, job_id=7) is True
        assert download.call_args.args == (51, 42)


def test_upgrade_runs_once_per_owner_not_once_per_row():
    """The sports upgrade job builds its own candidate list from history and
    takes an instance, not a media filter, so five selected events from one
    Sportarr must not run it five times."""
    from subtitles.mass_operations import _process_media_action

    items = [
        {'type': 'sports', 'sportsEventId': 61, 'arr_instance_id': 42},
        {'type': 'sports', 'sportsEventId': 62, 'arr_instance_id': 42},
        {'type': 'sportsLeague', 'sportsLeagueId': 51, 'arr_instance_id': 43},
    ]
    with patch('subtitles.mass_operations.upgrade_sports_subtitles') as upgrade, \
         patch('subtitles.mass_operations.upgrade_episodes_subtitles'), \
         patch('subtitles.mass_operations.upgrade_movies_subtitles'):
        result = _process_media_action(items, 'upgrade', job_id=1)

    assert [c.kwargs['arr_instance_id'] for c in upgrade.call_args_list] == [42, 43]
    assert result['queued'] == 2
