# coding=utf-8
"""The manual embedded-translate endpoint has to stay on the owning instance (#156).

The endpoint already scopes its media lookup by ``arr_instance_id``, then threw
that away for the two things that matter downstream: it mapped the video path
with the GLOBAL mapping and called ``extract_embedded_subtitle`` without the
owner.

Extraction reverses the path with ``path_replace_reverse_instance`` and looks the
row up scoped. Forward-global plus reverse-per-instance do not round-trip on any
secondary instance that has its own path mappings, so the row lookup misses and
the user is told the track is an unsupported bitmap codec. Where the mappings do
agree, the unscoped metadata cache can still hand back a sibling instance's
stream list and extract the wrong track.
"""
from types import SimpleNamespace

import pytest

from app.database import TableEpisodes, TableMovies, TableShows


@pytest.fixture
def endpoint(schema_session, monkeypatch, tmp_path):
    from api.subtitles import subtitles as api_mod

    monkeypatch.setattr(api_mod, 'database', schema_session)
    monkeypatch.setattr(api_mod, 'alpha3_from_alpha2', lambda code: 'eng')

    recorded = {'mapped': [], 'extracted': None}

    def _map_instance(path, arr_instance_id, media_type):
        recorded['mapped'].append((path, arr_instance_id, media_type))
        return f'/mapped{path}'

    def _map_global(path):
        recorded['mapped'].append((path, 'GLOBAL', 'episode'))
        return f'/mapped{path}'

    def _map_global_movie(path):
        recorded['mapped'].append((path, 'GLOBAL', 'movie'))
        return f'/mapped{path}'

    monkeypatch.setattr(api_mod.path_mappings, 'path_replace_instance', _map_instance)
    monkeypatch.setattr(api_mod.path_mappings, 'path_replace', _map_global)
    monkeypatch.setattr(api_mod.path_mappings, 'path_replace_movie', _map_global_movie)

    extracted_file = tmp_path / 'e.en.srt'
    extracted_file.write_text('1\n')

    def _extract(video_path, language_code2, media_type, hi=False, forced=False,
                 arr_instance_id=None):
        recorded['extracted'] = {
            'video_path': video_path, 'media_type': media_type,
            'arr_instance_id': arr_instance_id,
        }
        # None short-circuits the handler right here, which is all the
        # extraction assertions need. Tests that follow the request further
        # replace this with the real path.
        return recorded.get('extraction_result')

    monkeypatch.setattr(api_mod, 'extract_embedded_subtitle', _extract)

    def _translate(**kwargs):
        recorded['translated'] = kwargs

    monkeypatch.setattr(api_mod, 'translate_subtitles_file', _translate)
    recorded['extracted_file'] = str(extracted_file)

    def _call(**overrides):
        args = {
            'action': 'translate', 'language': 'nl', 'path': '', 'type': 'episode',
            'id': 42, 'arr_instance_id': 2, 'from_language': 'en',
            'forced': 'False', 'hi': 'False',
        }
        args.update(overrides)
        stub_self = SimpleNamespace(
            patch_request_parser=SimpleNamespace(parse_args=lambda: args))
        return api_mod.Subtitles.patch.__wrapped__(stub_self)

    return _call, recorded


def test_the_episode_video_path_is_mapped_for_the_owning_instance(schema_session, endpoint):
    call, recorded = endpoint
    schema_session.add(TableShows(id=1, arr_instance_id=2, sonarrSeriesId=7,
                                  title='S', path='/tv/s', profileId=None))
    schema_session.flush()
    schema_session.add(TableEpisodes(id=1, arr_instance_id=2, series_id=1, sonarrSeriesId=7,
                                     sonarrEpisodeId=42, title='E', path='/tv/s/e.mkv',
                                     season=1, episode=1))
    schema_session.commit()

    call()

    assert recorded['mapped'] == [('/tv/s/e.mkv', 2, 'episode')], (
        'the video path must be mapped with the owning instance mapping, because '
        f'extraction reverses it with that same one; got {recorded["mapped"]!r}')


def test_the_owner_reaches_episode_extraction(schema_session, endpoint):
    call, recorded = endpoint
    schema_session.add(TableShows(id=1, arr_instance_id=2, sonarrSeriesId=7,
                                  title='S', path='/tv/s', profileId=None))
    schema_session.flush()
    schema_session.add(TableEpisodes(id=1, arr_instance_id=2, series_id=1, sonarrSeriesId=7,
                                     sonarrEpisodeId=42, title='E', path='/tv/s/e.mkv',
                                     season=1, episode=1))
    schema_session.commit()

    call()

    assert recorded['extracted']['arr_instance_id'] == 2, (
        'extraction was handed no owner, so its row lookup and metadata cache '
        'run unscoped')


def test_the_owner_reaches_movie_extraction(schema_session, endpoint):
    call, recorded = endpoint
    schema_session.add(TableMovies(id=1, arr_instance_id=3, radarrId=9, title='M',
                                   path='/movies/m.mkv', tmdbId='1'))
    schema_session.commit()

    call(type='movie', id=9, arr_instance_id=3)

    assert recorded['mapped'] == [('/movies/m.mkv', 3, 'movie')]
    assert recorded['extracted']['arr_instance_id'] == 3


def test_the_queued_translation_keeps_the_instance_mapped_video_path(
        schema_session, endpoint):
    """The endpoint maps the video for extraction with the owning instance, then
    rebuilt video_path with the global mapper for everything after it.

    On a secondary instance with its own path mappings the two disagree, so the
    translation is handed a path under another instance's library: it either
    fails validation after the API has already answered 204, or writes the
    output beside the wrong media."""
    call, recorded = endpoint
    recorded['extraction_result'] = recorded['extracted_file']
    schema_session.add(TableShows(id=1, arr_instance_id=2, sonarrSeriesId=7,
                                  title='S', path='/tv/s', profileId=None))
    schema_session.flush()
    schema_session.add(TableEpisodes(id=1, arr_instance_id=2, series_id=1, sonarrSeriesId=7,
                                     sonarrEpisodeId=42, title='E', path='/tv/s/e.mkv',
                                     season=1, episode=1))
    schema_session.commit()

    call()

    assert ('/tv/s/e.mkv', 'GLOBAL', 'episode') not in recorded['mapped'], (
        'the video path was remapped with the global mapping after extraction '
        f'had already used the instance one; got {recorded["mapped"]!r}')
    assert recorded['translated']['video_path'] == '/mapped/tv/s/e.mkv'
    assert recorded['translated']['arr_instance_id'] == 2


def test_the_queued_movie_translation_keeps_the_instance_mapped_video_path(
        schema_session, endpoint):
    call, recorded = endpoint
    recorded['extraction_result'] = recorded['extracted_file']
    schema_session.add(TableMovies(id=1, arr_instance_id=3, radarrId=9, title='M',
                                   path='/movies/m.mkv', tmdbId='1'))
    schema_session.commit()

    call(type='movie', id=9, arr_instance_id=3)

    assert ('/movies/m.mkv', 'GLOBAL', 'movie') not in recorded['mapped'], (
        f'the movie path was remapped globally; got {recorded["mapped"]!r}')
    assert recorded['translated']['video_path'] == '/mapped/movies/m.mkv'
