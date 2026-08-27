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
def endpoint(schema_session, monkeypatch):
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

    def _extract(video_path, language_code2, media_type, hi=False, forced=False,
                 arr_instance_id=None):
        recorded['extracted'] = {
            'video_path': video_path, 'media_type': media_type,
            'arr_instance_id': arr_instance_id,
        }
        return None  # short-circuits the handler right after the call

    monkeypatch.setattr(api_mod, 'extract_embedded_subtitle', _extract)

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
