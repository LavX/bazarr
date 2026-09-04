# coding=utf-8
"""The indexer must index the row it was asked to, not whichever one is first.

``store_subtitles`` resolves the owning instance itself, from the media path
alone, with an unscoped ``.first()``. Neither the path nor the upstream file id
is unique across instances, so when two instances hold a row for the same remote
path the pick is arbitrary.

That resolved owner is not incidental. It feeds the ``_pr`` / ``_prr`` closures
used for every path the function touches, it selects which ffprobe cache row the
embedded pass reads and overwrites, and the unscoped UPDATE at the end writes the
computed listing into every row sharing the path. Per-instance path mappings are
the whole point of multi-instance support, so two instances can map the same
remote path at different local files: exactly when an arbitrary pick produces
wrong data rather than a harmless duplicate.

Callers know the owner. They must be able to say so.
"""
import ast

import pytest

from sqlalchemy import select

from app.database import TableEpisodes, TableMovies, TableShows


@pytest.fixture
def stub_indexing(monkeypatch):
    """Neutralise the filesystem and the external-subtitle scan.

    Leaves the database work, which is what these tests are about.
    """
    import subtitles.indexer.movies as mv
    import subtitles.indexer.series as se

    for mod in (se, mv):
        monkeypatch.setattr(mod.os.path, 'exists', lambda p: True)
        monkeypatch.setattr(mod, 'search_external_subtitles', lambda *a, **kw: {})
        monkeypatch.setattr(mod, 'add_sync_engine_outputs', lambda folder, subs, **kw: subs)
        monkeypatch.setattr(mod, 'add_combined_outputs', lambda folder, subs, **kw: subs)
        monkeypatch.setattr(mod, 'guess_external_subtitles', lambda *a, **kw: {})
        monkeypatch.setattr(mod, 'event_stream', lambda *a, **kw: None)
    # Named differently in the two modules.
    monkeypatch.setattr(se, 'list_missing_subtitles', lambda *a, **kw: None)
    monkeypatch.setattr(mv, 'list_missing_subtitles_movies', lambda *a, **kw: None)
    return se, mv


@pytest.fixture
def two_series_rows(schema_session, stub_indexing, monkeypatch):
    """Two episodes on different instances sharing one remote path."""
    se, _mv = stub_indexing
    monkeypatch.setattr(se, 'database', schema_session)
    monkeypatch.setattr(se.settings.general, 'use_embedded_subs', False, raising=False)

    schema_session.add_all([
        TableShows(id=1, arr_instance_id=1, sonarrSeriesId=1, title='S', path='/tv/s', profileId=None),
        TableShows(id=2, arr_instance_id=2, sonarrSeriesId=1, title='S', path='/tv/s', profileId=None),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(id=1, arr_instance_id=1, series_id=1, sonarrSeriesId=1,
                      sonarrEpisodeId=11, title='E', path='/tv/s/e.mkv', season=1, episode=1,
                      episode_file_id=500, file_size=111, subtitles="[['en', '/one.srt', 1]]"),
        TableEpisodes(id=2, arr_instance_id=2, series_id=2, sonarrSeriesId=1,
                      sonarrEpisodeId=22, title='E', path='/tv/s/e.mkv', season=1, episode=1,
                      episode_file_id=900, file_size=222, subtitles="[['fr', '/two.srt', 2]]"),
    ])
    schema_session.commit()
    return se, schema_session


def _subs(session, table, row_id):
    return session.execute(select(table.subtitles).where(table.id == row_id)).scalar()


@pytest.mark.parametrize('media_type', ['series', 'movie'])
@pytest.mark.parametrize('subfolder', ['current', 'relative', 'absolute'])
def test_sync_outputs_belong_to_the_indexed_video(schema_session, monkeypatch, tmp_path,
                                                 media_type, subfolder):
    from subzero.language import Language
    import subtitles.indexer.movies as mv
    import subtitles.indexer.series as se

    module = se if media_type == 'series' else mv
    table = TableEpisodes if media_type == 'series' else TableMovies
    store = se.store_subtitles if media_type == 'series' else mv.store_subtitles_movie
    stems = (['Show.S01E01', 'Show.S01E02', 'Show.S01E01.Extended'] if media_type == 'series'
             else ['Movie (2020)', 'Other (2021)', 'Movie (2020).Extended'])
    media_folder = tmp_path / 'media'
    media_folder.mkdir()
    subtitle_folder = {'current': media_folder, 'relative': media_folder / 'subs',
                       'absolute': tmp_path / 'shared-subs'}[subfolder]
    subtitle_folder.mkdir(exist_ok=True)
    custom_folder = 'subs' if subfolder == 'relative' else str(subtitle_folder)
    monkeypatch.setattr(module.settings.general, 'use_embedded_subs', False)
    monkeypatch.setattr(module.settings.general, 'single_language', False)
    monkeypatch.setattr(module.settings.general, 'subfolder', subfolder)
    monkeypatch.setattr(module.settings.general, 'subfolder_custom', custom_folder)
    monkeypatch.setattr(module.core, 'CUSTOM_PATHS', [])
    monkeypatch.setattr(module, 'database', schema_session)
    monkeypatch.setattr(module, 'get_language_set', lambda: {Language.fromietf('en')})
    monkeypatch.setattr(module, 'alpha2_from_alpha3', lambda code: Language(code).alpha2)
    monkeypatch.setattr(module.path_mappings, 'path_replace_instance', lambda p, *a: p)
    monkeypatch.setattr(module.path_mappings, 'path_replace_reverse_instance', lambda p, *a: p)
    monkeypatch.setattr(module, 'event_stream', lambda *a, **kw: None)
    missing = 'list_missing_subtitles' if media_type == 'series' else 'list_missing_subtitles_movies'
    monkeypatch.setattr(module, missing, lambda *a, **kw: None)

    if media_type == 'series':
        schema_session.add(TableShows(id=1, arr_instance_id=1, sonarrSeriesId=1, title='Show',
                                      path=str(media_folder), profileId=None))
        schema_session.flush()

    suffixes = {'en.srt': 'en', 'hu.ffsubsync.srt': 'hu:sync-ffsubsync',
                'en.hi.alass.ass': 'en:hi:sync-alass',
                'fr.forced.autosubsync.vtt': 'fr:forced:sync-autosubsync'}
    expected = {}
    for row_id, stem in enumerate(stems, 1):
        video = media_folder / f'{stem}.mkv'
        video.write_bytes(b'video')
        if media_type == 'series':
            row = TableEpisodes(id=row_id, arr_instance_id=1, series_id=1, sonarrSeriesId=1,
                                sonarrEpisodeId=row_id, title=stem, path=str(video), season=1,
                                episode=row_id, subtitles='[]')
        else:
            row = TableMovies(id=row_id, arr_instance_id=1, radarrId=row_id, title=stem,
                              path=str(video), tmdbId=str(row_id), subtitles='[]')
        schema_session.add(row)
        expected[row_id] = []
        for suffix, language in suffixes.items():
            subtitle = subtitle_folder / f'{stem}.{suffix}'
            subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nSubtitle\n', encoding='utf-8')
            expected[row_id].append([language, str(subtitle), subtitle.stat().st_size])
    schema_session.commit()

    first_scan = {}
    for scan in range(2):
        for row_id, stem in enumerate(stems, 1):
            video = str(media_folder / f'{stem}.mkv')
            actual = store(video, video, arr_instance_id=1)
            assert len(actual) == len(expected[row_id])
            assert {path: (set(language.split(':')), size) for language, path, size in actual} == {
                path: (set(language.split(':')), size) for language, path, size in expected[row_id]}
            assert ast.literal_eval(_subs(schema_session, table, row_id)) == actual
            if scan == 0:
                first_scan[row_id] = sorted(actual)
            else:
                assert sorted(actual) == first_scan[row_id]


def test_only_the_owning_episode_row_is_indexed(two_series_rows):
    se, session = two_series_rows

    se.store_subtitles('/tv/s/e.mkv', '/local/e.mkv', arr_instance_id=2)

    assert _subs(session, TableEpisodes, 2) == '[]', \
        'the row the caller named must be the one that gets the new listing'
    assert _subs(session, TableEpisodes, 1) == "[['en', '/one.srt', 1]]", \
        "the other instance's listing was overwritten with this file's result"


def test_the_supplied_owner_beats_the_path_lookup_for_the_metadata_cache(two_series_rows,
                                                                        monkeypatch):
    """The embedded pass must probe the named instance's file id, not whichever
    row the unscoped lookup happened to return first."""
    se, session = two_series_rows
    monkeypatch.setattr(se.settings.general, 'use_embedded_subs', True, raising=False)

    seen = {}

    def _reader(path, file_size=None, episode_file_id=None, use_cache=True,
                arr_instance_id=None):
        seen.update(file_size=file_size, episode_file_id=episode_file_id,
                    arr_instance_id=arr_instance_id)
        return []

    monkeypatch.setattr(se, 'embedded_subs_reader', _reader)

    se.store_subtitles('/tv/s/e.mkv', '/local/e.mkv', arr_instance_id=2)

    assert seen['arr_instance_id'] == 2
    assert seen['episode_file_id'] == 900, \
        f"probed the wrong instance's file id: {seen['episode_file_id']}"
    assert seen['file_size'] == 222


def test_the_owning_instance_mapping_is_used(two_series_rows, monkeypatch):
    """A caller-supplied owner has to reach the path mapping too, since that is
    what decides where subtitles are looked for on disk."""
    se, _session = two_series_rows
    calls = []
    monkeypatch.setattr(se.path_mappings, 'path_replace_instance',
                        lambda p, inst, kind: calls.append((p, inst, kind)) or p)

    se.store_subtitles('/tv/s/e.mkv', '/local/e.mkv', arr_instance_id=2)

    assert calls, 'the per-instance mapping was never consulted'
    assert {inst for _p, inst, _k in calls} == {2}, \
        f'the mapping ran for the wrong instance: {calls!r}'


def test_without_an_owner_the_single_instance_path_is_unchanged(schema_session,
                                                                stub_indexing, monkeypatch):
    se, _mv = stub_indexing
    monkeypatch.setattr(se, 'database', schema_session)
    monkeypatch.setattr(se.settings.general, 'use_embedded_subs', False, raising=False)

    schema_session.add(TableShows(id=1, arr_instance_id=None, sonarrSeriesId=1, title='S',
                                  path='/tv/s', profileId=None))
    schema_session.flush()
    schema_session.add(TableEpisodes(id=1, arr_instance_id=None, series_id=1, sonarrSeriesId=1,
                                     sonarrEpisodeId=11, title='E', path='/tv/s/e.mkv',
                                     season=1, episode=1, episode_file_id=500, file_size=111,
                                     subtitles="[['en', '/one.srt', 1]]"))
    schema_session.commit()

    se.store_subtitles('/tv/s/e.mkv', '/local/e.mkv')

    assert _subs(schema_session, TableEpisodes, 1) == '[]'


# ------------------------------------------------------------------- movies

@pytest.fixture
def two_movie_rows(schema_session, stub_indexing, monkeypatch):
    _se, mv = stub_indexing
    monkeypatch.setattr(mv, 'database', schema_session)
    monkeypatch.setattr(mv.settings.general, 'use_embedded_subs', False, raising=False)

    schema_session.add_all([
        TableMovies(id=1, arr_instance_id=1, radarrId=7, title='M', path='/movies/m.mkv',
                    tmdbId='1', movie_file_id=700, file_size=111,
                    subtitles="[['en', '/one.srt', 1]]"),
        TableMovies(id=2, arr_instance_id=2, radarrId=7, title='M', path='/movies/m.mkv',
                    tmdbId='2', movie_file_id=800, file_size=222,
                    subtitles="[['fr', '/two.srt', 2]]"),
    ])
    schema_session.commit()
    return mv, schema_session


def test_only_the_owning_movie_row_is_indexed(two_movie_rows):
    mv, session = two_movie_rows

    mv.store_subtitles_movie('/movies/m.mkv', '/local/m.mkv', arr_instance_id=2)

    assert _subs(session, TableMovies, 2) == '[]'
    assert _subs(session, TableMovies, 1) == "[['en', '/one.srt', 1]]", \
        "the other instance's listing was overwritten with this file's result"


def test_the_supplied_owner_beats_the_path_lookup_for_movies(two_movie_rows, monkeypatch):
    mv, _session = two_movie_rows
    monkeypatch.setattr(mv.settings.general, 'use_embedded_subs', True, raising=False)

    seen = {}

    def _reader(path, file_size=None, movie_file_id=None, use_cache=True,
                arr_instance_id=None):
        seen.update(file_size=file_size, movie_file_id=movie_file_id,
                    arr_instance_id=arr_instance_id)
        return []

    monkeypatch.setattr(mv, 'embedded_subs_reader', _reader)

    mv.store_subtitles_movie('/movies/m.mkv', '/local/m.mkv', arr_instance_id=2)

    assert seen['arr_instance_id'] == 2
    assert seen['movie_file_id'] == 800, \
        f"probed the wrong instance's file id: {seen['movie_file_id']}"


# ------------------------------------------------- the indexer's own scanners

def _record_indexing(monkeypatch, module):
    """Capture (path, owner) for every store_subtitles* call the scanner makes."""
    calls = []
    name = 'store_subtitles' if module.__name__.endswith('series') else 'store_subtitles_movie'
    monkeypatch.setattr(module, name,
                        lambda path, mapped, **kw: calls.append((path, kw.get('arr_instance_id'))))
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', lambda *a, **kw: None)
    monkeypatch.setattr(module.jobs_queue, 'update_job_name', lambda *a, **kw: None)
    return calls


def test_the_full_episode_scan_indexes_both_instances(two_series_rows, monkeypatch):
    """Scoping the UPDATE means an owner-less call now writes one row instead of
    all of them, so a scanner that does not pass the owner silently stops
    indexing every instance but one."""
    se, _session = two_series_rows
    calls = _record_indexing(monkeypatch, se)

    se.series_full_scan_subtitles(job_id=1, use_cache=False)

    assert sorted(owner for _p, owner in calls) == [1, 2], (
        f'the full scan must index each instance under its own owner; got {calls!r}')


def test_the_targeted_episode_scan_passes_the_owner_it_was_given(two_series_rows,
                                                                 monkeypatch):
    se, _session = two_series_rows
    calls = _record_indexing(monkeypatch, se)

    se.series_scan_subtitles(1, arr_instance_id=2)

    assert calls == [('/tv/s/e.mkv', 2)], (
        f'a scan scoped to instance 2 must index instance 2; got {calls!r}')


def test_the_full_movie_scan_indexes_both_instances(two_movie_rows, monkeypatch):
    mv, _session = two_movie_rows
    calls = _record_indexing(monkeypatch, mv)

    mv.movies_full_scan_subtitles(job_id=1, use_cache=False)

    assert sorted(owner for _p, owner in calls) == [1, 2], (
        f'the full scan must index each instance under its own owner; got {calls!r}')


def test_the_targeted_movie_scan_passes_the_owner_it_was_given(two_movie_rows, monkeypatch):
    mv, _session = two_movie_rows
    calls = _record_indexing(monkeypatch, mv)

    mv.movies_scan_subtitles(7, arr_instance_id=2)

    assert calls == [('/movies/m.mkv', 2)]
