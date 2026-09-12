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
from test_sportarr_manual_toolbox import sports_toolbox, indexed_library, migration_engine  # noqa: F401
from test_sportarr_notifications import sports_refresh_targets  # noqa: F401


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


def test_scan_all_runs_the_whole_library_once_per_owner(sports_library, schema_session):
    """Scan All must be library-wide per owner, not limited to the loaded rows.

    The wanted page holds one server page at a time, and the batch scan-disk
    arm mirrors the upgrade arm: one representative sports row per owner
    triggers the owner's whole-library rescan, so an event sitting on the
    second wanted page is indexed even though the client never sent it. The
    representative row carries no media id, which is what marks it as the
    owner-wide shape rather than a league-scoped selection.
    """
    from app.database import TableSportsEvents
    from subtitles.mass_operations import _process_media_action

    # Event 62 belongs to the same owner but sits on a later wanted page, so
    # it is never part of the Scan All payload.
    schema_session.add(TableSportsEvents(
        id=62, arr_instance_id=42, league_id=51, sportarrEventId=19, file_id=72,
        path='/remote/sports/highlights.mkv', title='Highlights'))
    schema_session.commit()

    result = None
    with patch('subtitles.indexer.sports.sports_full_scan_subtitles') as full_scan, \
            patch('subtitles.mass_operations._scan_sports',
                  side_effect=AssertionError('per-row scan is no longer used')):
        result = _process_media_action(
            [{'type': 'sports', 'arr_instance_id': 42}],
            'scan-disk', job_id=1)

    assert [call.kwargs['arr_instance_id'] for call in full_scan.call_args_list] == [42]
    assert result == {'queued': 1, 'skipped': 0, 'errors': []}
    # A Scan All that only held the first page must reach the owner's other
    # events, which is what delegating to the whole-library rescan buys.
    assert full_scan.call_args.kwargs['job_id'] == 1


def test_scan_all_deduplicates_representative_rows_from_one_owner(sports_library):
    """Duplicated owner-wide rows from one Sportarr scan that library once."""
    from subtitles.mass_operations import _process_media_action

    items = [
        {'type': 'sports', 'arr_instance_id': 42},
        {'type': 'sports', 'arr_instance_id': 42},
        {'type': 'sportsLeague', 'arr_instance_id': 42},
        {'type': 'sports', 'arr_instance_id': 43},
    ]
    with patch('subtitles.indexer.sports.sports_full_scan_subtitles') as full_scan:
        result = _process_media_action(items, 'scan-disk', job_id=1)

    assert [call.kwargs['arr_instance_id'] for call in full_scan.call_args_list] == [42, 43]
    assert result == {'queued': 2, 'skipped': 0, 'errors': []}


def test_scan_disk_mixed_batches_still_scan_selected_series_and_movies(sports_library):
    """The owner-wide sports arm must not swallow the other media types."""
    from subtitles.mass_operations import _process_media_action

    with patch('subtitles.indexer.sports.sports_full_scan_subtitles') as full_scan, \
            patch('subtitles.mass_operations.series_scan_subtitles') as series_scan, \
            patch('subtitles.mass_operations.movies_scan_subtitles') as movie_scan:
        result = _process_media_action(
            [
                {'type': 'sports', 'arr_instance_id': 42},
                {'type': 'series', 'sonarrSeriesId': 5, 'arr_instance_id': 9},
                {'type': 'movie', 'radarrId': 4, 'arr_instance_id': 9},
            ],
            'scan-disk', job_id=1)

    assert [call.kwargs['arr_instance_id'] for call in full_scan.call_args_list] == [42]
    assert [call.args for call in series_scan.call_args_list] == [(5,)]
    assert [call.args for call in movie_scan.call_args_list] == [(4,)]
    assert result == {'queued': 3, 'skipped': 0, 'errors': []}


@pytest.mark.parametrize("item", [
    {'type': 'sportsLeague', 'sportsLeagueId': 51, 'arr_instance_id': 42},
    {'type': 'sports', 'sportsEventId': 61, 'arr_instance_id': 42},
])
def test_scan_disk_rows_with_a_media_id_stay_selection_scoped(sports_library, item):
    """Scan Disk from the league toolbar and the events table sends the
    selected league (or event) with its media id. Those rows must re-index
    exactly that selection through _scan_sports, not silently widen into the
    owner's whole-library rescan the way the wanted page's representative
    Scan All rows do.
    """
    from subtitles.mass_operations import _process_media_action

    with patch('subtitles.mass_operations._scan_sports') as scan, \
            patch('subtitles.indexer.sports.sports_full_scan_subtitles',
                  side_effect=AssertionError('a media-scoped row must not widen')):
        result = _process_media_action([item], 'scan-disk', job_id=1)

    assert scan.call_args.args[0] == item
    assert result == {'queued': 1, 'skipped': 0, 'errors': []}


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


def test_a_disabled_owner_is_left_out_of_the_library_sweep(sports_library, schema_session):
    """The scheduled Mass Sync runs with no items, so it sweeps everything.

    A sports path mapping has no global fallback: resolving one against a
    disabled owner raises, and that exception would come out of the collector
    and take the whole scheduled run with it, not just the sports part.
    """
    from app.database import TableArrInstances
    from subtitles.mass_operations import (_collect_sports,
                                           _sports_event_ids_for_leagues)

    schema_session.get(TableArrInstances, 42).enabled = 0
    schema_session.commit()

    items, skipped = _collect_sports()
    assert items == []
    assert skipped == 0
    assert _sports_event_ids_for_leagues([51], {}) == {}


def test_the_library_sweep_is_gated_on_the_master_toggle():
    """Same shape as the two native media types, which gate on use_sonarr and
    use_radarr, so turning Sportarr off stops the scheduled sync touching it."""
    import inspect

    from subtitles import mass_operations

    source = inspect.getsource(mass_operations._collect_subtitle_items)
    assert "(items is None and settings.general.use_sportarr) or sports_ids" in source


def test_the_scheduled_mass_sync_runs_the_shared_entry_point():
    """It calls mass_batch_operation with no items, which is library mode, so
    the collector's own gate is what decides whether sports take part."""
    import inspect

    from app import scheduler

    source = inspect.getsource(scheduler.Scheduler._Scheduler__mass_sync_task)
    assert "mass_batch_operation" in source
    assert "id='mass_sync_subtitles'" in source


# --------------------------------------------------------------------------
# Mass translate from an embedded track inside a sports container.
# --------------------------------------------------------------------------

def _embedded_event(schema_session, event_id=62, file_id=72, title='Embedded',
                    subtitles="[['en', None, None]]"):
    from app.database import TableSportsEvents

    schema_session.add(TableSportsEvents(
        id=event_id, arr_instance_id=42, league_id=51,
        sportarrEventId=event_id, file_id=file_id,
        path='/remote/sports/embedded.mkv', title=title,
        audio_language='[]', subtitles=subtitles,
        missing_subtitles='[]', failedAttempts='[]'))
    schema_session.commit()


def _translate_settings(monkeypatch):
    from subtitles import mass_operations
    from unittest.mock import MagicMock

    mock_settings = MagicMock()
    mock_settings.subsync.max_offset_seconds = 60
    mock_settings.subsync.gss = True
    mock_settings.subsync.no_fix_framerate = True
    mock_settings.general.use_embedded_subs = True
    monkeypatch.setattr(mass_operations, 'settings', mock_settings)
    return mock_settings


def test_an_embedded_track_becomes_a_sports_translate_source(
        sports_library, schema_session, monkeypatch):
    """The indexer records an in-container track as [language, None, None],
    and the collector used to drop every pathless entry, so a release whose
    only subtitles are inside the container could never be mass-translated."""
    _embedded_event(schema_session)
    _translate_settings(monkeypatch)

    from subtitles.mass_operations import _collect_sports

    items, skipped = _collect_sports(
        event_ids=[62], sports_instance={62: {42}}, action='translate',
        target_lang='nl', source_lang='en')

    assert skipped == 0
    assert len(items) == 1
    item = items[0]
    assert item['embedded'] is True
    assert item['srt_path'] is None
    assert item['srt_lang'] == 'en'
    assert item['video_path'] == str(sports_library / "embedded.mkv")
    assert item['sports_event_id'] == 62
    assert item['arr_instance_id'] == 42
    assert item['metadata'] is None


def test_sync_and_mods_never_see_an_embedded_sports_track(
        sports_library, schema_session, monkeypatch):
    """There is no file to sync and no useful one to produce, so an embedded
    track is not a candidate. It must not reach the skipped tally either."""
    _embedded_event(schema_session)
    _translate_settings(monkeypatch)

    from subtitles.mass_operations import _collect_sports

    items, skipped = _collect_sports(
        event_ids=[62], sports_instance={62: {42}}, action='sync')
    assert items == []
    assert skipped == 0

    items, skipped = _collect_sports(
        event_ids=[62], sports_instance={62: {42}}, action='remove_HI')
    assert items == []
    assert skipped == 0


def test_embedded_sports_tracks_are_ignored_when_turned_off(
        sports_library, schema_session, monkeypatch):
    """The rows outlive the setting until the next index, so consuming them
    has to check it too."""
    _embedded_event(schema_session)
    mock_settings = _translate_settings(monkeypatch)
    mock_settings.general.use_embedded_subs = False

    from subtitles.mass_operations import _collect_sports

    items, skipped = _collect_sports(
        event_ids=[62], sports_instance={62: {42}}, action='translate',
        target_lang='nl', source_lang='en')
    assert items == []
    assert skipped == 0


def test_a_real_sports_file_wins_over_the_embedded_track_of_the_same_language(
        sports_library, schema_session, monkeypatch):
    """Queueing both would run the same translation twice into one output."""
    (sports_library / "embedded.en.srt").write_text("1\n")
    _embedded_event(schema_session,
                    subtitles="[['en', None, None], "
                              "['en', '/remote/sports/embedded.en.srt', 100]]")
    _translate_settings(monkeypatch)

    from subtitles.mass_operations import _collect_sports

    items, _skipped = _collect_sports(
        event_ids=[62], sports_instance={62: {42}}, action='translate',
        target_lang='nl', source_lang='en')

    assert len(items) == 1
    assert items[0]['srt_path'] == str(sports_library / "embedded.en.srt")
    assert not items[0]['embedded']


def test_an_embedded_sports_item_extracts_the_track_when_it_runs():
    """Extraction happens when the item runs, not when the batch was
    collected, and the sports arm of the shared extraction helper is what the
    event's owner resolves through."""
    from subtitles.mass_operations import _process_subtitle_item

    item = {
        'video_path': '/mapped/embedded.mkv', 'srt_path': None,
        'srt_lang': 'en', 'embedded': True, 'forced': False, 'hi': False,
        'sonarr_series_id': None, 'sonarr_episode_id': None, 'radarr_id': None,
        'sports_event_id': 62, 'arr_instance_id': 42, 'metadata': None,
    }
    operation = MagicMock()
    with patch('subtitles.tools.translate.batch.extract_embedded_subtitle',
               return_value='/config/extracted_subs/embedded.en.srt') as extract, \
         patch('sportarr.profile_hooks.manual_translation_operation',
               return_value=operation), \
         patch('subtitles.tools.translate.main.translate_subtitles_file') as translate:
        assert _process_subtitle_item(
            item, 'translate', {'to_lang': 'en'}, job_id=1) is True

    assert extract.call_args.args[:3] == ('/mapped/embedded.mkv', 'en', 'sports')
    assert extract.call_args.kwargs['arr_instance_id'] == 42
    kwargs = translate.call_args.kwargs
    assert kwargs['media_type'] == 'sports'
    assert kwargs['metadata'] is None
    assert kwargs['sports_operation'] is operation


def test_an_unextractable_sports_track_skips_the_item_not_the_batch():
    """Usually a bitmap track (PGS, VobSub). That is this item's problem; the
    batch carries on."""
    from subtitles.mass_operations import _process_subtitle_item

    item = {
        'video_path': '/mapped/embedded.mkv', 'srt_path': None,
        'srt_lang': 'en', 'embedded': True, 'forced': False, 'hi': False,
        'sonarr_series_id': None, 'sonarr_episode_id': None, 'radarr_id': None,
        'sports_event_id': 62, 'arr_instance_id': 42, 'metadata': None,
    }
    with patch('subtitles.tools.translate.batch.extract_embedded_subtitle',
               return_value=None), \
         patch('sportarr.profile_hooks.manual_translation_operation'), \
         patch('subtitles.tools.translate.main.translate_subtitles_file') as translate:
        assert _process_subtitle_item(
            item, 'translate', {'to_lang': 'en'}, job_id=1) is False
    translate.assert_not_called()


def test_mass_sports_mod_publishes_the_sports_owner(sports_toolbox, monkeypatch):  # noqa: F811
    from subtitles import mass_operations
    from subtitles.tools import mods

    _, session, folder = sports_toolbox
    monkeypatch.setattr(mass_operations, 'database', session)
    subtitle = folder / '1' / 'event.en.hi.srt'
    subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n<i>Race subtitle</i>\n')
    published = []
    monkeypatch.setattr(mods, 'publication_callback',
                        lambda kind, path, operation, owner: lambda output:
                        published.append((kind, path, operation, owner, str(output))))
    items, _ = mass_operations._collect_sports(event_ids=[61], sports_instance={61: {1}})
    assert mass_operations._process_subtitle_item(items[0], 'remove_tags', {}, 99)
    assert published == [('sports', str(folder / '1' / 'event.mkv'), 'edit', 1, str(subtitle))]
    assert '<i>' not in subtitle.read_text()


def test_mass_remove_hi_reindexes_the_renamed_owned_subtitle(sports_toolbox, monkeypatch):  # noqa: F811
    import ast
    from app.database import TableSportsEvents
    from subtitles import mass_operations

    endpoint, session, folder = sports_toolbox
    monkeypatch.setattr(mass_operations, 'database', session)
    items, _ = mass_operations._collect_sports(event_ids=[61], sports_instance={61: {1}})
    result = mass_operations._process_subtitle_item(items[0], 'remove_HI', {}, 99)
    assert result is True
    assert (folder / '1' / 'event.en.srt').exists()
    assert not (folder / '1' / 'event.en.hi.srt').exists()
    session.expire_all()
    entries = ast.literal_eval(session.get(TableSportsEvents, 61).subtitles)
    assert ['en', '/sports/event.en.srt'] in [entry[:2] for entry in entries]
    assert not any(entry[0] == 'en:hi' for entry in entries)
    assert session.get(TableSportsEvents, 62).subtitles == '[]'


def test_mass_invalid_sports_mod_is_not_counted_as_success(sports_toolbox, monkeypatch):  # noqa: F811
    from subtitles import mass_operations

    endpoint, session, folder = sports_toolbox
    monkeypatch.setattr(mass_operations, 'database', session)
    items, _ = mass_operations._collect_sports(event_ids=[61], sports_instance={61: {1}})
    (folder / '1' / 'event.en.hi.srt').write_bytes(b'not a subtitle')
    assert mass_operations._process_subtitle_item(items[0], 'remove_tags', {}, 99) is False


@pytest.mark.parametrize('end', ['success', 'invalid', 'cancelled'])
def test_mass_sports_mod_refreshes_once_for_successful_files(sports_toolbox, sports_refresh_targets, monkeypatch, end):  # noqa: F811
    from app.jobs_queue import JobCancelled
    from subtitles import mass_operations
    from subtitles.indexer import sports as indexer

    endpoint, session, folder = sports_toolbox
    refreshed, _ = sports_refresh_targets
    monkeypatch.setattr(mass_operations, 'database', session)
    monkeypatch.setattr(mass_operations, 'event_stream', lambda **kwargs: None)
    indexer.store_subtitles_sports(62, 2)
    sibling = folder / '2' / 'event.de.forced.srt'
    if end == 'invalid':
        sibling.write_bytes(b'not a subtitle')
    original = sibling.read_bytes()

    def progress(*args, **kwargs):
        if end == 'cancelled' and kwargs.get('progress_value') == 1:
            raise JobCancelled('stopped after first publication')

    monkeypatch.setattr(mass_operations.jobs_queue, 'update_job_progress', progress)
    args = dict(items=[{'type': 'sports', 'sportsEventId': 61, 'arr_instance_id': 1},
                       {'type': 'sports', 'sportsEventId': 62, 'arr_instance_id': 2}],
                action='remove_HI', job_id=99)
    if end == 'cancelled':
        with pytest.raises(JobCancelled):
            mass_operations.mass_batch_operation(**args)
    else:
        result = mass_operations.mass_batch_operation(**args)
        assert result == {'queued': 2 if end == 'success' else 1,
                          'skipped': 0 if end == 'success' else 1, 'errors': []}
    assert (folder / '1' / 'event.en.srt').exists()
    assert not (folder / '1' / 'event.en.hi.srt').exists()
    if end != 'success':
        assert sibling.read_bytes() == original
    assert refreshed.count(('plex', 'Sports')) == 1
    assert refreshed.count(('plex', 'Other Sports')) == 1
    assert refreshed.count(('jellyfin', 'sports-id')) == 1
    assert refreshed.count(('jellyfin', 'other-sports-id')) == 1
    assert refreshed.count(('sportarr', 1)) == 1
    assert refreshed.count(('sportarr', 2)) == (1 if end == 'success' else 0)


@pytest.mark.parametrize('when', ['before_write', 'after_write'])
def test_mass_mod_cancellation_preserves_only_completed_publications(
        sports_toolbox, sports_refresh_targets, monkeypatch, when):  # noqa: F811
    import ast
    from app.database import TableSportsEvents
    from app.jobs_queue import JobCancelled
    from subtitles import mass_operations
    from subtitles.tools import mods

    _, session, folder = sports_toolbox
    refreshed, _ = sports_refresh_targets
    monkeypatch.setattr(mass_operations, 'database', session)
    monkeypatch.setattr(mass_operations, 'event_stream', lambda **kwargs: None)
    stopped = False

    def progress(*args, **kwargs):
        if stopped:
            raise JobCancelled('mod cancelled')

    def publication(*args):
        def published(output):
            nonlocal stopped
            if when == 'after_write':
                stopped = True
        return published

    transform = mods.Subtitle.get_modified_content

    def transformed(subtitle, **kwargs):
        nonlocal stopped
        content = transform(subtitle, **kwargs)
        if when == 'before_write':
            stopped = True
        return content

    monkeypatch.setattr(mass_operations.jobs_queue, 'update_job_progress', progress)
    monkeypatch.setattr(mods, 'publication_callback', publication)
    monkeypatch.setattr(mods.Subtitle, 'get_modified_content', transformed)
    source = folder / '1' / 'event.en.hi.srt'
    original = source.read_bytes()
    with pytest.raises(JobCancelled):
        mass_operations.mass_batch_operation(
            items=[{'type': 'sports', 'sportsEventId': 61, 'arr_instance_id': 1}],
            action='remove_HI', job_id=99)
    session.expire_all()
    indexed = ast.literal_eval(session.get(TableSportsEvents, 61).subtitles)
    if when == 'before_write':
        assert source.read_bytes() == original
        assert not (folder / '1' / 'event.en.srt').exists()
        assert refreshed == []
    else:
        assert not source.exists()
        assert (folder / '1' / 'event.en.srt').exists()
        assert ['en', '/sports/event.en.srt'] in [entry[:2] for entry in indexed]
        assert refreshed.count(('plex', 'Sports')) == 1
        assert refreshed.count(('jellyfin', 'sports-id')) == 1
        assert refreshed.count(('sportarr', 1)) == 1
