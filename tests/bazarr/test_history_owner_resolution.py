# coding=utf-8
"""History ownership must survive missing media rows (LavX/bazarr#404)."""

import logging
from datetime import datetime
from functools import partial
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, event, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def history_session():
    from app.database import Base

    # The cutover migration requires an owner, while the ORM still allows NULL.
    # Clone the metadata so this fixture cannot change other tests' schemas.
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    for name in ('table_history', 'table_history_movie'):
        metadata.tables[name].c.arr_instance_id.nullable = False

    engine = create_engine('sqlite:///:memory:')

    @event.listens_for(engine, 'connect')
    def enable_foreign_keys(connection, connection_record):
        connection.execute('PRAGMA foreign_keys=ON')

    metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(params=['episode', 'movie'])
def history(request, history_session, monkeypatch):
    from app.database import TableHistory, TableHistoryMovie
    from radarr import history as movie_history
    from sonarr import history as episode_history

    if request.param == 'episode':
        case = SimpleNamespace(
            module=episode_history,
            log=partial(episode_history.history_log, sonarr_series_id=10),
            table=TableHistory,
            upstream_arg='sonarr_episode_id',
            upstream_column='sonarrEpisodeId',
            local_refs={'episode_id': 601, 'series_id': 501},
            resolver='_resolve_episode_owner_row',
            score_out_of=360,
            instance_kind='sonarr',
        )
    else:
        case = SimpleNamespace(
            module=movie_history,
            log=movie_history.history_log_movie,
            table=TableHistoryMovie,
            upstream_arg='radarr_id',
            upstream_column='radarrId',
            local_refs={'movie_id': 601},
            resolver='_resolve_movie_owner_row',
            score_out_of=180,
            instance_kind='radarr',
        )
    case.kind = request.param
    case.events = []
    monkeypatch.setattr(case.module, 'database', history_session)
    monkeypatch.setattr(case.module, 'event_stream', lambda **event: case.events.append(event))
    return case


@pytest.fixture
def download_result():
    return SimpleNamespace(
        message='Subtitle downloaded',
        path='/media/video.mkv',
        language_code='en',
        provider='provider-a',
        score=75,
        subs_id='subtitle-123',
        subs_path='/media/video.en.srt',
        matched=['title', 'year'],
        not_matched=['release_group'],
    )


def add_media(history, session, owner, local_id=601):
    from app.database import TableEpisodes, TableMovies, TableShows

    if history.kind == 'episode':
        session.add(TableShows(
            id=local_id - 100, sonarrSeriesId=10, arr_instance_id=owner,
            path=f'/media/series-{local_id}', title='Series',
        ))
        session.flush()
        session.add(TableEpisodes(
            id=local_id, series_id=local_id - 100,
            sonarrSeriesId=10, sonarrEpisodeId=101, arr_instance_id=owner,
            path=f'/media/video-{local_id}.mkv', title='Episode', season=1, episode=1,
        ))
    else:
        session.add(TableMovies(
            id=local_id, radarrId=101, arr_instance_id=owner,
            path=f'/media/video-{local_id}.mkv', title='Movie', tmdbId='123',
        ))
    session.flush()


def log_history(history, result, media_id=101, **kwargs):
    return history.log(action=1, result=result, **{history.upstream_arg: media_id}, **kwargs)


def test_explicit_owner_survives_missing_media(history, history_session, download_result):
    # A matching upstream ID in another instance must not supply local refs.
    add_media(history, history_session, owner=7, local_id=701)

    log_history(history, download_result, arr_instance_id=4)
    history_session.commit()

    row = history_session.execute(select(history.table)).scalar_one()
    assert row.arr_instance_id == 4
    assert getattr(row, history.upstream_column) == 101
    assert all(getattr(row, ref) is None for ref in history.local_refs)
    assert history.events == [{'type': f'{history.kind}-history'}]


def test_explicit_owner_without_media_id_skips_lookup(history, history_session, download_result, monkeypatch):
    def unexpected_lookup(*args):
        pytest.fail('History without an upstream media ID must not query its owner')

    monkeypatch.setattr(history.module, history.resolver, unexpected_lookup)

    log_history(history, download_result, media_id=None, arr_instance_id=4)
    history_session.commit()

    row = history_session.execute(select(history.table)).scalar_one()
    assert row.arr_instance_id == 4
    assert getattr(row, history.upstream_column) is None
    assert all(getattr(row, ref) is None for ref in history.local_refs)
    assert history.events == [{'type': f'{history.kind}-history'}]


def test_resolved_media_supplies_owner_and_local_refs(history, history_session, download_result):
    add_media(history, history_session, owner=4)

    log_history(history, download_result)
    history_session.commit()

    row = history_session.execute(select(history.table)).scalar_one()
    assert row.arr_instance_id == 4
    assert {ref: getattr(row, ref) for ref in history.local_refs} == history.local_refs
    assert row.provider == 'provider-a'
    assert row.score == 75
    assert row.score_out_of == history.score_out_of
    assert history.events == [{'type': f'{history.kind}-history'}]


@pytest.mark.parametrize('media_state', ['missing', 'no-upstream-id', 'unowned'])
def test_missing_owner_skips_history_and_event(history, history_session, download_result, caplog, media_state):
    from app.database import TableArrInstances

    # Even a configured default cannot establish ownership of missing media.
    history_session.add(TableArrInstances(
        id=4, kind=history.instance_kind, stable_key='default', name='Default',
        port=8989, enabled=1, is_default=1,
    ))
    if media_state == 'unowned':
        add_media(history, history_session, owner=None)
    history_session.flush()
    media_id = None if media_state == 'no-upstream-id' else 101

    with caplog.at_level(logging.WARNING):
        result = log_history(history, download_result, media_id=media_id)
    history_session.commit()

    assert result is None
    assert history_session.execute(select(history.table)).scalars().all() == []
    assert history.events == []
    assert any(
        record.levelno == logging.WARNING
        and history.kind in record.getMessage()
        and 'owning instance' in record.getMessage()
        and str(media_id) in record.getMessage()
        for record in caplog.records
    )


def test_explicit_owner_keeps_history_fields_and_scoped_refs(history, history_session, download_result):
    add_media(history, history_session, owner=7, local_id=701)
    add_media(history, history_session, owner=4)
    history_session.add(history.table(
        id=9, arr_instance_id=4, action=1, description='Previous download',
        timestamp=datetime(2026, 9, 1),
    ))
    history_session.flush()

    before = datetime.now()
    log_history(history, download_result, arr_instance_id=4,
                fake_provider='override-provider', fake_score=80, upgraded_from_id=9)
    history_session.commit()

    row = history_session.execute(select(history.table).where(history.table.id != 9)).scalar_one()
    assert row.arr_instance_id == 4
    assert {ref: getattr(row, ref) for ref in history.local_refs} == history.local_refs
    assert getattr(row, history.upstream_column) == 101
    if history.kind == 'episode':
        assert row.sonarrSeriesId == 10
    assert row.action == 1
    assert row.description == 'Subtitle downloaded'
    assert row.video_path == '/media/video.mkv'
    assert row.language == 'en'
    assert row.provider == 'override-provider'
    assert row.score == 80
    assert row.score_out_of == history.score_out_of
    assert row.subs_id == 'subtitle-123'
    assert row.subtitles_path == '/media/video.en.srt'
    assert row.matched == "['title', 'year']"
    assert row.not_matched == "['release_group']"
    assert row.upgradedFromId == 9
    assert before <= row.timestamp <= datetime.now()
    assert history.events == [{'type': f'{history.kind}-history'}]
