"""Exact episode metadata with deterministic source records and no provider browsing."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

import test_discover_metadata as metadata_fixtures
import test_discover_search as search_fixtures

upstream = metadata_fixtures.upstream
authenticated_client = metadata_fixtures.authenticated_client
get = metadata_fixtures.get
retrieval_database = search_fixtures.retrieval_database


@pytest.fixture
def episode_sources(upstream, monkeypatch):
    from subliminal_patch.refiners import tvdb_v4
    records = {
        "/search/tv": {"results": [{"id": 100, "name": "Northern Light", "first_air_date": "2020-03-01"}]},
        "/tv/100": {"id": 100, "name": "Northern Light", "first_air_date": "2020-03-01",
                    "seasons": [{"id": 200, "season_number": 0, "name": "Specials", "episode_count": 1},
                                {"id": 201, "season_number": 2, "name": "Season 2", "episode_count": 2}]},
        "/tv/100/external_ids": {"id": 100, "imdb_id": "tt1234567", "tvdb_id": 300},
        "/tv/100/season/2": {"id": 201, "season_number": 2, "episodes": [
            {"id": 401, "season_number": 2, "episode_number": 1, "name": "Home", "air_date": "2026-09-01"},
            {"id": 402, "season_number": 2, "episode_number": 2, "name": "Home", "air_date": None}]},
        "/tv/100/season/2/episode/1": {"id": 401, "season_number": 2, "episode_number": 1,
                                      "name": "Home", "air_date": "2026-09-01"},
        "/tv/100/season/2/episode/1/external_ids": {"id": 401, "imdb_id": "tt7654321", "tvdb_id": 501},
    }
    target = {"id": 501, "seriesId": 300, "seasonNumber": 2, "number": 1,
              "aired": "2026-09-01", "lastUpdated": "2026-09-02", "absoluteNumber": 9}
    calls = []
    def lookup(episode_id):
        calls.append(episode_id)
        return deepcopy(target)
    monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=lookup))
    upstream.payload = lambda url: deepcopy(records[url.removeprefix("https://api.themoviedb.org/3")])
    return SimpleNamespace(records=records, target=target, calls=calls)


def test_show_seasons_and_exact_episode_resolve_without_provider_submission(authenticated_client, episode_sources):
    show = get(authenticated_client, "shows/100").json["data"]["item"]
    assert show["source_id"] == "tmdb:show:100"
    assert show["imdb_id"] == "tt1234567" and show["year"] == 2020
    season = get(authenticated_client, "shows/100/seasons/2").json["data"]["season"]
    assert len(season["episodes"]) == 2
    assert season["episodes"][0]["title"] == season["episodes"][1]["title"] == "Home"
    assert season["episodes"][1]["air_date"] is None
    response = get(authenticated_client, "shows/100/seasons/2/episodes/1")
    assert response.status_code == 200
    episode = response.json["data"]["episode"]
    assert episode["season"] == 2 and episode["episode"] == 1
    assert episode["air_date"] == "2026-09-01"
    assert episode["identity_status"] == "resolved"
    assert episode["numbering"] == "tvdb_default"
    assert episode["imdb_id"] == "tt7654321" and episode["show_imdb_id"] == "tt1234567"
    assert episode["tvdb_id"] == 501 and episode["show_tvdb_id"] == 300
    assert episode["absolute_episode"] is None
    assert episode["tvdb_absolute_number"] == 9
    assert authenticated_client.provider_searches == []
    assert episode_sources.calls == [501]


@pytest.mark.parametrize("field,value", [("id", 999), ("seriesId", 999)])
def test_conflicting_identity_cannot_resolve(authenticated_client, episode_sources, field, value):
    episode_sources.target[field] = value
    episode = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]
    assert episode["identity_status"] == "conflict"
    assert episode["target_season"] is None and episode["target_episode"] is None


def test_empty_season_is_distinct_from_unavailable(authenticated_client, episode_sources, upstream):
    episode_sources.records["/tv/100/season/2"]["episodes"] = []
    data = get(authenticated_client, "shows/100/seasons/2").json["data"]
    assert data["status"] == "available" and data["season"]["episodes"] == []
    upstream.status = 503
    failed = get(authenticated_client, "shows/100/seasons/3").json["data"]
    assert failed["status"] == "unavailable" and failed["season"] is None


def test_tv_search_namespace_cannot_reuse_movie_id(authenticated_client, episode_sources):
    response = get(authenticated_client, "search?type=show&q=Northern")
    assert response.status_code == 200
    assert response.json["data"]["items"][0]["source_id"] == "tmdb:show:100"


@pytest.mark.parametrize("copies", [0, 2])
def test_episode_browsing_never_inspects_or_mutates_libraries(authenticated_client, episode_sources, retrieval_database, copies):
    import sqlalchemy as sa
    from app import database as db
    engine, session = retrieval_database
    for copy in range(1, copies + 1):
        session.add(db.TableShows(id=copy, arr_instance_id=copy, sonarrSeriesId=7, imdbId="tt1234567",
                                  title="Wrong local show", path=f"/synthetic/show-{copy}"))
        session.flush()
        session.add(db.TableEpisodes(id=copy, series_id=copy, arr_instance_id=copy, sonarrSeriesId=7,
                                     sonarrEpisodeId=9, season=2, episode=1, title="Wrong local episode",
                                     path=f"/synthetic/show-{copy}/ep.mkv"))
    session.commit()
    tables = db.Base.metadata.sorted_tables
    before = {table.name: session.execute(sa.select(table)).all() for table in tables}
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        response = get(authenticated_client, "shows/100/seasons/2/episodes/1")
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    assert statements == []
    assert {table.name: session.execute(sa.select(table)).all() for table in tables} == before


@pytest.mark.parametrize("field,value", [("number", None), ("number", True), ("number", 0),
                                        ("seasonNumber", -1), ("seasonNumber", 1.2)])
def test_invalid_target_numbers_stay_unverified(authenticated_client, episode_sources, field, value):
    episode_sources.target[field] = value
    episode = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]
    assert episode["identity_status"] == "unverified"
    assert episode["target_season"] is None and episode["target_episode"] is None


def test_verified_special_mapping_preserves_different_source_and_target_order(authenticated_client, episode_sources):
    episode_sources.target.update(seasonNumber=0, number=3)
    episode = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]
    assert (episode["season"], episode["episode"]) == (2, 1)
    assert (episode["target_season"], episode["target_episode"]) == (0, 3)
    assert episode["identity_status"] == "resolved" and episode["numbering"] == "tvdb_default"


def test_missing_external_ids_do_not_guess_numbering(authenticated_client, episode_sources):
    episode_sources.records["/tv/100/season/2/episode/1/external_ids"].update(imdb_id=None, tvdb_id=None)
    episode = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]
    assert episode["identity_status"] == "unverified" and episode["imdb_id"] is None
    assert episode_sources.calls == []


def test_missing_or_invalid_air_dates_are_never_guessed(authenticated_client, episode_sources):
    episode_sources.records["/tv/100/season/2/episode/1"]["air_date"] = "2026-02-31"
    episode = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]
    assert episode["air_date"] is None and episode["show_year"] == 2020


@pytest.mark.parametrize("path", ["shows/0", "shows/100/seasons/-1", "shows/100/seasons/2/episodes/0",
                                  "shows/100/seasons/2/episodes/1.2", "shows/100/seasons/true"])
def test_bad_source_links_fail_before_upstream(authenticated_client, episode_sources, upstream, path):
    assert get(authenticated_client, path).status_code == 400
    assert upstream.calls == [] and episode_sources.calls == []


def test_tvdb_none_is_unavailable_mapping_not_empty_episode(authenticated_client, episode_sources, monkeypatch):
    from subliminal_patch.refiners import tvdb_v4
    monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=lambda episode_id: None))
    episode = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]
    assert episode["title"] == "Home" and episode["identity_status"] == "unverified"


def test_tvdb_adapter_has_bounded_wait_and_no_late_cache_publish(authenticated_client, episode_sources, monkeypatch):
    import threading
    import time
    from discover import metadata
    from subliminal_patch.refiners import tvdb_v4
    done = threading.Event()
    finished = threading.Event()
    attempts = []
    def blocked(episode_id):
        attempts.append(episode_id)
        try:
            assert done.wait(3)
            return episode_sources.target
        finally:
            finished.set()
    monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=blocked))
    monkeypatch.setattr(metadata, "TVDB_WAIT_SECONDS", 0.01)
    try:
        start = time.monotonic()
        episode = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]
        assert time.monotonic() - start < 1
        assert episode["identity_status"] == "unverified"
    finally:
        done.set()
        assert finished.wait(3)
    assert attempts == [501]
    # A completed abandoned worker cannot make its identity available. A new
    # explicit lookup still fails when the dependency remains unavailable.
    def unavailable(episode_id):
        attempts.append(episode_id)
        return None
    monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=unavailable))
    assert get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"] == episode
    assert attempts == [501, 501]
    monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=lambda value: episode_sources.target))
    assert get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]["identity_status"] == "resolved"


def test_untrusted_duplicate_source_numbers_are_unavailable(authenticated_client, episode_sources):
    episode_sources.records["/tv/100/season/2"]["episodes"][1]["episode_number"] = 1
    data = get(authenticated_client, "shows/100/seasons/2").json["data"]
    assert data["status"] == "unavailable" and data["season"] is None


def test_source_updated_timestamp_and_absolute_number_are_preserved_without_provider_inference(authenticated_client, episode_sources):
    episode_sources.target["lastUpdated"] = "2026-09-02T11:23:45Z"
    episode = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]
    assert episode["mapping_updated_at"] == "2026-09-02T11:23:45Z"
    assert episode["tvdb_absolute_number"] == 9 and episode["absolute_episode"] is None


@pytest.mark.parametrize("row", [None, [], "not-a-show", {"id": True, "name": "Invalid"}])
def test_malformed_tv_search_rows_are_safe_unavailable_metadata(authenticated_client, episode_sources, row):
    episode_sources.records["/search/tv"]["results"] = [row]
    response = get(authenticated_client, "search?type=show&q=Northern")
    assert response.status_code == 200
    assert response.json["data"]["status"] == "unavailable"
    assert response.json["data"]["items"] == []


def fail_episode_dependency(monkeypatch, dependency):
    from discover import metadata
    from subliminal_patch.refiners import tvdb_v4
    if dependency == "tvdb_episode":
        monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=lambda value: None))
    else:
        original = metadata._request
        path = "/tv/100/external_ids" if dependency == "show_external_ids" else "/tv/100/season/2/episode/1/external_ids"
        def request(config, requested, params=None):
            if requested == path:
                raise metadata.UpstreamFailure()
            return original(config, requested, params)
        monkeypatch.setattr(metadata, "_request", request)


@pytest.mark.parametrize("dependency", ["show_external_ids", "episode_external_ids", "tvdb_episode"])
def test_partial_dependency_outage_preserves_bounded_stale_canonical_identity(authenticated_client, episode_sources, monkeypatch, dependency):
    from discover import metadata
    before = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    show_before = get(authenticated_client, "shows/100").json["data"]
    monkeypatch.setattr(metadata, "FRESH_SECONDS", 0)
    fail_episode_dependency(monkeypatch, dependency)
    after = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    assert after["episode"] == before["episode"]
    assert after["status"] == "cached" and after["service_status"] == "unavailable"
    assert after["unavailable_dependency"] == dependency
    assert after["fetched_at"] == before["fetched_at"]
    if dependency == "show_external_ids":
        show_after = get(authenticated_client, "shows/100").json["data"]
        assert show_after["item"] == show_before["item"]
        assert show_after["fetched_at"] == show_before["fetched_at"]
    assert authenticated_client.provider_searches == []


@pytest.mark.parametrize("dependency", ["show_external_ids", "episode_external_ids", "tvdb_episode"])
def test_cold_partial_dependency_outage_is_browseable_unverified_and_truthful(authenticated_client, episode_sources, monkeypatch, dependency):
    fail_episode_dependency(monkeypatch, dependency)
    result = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    assert result["episode"]["title"] == "Home" and result["episode"]["air_date"] == "2026-09-01"
    assert result["episode"]["identity_status"] == "unverified"
    assert result["status"] == result["service_status"] == "unavailable"
    assert result["unavailable_dependency"] == dependency
    assert result["episode"]["target_season"] is None


@pytest.mark.parametrize("dependency", ["show_external_ids", "episode_external_ids", "tvdb_episode"])
def test_expired_partial_dependency_mapping_is_not_extended(authenticated_client, episode_sources, monkeypatch, dependency):
    from discover import metadata
    clock = [0]
    monkeypatch.setattr(metadata, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    before = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    clock[0] = metadata.STALE_SECONDS + 1
    fail_episode_dependency(monkeypatch, dependency)
    after = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    assert after["status"] == "unavailable"
    assert after["episode"]["identity_status"] == "unverified"
    assert after["episode"] != before["episode"]


@pytest.mark.parametrize("source", ["show", "episode"])
def test_successful_absent_external_ids_are_authoritative_not_an_outage(authenticated_client, episode_sources, monkeypatch, source):
    from discover import metadata
    assert get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]["identity_status"] == "resolved"
    monkeypatch.setattr(metadata, "FRESH_SECONDS", 0)
    path = "/tv/100/external_ids" if source == "show" else "/tv/100/season/2/episode/1/external_ids"
    episode_sources.records[path].update(imdb_id=None, tvdb_id=None)
    after = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    assert after["status"] == "available" and after.get("service_status") is None
    assert after["episode"]["identity_status"] == "unverified"


@pytest.mark.parametrize("changed", ["show_parent", "episode_external"])
def test_partial_outage_never_revives_a_successfully_contradicted_mapping(authenticated_client, episode_sources, monkeypatch, changed):
    from discover import metadata
    before = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]
    monkeypatch.setattr(metadata, "FRESH_SECONDS", 0)
    if changed == "show_parent":
        episode_sources.records["/tv/100/external_ids"]["tvdb_id"] = 999
        fail_episode_dependency(monkeypatch, "episode_external_ids")
    else:
        episode_sources.records["/tv/100/season/2/episode/1/external_ids"]["tvdb_id"] = 777
        fail_episode_dependency(monkeypatch, "tvdb_episode")
    after = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    assert after["status"] == "unavailable"
    assert after["episode"]["identity_status"] == "unverified"
    assert after["episode"] != before
    assert after["episode"]["show_tvdb_id" if changed == "show_parent" else "tvdb_id"] == (999 if changed == "show_parent" else 777)
    if changed == "episode_external":
        def unavailable(*args, **kwargs):
            raise metadata.UpstreamFailure()
        monkeypatch.setattr(metadata, "_request", unavailable)
        assert get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"] is None


@pytest.mark.parametrize("dependency", ["show_external_ids", "episode_external_ids", "tvdb_episode"])
def test_successful_conflicting_ids_retire_a_prior_mapping(authenticated_client, episode_sources, monkeypatch, dependency):
    from discover import metadata
    assert get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]["identity_status"] == "resolved"
    monkeypatch.setattr(metadata, "FRESH_SECONDS", 0)
    if dependency == "tvdb_episode":
        episode_sources.target["seriesId"] = 999
    else:
        path = "/tv/100/external_ids" if dependency == "show_external_ids" else "/tv/100/season/2/episode/1/external_ids"
        episode_sources.records[path]["id"] = 999
    after = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    assert after["status"] == "available" and after.get("service_status") is None
    assert after["episode"]["identity_status"] == ("unverified" if dependency == "show_external_ids" else "conflict")
    assert after["episode"]["target_season"] is None


def test_dependency_failure_cannot_extend_stale_bound_while_waiting(authenticated_client, episode_sources, monkeypatch):
    from discover import metadata
    from subliminal_patch.refiners import tvdb_v4
    clock = [0]
    monkeypatch.setattr(metadata, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(metadata, "FRESH_SECONDS", 1)
    monkeypatch.setattr(metadata, "STALE_SECONDS", 5)
    assert get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]["episode"]["identity_status"] == "resolved"
    clock[0] = 4
    def failed_after_expiry(episode_id):
        clock[0] = 6
        return None
    monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=failed_after_expiry))
    after = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    assert after["status"] == "unavailable"
    assert after["episode"]["identity_status"] == "unverified"


def test_internal_dependency_deadline_is_not_public_episode_metadata(authenticated_client, episode_sources):
    from discover import metadata
    private, valid_until = metadata._episode_details_with_validity("100", "2", "1")
    assert isinstance(valid_until, (float, int)) and valid_until > metadata.time.monotonic()
    public = get(authenticated_client, "shows/100/seasons/2/episodes/1").json["data"]
    assert public["episode"] == private["data"]["episode"]
    assert set(public) == {"source", "status", "configured", "revision", "locale", "message", "checked_at", "fetched_at", "service_status", "episode"}
    assert not ({"valid_until", "metadata_valid_until", "dependency_valid_until"} & set(public["episode"]))
