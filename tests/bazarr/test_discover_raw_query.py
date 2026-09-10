"""Explicit release queries use catalog fanout without adopting an identity."""
import pytest

import test_discover_search as search_fixtures


authenticated_client = search_fixtures.authenticated_client
providers = search_fixtures.providers
retrieval_database = search_fixtures.retrieval_database
post = search_fixtures.post
QUERY = "Example.Movie.2024.1080p.WEB-DL"
RAW = {"mode": "release", "query": QUERY, "language": "eng"}


@pytest.mark.parametrize("query", ["", "   ", ".-_/!?", "💫", None, 42, "a" * 501, "Movie\nTitle", "Movie\x7f"])
def test_invalid_release_query_fails_before_fanout(authenticated_client, providers, query):
    providers.add("discover_raw")
    assert post(authenticated_client, {**RAW, "query": query}).status_code == 400
    assert providers.videos == []


@pytest.mark.parametrize("changes", [
    {"language": ""}, {"language": "zzz"}, {"mode": "unknown"}, {"refresh": "yes"},
    {"imdb_id": "tt0133093"}, {"media_type": "episode"}, {"season": 1}, {"episode": 2},
    {"title": "Assumed title"}, {"year": 2024}, {"moviehash": "hash"}, {"matching_mode": "title"},
])
def test_raw_mode_rejects_mixed_or_unverified_identity(authenticated_client, providers, changes):
    providers.add("discover_raw")
    assert post(authenticated_client, {**RAW, **changes}).status_code == 400
    assert providers.videos == []


def test_release_requires_explicit_mode_and_ui_authentication(authenticated_client, providers):
    providers.add("discover_raw")
    assert authenticated_client.post("/api/discover/search", json=RAW).status_code == 401
    assert post(authenticated_client, {"query": QUERY, "language": "eng"}).status_code == 400
    assert providers.videos == []


@pytest.mark.parametrize("copies", [0, 2])
def test_release_search_has_no_identity_library_or_metadata_dependency(
    authenticated_client, providers, retrieval_database, monkeypatch, copies,
):
    import sqlalchemy as sa
    from app import database as db
    from compat import service, meter
    from discover.handles import resolve_result
    from provider_hub.protocol import candidate_from_worker

    engine, session = retrieval_database
    for number in range(1, copies + 1):
        session.add(db.TableMovies(id=number, arr_instance_id=number, radarrId=7, tmdbId="11", imdbId="tt0133093",
                                  path=f"/private/library-{number}/local.mkv", title=QUERY))
    session.commit()
    tables = db.Base.metadata.sorted_tables
    before = {table.name: session.execute(sa.select(table)).all() for table in tables}

    def forbidden(*args, **kwargs):
        pytest.fail("Release query attempted library, file, refiner or metering access")

    for name in ("_lookup_library_metadata", "_parse_video_from_library", "_refine_from_imdb", "search_local"):
        monkeypatch.setattr(service, name, forbidden)
    monkeypatch.setattr(meter, "record", forbidden)
    providers.add("discover_raw", [candidate_from_worker("discover_raw", {
        "id": "raw", "language": {"alpha3": "eng"}, "release_info": QUERY,
        "matches": ["imdb_id", "episode", "title", "hash"], "provider_payload": {},
    })])
    sql = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record_sql)
    try:
        response = post(authenticated_client, {**RAW, "query": "  " + QUERY + "  "})
    finally:
        sa.event.remove(engine, "before_cursor_execute", record_sql)
    assert response.status_code == 200
    assert response.json["context"] == {"mode": "release", "query": QUERY, "language": "en", "matching_mode": "release"}
    assert sql == []
    assert {table.name: session.execute(sa.select(table)).all() for table in tables} == before
    row = response.json["results"][0]
    assert row["matches"] is None
    assert row["compatibility_score"] is None and row["compatibility_score_max"] is None
    assert resolve_result(row["id"], row["search_id"])["context"] == response.json["context"]
    video = providers.videos[0][1]
    assert video.name == QUERY and video.title == "Example Movie"
    assert video.year == 2024
    assert video.imdb_id is None and not getattr(video, "episode", None)
    assert video.hashes == {} and video.size is None


def test_modes_queries_and_languages_cannot_share_cached_results(authenticated_client, providers):
    from subzero.language import Language
    providers.add("discover_raw", languages={Language("eng"), Language("hun")})
    contexts = [RAW, {**RAW, "query": "Different.Release"}, {**RAW, "language": "hun"},
                {"media_type": "movie", "imdb_id": "tt0133093", "title": QUERY, "language": "eng"}]
    ids = []
    for context in contexts:
        response = post(authenticated_client, context)
        assert response.status_code == 200
        ids.append(response.json["search_id"])
        assert post(authenticated_client, context).json["search_id"] == ids[-1]
    assert len(set(ids)) == 4 and len(providers.videos) == 4


def test_raw_provider_failures_keep_context_and_retained_handle(authenticated_client, providers, monkeypatch):
    from provider_hub.protocol import candidate_from_worker
    from subliminal_patch.exceptions import APIThrottled
    from subliminal.exceptions import AuthenticationError

    providers.add("discover_raw", [candidate_from_worker("discover_raw", {
        "id": "raw", "language": {"alpha3": "eng"}, "provider_payload": {},
    })])
    providers.add("discover_auth", error=AuthenticationError("fixture"))
    first_response = post(authenticated_client, RAW)
    assert first_response.status_code == 200
    first = first_response.json
    assert first["status"] == "partial"

    def throttled(*args):
        raise APIThrottled(retry_after=600)

    monkeypatch.setattr(providers.pool["discover_raw"], "list_subtitles", throttled)
    second = post(authenticated_client, {**RAW, "refresh": True}).json
    assert second["context"] == first["context"]
    assert second["results"] == [{**first["results"][0], "stale": True}]
    assert {item["status"] for item in second["coverage"]["providers"]} == {"cooldown", "authentication_required"}


@pytest.mark.parametrize("query", ["Example.Show.S02E03E04", "Example.Show.E03", "Example.Show.103", "1080p.WEB-DL"])
def test_ambiguous_release_requires_recovery_before_fanout(authenticated_client, providers, query):
    providers.add("discover_raw")
    response = post(authenticated_client, {**RAW, "query": query})
    assert response.status_code == 400
    assert "release name" in response.json["message"] or "episode" in response.json["message"]
    assert providers.videos == []


def test_episode_hints_remain_unverified_and_unsupported_media_is_honest(authenticated_client, providers):
    from subliminal.video import Movie, Episode
    from provider_hub.protocol import candidate_from_worker
    providers.add("discover_episode", [candidate_from_worker("discover_episode", {
        "id": "episode", "language": {"alpha3": "eng"}, "provider_payload": {},
    })], video_types=(Episode,))
    providers.add("discover_movie", video_types=(Movie,))
    response = post(authenticated_client, {**RAW, "query": "Example.Show.S02E03.1080p"})
    assert response.status_code == 200
    assert response.json["context"] == {"mode": "release", "query": "Example.Show.S02E03.1080p", "language": "en", "matching_mode": "release"}
    assert len(response.json["results"]) == 1
    video = providers.videos[0][1]
    assert (video.series, video.season, video.episode, video.series_imdb_id) == ("Example Show", 2, 3, None)
    assert response.json["status"] == "partial"
    movie = next(item for item in response.json["coverage"]["providers"] if item["provider"] == "discover_movie")
    assert (movie["status"], movie["reason"]) == ("skipped", "unsupported_media")


def test_empty_raw_results_do_not_claim_supported_query_or_no_subtitles(authenticated_client, providers):
    providers.add("discover_empty")
    response = post(authenticated_client, RAW)
    assert response.status_code == 200
    assert response.json["status"] == "failed"
    assert response.json["coverage"]["complete"] is False
    assert response.json["coverage"]["completed_count"] == 0
    outcome = response.json["coverage"]["providers"][0]
    assert (outcome["status"], outcome["reason"]) == ("unverified", "query_support_unverified")
    assert len(providers.videos) == 1
