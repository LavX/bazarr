# coding=utf-8
"""The ffprobe metadata cache has to belong to one instance (#156).

``parse_video_metadata`` cached the parsed stream list against
``episode_file_id`` or ``movie_file_id`` alone. Those ids come from the arr
server, so two Sonarr or Radarr instances can hand out the same one for
different files. When they do, one instance reads the other's cached stream
list, and probing on either side overwrites the other's row.

Embedded-subtitle extraction picks a stream index out of that list, so a
collision selects the wrong track and translates a different subtitle than the
one asked for, with nothing in the log to say so.
"""
import pickle

import pytest
from sqlalchemy import select

from app.database import TableEpisodes, TableMovies, TableShows


def _cached(file_id, file_size, parser='ffprobe'):
    return pickle.dumps({
        'ffprobe': {'subtitle': [{'name': f'stream-for-{file_id}'}]},
        'mediainfo': {},
        'file_id': file_id,
        'file_size': file_size,
    }, pickle.HIGHEST_PROTOCOL)


@pytest.fixture
def two_instances(schema_session, monkeypatch):
    """Two episodes on different instances sharing one episode_file_id."""
    from app import database as db_module

    monkeypatch.setattr(db_module, 'database', schema_session)
    import utilities.video_analyzer as va
    monkeypatch.setattr(va, 'database', schema_session)
    monkeypatch.setattr(va.settings.general, 'embedded_subtitles_parser', 'ffprobe',
                        raising=False)

    schema_session.add_all([
        TableShows(id=1, arr_instance_id=1, sonarrSeriesId=1, title='S', path='/a', profileId=None),
        TableShows(id=2, arr_instance_id=2, sonarrSeriesId=1, title='S', path='/b', profileId=None),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(id=1, arr_instance_id=1, series_id=1, sonarrSeriesId=1,
                      sonarrEpisodeId=1, title='E', path='/a/e.mkv', season=1, episode=1,
                      episode_file_id=500, file_size=111,
                      ffprobe_cache=_cached(500, 111)),
        TableEpisodes(id=2, arr_instance_id=2, series_id=2, sonarrSeriesId=1,
                      sonarrEpisodeId=1, title='E', path='/b/e.mkv', season=1, episode=1,
                      episode_file_id=500, file_size=111,
                      ffprobe_cache=_cached(500, 111)),
    ])
    schema_session.commit()
    return schema_session


def test_the_cache_read_is_scoped_to_the_owning_instance(two_instances, monkeypatch):
    """Without scoping, first() returns whichever row the database felt like."""
    import utilities.video_analyzer as va

    # Give the two rows distinguishable cached payloads.
    two_instances.execute(
        TableEpisodes.__table__.update()
        .where(TableEpisodes.id == 2)
        .values(ffprobe_cache=pickle.dumps({
            'ffprobe': {'subtitle': [{'name': 'second-instance'}]},
            'mediainfo': {}, 'file_id': 500, 'file_size': 111,
        }, pickle.HIGHEST_PROTOCOL)))
    two_instances.commit()

    data = va.parse_video_metadata('/b/e.mkv', 111, episode_file_id=500,
                                   arr_instance_id=2, use_cache=True)

    assert data['ffprobe']['subtitle'][0]['name'] == 'second-instance'


def test_probing_one_instance_leaves_the_other_cache_alone(two_instances, monkeypatch,
                                                          tmp_path):
    """The write half of the scoping, which is the half that corrupts data.

    Two things this test has to get right or it asserts nothing at all.
    ``get_binary`` is imported INSIDE ``parse_video_metadata``, so patching the
    name on ``video_analyzer`` has no effect; it has to be patched on
    ``utilities.binaries``. And the function returns early when the video file
    does not exist, before it ever probes or writes. Miss either and the
    "other instance untouched" assertion passes because nothing was written to
    anything, which is why the probed row is asserted too.
    """
    import utilities.binaries as binaries
    import utilities.video_analyzer as va

    video = tmp_path / 'e.mkv'
    video.write_bytes(b'stand-in, knowit is stubbed out')

    monkeypatch.setattr(va, 'know', lambda *a, **kw: {'subtitle': [{'name': 'freshly-probed'}]})
    monkeypatch.setattr(binaries, 'get_binary', lambda name: '/usr/bin/ffprobe')

    va.parse_video_metadata(str(video), 999, episode_file_id=500,
                            arr_instance_id=1, use_cache=False)

    probed = two_instances.execute(
        select(TableEpisodes.ffprobe_cache).where(TableEpisodes.id == 1)).scalar()
    assert pickle.loads(probed)['ffprobe']['subtitle'][0]['name'] == 'freshly-probed', \
        'the probe never reached the cache write, so this test proves nothing'

    untouched = two_instances.execute(
        select(TableEpisodes.ffprobe_cache).where(TableEpisodes.id == 2)).scalar()
    assert pickle.loads(untouched)['file_size'] == 111, \
        "the other instance's cached metadata was overwritten"


def test_without_an_instance_the_behaviour_is_unchanged(two_instances, monkeypatch):
    """A single-instance install passes None and must read exactly as before."""
    import utilities.video_analyzer as va

    data = va.parse_video_metadata('/a/e.mkv', 111, episode_file_id=500, use_cache=True)

    assert data is not None
    assert data['file_id'] == 500


def test_the_movie_cache_is_scoped_too(schema_session, monkeypatch):
    import utilities.video_analyzer as va

    monkeypatch.setattr(va, 'database', schema_session)
    monkeypatch.setattr(va.settings.general, 'embedded_subtitles_parser', 'ffprobe',
                        raising=False)

    schema_session.add_all([
        TableMovies(id=1, arr_instance_id=1, radarrId=1, title='M', path='/a/m.mkv',
                    tmdbId='1', movie_file_id=700, file_size=222,
                    ffprobe_cache=pickle.dumps({
                        'ffprobe': {'subtitle': [{'name': 'first'}]}, 'mediainfo': {},
                        'file_id': 700, 'file_size': 222}, pickle.HIGHEST_PROTOCOL)),
        TableMovies(id=2, arr_instance_id=2, radarrId=1, title='M', path='/b/m.mkv',
                    tmdbId='2', movie_file_id=700, file_size=222,
                    ffprobe_cache=pickle.dumps({
                        'ffprobe': {'subtitle': [{'name': 'second'}]}, 'mediainfo': {},
                        'file_id': 700, 'file_size': 222}, pickle.HIGHEST_PROTOCOL)),
    ])
    schema_session.commit()

    data = va.parse_video_metadata('/b/m.mkv', 222, movie_file_id=700,
                                   arr_instance_id=2, use_cache=True)

    assert data['ffprobe']['subtitle'][0]['name'] == 'second'
