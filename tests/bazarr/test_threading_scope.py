# coding=utf-8
"""Multi-instance threading-scope regression tests (#156).

Each test seeds TWO arr instances that share the SAME upstream id
(sonarrSeriesId / sonarrEpisodeId / radarrId) and asserts that the owning
``arr_instance_id`` is threaded through write/notify paths so an action on
instance B never gets re-stamped to / leak instance A's metadata.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import scoped_session, sessionmaker

from app.database import (
    Base,
    TableEpisodes,
    TableHistory,
    TableHistoryMovie,
    TableMovies,
    TableShows,
)


# ---------------------------------------------------------------------------
# Finding 1: subtitle-upgrade job must stamp history on the OWNING instance
# ---------------------------------------------------------------------------


@pytest.fixture
def upgrade_db(monkeypatch):
    from subtitles import upgrade

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine))

    settings = SimpleNamespace(
        general=SimpleNamespace(
            days_to_upgrade_subs=365,
            upgrade_manual=False,
            upgrade_subs=True,
            use_sonarr=True,
            use_radarr=True,
        )
    )
    monkeypatch.setattr(upgrade, "database", session)
    monkeypatch.setattr(upgrade, "settings", settings)

    # neutralise the heavy collaborators so we can drive the orchestration loop
    monkeypatch.setattr(upgrade, "get_providers", lambda: ["a-provider"])
    monkeypatch.setattr(upgrade, "store_subtitles", lambda *a, **k: None)
    monkeypatch.setattr(upgrade, "store_subtitles_movie", lambda *a, **k: None)
    monkeypatch.setattr(upgrade, "event_stream", lambda *a, **k: None)
    monkeypatch.setattr(upgrade, "get_audio_profile_languages", lambda *a, **k: [])
    monkeypatch.setattr(upgrade, "_language_still_desired", lambda *a, **k: True)
    monkeypatch.setattr(upgrade, "_is_hi_required", lambda *a, **k: False)
    monkeypatch.setattr(upgrade.path_mappings, "path_replace", lambda p: p)
    monkeypatch.setattr(upgrade.path_mappings, "path_replace_movie", lambda p: p)

    class _JobsQueue:
        def update_job_progress(self, *a, **k):
            pass

        def update_job_name(self, *a, **k):
            pass

    monkeypatch.setattr(upgrade, "jobs_queue", _JobsQueue())

    result = SimpleNamespace(message="done")
    monkeypatch.setattr(upgrade, "generate_subtitles", lambda *a, **k: [result])

    try:
        yield session
    finally:
        session.remove()
        engine.dispose()


def _seed_two_episode_instances(db):
    """Two series/episodes sharing sonarrSeriesId=10 / sonarrEpisodeId=20,
    owned by instances 1 and 3, each with an upgradable history row."""
    now = datetime.now()
    for local_show, local_ep, owner, path in [
        (10, 20, 1, "/series/a/s01e01.mkv"),
        (11, 21, 3, "/series/b/s01e01.mkv"),
    ]:
        db.execute(insert(TableShows).values(
            id=local_show, arr_instance_id=owner, sonarrSeriesId=10,
            path=f"/series/{owner}", title="Show"))
        sub_path = path + ".en.srt"
        db.execute(insert(TableEpisodes).values(
            id=local_ep, series_id=local_show, arr_instance_id=owner,
            episode=1, monitored="True", path=path, season=1,
            sonarrEpisodeId=20, sonarrSeriesId=10, title="Pilot",
            audio_language="[]", subtitles=f'[["en", "{sub_path}"]]'))
        db.execute(insert(TableHistory).values(
            arr_instance_id=owner, series_id=local_show, episode_id=local_ep,
            action=1, description="x", language="en", provider="prov",
            score=90, score_out_of=100, sonarrEpisodeId=20, sonarrSeriesId=10,
            timestamp=now, video_path=path, subtitles_path=sub_path))


def test_episode_upgrade_stamps_history_on_owning_instance(upgrade_db, monkeypatch):
    from subtitles import upgrade

    _seed_two_episode_instances(upgrade_db)
    # Only instance 3's episode is upgradable.
    upgradable_id = upgrade_db.execute(
        TableHistory.__table__.select().where(TableHistory.arr_instance_id == 3)
    ).first().id
    monkeypatch.setattr(
        upgrade, "get_upgradable_episode_subtitles",
        lambda *a, **k: {upgradable_id: None})

    history_calls = []
    notify_calls = []
    monkeypatch.setattr(
        upgrade, "history_log",
        lambda *a, **k: history_calls.append((a, k)))
    monkeypatch.setattr(
        upgrade, "send_notifications",
        lambda *a, **k: notify_calls.append((a, k)))

    upgrade.upgrade_episodes_subtitles(job_id="job-1")

    assert len(history_calls) == 1, "exactly one upgrade should fire"
    _, hkwargs = history_calls[0]
    assert hkwargs.get("arr_instance_id") == 3, (
        "history must be stamped on the owning instance, not an arbitrary "
        f"colliding row; got {hkwargs.get('arr_instance_id')}")
    _, nkwargs = notify_calls[0]
    assert nkwargs.get("arr_instance_id") == 3


def _seed_two_movie_instances(db):
    now = datetime.now()
    for local_movie, owner, path in [
        (30, 2, "/movies/a.mkv"),
        (31, 4, "/movies/b.mkv"),
    ]:
        sub_path = path + ".en.srt"
        db.execute(insert(TableMovies).values(
            id=local_movie, arr_instance_id=owner, path=path, radarrId=30,
            title="Movie", tmdbId=str(local_movie),
            audio_language="[]", subtitles=f'[["en", "{sub_path}"]]'))
        db.execute(insert(TableHistoryMovie).values(
            arr_instance_id=owner, movie_id=local_movie, action=1,
            description="x", language="en", provider="prov", score=90,
            score_out_of=100, radarrId=30, timestamp=now, video_path=path,
            subtitles_path=sub_path))


def test_movie_upgrade_stamps_history_on_owning_instance(upgrade_db, monkeypatch):
    from subtitles import upgrade

    _seed_two_movie_instances(upgrade_db)
    upgradable_id = upgrade_db.execute(
        TableHistoryMovie.__table__.select().where(
            TableHistoryMovie.arr_instance_id == 4)
    ).first().id
    monkeypatch.setattr(
        upgrade, "get_upgradable_movies_subtitles",
        lambda *a, **k: {upgradable_id: None})

    history_calls = []
    notify_calls = []
    monkeypatch.setattr(
        upgrade, "history_log_movie",
        lambda *a, **k: history_calls.append((a, k)))
    monkeypatch.setattr(
        upgrade, "send_notifications_movie",
        lambda *a, **k: notify_calls.append((a, k)))

    upgrade.upgrade_movies_subtitles(job_id="job-1")

    assert len(history_calls) == 1
    _, hkwargs = history_calls[0]
    assert hkwargs.get("arr_instance_id") == 4, (
        "movie history must be stamped on the owning instance; got "
        f"{hkwargs.get('arr_instance_id')}")
    _, nkwargs = notify_calls[0]
    assert nkwargs.get("arr_instance_id") == 4


def test_get_upgradable_episode_scopes_max_timestamp_by_instance(upgrade_db, monkeypatch):
    """Two instances share a library path. Both have an upgradable download
    (action=1) at the SAME video_path but different timestamps. Without
    scoping the (video_path, language) max-timestamp grouping by instance, the
    older instance's candidate is silently dropped (only the global newest
    survives). Both must remain upgradable."""
    from subtitles.upgrade import get_upgradable_episode_subtitles

    now = datetime.now()
    shared_path = "/library/shared/s01e01.mkv"
    for local_show, local_ep, owner, hid, ts in [
        (10, 20, 1, 101, now - timedelta(minutes=10)),
        (11, 21, 3, 102, now),
    ]:
        upgrade_db.execute(insert(TableShows).values(
            id=local_show, arr_instance_id=owner, sonarrSeriesId=10,
            path=f"/s{owner}", title="S"))
        upgrade_db.execute(insert(TableEpisodes).values(
            id=local_ep, series_id=local_show, arr_instance_id=owner, episode=1,
            monitored="True", path=shared_path, season=1, sonarrEpisodeId=20,
            sonarrSeriesId=10, title="Pilot"))
        upgrade_db.execute(insert(TableHistory).values(
            id=hid, arr_instance_id=owner, series_id=local_show,
            episode_id=local_ep, action=1, description="dl", language="en",
            provider="opensubtitlescom", score=90, score_out_of=100,
            sonarrEpisodeId=20, sonarrSeriesId=10, timestamp=ts,
            video_path=shared_path))

    # Both instances' candidates must survive the max-timestamp grouping.
    assert get_upgradable_episode_subtitles() == {101: None, 102: None}


# ---------------------------------------------------------------------------
# Finding 2: custom notifications must scope media metadata to the OWNING
# instance, never expand a colliding upstream id's row from a sibling instance.
# ---------------------------------------------------------------------------


@pytest.fixture
def notifier_db(schema_session, monkeypatch):
    from app import notifier

    monkeypatch.setattr(notifier, "database", schema_session)
    # one custom (JSON) provider so the {bazarr_*} expansion path runs
    monkeypatch.setattr(
        notifier, "get_notifier_providers",
        lambda: [SimpleNamespace(name="JSON", url="json://h/?:t={bazarr_series_title}")])
    return schema_session


def test_send_notifications_scopes_series_metadata_to_owner(notifier_db, monkeypatch):
    from app import notifier

    # Two series share sonarrSeriesId=10; owners 1 and 3 with DIFFERENT titles.
    for local_id, owner, title in [(10, 1, "Wrong"), (11, 3, "Right")]:
        notifier_db.add(TableShows(
            id=local_id, sonarrSeriesId=10, arr_instance_id=owner,
            path=f"/s{owner}", title=title, year="2020", tags="[]"))
    notifier_db.flush()
    for local_id, owner in [(10, 1), (11, 3)]:
        notifier_db.add(TableEpisodes(
            id=local_id + 100, series_id=local_id, sonarrEpisodeId=20,
            sonarrSeriesId=10, arr_instance_id=owner, path=f"/s{owner}/e.mkv",
            title="Ep", season=1, episode=1, monitored="True", subtitles="[]"))
    notifier_db.flush()

    captured = []

    class _Apobj:
        def add(self, url):
            captured.append(url)

        def notify(self, *a, **k):
            pass

    monkeypatch.setattr(notifier, "Apprise", lambda *a, **k: _Apobj())
    monkeypatch.setattr(notifier, "AppriseAsset", lambda *a, **k: None)

    notifier.send_notifications(10, 20, "msg", arr_instance_id=3)

    assert captured, "a notifier URL must be added"
    assert "t=Right" in captured[0], (
        f"custom notifier must expand the OWNING instance's title; got {captured[0]}")
    assert "Wrong" not in captured[0]


def test_send_notifications_movie_scopes_metadata_to_owner(notifier_db, monkeypatch):
    from app import notifier

    monkeypatch.setattr(
        notifier, "get_notifier_providers",
        lambda: [SimpleNamespace(name="JSON", url="json://h/?:t={bazarr_movie_title}")])

    for local_id, owner, title in [(30, 2, "WrongMovie"), (31, 4, "RightMovie")]:
        notifier_db.add(TableMovies(
            id=local_id, radarrId=30, arr_instance_id=owner, path=f"/m{owner}.mkv",
            title=title, year="2020", tmdbId=str(local_id), monitored="True",
            subtitles="[]", tags="[]"))
    notifier_db.flush()

    captured = []

    class _Apobj:
        def add(self, url):
            captured.append(url)

        def notify(self, *a, **k):
            pass

    monkeypatch.setattr(notifier, "Apprise", lambda *a, **k: _Apobj())
    monkeypatch.setattr(notifier, "AppriseAsset", lambda *a, **k: None)

    notifier.send_notifications_movie(30, "msg", arr_instance_id=4)

    assert captured
    assert "t=RightMovie" in captured[0]
    assert "WrongMovie" not in captured[0]


# ---------------------------------------------------------------------------
# Finding 3: notify_sonarr/notify_radarr must build the arr_client from the
# OWNING instance so the RescanSeries/RescanMovie hits the right server.
# ---------------------------------------------------------------------------


def test_delete_subtitles_routes_rescan_to_owning_instance(monkeypatch, tmp_path):
    from subtitles.tools import delete as delete_mod

    sub_file = tmp_path / "movie.en.srt"
    sub_file.write_text("1\n")

    client_calls = []
    notify_calls = []
    sentinel_client = object()

    monkeypatch.setattr(
        delete_mod, "client_for_instance",
        lambda db, instance_id, *a, **k: client_calls.append(instance_id) or sentinel_client)
    monkeypatch.setattr(
        delete_mod, "notify_radarr",
        lambda radarr_id, arr_client=None: notify_calls.append((radarr_id, arr_client)))
    monkeypatch.setattr(delete_mod, "history_log_movie", lambda *a, **k: None)
    monkeypatch.setattr(delete_mod, "store_subtitles_movie", lambda *a, **k: None)
    monkeypatch.setattr(delete_mod, "call_external_webhook", lambda *a, **k: None)
    monkeypatch.setattr(delete_mod, "event_stream", lambda *a, **k: None)
    monkeypatch.setattr(delete_mod, "language_from_alpha2", lambda lang: "English")
    monkeypatch.setattr(delete_mod.path_mappings, "path_replace_movie", lambda p: p)
    monkeypatch.setattr(delete_mod.path_mappings, "path_replace_reverse_movie", lambda p: p)

    class _Db:
        def execute(self, *a, **k):
            class _R:
                def first(self_inner):
                    return SimpleNamespace(imdbId=None, tmdbId=None)
            return _R()

    monkeypatch.setattr(delete_mod, "database", _Db())
    monkeypatch.setattr(
        delete_mod.settings, "general",
        SimpleNamespace(use_plex=False, use_jellyfin=False), raising=False)

    ok = delete_mod.delete_subtitles(
        media_type="movie", language="en", forced=False, hi=False,
        media_path=str(tmp_path / "movie.mkv"), subtitles_path=str(sub_file),
        radarr_id=30, arr_instance_id=7)

    assert ok is True
    assert client_calls == [7], (
        f"rescan client must be built from the owning instance; got {client_calls}")
    assert notify_calls == [(30, sentinel_client)], (
        "notify_radarr must receive the owning instance's client")


# ---------------------------------------------------------------------------
# Finding 5: when arr_instance_id is None and an upstream id collides across
# instances, the owner resolver must prefer the DEFAULT instance, not an
# arbitrary .first() row.
# ---------------------------------------------------------------------------


def _result_stub():
    return SimpleNamespace(
        message="English subtitles downloaded.",
        path="/v/x.mkv", language_code="en", provider="prov", score=90,
        subs_id="sub-1", subs_path="/v/x.en.srt", matched=None, not_matched=None)


def _seed_arr_instance(db, table, instance_id, kind, port, is_default):
    db.add(table(
        id=instance_id, kind=kind, stable_key=f"k{instance_id}", name=f"i{instance_id}",
        enabled=1, is_default=1 if is_default else 0, ip="127.0.0.1", port=port,
        base_url="/", ssl=0, verify_ssl=0, http_timeout=60, api_key=""))


def test_history_log_owner_resolver_prefers_default_on_collision(schema_session, monkeypatch):
    from sonarr import history
    from app.database import TableArrInstances, TableHistory

    monkeypatch.setattr(history, "database", schema_session)
    monkeypatch.setattr(history, "event_stream", lambda *a, **k: None)

    _seed_arr_instance(schema_session, TableArrInstances, 1, "sonarr", 8989, True)
    _seed_arr_instance(schema_session, TableArrInstances, 3, "sonarr", 8990, False)
    schema_session.flush()
    # Two episodes share sonarrEpisodeId=20, owners 1 (default) and 3. The
    # NON-default instance 3 owns the LOWER-id episode row, so an unscoped
    # .first() (rowid order) would wrongly pick instance 3 - the fix must still
    # land on the default instance 1.
    for local_show, owner in [(11, 3), (10, 1)]:
        schema_session.add(TableShows(
            id=local_show, sonarrSeriesId=10, arr_instance_id=owner,
            path=f"/s{owner}", title="S", tags="[]"))
    schema_session.flush()
    for local_ep, local_show, owner in [(150, 11, 3), (200, 10, 1)]:
        schema_session.add(TableEpisodes(
            id=local_ep, series_id=local_show, sonarrEpisodeId=20, sonarrSeriesId=10,
            arr_instance_id=owner, path=f"/s{owner}/e.mkv", title="Ep", season=1,
            episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    history.history_log(1, 10, 20, _result_stub(), arr_instance_id=None)

    rows = schema_session.execute(TableHistory.__table__.select()).fetchall()
    assert len(rows) == 1
    assert rows[0].arr_instance_id == 1, (
        "unscoped owner resolution on a collision must prefer the DEFAULT "
        f"instance (1), not an arbitrary row; got {rows[0].arr_instance_id}")


def test_history_log_owner_resolver_single_instance_unaffected(schema_session, monkeypatch):
    from sonarr import history
    from app.database import TableArrInstances, TableHistory

    monkeypatch.setattr(history, "database", schema_session)
    monkeypatch.setattr(history, "event_stream", lambda *a, **k: None)

    _seed_arr_instance(schema_session, TableArrInstances, 5, "sonarr", 8989, True)
    schema_session.flush()
    schema_session.add(TableShows(
        id=10, sonarrSeriesId=10, arr_instance_id=5, path="/s", title="S", tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=200, series_id=10, sonarrEpisodeId=20, sonarrSeriesId=10,
        arr_instance_id=5, path="/s/e.mkv", title="Ep", season=1, episode=1,
        monitored="True", subtitles="[]"))
    schema_session.flush()

    history.history_log(1, 10, 20, _result_stub(), arr_instance_id=None)

    rows = schema_session.execute(TableHistory.__table__.select()).fetchall()
    assert len(rows) == 1
    assert rows[0].arr_instance_id == 5


# ---------------------------------------------------------------------------
# Finding 6: embedded-subtitle history indexer must scope its dedup + stamp by
# the owning instance, so one instance's embedded row can't suppress another's.
# ---------------------------------------------------------------------------


@pytest.fixture
def embedded_db(monkeypatch):
    from subtitles.indexer import movies, series
    import radarr.history as radarr_history
    import sonarr.history as sonarr_history

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine))
    for mod in (series, movies, sonarr_history, radarr_history):
        monkeypatch.setattr(mod, "database", session)
    monkeypatch.setattr(sonarr_history, "event_stream", lambda **_: None)
    monkeypatch.setattr(radarr_history, "event_stream", lambda **_: None)
    try:
        yield session
    finally:
        session.remove()
        engine.dispose()


def test_embedded_history_episode_dedup_is_instance_scoped(embedded_db):
    from subtitles.indexer.series import _log_embedded_history

    # Two episodes share sonarrEpisodeId=20 (owners 1 and 3). Instance 1 already
    # has an action=7 embedded row; logging for instance 3 must NOT be deduped
    # away and must stamp instance 3.
    for local_show, local_ep, owner in [(10, 200, 1), (11, 201, 3)]:
        embedded_db.execute(insert(TableShows).values(
            id=local_show, arr_instance_id=owner, sonarrSeriesId=10,
            path=f"/s{owner}", title="S"))
        embedded_db.execute(insert(TableEpisodes).values(
            id=local_ep, series_id=local_show, arr_instance_id=owner, episode=1,
            monitored="True", path=f"/s{owner}/e.mkv", season=1,
            sonarrEpisodeId=20, sonarrSeriesId=10, title="Pilot"))
    embedded_db.execute(insert(TableHistory).values(
        arr_instance_id=1, series_id=10, episode_id=200, action=7,
        description="en embedded subtitles detected.", language="en",
        provider="embedded", score=100, score_out_of=100, sonarrEpisodeId=20,
        sonarrSeriesId=10, timestamp=datetime.now(), video_path="/s1/e.mkv"))

    _log_embedded_history(10, 20, ["en"], "/s3/e.mkv", arr_instance_id=3)

    rows = embedded_db.execute(
        TableHistory.__table__.select().where(TableHistory.action == 7)).fetchall()
    owners = sorted(r.arr_instance_id for r in rows)
    assert owners == [1, 3], (
        f"instance 3's embedded row must not be deduped by instance 1; got {owners}")


def test_embedded_history_movie_dedup_is_instance_scoped(embedded_db):
    from subtitles.indexer.movies import _log_embedded_history_movie

    for local_movie, owner in [(30, 2), (31, 4)]:
        embedded_db.execute(insert(TableMovies).values(
            id=local_movie, arr_instance_id=owner, path=f"/m{owner}.mkv",
            radarrId=30, title="Movie", tmdbId=str(local_movie)))
    embedded_db.execute(insert(TableHistoryMovie).values(
        arr_instance_id=2, movie_id=30, action=7,
        description="en embedded subtitles detected.", language="en",
        provider="embedded", score=100, score_out_of=100, radarrId=30,
        timestamp=datetime.now(), video_path="/m2.mkv"))

    _log_embedded_history_movie(30, ["en"], "/m4.mkv", arr_instance_id=4)

    rows = embedded_db.execute(
        TableHistoryMovie.__table__.select().where(
            TableHistoryMovie.action == 7)).fetchall()
    owners = sorted(r.arr_instance_id for r in rows)
    assert owners == [2, 4], (
        f"instance 4's embedded row must not be deduped by instance 2; got {owners}")


# ---------------------------------------------------------------------------
# Finding 7: subsync original-language lookup must be scoped to the OWNING
# instance, never read a colliding sibling instance's originalLanguage.
# ---------------------------------------------------------------------------


def test_subsync_original_language_scoped_to_owner(schema_session, monkeypatch):
    from subtitles.tools import subsyncer

    monkeypatch.setattr(subsyncer, "database", schema_session)
    # Two shows share sonarrSeriesId=10; owners 1 and 3 with DIFFERENT langs.
    for local_id, owner, lang in [(10, 1, "French"), (11, 3, "Japanese")]:
        schema_session.add(TableShows(
            id=local_id, sonarrSeriesId=10, arr_instance_id=owner,
            path=f"/s{owner}", title="S", tags="[]", originalLanguage=lang))
    schema_session.flush()

    got = subsyncer.SubSyncer._original_language_name(
        sonarr_series_id=10, radarr_id=None, arr_instance_id=3)
    assert got == "Japanese", (
        f"subsync must read the OWNING instance's originalLanguage; got {got!r}")


def test_subsync_original_language_collision_prefers_default(schema_session, monkeypatch):
    from subtitles.tools import subsyncer
    from app.database import TableArrInstances

    monkeypatch.setattr(subsyncer, "database", schema_session)
    _seed_arr_instance(schema_session, TableArrInstances, 1, "sonarr", 8989, True)
    _seed_arr_instance(schema_session, TableArrInstances, 3, "sonarr", 8990, False)
    schema_session.flush()
    # Non-default instance 3 owns the lower-id row, so an unscoped .first()
    # would read its language; the fix must prefer the default instance 1.
    for local_id, owner, lang in [(150, 3, "Japanese"), (200, 1, "French")]:
        schema_session.add(TableShows(
            id=local_id, sonarrSeriesId=10, arr_instance_id=owner,
            path=f"/s{owner}", title="S", tags="[]", originalLanguage=lang))
    schema_session.flush()

    got = subsyncer.SubSyncer._original_language_name(
        sonarr_series_id=10, radarr_id=None, arr_instance_id=None)
    assert got == "French", (
        f"unscoped collision must prefer the DEFAULT instance's language; got {got!r}")


# ---------------------------------------------------------------------------
# Finding 12: the episode event_stream payload must carry the LOCAL episode id
# (the frontend caches episode detail by local id), not the upstream id.
# ---------------------------------------------------------------------------


def test_list_missing_subtitles_emits_local_episode_id(schema_session, monkeypatch):
    from subtitles.indexer import series as series_indexer

    monkeypatch.setattr(series_indexer, "database", schema_session)
    monkeypatch.setattr(series_indexer, "get_profiles_list", lambda **k: None)
    monkeypatch.setattr(series_indexer, "get_audio_profile_languages", lambda v: [])

    events = []
    monkeypatch.setattr(series_indexer, "event_stream",
                        lambda **kwargs: events.append(kwargs))

    # Non-default instance: local id 901 != upstream sonarrEpisodeId 42.
    schema_session.add(TableShows(
        id=801, sonarrSeriesId=7, arr_instance_id=3, path="/s3", title="S",
        tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=901, series_id=801, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=3, path="/s3/e.mkv", title="Ep", season=1, episode=1,
        monitored="True", subtitles="[]"))
    schema_session.flush()

    series_indexer.list_missing_subtitles(epno=42, arr_instance_id=3)

    episode_events = [e for e in events if e.get("type") == "episode"]
    assert episode_events, "an episode event must be emitted"
    assert episode_events[0]["payload"] == 901, (
        "episode event must carry the LOCAL episode id (901), not the upstream "
        f"sonarrEpisodeId (42); got {episode_events[0]['payload']}")
