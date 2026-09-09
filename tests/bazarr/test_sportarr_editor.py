# coding=utf-8
"""The subtitle editor, opened on a sports event.

Every editor entry point validated its media type against a hardcoded
('episode', 'movie') pair, in six separate places. A sports subtitle could be
searched, downloaded, indexed and blacklisted, and then not opened in the
editor at all: the request was rejected before any lookup happened. The tuple
now lives in one place, which is the point, since it is the copies that let the
gates drift apart.
"""

import pytest


@pytest.fixture
def sports_event(schema_session, monkeypatch, tmp_path):
    from api.editor import editor
    from app.database import TableArrInstances, TableSportsEvents, TableSportsLeagues
    from utilities import path_mappings

    library = tmp_path / "sports"
    library.mkdir()
    (library / "race.mkv").write_bytes(b"video")

    schema_session.add(TableArrInstances(id=42, kind='sportarr', stable_key='sportarr-1',
                                         name='Sportarr', enabled=1, port=1867, api_key='k'))
    schema_session.add(TableSportsLeagues(id=51, arr_instance_id=42, sportarrLeagueId=7,
                                          title='Formula 1', path='/remote/sports'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=61, arr_instance_id=42, league_id=51, sportarrEventId=9, file_id=71,
        path='/remote/sports/race.mkv', title='Italian Grand Prix', file_size=5,
        subtitles="[['hu', '/remote/sports/race.hu.srt']]",
    ))
    schema_session.commit()

    monkeypatch.setattr(editor, 'database', schema_session)
    monkeypatch.setattr(path_mappings, '_sports_instance_mapping',
                        lambda _id: [['/remote/sports', str(library)]])
    return library


def test_the_media_type_list_is_defined_once():
    """Six copies of ('episode', 'movie') is how sports ended up rejected at
    five gates while a sixth had already been taught about it."""
    import inspect

    from api.editor import editor

    source = inspect.getsource(editor)
    assert source.count("('episode', 'movie')") == 0
    assert "MEDIA_TYPES = ('episode', 'movie', 'sports')" in source


def test_the_event_video_resolves_through_the_owning_instance(sports_event):
    from api.editor.editor import _resolve_video_path

    assert _resolve_video_path('sports', 61, arr_instance_id=42) == str(sports_event / "race.mkv")


def test_the_owner_is_read_off_the_row_when_the_caller_omits_it(sports_event):
    """Sports path mappings are per instance with no global fallback, so an
    unscoped request must not silently return an unmapped remote path."""
    from api.editor.editor import _resolve_video_path

    assert _resolve_video_path('sports', 61) == str(sports_event / "race.mkv")


def test_another_owner_cannot_open_the_event(sports_event):
    from api.editor.editor import _resolve_video_path

    assert _resolve_video_path('sports', 61, arr_instance_id=43) == ('Sports event not found', 404)


def test_an_unknown_media_type_is_still_rejected(sports_event):
    from api.editor.editor import MEDIA_TYPE_ERROR, _resolve_video_path

    assert _resolve_video_path('podcast', 61) == (MEDIA_TYPE_ERROR, 400)


def test_the_editor_lists_the_events_subtitles(sports_event, monkeypatch):
    from api.editor import editor

    class _Request:
        args = {'mediaType': 'sports', 'mediaId': '61', 'arr_instance_id': '42'}

    monkeypatch.setattr(editor, 'request', _Request())
    result = editor.EditorSubtitles.get.__wrapped__(editor.EditorSubtitles())
    assert [entry['language'] for entry in result['subtitles']] == ['hu']


def test_the_content_routes_do_not_shadow_the_indexer():
    """POST /sports/events/<id>/subtitles belongs to the indexer. A second
    Resource on that path silently takes it over, which is exactly how the
    upload route broke indexing once already."""
    import inspect

    from api.subtitles import content

    source = inspect.getsource(content)
    assert "'sports/events/<int:eventId>/subtitles/create'" in source
    assert "'sports/events/<int:eventId>/subtitles'" not in source
    assert "'sports/events/<int:eventId>/subtitles/<language>/content'" in source
    assert "'sports/events/<int:eventId>/subtitles/<language>/promote'" in source
    assert "'sports/events/<int:eventId>/subtitles/<language>/sync-status'" in source


def test_a_promoted_sync_is_recorded_in_the_sports_history():
    import inspect

    from api.subtitles import content

    source = inspect.getsource(content._log_promoted_sync_history)
    sports = source[source.index("elif media_type == 'sports':"):]
    # Action 5 is "subtitles synced", the same code the episode and movie
    # branches log, and keyed on the local event id like every sports table.
    assert "sports_history_log(5, metadata.get('episodeId', media_id), arr_instance_id, result)" in sports


def test_saving_reindexes_the_event():
    import inspect

    from api.subtitles import content

    source = inspect.getsource(content._refresh_media_subtitles)
    assert "store_subtitles_sports(metadata.get('episodeId', media_id), arr_instance_id)" in source
    assert "event_stream(type='sports'" in source


def test_a_created_subtitle_keeps_the_videos_own_filename():
    """secure_filename over the whole composed name rewrote every space in the
    video filename to an underscore. External subtitles are matched by exact
    basename, so "Create subtitle" wrote a file the indexer could never see,
    for episodes and movies as much as for sports."""
    from api.subtitles.content import _subtitle_filename

    assert _subtitle_filename(
        'Formula 1 - S2026E71 - Italian Grand Prix - Race', 'en', '.srt',
    ) == 'Formula 1 - S2026E71 - Italian Grand Prix - Race.en.srt'


def test_the_language_half_is_still_sanitised():
    from api.subtitles.content import _subtitle_filename

    name = _subtitle_filename('Race', '../../etc/passwd', '.srt')
    assert '/' not in name
    assert '..' not in name.split('.')
