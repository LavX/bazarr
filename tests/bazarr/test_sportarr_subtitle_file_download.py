# coding=utf-8
"""Downloading a sports event's subtitle file, and resolving its path at all.

resolve_subtitle_path is the shared front door: the file-download route, the
subtitle editor and the content viewer all resolve through it, and it answered
'Invalid media type', 400 for anything that was not an episode or a movie. So a
sports subtitle could be searched, downloaded and indexed by Bazarr, and then
neither opened nor saved to disk by the user.
"""

import os

import pytest


@pytest.fixture
def sports_event(schema_session, monkeypatch, tmp_path):
    from api.subtitles import content
    from app.database import TableArrInstances, TableSportsEvents, TableSportsLeagues
    from utilities import path_mappings

    library = tmp_path / "sports"
    library.mkdir()
    (library / "race.mkv").write_bytes(b"video")
    (library / "race.hu.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nRajt\n")

    schema_session.add(TableArrInstances(id=42, kind='sportarr', stable_key='sportarr-1',
                                         name='Sportarr', enabled=1, port=1867, api_key='k'))
    schema_session.add(TableSportsLeagues(id=51, arr_instance_id=42, sportarrLeagueId=7,
                                          title='Formula 1', path='/remote/sports'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=61, arr_instance_id=42, league_id=51, sportarrEventId=9, file_id=71,
        path='/remote/sports/race.mkv', title='Italian Grand Prix', file_size=17,
        subtitles="[['hu', '/remote/sports/race.hu.srt']]",
    ))
    schema_session.commit()

    monkeypatch.setattr(content, 'database', schema_session)
    monkeypatch.setattr(path_mappings, '_sports_instance_mapping',
                        lambda _id: [['/remote/sports', str(library)]])
    # No configured subtitle subfolder: the event's own directory is the only
    # trusted root, which is what the path re-anchoring has to accept.
    monkeypatch.setattr(content, 'get_target_folder', lambda *args, **kwargs: None)
    return library


def test_a_sports_subtitle_resolves_to_its_file(sports_event):
    from api.subtitles.content import resolve_subtitle_path

    path, metadata = resolve_subtitle_path('sports', 61, 'hu', arr_instance_id=42)

    assert path == str(sports_event / "race.hu.srt")
    assert os.path.isfile(path)
    # Episode-shaped, because an event belongs to a league the way an episode
    # belongs to a show; the editor header reads these.
    assert metadata['mediaTitle'] == 'Formula 1'
    assert metadata['mediaId'] == 51
    assert metadata['episodeId'] == 61
    assert metadata['episodeTitle'] == 'Italian Grand Prix'
    assert metadata['arrInstanceId'] == 42


def test_another_owner_cannot_read_the_event(sports_event):
    from api.subtitles.content import resolve_subtitle_path

    assert resolve_subtitle_path('sports', 61, 'hu', arr_instance_id=43) == ('Media not found', 404)


def test_an_unindexed_language_is_not_invented(sports_event):
    from api.subtitles.content import resolve_subtitle_path

    result = resolve_subtitle_path('sports', 61, 'en', arr_instance_id=42)
    assert result == ('No subtitle found for requested language', 404)


def test_the_local_event_id_is_never_treated_as_ambiguous():
    """The ambiguity check exists because upstream ids repeat across instances.

    A sports event is addressed by its primary key, so it cannot repeat. Running
    the episode branch on it would count Sonarr episodes that happen to share
    the number and reject a perfectly unambiguous request.
    """
    from api.subtitles.download import _ambiguous_media_error

    assert _ambiguous_media_error('sports', 61, None) is None


def test_the_route_exists_and_resolves_as_sports():
    import inspect

    from api.subtitles import download

    source = inspect.getsource(download)
    assert "'sports/events/<int:eventId>/subtitles/<language>/download'" in source
    assert "_send_single_subtitle('sports', eventId, language)" in source
