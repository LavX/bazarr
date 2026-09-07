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
import unicodedata

import pytest

from sqlalchemy import select, update

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
@pytest.mark.parametrize('naming,single_language', [
    ('original', False), ('case', False), ('unicode', False),
    ('single-language', True), ('single-language', False),
    ('legacy-tagged', True), ('legacy-tagged', False),
    ('legacy-case', True), ('legacy-unicode', True),
])
def test_sync_outputs_belong_to_the_indexed_video(schema_session, monkeypatch, tmp_path,
                                                 media_type, subfolder, naming, single_language):
    from app import database as database_module
    from subzero.language import Language
    from subtitles.tools.subsync_engines import SyncOutputOwnerIndex
    import subtitles.indexer.movies as mv
    import subtitles.indexer.series as se

    module = se if media_type == 'series' else mv
    table = TableEpisodes if media_type == 'series' else TableMovies
    store = se.store_subtitles if media_type == 'series' else mv.store_subtitles_movie
    stems = (['Show.S01E01', 'Show.S01E02', 'Show.S01E01.Extended'] if media_type == 'series'
             else ['Movie (2020)', 'Other (2021)', 'Movie (2020).Extended'])
    if naming in ['unicode', 'legacy-unicode']:
        stems = ['\u00c9pisode.S01E01', '\u00c9pisode.S01E02', '\u00c9pisode.S01E01.Extended']
    elif naming == 'single-language':
        stems = ['Movie', 'Movie.en', 'Movie.en.Extended', 'Movie.hi', 'Movie.forced']
    media_folder = tmp_path / 'media'
    media_folder.mkdir()
    subtitle_folder = {'current': media_folder, 'relative': media_folder / 'subs',
                       'absolute': tmp_path / 'shared-subs'}[subfolder]
    subtitle_folder.mkdir(exist_ok=True)
    custom_folder = 'subs' if subfolder == 'relative' else str(subtitle_folder)
    monkeypatch.setattr(module.settings.general, 'use_embedded_subs', False)
    monkeypatch.setattr(module.settings.general, 'single_language', single_language)
    monkeypatch.setattr(module.settings.general, 'subfolder', subfolder)
    monkeypatch.setattr(module.settings.general, 'subfolder_custom', custom_folder)
    monkeypatch.setattr(module.core, 'CUSTOM_PATHS', [])
    monkeypatch.setattr(module, 'database', schema_session)
    monkeypatch.setattr(database_module, 'database', schema_session)
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
    if naming == 'single-language':
        suffixes = {f'{engine}.srt': f'en:sync-{engine}'
                    for engine in ['ffsubsync', 'alass', 'autosubsync']}
        suffixes.update({'hu.ffsubsync.srt': 'hu:sync-ffsubsync',
                         'en.hi.alass.ass': 'en:hi:sync-alass'})
    elif naming.startswith('legacy-'):
        suffixes['en.ffsubsync.srt'] = 'en:sync-ffsubsync'
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
        subtitle_stem = stem.lower() if naming in ['case', 'legacy-case'] else stem
        if naming in ['unicode', 'legacy-unicode']:
            subtitle_stem = unicodedata.normalize('NFD', stem.lower())
        for suffix, language in suffixes.items():
            subtitle = subtitle_folder / f'{subtitle_stem}.{suffix}'
            subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n'
                                'This is an English subtitle. We are going to the house together. '
                                'There is a friend waiting for us in the garden. '
                                'Please tell everyone that we will arrive before dinner.\n', encoding='utf-8')
            expected[row_id].append([language, str(subtitle), subtitle.stat().st_size])
    schema_session.flush()
    contaminated = [entry for entries in expected.values() for entry in entries]
    schema_session.execute(update(table).values(subtitles=str(contaminated)))
    schema_session.commit()

    first_scan = {}
    for scan in range(3):
        ownership_index = SyncOutputOwnerIndex()
        if scan == 2:
            schema_session.execute(update(table).values(subtitles='[]'))
            schema_session.commit()
        for row_id, stem in enumerate(stems, 1):
            video = str(media_folder / f'{stem}.mkv')
            actual = store(video, video, arr_instance_id=1, ownership_index=ownership_index)
            assert len(actual) == len(expected[row_id])
            assert {path: (set(language.split(':')), size) for language, path, size in actual} == {
                path: (set(language.split(':')), size) for language, path, size in expected[row_id]}
            assert ast.literal_eval(_subs(schema_session, table, row_id)) == actual
            if scan == 0:
                first_scan[row_id] = sorted(actual)
            else:
                assert sorted(actual) == first_scan[row_id]


@pytest.mark.parametrize('media_type', ['series', 'movie'])
@pytest.mark.parametrize('subfolder', ['relative', 'absolute'])
@pytest.mark.parametrize('library_size', [8, 32])
def test_full_scan_amortizes_sync_output_ownership(schema_session, monkeypatch, tmp_path,
                                                 media_type, subfolder, library_size):
    from app import database as database_module
    from subzero.language import Language
    import subtitles.indexer.movies as mv
    import subtitles.indexer.series as se

    module = se if media_type == 'series' else mv
    table = TableEpisodes if media_type == 'series' else TableMovies
    full_scan = se.series_full_scan_subtitles if media_type == 'series' else mv.movies_full_scan_subtitles
    mapping_calls = []
    owner_queries = []
    execute = schema_session.execute

    def counted_execute(statement, *args, **kwargs):
        if getattr(statement, 'is_select', False):
            columns = list(statement.selected_columns)
            if [column.name for column in columns] == ['path', 'arr_instance_id']:
                owner_queries.append(columns[0].table.name)
        return execute(statement, *args, **kwargs)

    def mapped(path, instance, kind):
        mapping_calls.append((instance, kind))
        assert instance == 7
        assert kind in ('episode', 'series', 'movie')
        return str(tmp_path / 'media' / path.removeprefix('/remote/'))

    monkeypatch.setattr(schema_session, 'execute', counted_execute)
    monkeypatch.setattr(module, 'database', schema_session)
    monkeypatch.setattr(database_module, 'database', schema_session)
    monkeypatch.setattr(module.path_mappings, 'path_replace_instance', mapped)
    monkeypatch.setattr(module.path_mappings, 'path_replace_reverse_instance', lambda path, *a: path)
    monkeypatch.setattr(module.settings.general, 'use_embedded_subs', False)
    monkeypatch.setattr(module.settings.general, 'single_language', False)
    monkeypatch.setattr(module.settings.general, 'subfolder', subfolder)
    monkeypatch.setattr(module.settings.general, 'subfolder_custom',
                        '../subs' if subfolder == 'relative' else str(tmp_path / 'subs'))
    monkeypatch.setattr(module.core, 'CUSTOM_PATHS', [])
    monkeypatch.setattr(module, 'get_language_set', lambda: {Language.fromietf('en')})
    monkeypatch.setattr(module, 'alpha2_from_alpha3', lambda code: Language(code).alpha2)
    monkeypatch.setattr(module, 'event_stream', lambda **kw: None)
    missing = 'list_missing_subtitles' if media_type == 'series' else 'list_missing_subtitles_movies'
    monkeypatch.setattr(module, missing, lambda **kw: None)
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', lambda **kw: None)
    monkeypatch.setattr(module.jobs_queue, 'update_job_name', lambda **kw: None)
    if media_type == 'series':
        schema_session.add(TableShows(id=1, arr_instance_id=7, sonarrSeriesId=10,
                                      title='Show', path='/remote', profileId=None))
        schema_session.flush()

    expected = {}
    for row_id in range(1, library_size + 1):
        filename = f'Video{row_id:04}.mkv'
        remote = f'/remote/{row_id}/{filename}'
        video = tmp_path / 'media' / str(row_id) / filename
        video.parent.mkdir(parents=True)
        video.touch()
        folder = tmp_path / 'media' / 'subs' if subfolder == 'relative' else tmp_path / 'subs'
        folder.mkdir(exist_ok=True)
        output = folder / f'Video{row_id:04}.en.ffsubsync.srt'
        output.write_text('1\n00:00:00,000 --> 00:00:01,000\nAn English subtitle.\n')
        indexed_path = video.parent / '..' / 'subs' / output.name if subfolder == 'relative' else output
        expected[row_id] = [['en:sync-ffsubsync', str(indexed_path), output.stat().st_size]]
        if media_type == 'series':
            row = TableEpisodes(id=row_id, arr_instance_id=7, series_id=1, sonarrSeriesId=10,
                                sonarrEpisodeId=row_id, title=filename, path=remote, season=1,
                                episode=row_id, subtitles='[]')
        else:
            row = TableMovies(id=row_id, arr_instance_id=7, radarrId=row_id, title=filename,
                              path=remote, tmdbId=str(row_id), subtitles='[]')
        schema_session.add(row)
    schema_session.commit()

    full_scan(job_id=1, use_cache=False)

    counts = {'library_size': library_size, 'owner_queries': len(owner_queries),
              'path_mappings': len(mapping_calls)}
    print(f'Ownership scan counts: {counts}')
    for row_id, subtitles in expected.items():
        assert ast.literal_eval(_subs(schema_session, table, row_id)) == subtitles
    assert len(owner_queries) == 2, counts
    assert len(mapping_calls) <= 8 * library_size, counts

    # A new scan must see another media type claiming the same generated name.
    conflict = tmp_path / 'media' / 'conflict' / 'Video0001.en.mkv'
    conflict.parent.mkdir()
    conflict.touch()
    if media_type == 'series':
        row = TableMovies(id=1, arr_instance_id=7, radarrId=1, title='Conflict', tmdbId='1',
                          path='/remote/conflict/Video0001.en.mkv', subtitles='[]')
    else:
        schema_session.add(TableShows(id=1, arr_instance_id=7, sonarrSeriesId=10,
                                      title='Show', path='/remote', profileId=None))
        schema_session.flush()
        row = TableEpisodes(id=1, arr_instance_id=7, series_id=1, sonarrSeriesId=10,
                            sonarrEpisodeId=1, title='Conflict', season=1, episode=1,
                            path='/remote/conflict/Video0001.en.mkv', subtitles='[]')
    schema_session.add(row)
    schema_session.commit()
    owner_queries.clear()
    full_scan(job_id=2, use_cache=False)
    assert len(owner_queries) == 2, 'independent scans must rebuild their ownership snapshot'
    assert ast.literal_eval(_subs(schema_session, table, 1)) == []
    for row_id in range(2, library_size + 1):
        assert ast.literal_eval(_subs(schema_session, table, row_id)) == expected[row_id]


@pytest.mark.parametrize('subfolder', ['relative', 'absolute'])
@pytest.mark.parametrize('collision', [False, True])
def test_scan_owner_index_preserves_instance_mapping_and_normalized_conflicts(
        schema_session, monkeypatch, tmp_path, subfolder, collision):
    from app import database as database_module
    from app.config import settings
    from subtitles.tools.subsync_engines import SyncOutputOwnerIndex, sync_output_owner_is_unique
    from utilities.path_mappings import path_mappings

    video = tmp_path / 'one' / '\u00c9pisode.mkv'
    other = tmp_path / 'two' / 'e\u0301PISODE.en.mkv'
    folder = tmp_path / 'subs'
    for path in (video, other):
        path.parent.mkdir()
        path.touch()
    folder.mkdir()
    source = folder / 'e\u0301pisode.en.srt'
    source.touch()
    monkeypatch.setattr(settings.general, 'subfolder', subfolder)
    monkeypatch.setattr(settings.general, 'subfolder_custom', '../subs' if subfolder == 'relative' else str(folder))
    monkeypatch.setattr(database_module, 'database', schema_session)
    for owner in (7, 8):
        schema_session.add(TableMovies(id=owner, arr_instance_id=owner, radarrId=1,
                                      title='Mapped video', path='/same/remote.mkv', tmdbId=str(owner)))
    schema_session.commit()
    mappings = []

    def mapped(path, owner, media_type):
        assert (path, media_type) == ('/same/remote.mkv', 'movie')
        mappings.append(owner)
        return str(other if collision and owner == 8 else video)

    monkeypatch.setattr(path_mappings, 'path_replace_instance', mapped)
    index = SyncOutputOwnerIndex()
    for _ in range(2):
        assert sync_output_owner_is_unique(str(video), str(source), ownership_index=index) is (not collision)
    assert sorted(mappings) == [7, 8]

    # Standalone mutation checks must not inherit the earlier scan snapshot.
    monkeypatch.setattr(path_mappings, 'path_replace_instance', lambda path, owner, kind: str(other if owner == 8 else video))
    assert sync_output_owner_is_unique(str(video), str(source)) is False


def test_scan_owner_index_fails_closed_after_incomplete_library_read(schema_session, monkeypatch, tmp_path):
    from app import database as database_module
    from app.config import settings
    from subtitles.tools.subsync_engines import SyncOutputOwnerIndex, sync_output_owner_is_unique
    from utilities.path_mappings import path_mappings

    video = tmp_path / 'Video.mkv'
    video.touch()
    folder = tmp_path / 'subs'
    folder.mkdir()
    source = folder / 'Video.en.srt'
    source.touch()
    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', 'subs')
    monkeypatch.setattr(database_module, 'database', schema_session)
    monkeypatch.setattr(path_mappings, 'path_replace_instance', lambda path, *args: path)
    schema_session.add(TableShows(id=1, arr_instance_id=7, sonarrSeriesId=10, title='Show', path=str(tmp_path)))
    schema_session.flush()
    schema_session.add(TableEpisodes(id=1, arr_instance_id=7, series_id=1, sonarrSeriesId=10,
                                    sonarrEpisodeId=1, title='Video', path=str(video), season=1, episode=1))
    schema_session.commit()
    execute = schema_session.execute
    queries = []

    def fail_movie_read(statement, *args, **kwargs):
        queries.append(statement)
        if list(statement.selected_columns)[0].table.name == 'table_movies':
            raise RuntimeError('controlled owner lookup failure')
        return execute(statement, *args, **kwargs)

    monkeypatch.setattr(schema_session, 'execute', fail_movie_read)
    index = SyncOutputOwnerIndex()
    assert sync_output_owner_is_unique(str(video), str(source), ownership_index=index) is False
    assert len(queries) == 2
    monkeypatch.setattr(schema_session, 'execute', execute)
    assert sync_output_owner_is_unique(str(video), str(source), ownership_index=index) is False
    assert sync_output_owner_is_unique(str(video), str(source), ownership_index=SyncOutputOwnerIndex()) is True


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
