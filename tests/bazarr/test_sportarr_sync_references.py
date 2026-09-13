# coding=utf-8
"""Reference tracks offered by the manual sync dialog for a sports event.

subtitles_sync_references only ever knew about episodes and movies, so opening
"Sync" on a sports event returned an empty references_dict: the audio track and
embedded subtitle pickers had nothing in them, and the external subtitle list
was always blank. The event row carries everything needed (path, file_size,
subtitles) and parse_video_metadata has accepted a sports_event_id all along.
"""

import pytest


FFPROBE = {
    "ffprobe": {
        "audio": [
            {"name": "Dolby Digital Audio / 2.0", "language": "hun"},
            {"name": "AAC Audio / 2.0"},
            {"name": "AAC Audio / 2.0", "language": "eng"},
        ],
        "subtitle": [
            {"name": "English SDH", "language": "eng", "hearing_impaired": True},
            {"name": "PGS", "language": "eng"},
        ],
    }
}


LANGUAGES = [
    {"code3": "eng", "code3b": None, "code2": "en", "name": "English", "enabled": 1},
    {"code3": "hun", "code3b": None, "code2": "hu", "name": "Hungarian", "enabled": 1},
]


@pytest.fixture
def sports_event(schema_session, monkeypatch):
    import languages.get_languages as get_languages
    from app.database import TableArrInstances, TableSportsEvents, TableSportsLeagues
    from utilities import path_mappings, video_analyzer

    monkeypatch.setattr(get_languages, 'languages_dict', LANGUAGES, raising=False)

    schema_session.add(TableArrInstances(id=42, kind='sportarr', stable_key='sportarr-1',
                                         name='Sportarr', enabled=1, port=1867, api_key='k'))
    schema_session.add(TableSportsLeagues(id=51, arr_instance_id=42, sportarrLeagueId=7,
                                          title='Formula 1', path='/data/sports/f1'))
    # Flushed in dependency order by hand: the composite league FK is not
    # declared as a relationship, so the unit of work has nothing to sort on.
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=61, arr_instance_id=42, league_id=51, sportarrEventId=9, file_id=71,
        path='/data/sports/f1/race.mkv', title='Race', file_size=8123456789,
        subtitles="[['hu', '/data/sports/f1/race.hu.srt'], ['en', '/data/sports/f1/race.en.srt']]",
    ))
    schema_session.commit()

    monkeypatch.setattr(video_analyzer, 'database', schema_session)
    # The owning Sportarr mounts its library elsewhere than Bazarr does, which
    # is the whole point of scoping the mapping to the instance.
    monkeypatch.setattr(path_mappings, '_sports_instance_mapping',
                        lambda _id: [['/data/sports', '/mnt/sports']])
    return schema_session


def test_the_pickers_are_populated_from_the_event_file(sports_event, monkeypatch):
    from utilities import video_analyzer

    seen = {}

    def fake_parse(file, file_size, episode_file_id=None, movie_file_id=None, use_cache=True,
                   arr_instance_id=None, *, sports_event_id=None):
        seen.update(file=file, file_size=file_size, arr_instance_id=arr_instance_id,
                    sports_event_id=sports_event_id)
        return FFPROBE

    monkeypatch.setattr(video_analyzer, 'parse_video_metadata', fake_parse)

    result = video_analyzer.subtitles_sync_references(
        subtitles_path='/mnt/sports/f1/race.hu.srt', sports_event_id=61, arr_instance_id=42)

    # Probed through the owning instance's mapping, not the global series one.
    assert seen == {'file': '/mnt/sports/f1/race.mkv', 'file_size': 8123456789,
                    'arr_instance_id': 42, 'sports_event_id': 61}
    assert [track['stream'] for track in result['audio_tracks']] == ['a:0', 'a:1', 'a:2']
    assert [track['language'] for track in result['audio_tracks']] == ['Hungarian', 'Undefined', 'English']
    # Bitmap subtitles stay excluded, and skipping one still advances the index
    # so the remaining streams keep their real ffmpeg numbering.
    assert [track['stream'] for track in result['embedded_subtitles_tracks']] == ['s:0']
    assert result['embedded_subtitles_tracks'][0]['hearing_impaired'] is True


def test_the_subtitle_being_synced_is_not_offered_as_its_own_reference(sports_event, monkeypatch):
    from utilities import video_analyzer

    monkeypatch.setattr(video_analyzer, 'parse_video_metadata',
                        lambda *args, **kwargs: FFPROBE)

    result = video_analyzer.subtitles_sync_references(
        subtitles_path='/mnt/sports/f1/race.hu.srt', sports_event_id=61, arr_instance_id=42)

    # The dialog passes the local path; the rows hold the Sportarr-side one, so
    # the comparison only works if the local path is reversed through the sports
    # mapping. Reversed as a movie it would not match and the file would offer
    # itself as its own sync reference.
    names = [track['name'] for track in result['external_subtitles_tracks']]
    assert names == ['race.en.srt']
    # The reference is handed straight to the sync engine, which opens it on
    # this machine, so it has to come back mapped local.
    assert result['external_subtitles_tracks'][0]['path'] == '/mnt/sports/f1/race.en.srt'


def test_an_event_owned_by_another_instance_is_not_readable(sports_event, monkeypatch):
    from utilities import video_analyzer

    monkeypatch.setattr(video_analyzer, 'parse_video_metadata',
                        lambda *args, **kwargs: FFPROBE)

    result = video_analyzer.subtitles_sync_references(
        subtitles_path='/mnt/sports/f1/race.hu.srt', sports_event_id=61, arr_instance_id=43)

    assert result == {'audio_tracks': [], 'embedded_subtitles_tracks': [],
                      'external_subtitles_tracks': []}


def test_the_api_passes_the_event_through():
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles)
    assert '"sportsEventId"' in source
    assert 'sports_event_id=sportsEventId' in source
