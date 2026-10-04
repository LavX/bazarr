# coding=utf-8
"""Deleting or blacklisting a subtitle only removes a file the media's own index records.

The episode and movie endpoints used to pass the caller's path to the deletion
helper, whose only check was the file extension, so an authenticated request
could remove any subtitle-like file the process can write, another item's
subtitle included, and a blacklist request recorded its entry before anything
was checked. The path now has to name an entry in the owning row's index, the
file removed is that entry's mapped path, the check is asked again under the
subtitle write locks, and a blacklist entry is recorded only once the file is
really removed, so a refused or failed request records nothing.
"""

import json
import os
from contextlib import contextmanager
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

from flask import Flask
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

import pytest

OWNERS = {'series': (1, 2), 'movie': (3, 4)}
KINDS = pytest.mark.parametrize('kind', ['series', 'movie'])
ENDPOINTS = pytest.mark.parametrize('blacklist', [False, True], ids=['delete', 'blacklist'])


@pytest.fixture(scope='module', params=['sqlite', 'postgresql'])
def schema_engine(request, tmp_path_factory):
    """The full schema, built once per engine for the whole module.

    Building it again for every case cost far more than the cases themselves,
    so each case empties it instead. Both engines run in AUTOCOMMIT, as the
    application's does.
    """
    from app.database import Base, configure_sqlite_connection

    admin = schema = None
    if request.param == 'sqlite':
        engine = sa.create_engine(f"sqlite:///{tmp_path_factory.mktemp('ownership') / 'bazarr.db'}",
                                  poolclass=NullPool, isolation_level='AUTOCOMMIT')
        sa.event.listen(engine, 'connect', configure_sqlite_connection)
    else:
        url = os.environ.get('BAZARR_PG_TEST_URL')
        if not url:
            pytest.skip('Set BAZARR_PG_TEST_URL to exercise PostgreSQL')
        # A schema of its own, leaving other tests and services alone.
        schema = f'delete_ownership_{uuid4().hex}'
        admin = sa.create_engine(url, isolation_level='AUTOCOMMIT')
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine = sa.create_engine(url, isolation_level='AUTOCOMMIT',
                                  connect_args={'options': f'-csearch_path={schema}'})
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        if admin is not None:
            with admin.connect() as conn:
                conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


@pytest.fixture
def library(schema_engine, tmp_path, monkeypatch):
    """Two instances per media type, each with two items that share a folder.

    Every instance stores remote paths and maps them to its own local folder,
    so a path only resolves through the owner's mapping.
    """
    from app.database import Base
    from utilities.path_mappings import path_mappings

    session = scoped_session(sessionmaker(bind=schema_engine, autoflush=False,
                                          expire_on_commit=False))
    # Per-instance path mappings are read through the application session.
    monkeypatch.setattr('app.database.database', session)
    monkeypatch.setattr(path_mappings, 'path_mapping_series', [])
    monkeypatch.setattr(path_mappings, 'path_mapping_movies', [])
    try:
        yield _populate(session, tmp_path)
    finally:
        session.remove()
        # The next case starts from an empty schema again.
        with schema_engine.connect() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


def _populate(session, tmp_path):
    from app.database import TableArrInstances, TableEpisodes, TableMovies, TableShows

    items = {}
    for kind, owners in OWNERS.items():
        for owner in owners:
            remote = f'/remote/{kind}/{owner}'
            local = tmp_path / kind / str(owner)
            local.mkdir(parents=True)
            session.add(TableArrInstances(
                id=owner, kind='sonarr' if kind == 'series' else 'radarr', stable_key=f'k{owner}',
                name=f'Instance {owner}', port=8989, path_mappings=json.dumps([[remote, str(local)]])))
            session.flush()
            if kind == 'series':
                session.add(TableShows(id=100 + owner, sonarrSeriesId=4, arr_instance_id=owner,
                                       path=remote, title=f'Show {owner}', tags='[]'))
                session.flush()
            for media_id, name in ((5, 'Video'), (6, 'Other')):
                (local / f'{name}.mkv').write_bytes(b'video')
                (local / f'{name}.en.srt').write_bytes(b'subtitle')
                stored = f'{remote}/{name}.en.srt'
                index = str([['en', stored, 8]])
                if kind == 'series':
                    row = TableEpisodes(
                        id=owner * 10 + media_id, series_id=100 + owner, sonarrSeriesId=4,
                        sonarrEpisodeId=media_id, arr_instance_id=owner, path=f'{remote}/{name}.mkv',
                        title=name, season=1, episode=media_id, subtitles=index)
                else:
                    row = TableMovies(
                        id=owner * 10 + media_id, radarrId=media_id, arr_instance_id=owner,
                        path=f'{remote}/{name}.mkv', title=name, tmdbId=str(owner * 10 + media_id),
                        subtitles=index)
                session.add(row)
                items[kind, owner, media_id] = SimpleNamespace(
                    row=row, stored=stored, subtitle=local / f'{name}.en.srt', video=local / f'{name}.mkv')
    session.flush()
    return SimpleNamespace(session=session, items=items, root=tmp_path)


def _endpoint(kind, blacklist):
    package = 'episodes' if kind == 'series' else 'movies'
    module = import_module(f'api.{package}.blacklist' if blacklist else f'api.{package}.{package}_subtitles')
    if kind == 'series':
        resource = module.EpisodesBlacklist if blacklist else module.EpisodesSubtitles
    else:
        resource = module.MoviesBlacklist if blacklist else module.MoviesSubtitles
    return module, resource


def _request(kind, blacklist, subtitle_path, owner, media_id=5):
    values = {'episodeid' if kind == 'series' else 'radarrid': media_id, 'language': 'en',
              'subtitles_path' if blacklist else 'path': str(subtitle_path)}
    if kind == 'series':
        values['seriesid'] = 4
    if blacklist:
        values.update(provider='provider', subs_id='subtitle-id')
    else:
        values.update(forced='false', hi='false')
    if owner is not None:
        values['arr_instance_id'] = owner
    _, resource = _endpoint(kind, blacklist)
    with Flask(__name__).test_request_context('/api/test?' + urlencode(values),
                                              method='POST' if blacklist else 'DELETE'):
        handler = resource.post if blacklist else resource.delete
        return handler.__wrapped__(resource())


@pytest.fixture
def recorded(library, monkeypatch):
    """Both endpoint pairs with every side effect recorded instead of performed."""
    calls = []

    def deleted(**kwargs):
        calls.append(('delete', kwargs))
        if kwargs.get('after_delete'):
            kwargs['after_delete']()
        return True

    for kind in OWNERS:
        for blacklist in (False, True):
            module, _ = _endpoint(kind, blacklist)
            monkeypatch.setattr(module, 'database', library.session)
            monkeypatch.setattr(module, 'delete_subtitles', deleted)
            for name in ('blacklist_log', 'blacklist_log_movie', 'episode_download_subtitles',
                         'movies_download_subtitles', 'event_stream'):
                if hasattr(module, name):
                    monkeypatch.setattr(module, name,
                                        lambda *args, _name=name, **kwargs: calls.append((_name, kwargs)))
    return calls


def _untouched(library):
    return all(item.subtitle.read_bytes() == b'subtitle' for item in library.items.values())


@KINDS
@ENDPOINTS
def test_another_instances_subtitle_is_refused(library, recorded, kind, blacklist):
    first, second = OWNERS[kind]

    answer = _request(kind, blacklist, library.items[kind, first, 5].subtitle, owner=second)

    assert answer[1] == 403
    assert recorded == []
    assert _untouched(library)


@KINDS
@ENDPOINTS
def test_another_items_subtitle_in_the_same_folder_is_refused(library, recorded, kind, blacklist):
    owner = OWNERS[kind][0]
    other = library.items[kind, owner, 6]

    for path in (other.subtitle, other.stored):
        answer = _request(kind, blacklist, path, owner=owner)

        assert answer[1] == 403
    assert recorded == []
    assert _untouched(library)


@KINDS
@ENDPOINTS
@pytest.mark.parametrize('target', ['outside', 'unindexed', 'relative', 'traversal', 'other media type'])
def test_a_path_outside_the_index_is_refused(library, recorded, kind, blacklist, target):
    owner = OWNERS[kind][0]
    item = library.items[kind, owner, 5]
    outside = library.root / 'elsewhere' / 'notes.txt'
    outside.parent.mkdir()
    outside.write_bytes(b'keep')
    unindexed = item.video.with_name('Video.fr.srt')
    unindexed.write_bytes(b'keep')
    other_kind = 'movie' if kind == 'series' else 'series'
    path = {
        'outside': outside,
        'unindexed': unindexed,
        'relative': 'Video.en.srt',
        # Normalises to the other instance's indexed subtitle.
        'traversal': f'{item.subtitle.parent}/../{OWNERS[kind][1]}/Video.en.srt',
        # An episode's subtitle named through the movie endpoint, and back.
        'other media type': library.items[other_kind, OWNERS[other_kind][0], 5].subtitle,
    }[target]

    answer = _request(kind, blacklist, path, owner=owner)

    assert answer[1] == 403
    assert recorded == []
    assert outside.read_bytes() == b'keep' and unindexed.read_bytes() == b'keep'
    assert _untouched(library)


@KINDS
@ENDPOINTS
def test_an_ambiguous_unscoped_id_is_refused(library, recorded, kind, blacklist):
    answer = _request(kind, blacklist, library.items[kind, OWNERS[kind][0], 5].subtitle, owner=None)

    assert answer[1] == 409
    assert recorded == []
    assert _untouched(library)


@KINDS
@ENDPOINTS
@pytest.mark.parametrize('owner', ['missing item', 'missing instance'])
def test_unknown_media_is_not_found(library, recorded, kind, blacklist, owner):
    item = library.items[kind, OWNERS[kind][0], 5]

    if owner == 'missing item':
        answer = _request(kind, blacklist, item.subtitle, owner=OWNERS[kind][0], media_id=7)
    else:
        answer = _request(kind, blacklist, item.subtitle, owner=99)

    assert answer[1] == 404
    assert recorded == []
    assert _untouched(library)


@KINDS
@ENDPOINTS
@pytest.mark.parametrize('form', ['local', 'stored', 'global'])
@pytest.mark.parametrize('explicit_owner', [True, False], ids=['owner', 'no-owner'])
def test_the_indexed_subtitle_resolves_through_its_owner(
        library, recorded, monkeypatch, kind, blacklist, form, explicit_owner):
    from utilities.path_mappings import path_mappings

    first, second = OWNERS[kind]
    item = library.items[kind, second, 5]
    if not explicit_owner:
        library.session.delete(library.items[kind, first, 5].row)
        library.session.flush()
    # The media pages show paths through the global mapping, which can differ
    # from the owning instance's own mapping.
    shown = library.root / 'global'
    monkeypatch.setattr(path_mappings, 'path_mapping_movies' if kind == 'movie' else 'path_mapping_series',
                        [['/remote', str(shown)]])
    path = {'local': item.subtitle, 'stored': item.stored,
            'global': f'{shown}/{kind}/{second}/Video.en.srt'}[form]

    answer = _request(kind, blacklist, path, owner=second if explicit_owner else None)

    expected_body = {'job_id': None} if kind == 'series' and blacklist else ''
    assert answer == (expected_body, 200 if blacklist else 204)
    assert recorded[0][0] == 'delete'
    target = recorded[0][1]
    assert target['arr_instance_id'] == second
    # The indexed entry is what gets removed, through the owner's mapping.
    assert target['subtitles_path'] == item.stored
    assert target['media_path'] == str(item.video)
    assert callable(target['revalidate'])
    if kind == 'series':
        assert target['sonarr_series_id'] == 4 and target['sonarr_episode_id'] == 5
    else:
        assert target['radarr_id'] == 5
    if blacklist:
        logged, queued, _ = recorded[1:]
        assert logged[0] == ('blacklist_log' if kind == 'series' else 'blacklist_log_movie')
        assert logged[1]['arr_instance_id'] == second
        assert queued[1]['arr_instance_id'] == second
    else:
        assert 'after_delete' not in target or target['after_delete'] is None


@KINDS
@ENDPOINTS
@pytest.mark.parametrize('index', [
    None,
    'not a list',
    "{'en': '/remote/x.en.srt'}",
    "[None, 'x', ['en'], ['en', None, 8], ['en', 5, 8], ['en', '', 8]]",
    '[' * 200,
], ids=['empty', 'unparsable', 'a mapping', 'unusable entries', 'too deeply nested'])
def test_a_malformed_index_refuses_deletion(library, recorded, kind, blacklist, index):
    owner = OWNERS[kind][0]
    item = library.items[kind, owner, 5]
    item.row.subtitles = index
    library.session.flush()

    answer = _request(kind, blacklist, item.subtitle, owner=owner)

    assert answer[1] == 403
    assert recorded == []
    assert _untouched(library)


@KINDS
@ENDPOINTS
def test_malformed_entries_do_not_hide_a_valid_one(library, recorded, kind, blacklist):
    owner = OWNERS[kind][0]
    item = library.items[kind, owner, 5]
    item.row.subtitles = str([None, ['en'], ['en', None, 8], ['en', item.stored, 8]])
    library.session.flush()

    answer = _request(kind, blacklist, item.subtitle, owner=owner)

    expected_body = {'job_id': None} if kind == 'series' and blacklist else ''
    assert answer == (expected_body, 200 if blacklist else 204)
    assert recorded[0][1]['subtitles_path'] == item.stored


@KINDS
@ENDPOINTS
def test_an_entry_that_does_not_map_to_an_absolute_file_is_refused(library, recorded, kind, blacklist):
    owner = OWNERS[kind][0]
    item = library.items[kind, owner, 5]
    item.row.subtitles = str([['en', 'Video.en.srt', 8]])
    library.session.flush()

    answer = _request(kind, blacklist, 'Video.en.srt', owner=owner)

    assert answer[1] == 403
    assert recorded == []


@KINDS
def test_excluding_a_subtitle_that_is_no_longer_indexed_says_so(library, recorded, kind):
    """History offers Exclude on every download, including ones deleted or upgraded since."""
    owner = OWNERS[kind][0]
    item = library.items[kind, owner, 5]
    item.row.subtitles = '[]'
    library.session.flush()

    # History sends the path as it was stored.
    answer = _request(kind, True, item.stored, owner=owner)

    label = 'episode' if kind == 'series' else 'movie'
    assert answer == (f"Subtitle is not one of this {label}'s current subtitles", 403)
    assert recorded == []


# The real deletion helper from here on, with only its notifications stubbed,
# so the blacklist row, the unlink and the revalidation are the real ones.

@pytest.fixture
def real_delete(library, monkeypatch):
    from radarr import blacklist as radarr_blacklist
    from sonarr import blacklist as sonarr_blacklist
    from subtitles.tools import delete

    effects = []
    for module in (sonarr_blacklist, radarr_blacklist):
        monkeypatch.setattr(module, 'database', library.session)
        monkeypatch.setattr(module, 'event_stream', lambda *args, **kwargs: None)
    for name in ('store_subtitles', 'store_subtitles_movie', 'history_log', 'history_log_movie',
                 'notify_sonarr', 'notify_radarr', 'event_stream', 'call_external_webhook'):
        monkeypatch.setattr(delete, name, lambda *args, _name=name, **kwargs: effects.append(_name))
    monkeypatch.setattr(delete, 'client_for_instance', lambda *args, **kwargs: None)
    monkeypatch.setattr(delete, 'publication_callback', lambda *args, **kwargs: None)
    monkeypatch.setattr(delete, 'language_from_alpha2', lambda language: 'English')
    for kind in OWNERS:
        for blacklist in (False, True):
            module, _ = _endpoint(kind, blacklist)
            monkeypatch.setattr(module, 'database', library.session)
            for name in ('episode_download_subtitles', 'movies_download_subtitles', 'event_stream'):
                if hasattr(module, name):
                    monkeypatch.setattr(module, name,
                                        lambda *args, _name=name, **kwargs: effects.append(_name))
    return SimpleNamespace(delete=delete, effects=effects)


def _blacklisted(library, kind):
    from app.database import TableBlacklist, TableBlacklistMovie

    table = TableBlacklist if kind == 'series' else TableBlacklistMovie
    return [row.arr_instance_id for row in library.session.execute(select(table)).scalars()]


@KINDS
def test_blacklist_is_recorded_only_for_an_accepted_delete(library, real_delete, kind):
    first, second = OWNERS[kind]
    outside = library.root / 'notes.txt'
    outside.write_bytes(b'keep')

    for path, owner in ((library.items[kind, first, 5].subtitle, second), (outside, first)):
        assert _request(kind, True, path, owner=owner)[1] == 403
    assert _blacklisted(library, kind) == []
    assert real_delete.effects == []
    assert outside.read_bytes() == b'keep'
    assert _untouched(library)

    item = library.items[kind, second, 5]
    expected_body = {'job_id': None} if kind == 'series' else ''
    assert _request(kind, True, item.subtitle, owner=second) == (expected_body, 200)
    assert _blacklisted(library, kind) == [second]
    assert not item.subtitle.exists()
    assert library.items[kind, first, 5].subtitle.read_bytes() == b'subtitle'


@KINDS
@pytest.mark.parametrize('failure', ['already gone', 'permission denied'])
def test_a_failed_unlink_blacklists_nothing(library, real_delete, monkeypatch, kind, failure):
    owner = OWNERS[kind][0]
    item = library.items[kind, owner, 5]
    if failure == 'already gone':
        # Still in the index, but no longer on disk.
        item.subtitle.unlink()
    else:
        def refuse(path):
            raise PermissionError(path)

        monkeypatch.setattr(real_delete.delete.os, 'remove', refuse)

    answer = _request(kind, True, item.subtitle, owner=owner)

    assert answer == ('Subtitles file not found or permission issue.', 500)
    assert _blacklisted(library, kind) == []
    # Reindexed, and nothing downloaded to replace a subtitle that was not removed.
    assert real_delete.effects == ['store_subtitles' if kind == 'series' else 'store_subtitles_movie']
    if failure == 'permission denied':
        assert item.subtitle.read_bytes() == b'subtitle'


@KINDS
def test_a_repeated_blacklist_request_records_one_entry(library, real_delete, kind):
    owner = OWNERS[kind][0]
    item = library.items[kind, owner, 5]

    expected_body = {'job_id': None} if kind == 'series' else ''
    assert _request(kind, True, item.subtitle, owner=owner) == (expected_body, 200)
    # The reindex is recorded rather than run, so the index still lists the
    # removed file, as it does for a concurrent duplicate that passed its
    # check before the first request's reindex landed.
    assert _request(kind, True, item.subtitle, owner=owner)[1] == 500

    assert _blacklisted(library, kind) == [owner]


@KINDS
@ENDPOINTS
@pytest.mark.parametrize('change', ['reindexed', 'moved', 'reassigned'])
def test_ownership_is_asked_again_under_the_write_locks(
        library, real_delete, monkeypatch, kind, blacklist, change):
    owner = OWNERS[kind][0]
    item = library.items[kind, owner, 5]
    real_locks = real_delete.delete.subtitle_write_locks

    @contextmanager
    def locks(*paths, **kwargs):
        # The index changes after the request was checked but before the locks
        # are held, which is the window the second check closes.
        if change == 'reindexed':
            item.row.subtitles = '[]'
        elif change == 'moved':
            item.row.path = f'/remote/{kind}/{owner}/Renamed.mkv'
        else:
            item.row.arr_instance_id = OWNERS[kind][1] + 10
        library.session.flush()
        with real_locks(*paths, **kwargs) as states:
            yield states

    monkeypatch.setattr(real_delete.delete, 'subtitle_write_locks', locks)

    answer = _request(kind, blacklist, item.subtitle, owner=owner)

    assert answer[1] == 409
    assert item.subtitle.read_bytes() == b'subtitle'
    assert _blacklisted(library, kind) == []
    assert real_delete.effects == []


@KINDS
@ENDPOINTS
def test_a_row_without_an_owner_resolves_through_the_global_mapping(
        library, real_delete, monkeypatch, kind, blacklist):
    """The single-instance install, where rows carry no owner at all."""
    from utilities.path_mappings import path_mappings

    first, second = OWNERS[kind]
    item = library.items[kind, first, 5]
    library.session.delete(library.items[kind, second, 5].row)
    item.row.arr_instance_id = None
    library.session.flush()
    monkeypatch.setattr(path_mappings, 'path_mapping_movies' if kind == 'movie' else 'path_mapping_series',
                        [[f'/remote/{kind}/{first}', str(item.subtitle.parent)]])

    expected_body = {'job_id': None} if kind == 'series' and blacklist else ''
    assert _request(kind, blacklist, item.subtitle, owner=None) == (expected_body, 200 if blacklist else 204)

    assert not item.subtitle.exists()
    assert _blacklisted(library, kind) == ([None] if blacklist else [])
    assert library.items[kind, second, 5].subtitle.read_bytes() == b'subtitle'


@KINDS
def test_an_accepted_delete_removes_only_the_indexed_file(library, real_delete, kind):
    owner = OWNERS[kind][0]
    item = library.items[kind, owner, 5]

    assert _request(kind, False, item.subtitle, owner=owner) == ('', 204)

    assert not item.subtitle.exists()
    assert all(other.subtitle.exists() for key, other in library.items.items() if key != (kind, owner, 5))
    assert _blacklisted(library, kind) == []
    assert ('store_subtitles' if kind == 'series' else 'store_subtitles_movie') in real_delete.effects


def test_shared_unlink_orders_revalidation_publication_and_blacklist(tmp_path, monkeypatch):
    from subtitles.tools import delete

    media = tmp_path / 'video.mkv'
    media.write_bytes(b'video')
    subtitle = tmp_path / 'video.en.srt'
    subtitle.write_bytes(b'subtitle')
    calls = []
    original_remove = delete.os.remove
    real_locks = delete.subtitle_write_locks

    @contextmanager
    def locks(*paths, **kwargs):
        with real_locks(*paths, **kwargs) as states:
            calls.append('locked')
            yield states
        calls.append('released')

    def remove(path):
        calls.append('unlink')
        assert subtitle.exists()
        original_remove(path)

    def revalidate():
        calls.append('revalidate')
        assert subtitle.exists()

    def after_delete():
        calls.append('blacklist')
        assert not subtitle.exists()

    monkeypatch.setattr(delete, 'subtitle_write_locks', locks)
    monkeypatch.setattr(delete.os, 'remove', remove)

    assert delete._delete_subtitle_file(
        str(media), str(subtitle), calls.append,
        revalidate=revalidate, after_delete=after_delete)
    assert calls == ['locked', 'revalidate', 'unlink', str(subtitle), 'blacklist', 'released']
    assert not subtitle.exists()


@pytest.mark.parametrize('failure', ['revalidate', 'already gone', 'permission denied'])
def test_shared_unlink_records_nothing_when_the_deletion_fails(tmp_path, monkeypatch, failure):
    from subtitles.tools import delete

    media = tmp_path / 'video.mkv'
    media.write_bytes(b'video')
    subtitle = tmp_path / 'video.en.srt'
    subtitle.write_bytes(b'subtitle')
    calls = []

    def revalidate():
        calls.append('revalidate')
        if failure == 'revalidate':
            raise RuntimeError(failure)

    def refuse(path):
        raise PermissionError(path)

    if failure == 'already gone':
        subtitle.unlink()
    elif failure == 'permission denied':
        monkeypatch.setattr(delete.os, 'remove', refuse)

    def unlink():
        return delete._delete_subtitle_file(
            str(media), str(subtitle), calls.append,
            revalidate=revalidate, after_delete=lambda: calls.append('blacklist'))

    if failure == 'revalidate':
        with pytest.raises(RuntimeError, match=failure):
            unlink()
    else:
        assert unlink() is False
    assert calls == ['revalidate']
    assert failure == 'already gone' or subtitle.read_bytes() == b'subtitle'
