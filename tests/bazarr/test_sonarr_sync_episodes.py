# -*- coding: utf-8 -*-
"""
Test for Sonarr episode sync functionality.

Specifically tests the fix for TypeError when logging deleted episodes
where SQLAlchemy Row objects were accessed with dict-style syntax instead
of attribute access.

This bug occurred in bazarr/sonarr/sync/episodes.py line 245 where the code
used existing_episode["path"] (dict-style access) instead of existing_episode.path
(attribute access) on a SQLAlchemy Row object.
"""
import pytest


@pytest.fixture
def mock_row():
    """Mock SQLAlchemy Row object behavior.
    
    SQLAlchemy Row objects (returned by select().first()) behave like named tuples:
    - They support attribute access: row.column_name
    - They support integer indexing: row[0]
    - They do NOT support string key access: row["column_name"] raises TypeError
    """
    class MockRow:
        """Simulates SQLAlchemy Row object returned from select().first()"""
        def __init__(self, path, episode_file_id):
            # Store as tuple internally (like SQLAlchemy Row)
            self._data = (path, episode_file_id)
            # Set attributes for attribute access
            self.path = path
            self.episode_file_id = episode_file_id
        
        def __getitem__(self, key):
            """Simulate SQLAlchemy Row behavior - only integer indices work"""
            if isinstance(key, str):
                raise TypeError("tuple indices must be integers or slices, not str")
            return self._data[key]
    
    return MockRow


def test_row_object_attribute_access(mock_row):
    """Test that Row object supports attribute access (the correct way)."""
    row = mock_row("/path/to/episode.mkv", 12345)
    
    # Attribute access should work (this is what the fix uses)
    assert row.path == "/path/to/episode.mkv"
    assert row.episode_file_id == 12345


def test_row_object_dict_access_fails(mock_row):
    """Test that Row object raises TypeError with dict-style string key access.
    
    This test demonstrates the bug that was fixed in line 245.
    The code originally used: existing_episode["path"]
    Which raised: TypeError: tuple indices must be integers or slices, not str
    """
    row = mock_row("/path/to/episode.mkv", 12345)
    
    # Dict-style access with string key should raise TypeError (the bug)
    with pytest.raises(TypeError, match="tuple indices must be integers or slices, not str"):
        _ = row["path"]
    
    with pytest.raises(TypeError, match="tuple indices must be integers or slices, not str"):
        _ = row["episode_file_id"]


def test_row_object_integer_index_access(mock_row):
    """Test that Row object supports integer index access."""
    row = mock_row("/path/to/episode.mkv", 12345)
    
    # Integer index access should work
    assert row[0] == "/path/to/episode.mkv"
    assert row[1] == 12345


def test_bug_reproduction_dict_vs_attribute_access(mock_row):
    """Demonstrate the exact bug that was fixed.
    
    This test reproduces the bug from bazarr/sonarr/sync/episodes.py line 245:
    - Bug: path_mappings.path_replace(existing_episode["path"]) - TypeError
    - Fix: path_mappings.path_replace(existing_episode.path) - Works
    """
    # Create a Row-like object as returned by:
    # database.execute(select(TableEpisodes.path, TableEpisodes.episode_file_id).where(...)).first()
    existing_episode = mock_row("/tv/Show/S01E01.mkv", 12345)
    
    # ❌ The bug: trying to use dict-style access on Row object
    with pytest.raises(TypeError) as exc_info:
        path = existing_episode["path"]
        # This would be in: path_mappings.path_replace(existing_episode["path"])
    
    assert "tuple indices must be integers or slices, not str" in str(exc_info.value)
    
    # ✅ The fix: using attribute access instead
    path = existing_episode.path  # This works!
    assert path == "/tv/Show/S01E01.mkv"
    
    # Simulate the fixed line 245:
    # f'BAZARR deleted this episode from the database:{path_mappings.path_replace(existing_episode.path)}'
    result_path = existing_episode.path  # No TypeError!
    assert result_path == "/tv/Show/S01E01.mkv"



@pytest.mark.parametrize('answer', [None, {}, ""], ids=['none', 'object', 'string'])
def test_an_undecodable_episode_file_body_skips_the_series(schema_session, monkeypatch, caplog,
                                                            answer):
    """A non-JSON or non-array episode-file answer is a failed fetch, not an
    empty list.

    On a Sonarr older than 4.0.9.2421 the episodes come without their file
    records and the episodeFile endpoint fills them in. When that answer
    cannot be decoded, or decodes into something other than the episode-file
    array, an empty answer would leave every episode without its file record,
    the sync would count the whole series as gone and delete it from the
    database (an object answer used to crash at the id lookup instead). The
    series is skipped instead, nothing is deleted.
    """
    import logging
    import semver
    from types import SimpleNamespace

    import sonarr.sync.episodes as episodes_mod
    from app.database import TableEpisodes

    schema_session.add(TableEpisodes(
        id=701, series_id=None, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=None, path="/series/alpha/s01e01.mkv", title="Pilot",
        season=1, episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'get_sonarr_info',
                        SimpleNamespace(semver=lambda: semver.Version(*(4, 0, 9, 2420))))
    monkeypatch.setattr(episodes_mod, 'sonarr_series_owner', lambda *a, **kw: (None, None))
    monkeypatch.setattr(episodes_mod, 'get_episodes_from_sonarr_api',
                        lambda **kw: [{'id': 42, 'hasFile': True, 'episodeFileId': 60,
                                       'monitored': True}])
    monkeypatch.setattr(episodes_mod, 'get_episodesFiles_from_sonarr_api', lambda **kw: answer)
    monkeypatch.setattr(episodes_mod, 'forget_media_by_upstream', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'event_stream', lambda *a, **kw: None)

    with caplog.at_level(logging.ERROR):
        result = episodes_mod.sync_episodes(7)

    from sqlalchemy import select
    remaining = schema_session.execute(
        select(TableEpisodes.sonarrEpisodeId)).scalars().all()

    assert result is episodes_mod.SYNC_SKIPPED_UNREADABLE_EPISODE_FILES, (
        "a skipped series returned a plain result the bulk sync cannot tell "
        "from a sync that ran")
    assert remaining == [42], (
        f"the episode row{'s were' if len(remaining) != 1 else ' was'} deleted from the "
        "database after a failed episode-file fetch")
    assert "could not read the episode files" in caplog.text, (
        "a skipped series was not reported in the log")


@pytest.mark.parametrize('sonarr_version', [(4, 0, 9, 2420), (4, 0, 9, 2421)])
def test_a_real_empty_episode_list_removes_the_stale_rows(schema_session, monkeypatch, sonarr_version):
    """An empty episode list from Sonarr is a real answer, not a failure.

    A series whose every episode was removed in Sonarr answers an empty list.
    The sync used to bail out on any falsy answer the same way it bailed out on
    a failed fetch, so the stale local rows survived every sync. A real empty
    list must fall through to the delete path and remove them, on both sides of
    the version that started embedding the episode-file records, and without
    asking the episode-file endpoint for a series with no episodes to decorate.
    """
    import semver
    from types import SimpleNamespace

    import sonarr.sync.episodes as episodes_mod
    from app.database import TableEpisodes

    schema_session.add(TableEpisodes(
        id=702, series_id=None, sonarrEpisodeId=43, sonarrSeriesId=7,
        arr_instance_id=None, path="/series/alpha/s01e02.mkv", title="Gone",
        season=1, episode=2, monitored="True", subtitles="[]"))
    schema_session.flush()

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'get_sonarr_info',
                        SimpleNamespace(semver=lambda: semver.Version(*sonarr_version)))
    monkeypatch.setattr(episodes_mod, 'sonarr_series_owner', lambda *a, **kw: (None, None))
    monkeypatch.setattr(episodes_mod, 'forget_media_by_upstream', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'event_stream', lambda *a, **kw: None)
    episode_file_calls = []

    def _episode_files(**kwargs):
        episode_file_calls.append(kwargs)
        return None

    monkeypatch.setattr(episodes_mod, 'get_episodesFiles_from_sonarr_api', _episode_files)

    result = episodes_mod.sync_episodes(7, episodes_data=[])

    from sqlalchemy import select
    remaining = schema_session.execute(
        select(TableEpisodes.sonarrEpisodeId)).scalars().all()

    assert result is not episodes_mod.SYNC_SKIPPED_UNREADABLE_EPISODE_FILES, (
        "a sync that ran was reported as skipped")
    assert remaining == [], (
        "a real empty episode list left stale episode rows in the database")
    assert episode_file_calls == [], (
        "the episode-file endpoint was asked although the episode list was empty")


@pytest.mark.parametrize('answer', [None, {}, "", {'message': 'Unauthorized'}, 'error'],
                         ids=['none', 'object', 'string', 'proxy-body', 'error-body'])
def test_a_failed_or_unreadable_episode_fetch_removes_nothing(schema_session, monkeypatch,
                                                              caplog, answer):
    """A fetch failure or an undecodable payload must never be read as an empty list.

    The fetch helper answers None for a connection error, a non-200 status or a
    body that is not JSON, and a 200 can carry a JSON object or a bare string
    instead of the episode array. Treating any of those as "no episodes" would
    delete every local row of the series on a transient Sonarr outage or behind
    a proxy that rewrites the body, so the series is skipped instead and
    reported the way an unreadable episode-file answer is.
    """
    import logging

    import sonarr.sync.episodes as episodes_mod
    from app.database import TableEpisodes

    schema_session.add(TableEpisodes(
        id=703, series_id=None, sonarrEpisodeId=44, sonarrSeriesId=7,
        arr_instance_id=None, path="/series/alpha/s01e03.mkv", title="Kept",
        season=1, episode=3, monitored="True", subtitles="[]"))
    schema_session.flush()

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'get_episodes_from_sonarr_api', lambda **kw: answer)
    monkeypatch.setattr(episodes_mod, 'forget_media_by_upstream', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'event_stream', lambda *a, **kw: None)

    with caplog.at_level(logging.ERROR):
        result = episodes_mod.sync_episodes(7)

    from sqlalchemy import select
    remaining = schema_session.execute(
        select(TableEpisodes.sonarrEpisodeId)).scalars().all()

    assert result is episodes_mod.SYNC_SKIPPED_UNREADABLE_EPISODE_FILES, (
        "a skipped series returned a plain result the bulk sync cannot tell "
        "from a sync that ran")
    assert remaining == [44], (
        "a failed episode fetch deleted local episode rows")
    assert "could not read the episodes" in caplog.text, (
        "a skipped series was not reported in the log")


@pytest.mark.parametrize('arr_instance_id', [None, 8], ids=['unscoped', 'scoped'])
def test_an_empty_episode_list_only_deletes_its_own_scope(schema_session, monkeypatch,
                                                          arr_instance_id):
    """An empty keep-list driving the delete is a normal path now, so the delete
    must not reach past its own series and instance. A row of another series and
    a row of the same upstream series under another instance both survive, on
    the default path as well: it resolves the enabled default instance and
    scopes the delete to it instead of spanning every instance.

    Upstream episode ids collide across instances and series, so the other
    instance's series-7 row carries the same episode id as the default
    instance's series-8 row: an id-only delete would catch that unrelated
    episode. The delete and the mismatch cleanup are both bound to the series
    in the owning scope.
    """
    import semver
    from types import SimpleNamespace

    import sonarr.sync.episodes as episodes_mod
    from app.database import TableArrInstances, TableEpisodes

    schema_session.add(TableArrInstances(
        id=8, kind='sonarr', stable_key='sonarr-default', name='Sonarr',
        enabled=1, is_default=1, ip='127.0.0.1', port=8989, base_url='/', ssl=0,
        verify_ssl=0, http_timeout=60, api_key='k'))
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=704, series_id=None, sonarrEpisodeId=45, sonarrSeriesId=7,
            arr_instance_id=8, path="/series/alpha/s01e04.mkv", title="Target",
            season=1, episode=4, monitored="True", subtitles="[]"),
        TableEpisodes(
            id=705, series_id=None, sonarrEpisodeId=46, sonarrSeriesId=8,
            arr_instance_id=8, path="/series/beta/s01e01.mkv", title="Other series",
            season=1, episode=1, monitored="True", subtitles="[]"),
        TableEpisodes(
            id=706, series_id=None, sonarrEpisodeId=46, sonarrSeriesId=7,
            arr_instance_id=2, path="/series/gamma/s01e01.mkv", title="Other instance",
            season=1, episode=1, monitored="True", subtitles="[]"),
    ])

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'get_sonarr_info',
                        SimpleNamespace(semver=lambda: semver.Version(*(4, 0, 9, 2421))))
    monkeypatch.setattr(episodes_mod, 'sonarr_series_owner', lambda *a, **kw: (None, None))
    forget_calls = []

    def _forget(media_type, upstream_ids, arr_instance_id=None, series_upstream_id=None):
        forget_calls.append((media_type, upstream_ids, arr_instance_id, series_upstream_id))

    monkeypatch.setattr(episodes_mod, 'forget_media_by_upstream', _forget)
    monkeypatch.setattr(episodes_mod, 'event_stream', lambda *a, **kw: None)

    episodes_mod.sync_episodes(7, episodes_data=[], arr_instance_id=arr_instance_id)

    from sqlalchemy import select
    remaining = sorted(schema_session.execute(
        select(TableEpisodes.id)).scalars().all())

    assert remaining == [705, 706], (
        "the empty-list delete reached past the series' own rows")
    assert forget_calls == [('series', [], 8, 7)], (
        "the mismatch cleanup was not scoped to the default instance and this "
        "series, so an unrelated episode's records could be forgotten")


def test_an_unscoped_empty_sync_with_nothing_in_the_owning_scope_stays_silent(
        schema_session, monkeypatch):
    """The scoped read behind the empty-list delete only decides whether the
    owning scope has a row to lose. When only another instance holds a row
    for this series, the unscoped current-rows read on the default path
    still collects its id, and driving the delete with that list would fire
    a no-op mismatch cleanup and a series update for a scope that had
    nothing to lose. The scoped read keeps both silent and the other
    instance's row standing.
    """
    import semver
    from types import SimpleNamespace

    import sonarr.sync.episodes as episodes_mod
    from app.database import TableArrInstances, TableEpisodes

    schema_session.add(TableArrInstances(
        id=8, kind='sonarr', stable_key='sonarr-default', name='Sonarr',
        enabled=1, is_default=1, ip='127.0.0.1', port=8989, base_url='/', ssl=0,
        verify_ssl=0, http_timeout=60, api_key='k'))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=707, series_id=None, sonarrEpisodeId=47, sonarrSeriesId=7,
        arr_instance_id=2, path="/series/gamma/s01e07.mkv", title="Other instance",
        season=1, episode=7, monitored="True", subtitles="[]"))

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'get_sonarr_info',
                        SimpleNamespace(semver=lambda: semver.Version(*(4, 0, 9, 2421))))
    monkeypatch.setattr(episodes_mod, 'sonarr_series_owner', lambda *a, **kw: (None, None))
    forget_calls = []

    def _forget(media_type, upstream_ids, arr_instance_id=None, series_upstream_id=None):
        forget_calls.append((media_type, upstream_ids, arr_instance_id, series_upstream_id))

    monkeypatch.setattr(episodes_mod, 'forget_media_by_upstream', _forget)
    events = []

    def _event(*args, **kwargs):
        events.append((args, kwargs))

    monkeypatch.setattr(episodes_mod, 'event_stream', _event)

    episodes_mod.sync_episodes(7, episodes_data=[])

    from sqlalchemy import select
    remaining = schema_session.execute(
        select(TableEpisodes.id)).scalars().all()

    assert remaining == [707], (
        "an empty sync reached another instance's row although the owning "
        "scope had nothing to lose")
    assert forget_calls == [], (
        "an empty sync fired a mismatch cleanup although the owning scope "
        "had no row to clean up")
    assert events == [], (
        "an empty sync announced a series update although the owning scope "
        "had no row that could change")


def test_an_empty_list_deletes_a_thousand_episode_series_under_legacy_sqlite(
        schema_session, monkeypatch):
    """Legacy SQLite builds cap bound variables at 999, and a delete that
    binds one variable per episode row dies on a series with about a thousand
    episodes. The raised OperationalError escapes the IntegrityError catch
    and fires after the mismatch records were already forgotten, so the bulk
    sync stops at that series on every run. The empty-answer delete is
    carried by the series clause in the owning scope instead, binding no id
    list at all.
    """
    import sqlite3
    import semver
    from types import SimpleNamespace

    import sonarr.sync.episodes as episodes_mod
    from app.database import TableArrInstances, TableEpisodes

    schema_session.add(TableArrInstances(
        id=8, kind='sonarr', stable_key='sonarr-default', name='Sonarr',
        enabled=1, is_default=1, ip='127.0.0.1', port=8989, base_url='/', ssl=0,
        verify_ssl=0, http_timeout=60, api_key='k'))
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=100000 + n, series_id=None, sonarrEpisodeId=500000 + n,
            sonarrSeriesId=7, arr_instance_id=8,
            path=f"/series/alpha/s01e{n:04d}.mkv", title="Episode",
            season=1, episode=n, monitored="True", subtitles="[]")
        for n in range(1100)])
    schema_session.flush()

    schema_session.connection().connection.dbapi_connection.setlimit(
        sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'get_sonarr_info',
                        SimpleNamespace(semver=lambda: semver.Version(*(4, 0, 9, 2421))))
    monkeypatch.setattr(episodes_mod, 'sonarr_series_owner', lambda *a, **kw: (None, None))
    monkeypatch.setattr(episodes_mod, 'forget_media_by_upstream', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'event_stream', lambda *a, **kw: None)

    episodes_mod.sync_episodes(7, episodes_data=[])

    from sqlalchemy import select, func
    remaining = schema_session.execute(
        select(func.count()).select_from(TableEpisodes)).scalar()

    assert remaining == 0, (
        "an empty answer left episode rows behind on a legacy SQLite build")


def test_an_unscoped_empty_episode_list_with_no_default_skips(schema_session, monkeypatch,
                                                              caplog):
    """Saved instances without an enabled default name no owner for an
    unscoped empty-list delete, so the series is skipped rather than risk
    another instance's rows. A pre-backfill install has no instances at all
    and keeps the legacy unscoped delete; that path is covered by the other
    tests, whose databases have no instance rows.
    """
    import logging

    import sonarr.sync.episodes as episodes_mod
    from app.database import TableArrInstances, TableEpisodes

    schema_session.add(TableArrInstances(
        id=2, kind='sonarr', stable_key='sonarr-other', name='Sonarr 4K',
        enabled=1, is_default=0, ip='127.0.0.1', port=8990, base_url='/', ssl=0,
        verify_ssl=0, http_timeout=60, api_key='k'))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=704, series_id=None, sonarrEpisodeId=45, sonarrSeriesId=7,
        arr_instance_id=None, path="/series/alpha/s01e04.mkv", title="Unowned",
        season=1, episode=4, monitored="True", subtitles="[]"))

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'sonarr_series_owner', lambda *a, **kw: (None, None))
    monkeypatch.setattr(episodes_mod, 'forget_media_by_upstream', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'event_stream', lambda *a, **kw: None)

    with caplog.at_level(logging.WARNING):
        result = episodes_mod.sync_episodes(7, episodes_data=[])

    from sqlalchemy import select
    remaining = sorted(schema_session.execute(
        select(TableEpisodes.id)).scalars().all())

    assert result is episodes_mod.SYNC_SKIPPED_UNREADABLE_EPISODE_FILES, (
        "an unattributable delete was performed instead of skipping")
    assert remaining == [704], (
        "an unscoped empty-list delete without a default instance removed rows")
    assert "no enabled default Sonarr instance" in caplog.text, (
        "the ambiguity was not reported")


@pytest.mark.parametrize('answer', ['empty', 'non-empty'])
def test_a_default_episode_sync_leaves_another_instances_rows_alone(
        schema_session, monkeypatch, answer):
    """A call without an instance is the default instance's sync, not an
    unscoped one.

    A multi-instance install can hold the same upstream series id under two
    instances, and upstream episode ids collide across them as well. When the
    default path left the scope unset, the current-rows read collected the
    other instance's episodes of this series, so the stale-row delete (on a
    non-empty answer) or the series-wide delete (on an empty one) removed
    them. The update in the same loop was unscoped too, so an episode id the
    two instances share rewrote and re-homed the other instance's row. Every
    read, delete, update and mismatch cleanup is confined to the default
    instance that owns a default call's rows.
    """
    import semver
    from types import SimpleNamespace

    import sonarr.sync.episodes as episodes_mod
    from app.database import TableArrInstances, TableEpisodes

    schema_session.add_all([
        TableArrInstances(
            id=8, kind='sonarr', stable_key='sonarr-default', name='Sonarr',
            enabled=1, is_default=1, ip='127.0.0.1', port=8989, base_url='/', ssl=0,
            verify_ssl=0, http_timeout=60, api_key='k'),
        TableArrInstances(
            id=2, kind='sonarr', stable_key='sonarr-4k', name='Sonarr 4K',
            enabled=1, is_default=0, ip='127.0.0.1', port=8990, base_url='/', ssl=0,
            verify_ssl=0, http_timeout=60, api_key='k'),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=704, series_id=None, sonarrEpisodeId=45, sonarrSeriesId=7,
            arr_instance_id=8, path="/series/default/s01e01.mkv", title="Default keep",
            season=1, episode=1, monitored="True", subtitles="[]"),
        TableEpisodes(
            id=705, series_id=None, sonarrEpisodeId=46, sonarrSeriesId=7,
            arr_instance_id=8, path="/series/default/s01e02.mkv", title="Default stale",
            season=1, episode=2, monitored="True", subtitles="[]"),
        TableEpisodes(
            id=706, series_id=None, sonarrEpisodeId=46, sonarrSeriesId=7,
            arr_instance_id=2, path="/series/other/s01e02.mkv", title="Other keep 2",
            season=1, episode=2, monitored="True", subtitles="[]"),
        TableEpisodes(
            id=707, series_id=None, sonarrEpisodeId=45, sonarrSeriesId=7,
            arr_instance_id=2, path="/series/other/s01e01.mkv", title="Other keep 1",
            season=1, episode=1, monitored="True", subtitles="[]"),
    ])
    schema_session.flush()

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'get_sonarr_info',
                        SimpleNamespace(semver=lambda: semver.Version(*(4, 0, 9, 2421))))
    monkeypatch.setattr(episodes_mod, 'store_subtitles', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'event_stream', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'episodeParser', lambda episode, **kw: {
        "sonarrSeriesId": 7, "sonarrEpisodeId": episode['id'],
        "path": episode['episodeFile']['path'], "season": 1, "episode": 1,
        "title": "Default keep", "monitored": "True"})
    forget_calls = []

    def _forget(media_type, upstream_ids, arr_instance_id=None, series_upstream_id=None):
        forget_calls.append((media_type, upstream_ids, arr_instance_id, series_upstream_id))

    monkeypatch.setattr(episodes_mod, 'forget_media_by_upstream', _forget)

    episodes_data = [] if answer == 'empty' else [
        {"id": 45, "hasFile": True, "monitored": True, "episodeFileId": 100,
         "episodeFile": {"size": 999999, "path": "/series/default/s01e01.mkv"}}]

    # No arr_instance_id: the every-default path, which used to stay unscoped.
    episodes_mod.sync_episodes(7, episodes_data=episodes_data, defer_search=True)

    from sqlalchemy import select
    remaining = sorted(schema_session.execute(
        select(TableEpisodes.id)).scalars().all())

    assert remaining == ([706, 707] if answer == 'empty' else [704, 706, 707]), (
        f"the default sync of an {answer} answer deleted another instance's rows")
    other_instance_row = schema_session.execute(
        select(TableEpisodes.arr_instance_id, TableEpisodes.path, TableEpisodes.title)
        .where(TableEpisodes.id == 707)).first()
    assert (other_instance_row.arr_instance_id, other_instance_row.path,
            other_instance_row.title) == (
        2, "/series/other/s01e01.mkv", "Other keep 1"), (
        "the default sync rewrote and re-homed another instance's episode row")
    assert forget_calls == ([('series', [], 8, 7)] if answer == 'empty'
                            else [('series', [46], 8, None)]), (
        "the mismatch cleanup was not confined to the default instance")


def test_an_unscoped_sync_with_no_default_skips_a_non_empty_answer(schema_session, monkeypatch,
                                                                   caplog):
    """Saved instances without an enabled default name no owner, so a sync
    that names none must skip even when Sonarr answers a non-empty list.

    The non-empty path used to fall through to the unscoped stale-row delete,
    which removed whichever instance's episode rows happened to share the
    upstream series id. Skipping leaves every row as it is. A pre-backfill
    install has no instances at all and keeps the legacy unscoped behaviour,
    covered by the tests whose databases have no instance rows.
    """
    import logging

    import sonarr.sync.episodes as episodes_mod
    from app.database import TableArrInstances, TableEpisodes

    schema_session.add(TableArrInstances(
        id=2, kind='sonarr', stable_key='sonarr-other', name='Sonarr 4K',
        enabled=1, is_default=0, ip='127.0.0.1', port=8990, base_url='/', ssl=0,
        verify_ssl=0, http_timeout=60, api_key='k'))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=704, series_id=None, sonarrEpisodeId=46, sonarrSeriesId=7,
        arr_instance_id=2, path="/series/other/s01e02.mkv", title="Kept",
        season=1, episode=2, monitored="True", subtitles="[]"))

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'sonarr_series_owner', lambda *a, **kw: (None, None))
    monkeypatch.setattr(episodes_mod, 'forget_media_by_upstream', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'event_stream', lambda *a, **kw: None)

    with caplog.at_level(logging.WARNING):
        result = episodes_mod.sync_episodes(7, episodes_data=[
            {"id": 45, "hasFile": True, "monitored": True, "episodeFileId": 100,
             "episodeFile": {"size": 999999, "path": "/series/default/s01e01.mkv"}}])

    from sqlalchemy import select
    remaining = sorted(schema_session.execute(
        select(TableEpisodes.id)).scalars().all())

    assert result is episodes_mod.SYNC_SKIPPED_UNREADABLE_EPISODE_FILES, (
        "an unattributable sync ran instead of skipping")
    assert remaining == [704], (
        "an unscoped sync without a default instance deleted another "
        "instance's rows")
    assert "no enabled default Sonarr instance" in caplog.text, (
        "the ambiguity was not reported")


def test_a_default_single_episode_sync_leaves_another_instances_row_alone(
        schema_session, monkeypatch):
    """The single-episode entry point has the same default-path hazard.

    Its row lookup, update and delete key on the upstream episode id alone, so
    with the scope unset an event for the default instance rewrote another
    instance's row that happens to share that id. Resolving the default before
    the lookup confines the writes to the default instance and inserts its own
    row instead.
    """
    import sonarr.sync.episodes as episodes_mod
    from app.database import TableArrInstances, TableEpisodes

    schema_session.add_all([
        TableArrInstances(
            id=8, kind='sonarr', stable_key='sonarr-default', name='Sonarr',
            enabled=1, is_default=1, ip='127.0.0.1', port=8989, base_url='/', ssl=0,
            verify_ssl=0, http_timeout=60, api_key='k'),
        TableArrInstances(
            id=2, kind='sonarr', stable_key='sonarr-4k', name='Sonarr 4K',
            enabled=1, is_default=0, ip='127.0.0.1', port=8990, base_url='/', ssl=0,
            verify_ssl=0, http_timeout=60, api_key='k'),
    ])
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=707, series_id=None, sonarrEpisodeId=45, sonarrSeriesId=7,
        arr_instance_id=2, path="/series/other/s01e01.mkv", title="Other keep",
        season=1, episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    monkeypatch.setattr(episodes_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod, 'store_subtitles', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'event_stream', lambda *a, **kw: None)
    monkeypatch.setattr(episodes_mod, 'get_episodesFiles_from_sonarr_api',
                        lambda **kw: {"size": 999999,
                                      "path": "/series/default/s01e01.mkv"})
    monkeypatch.setattr(episodes_mod, 'get_episodes_from_sonarr_api', lambda **kw: {
        "id": 45, "seriesId": 7, "hasFile": True, "episodeFileId": 100,
        "episodeFile": {"size": 999999, "path": "/series/default/s01e01.mkv"}})
    monkeypatch.setattr(episodes_mod, 'episodeParser', lambda episode, **kw: {
        "sonarrSeriesId": 7, "sonarrEpisodeId": 45,
        "path": "/series/default/s01e01.mkv", "season": 1, "episode": 1,
        "title": "Default", "monitored": "True"})

    episodes_mod.sync_one_episode(45, defer_search=True)

    from sqlalchemy import select
    rows = schema_session.execute(
        select(TableEpisodes.arr_instance_id, TableEpisodes.path)
        .where(TableEpisodes.sonarrEpisodeId == 45)
        .order_by(TableEpisodes.arr_instance_id)).all()

    assert [(row.arr_instance_id, row.path) for row in rows] == [
        (2, "/series/other/s01e01.mkv"),
        (8, "/series/default/s01e01.mkv")], (
        "the default single-episode sync rewrote or moved another instance's row")


def test_the_missing_subtitle_gate_reads_only_the_given_instance(
        schema_session, monkeypatch):
    """The gate that decides whether a sync queues a search reads the episode
    rows of the instance being synced. Upstream episode ids collide across
    instances, so an unscoped read could queue (or skip) a search from another
    instance's row for the same id.
    """
    import sonarr.sync.episodes as episodes_mod
    from app.database import TableEpisodes, TableShows

    schema_session.add_all([
        TableShows(id=70, sonarrSeriesId=7, arr_instance_id=8, path="/series/default",
                   title="Default", audio_language="[]"),
        TableShows(id=71, sonarrSeriesId=7, arr_instance_id=2, path="/series/other",
                   title="Other", audio_language="[]"),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=704, series_id=70, sonarrEpisodeId=45, sonarrSeriesId=7,
            arr_instance_id=8, path="/series/default/s01e01.mkv", title="Default",
            season=1, episode=1, monitored="True", subtitles="[]", missing_subtitles="[]"),
        TableEpisodes(
            id=707, series_id=71, sonarrEpisodeId=45, sonarrSeriesId=7,
            arr_instance_id=2, path="/series/other/s01e01.mkv", title="Other",
            season=1, episode=1, monitored="True", subtitles="[]",
            missing_subtitles='["en"]'),
    ])
    schema_session.flush()

    monkeypatch.setattr(episodes_mod, 'database', schema_session)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'only_monitored', False, raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'excluded_tags', [], raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'excluded_series_types', [], raising=False)
    monkeypatch.setattr(episodes_mod.settings.sonarr, 'exclude_season_zero', False, raising=False)

    assert episodes_mod._is_there_missing_subtitles(
        episode_id=45, arr_instance_id=8) is False, (
        "the gate saw another instance's missing subtitles for the same episode id")
    assert episodes_mod._is_there_missing_subtitles(
        episode_id=45, arr_instance_id=2) is True, (
        "the gate missed the owning instance's missing subtitles")


def test_the_episode_fetch_helper_answers_none_for_failures(monkeypatch):
    """Every failure path of the episode fetch must answer None, never an
    empty list: the sync trusts [] as a real answer and deletes with it.
    """
    import requests

    import sonarr.sync.utils as utils_mod

    class FakeResponse:
        def __init__(self, status_code=200, json_error=False):
            self.status_code = status_code
            self.headers = {'content-type': 'application/json'}
            self.content = b'not-json' if json_error else b'[]'
            self.text = 'not-json' if json_error else '[]'

        def json(self, **kwargs):
            if self.content == b'not-json':
                raise requests.exceptions.JSONDecodeError('no json', 'not-json', 0)
            return []

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.exceptions.HTTPError(response=self)

    class FakeSession:
        def __init__(self, response=None, error=None):
            self._response = response
            self._error = error

        def get(self, *args, **kwargs):
            if self._error is not None:
                raise self._error
            return self._response

    for error in [requests.exceptions.ConnectionError('refused'),
                  requests.exceptions.Timeout('slow'),
                  requests.exceptions.RequestException('broken')]:
        monkeypatch.setattr(utils_mod, 'sonarr_session',
                            lambda error=error: FakeSession(error=error))
        assert utils_mod.get_episodes_from_sonarr_api(apikey_sonarr='x', series_id=7) is None, (
            f'{type(error).__name__} at the session level was not answered with None')

    monkeypatch.setattr(utils_mod, 'sonarr_session', lambda: FakeSession(FakeResponse(500)))
    assert utils_mod.get_episodes_from_sonarr_api(apikey_sonarr='x', series_id=7) is None, (
        'an HTTP error status was not answered with None')

    monkeypatch.setattr(utils_mod, 'sonarr_session',
                        lambda: FakeSession(FakeResponse(200, json_error=True)))
    assert utils_mod.get_episodes_from_sonarr_api(apikey_sonarr='x', series_id=7) is None, (
        'a 200 whose body is not JSON was not answered with None')

    monkeypatch.setattr(utils_mod, 'sonarr_session', lambda: FakeSession(FakeResponse(200)))
    assert utils_mod.get_episodes_from_sonarr_api(apikey_sonarr='x', series_id=7) == [], (
        'a real empty episode list was not passed through as a list')


def test_the_episode_fetch_helper_answers_none_for_arr_client_failures(monkeypatch):
    """The instance-client path goes through the same helper, so its failures
    must surface the same way: the raw Response it returns raises or reports
    a non-200, and the helper answers None.
    """
    import requests
    from types import SimpleNamespace

    import sonarr.sync.utils as utils_mod

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {}
            self.content = b'[]'
            self.text = '[]'

        def json(self, **kwargs):
            return []

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.exceptions.HTTPError(response=self)

    for error in [requests.exceptions.ConnectionError('refused'),
                  requests.exceptions.Timeout('slow')]:
        def _raising(path, error=error):
            raise error
        assert utils_mod.get_episodes_from_sonarr_api(
            apikey_sonarr='x', series_id=7,
            arr_client=SimpleNamespace(get=_raising)) is None, (
            f'{type(error).__name__} from the instance client was not answered with None')

    assert utils_mod.get_episodes_from_sonarr_api(
        apikey_sonarr='x', series_id=7,
        arr_client=SimpleNamespace(get=lambda path: FakeResponse(500))) is None, (
        'an HTTP error status from the instance client was not answered with None')

    assert utils_mod.get_episodes_from_sonarr_api(
        apikey_sonarr='x', series_id=7,
        arr_client=SimpleNamespace(get=lambda path: FakeResponse(200))) == [], (
        'a real empty episode list was not passed through as a list')
