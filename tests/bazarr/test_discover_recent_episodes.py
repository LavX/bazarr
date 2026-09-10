"""Recent episodes require dated source records and never infer provider targets."""
from datetime import date, datetime, timezone
import threading
import time

import pytest
import test_discover_metadata as fixtures
import test_discover_episodes as episode_fixtures
import test_discover_search as search_fixtures

upstream = fixtures.upstream
authenticated_client = fixtures.authenticated_client
retrieval_database = fixtures.retrieval_database
episode_sources = episode_fixtures.episode_sources
providers = search_fixtures.providers


def get(client, query=""):
    return client.get("/api/discover/feeds/recent-episodes" + query,
                      headers={"X-API-KEY": "metadata-test-key"})


def episode(identity, number, day="2026-09-08", season=2):
    return {"id": identity, "episode_number": number, "season_number": season,
            "name": f"Episode {identity}", "air_date": day}


def configure(upstream, monkeypatch, *, rows=None, seasons=None):
    from discover import feeds, metadata
    monkeypatch.setattr(metadata, "_now", lambda: "2026-09-08T12:00:00+00:00")
    monkeypatch.setattr(feeds, "_now", lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    rows = rows if rows is not None else [episode(401, 1), episode(402, 2, "2026-08-10"),
        episode(403, 3, "2026-08-09"), episode(404, 4, "2026-09-09"),
        episode(405, 5, None), episode(406, 6, "bad-date"), episode(401, 1)]
    seasons = seasons if seasons is not None else [{"id": 201, "season_number": 2}]
    def payload(url):
        if url.endswith("/configuration"):
            return {"images": {"secure_base_url": "https://image.tmdb.org/t/p/", "poster_sizes": ["w342"]}}
        if url.endswith("/trending/tv/week"):
            return {"results": [{"id": 100, "name": "Working show", "media_type": "tv"},
                                {"id": 101, "name": "Missing show", "media_type": "tv"}], "total_pages": 1}
        if url.endswith("/tv/100"):
            return {"id": 100, "name": "Working show", "seasons": seasons}
        if url.endswith("/tv/101"):
            return {"broken": True}
        number = int(url.rsplit("/", 1)[1])
        return {"id": 201 + number - 2, "season_number": number,
                "episodes": rows if number == 2 else [episode(500 + number, 1, season=number)]}
    upstream.payload = payload


def test_real_dates_duplicates_and_partial_show_coverage(authenticated_client, upstream, monkeypatch):
    configure(upstream, monkeypatch)
    response = get(authenticated_client)
    assert response.status_code == 200
    feed = response.json
    assert [row["id"] for row in feed["items"]] == [401, 402]
    assert feed["window"] == {"start": "2026-08-10", "end": "2026-09-08"}
    assert all(date(2026, 8, 10) <= date.fromisoformat(row["air_date"]) <= date(2026, 9, 8) for row in feed["items"])
    assert feed["coverage"]["complete"] is False
    assert any(row["show_id"] == 100 for row in feed["items"])
    assert feed["coverage"]["missing_dates"] == 2
    assert feed["items"][0]["identity_status"] == "unverified"
    assert feed["items"][0]["provenance"]["air_date"] == "2026-09-08"
    assert not any("target_season" in row or "heat" in row for row in feed["items"])
    assert authenticated_client.provider_searches == []


def test_show_artwork_is_resolved_on_the_cold_path(authenticated_client, upstream, monkeypatch):
    """The feed carries a show poster whether or not the homepage warmed it.

    The cold path used to pass an empty image configuration, so poster_url was a
    field that could never be filled from this producer. One configuration
    request at most, and none when no row carries a poster to build.
    """
    from discover import feeds
    configure(upstream, monkeypatch)
    base = upstream.payload
    def payload(url):
        raw = base(url)
        if url.endswith("/configuration"):
            raw["images"]["backdrop_sizes"] = ["w780"]
        if url.endswith("/trending/tv/week"):
            for row in raw["results"]:
                row["poster_path"] = f"/show{row['id']}.jpg"
        return raw
    upstream.payload = payload
    items = get(authenticated_client).json["items"]
    assert items and all(row["poster_url"] == "https://image.tmdb.org/t/p/w342/show100.jpg" for row in items)
    assert sum(1 for url, _ in upstream.calls if url.endswith("/configuration")) == 1
    feeds._cache.clear()
    get(authenticated_client)
    assert sum(1 for url, _ in upstream.calls if url.endswith("/configuration")) == 1


def test_show_artwork_survives_the_warm_path(authenticated_client, upstream, monkeypatch):
    """The homepage loads weekly trending on the same page, so this feed takes
    the warm path in ordinary use: the shows arrive with their posters already
    resolved and no path map to resolve them from. Hydration must fill in what is
    missing and never clear what another producer already resolved.

    The cold-path test clears the cache first, which is exactly why it could not
    see the wipe this case now pins.
    """
    from discover import feeds
    configure(upstream, monkeypatch)
    base = upstream.payload
    def payload(url):
        raw = base(url)
        if url.endswith("/configuration"):
            raw["images"]["backdrop_sizes"] = ["w780"]
        if "trending/tv/week" in url:
            for row in raw["results"]:
                row["poster_path"] = f"/show{row['id']}.jpg"
        return raw
    upstream.payload = payload

    # Warm the shared weekly series observation, exactly as the homepage does.
    assert feeds.trending("series")["items"]
    warmed = sum(1 for url, _ in upstream.calls if url.endswith("/trending/tv/week"))
    items = get(authenticated_client).json["items"]
    # The feed really did take the warm path: no second trending request.
    assert sum(1 for url, _ in upstream.calls if url.endswith("/trending/tv/week")) == warmed
    assert items, "the warm path produced no episodes"
    assert all(row["poster_url"] == "https://image.tmdb.org/t/p/w342/show100.jpg" for row in items), \
        [row["poster_url"] for row in items]


def test_artwork_hydration_never_clears_a_poster_another_producer_resolved():
    """The contract at its source, so a future edit cannot reintroduce half of
    the wipe invisibly behind the back-fill guard.

    _recent_artwork is not the only producer of poster_url: the warm path hands
    it shows the homepage feed already resolved, with no path map to resolve
    them from. It fills in; it never clears.
    """
    import time
    from discover import feeds
    warm = {"id": 100, "poster_url": "https://image.tmdb.org/t/p/w342/warm.jpg"}
    job = feeds._RecentJob(deadline=time.monotonic() + 30)
    feeds._recent_artwork(None, job, {100: warm}, {})
    assert warm["poster_url"] == "https://image.tmdb.org/t/p/w342/warm.jpg"
    # A path map that carries only empty values is the same situation.
    feeds._recent_artwork(None, job, {100: warm}, {100: None})
    assert warm["poster_url"] == "https://image.tmdb.org/t/p/w342/warm.jpg"


def test_malformed_source_body_is_not_cached_for_the_next_refresh(authenticated_client, upstream, monkeypatch):
    """A 200 body that cannot carry the records this feed asks for must not
    occupy the shared metadata region. Refresh has to reach the source again
    instead of reproducing the same partial coverage from a cached malformation.
    The sibling that answered correctly stays cached, so only the broken record
    costs a second request."""
    from discover import feeds
    configure(upstream, monkeypatch)
    broken = lambda: sum(1 for url, _ in upstream.calls if url.endswith("/tv/101"))  # noqa: E731
    trending = lambda: sum(1 for url, _ in upstream.calls if url.endswith("/trending/tv/week"))  # noqa: E731
    assert get(authenticated_client).json["coverage"]["failed"] >= 1
    assert (broken(), trending()) == (1, 1)
    feeds._cache.clear()
    assert get(authenticated_client).json["coverage"]["failed"] >= 1
    assert (broken(), trending()) == (2, 1)


@pytest.mark.parametrize("bad", [None, {}, {"id": 999, "episode_number": True}, "bad"])
def test_bad_episode_does_not_erase_good_siblings(authenticated_client, upstream, monkeypatch, bad):
    configure(upstream, monkeypatch, rows=[bad, episode(401, 1), episode(401, 1)])
    response = get(authenticated_client)
    assert response.status_code == 200
    assert [row["id"] for row in response.json["items"]] == [401]
    assert response.json["coverage"]["failed"] >= 1


def test_specials_and_conflicting_source_numbers_are_not_verified(authenticated_client, upstream, monkeypatch):
    configure(upstream, monkeypatch, rows=[episode(401, 1), episode(402, 1)],
              seasons=[{"id": 201, "season_number": 2}, {"id": 199, "season_number": 0}])
    response = get(authenticated_client)
    assert response.status_code == 200
    items = response.json["items"]
    assert any(row["season"] == 0 and row["identity_status"] == "unverified" for row in items)
    assert all(row["identity_status"] == "conflict" for row in items if row["season"] == 2)
    assert authenticated_client.provider_searches == []


@pytest.mark.parametrize("query", ["?today=2026-09-08", "?region=US", "?language=eng", "?show=100"])
def test_feed_has_no_user_fanout_or_date_override(authenticated_client, upstream, query):
    assert get(authenticated_client, query).status_code == 400
    assert upstream.calls == []


def test_auth_and_unconfigured_never_start_source_work(authenticated_client, upstream, monkeypatch):
    from app import tmdb
    from app.config import settings
    assert authenticated_client.get("/api/discover/feeds/recent-episodes").status_code == 401
    # Unconfigured now means no credential anywhere, built-in key included.
    monkeypatch.setattr(tmdb, "builtin_api_key", lambda: "")
    settings.discover.tmdb_access_token = ""
    response = get(authenticated_client)
    assert response.status_code == 200 and response.json["status"] == "unconfigured"
    assert upstream.calls == []


@pytest.mark.parametrize("copies", [0, 2])
def test_browsing_does_not_read_or_mutate_library_or_submit_providers(authenticated_client, upstream, monkeypatch, retrieval_database, copies):
    import sqlalchemy as sa
    from app import database as db
    engine, session = retrieval_database
    for copy in range(1, copies + 1):
        session.add(db.TableShows(id=copy, arr_instance_id=copy, sonarrSeriesId=100,
                                title="Working show", path=f"/synthetic/{copy}", tvdbId=300))
    session.commit()
    before = {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables}
    configure(upstream, monkeypatch)
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        assert get(authenticated_client).status_code == 200
        assert get(authenticated_client).status_code == 200
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements == []
    assert before == {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables}
    assert authenticated_client.provider_searches == []


def clock_control(monkeypatch):
    from datetime import timedelta
    from discover import feeds, metadata
    clock = [100.0]
    base = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(feeds.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(feeds, "_now", lambda: base + timedelta(seconds=clock[0] - 100))
    monkeypatch.setattr(metadata, "_now", lambda: feeds._now().isoformat())
    return clock


def test_warm_source_outage_keeps_original_dates_and_freshness(authenticated_client, upstream, monkeypatch):
    configure(upstream, monkeypatch)
    clock = clock_control(monkeypatch)
    first = get(authenticated_client).json
    clock[0] += 301
    upstream.status = 503
    retained = get(authenticated_client).json
    assert retained["items"] == first["items"]
    assert retained["service_status"] == "unavailable" and not retained["coverage"]["complete"]
    assert all(retained[key] == first[key] for key in ("fetched_at", "expires_at", "stale_until", "last_success"))
    clock[0] += 31
    upstream.status = 401
    rejected = get(authenticated_client).json
    assert rejected["status"] == "authentication_failed" and rejected["items"] == []
    clock[0] += 3601
    upstream.status = 503
    assert get(authenticated_client).json["items"] == []
    assert authenticated_client.provider_searches == []


def test_full_admission_outage_keeps_observation_and_age(authenticated_client, upstream, monkeypatch):
    from discover import metadata
    configure(upstream, monkeypatch)
    clock = clock_control(monkeypatch)
    first = get(authenticated_client).json
    clock[0] += 301
    metadata._cache.invalidate(hard=True)
    upstream.status = 503
    retained = get(authenticated_client).json
    assert retained["status"] == "cached" and retained["service_status"] == "unavailable"
    assert all(retained[key] == first[key] for key in ("items", "coverage", "fetched_at", "expires_at", "stale_until", "last_success"))
    count = len(upstream.calls)
    get(authenticated_client)
    assert len(upstream.calls) == count


def test_cache_keys_include_window_configuration_and_locale(authenticated_client, upstream, monkeypatch):
    from app.config import settings
    from discover import feeds
    configure(upstream, monkeypatch)
    first = feeds.recent_episodes(date(2026, 9, 8))
    count = len(upstream.calls)
    assert feeds.recent_episodes(date(2026, 9, 8))["status"] == "cached"
    tomorrow = feeds.recent_episodes(date(2026, 9, 9))
    assert tomorrow["window"]["end"] == "2026-09-09"
    assert 404 in [row["id"] for row in tomorrow["items"]]
    # A different window reuses every admitted source record. The one repeat is
    # the malformed 200 body, which is deliberately never admitted to the cache.
    assert [url for url, _ in upstream.calls[count:]] == ["https://api.themoviedb.org/3/tv/101"]
    settings.discover.locale = "hu-HU"
    localized = feeds.recent_episodes(date(2026, 9, 8))
    assert localized["revision"] != first["revision"] and localized["locale"] == "hu-HU"
    settings.discover.tmdb_access_token = "5ecafe33cafe33cafe33cafe33cafe33"
    assert feeds.recent_episodes(date(2026, 9, 8))["revision"] != localized["revision"]


def test_bounded_request_scan_output_and_special_selection(authenticated_client, upstream, monkeypatch):
    configure(upstream, monkeypatch)
    def payload(url):
        if url.endswith("/trending/tv/week"):
            return {"results": [{"id": i, "name": f"Show {i}"} for i in range(1, 40)], "total_pages": 3}
        show = int(url.split("/")[5])
        if "/season/" not in url:
            return {"id": show, "seasons": [{"id": 100 + n, "season_number": n} for n in range(10)]}
        season = int(url.rsplit("/", 1)[1])
        return {"id": 100 + season, "season_number": season,
                "episodes": [episode(show * 100000 + season * 1000 + n, n, season=season) for n in range(1, 1002)]}
    upstream.payload = payload
    feed = get(authenticated_client).json
    assert feed["status"] == "live"
    assert len(upstream.calls) == 25
    assert feed["coverage"]["shows_checked"] == 6
    assert feed["coverage"]["seasons_checked"] == 18
    assert feed["coverage"]["episodes_checked"] == 18000
    assert len(feed["items"]) == 120
    assert feed["coverage"]["truncated"] and not feed["coverage"]["complete"]
    assert {int(url.rsplit("/", 1)[1]) for url, _ in upstream.calls if "/season/" in url} == {0, 8, 9}


def test_reuses_accepted_weekly_series_feed_without_guessing_pagination(authenticated_client, upstream, monkeypatch):
    from discover import feeds
    configure(upstream, monkeypatch)
    assert feeds.trending("series")["items"]
    calls = sum(url.endswith("/trending/tv/week") for url, _ in upstream.calls)
    recent = get(authenticated_client).json
    assert recent["items"] and recent["coverage"]["truncated"]
    assert sum(url.endswith("/trending/tv/week") for url, _ in upstream.calls) == calls


@pytest.mark.parametrize("path", ["trending", "digital", "recent-episodes"])
def test_shared_saturation_recovers_without_cache_poisoning(authenticated_client, upstream, monkeypatch, path):
    from discover import feeds
    configure(upstream, monkeypatch)
    monkeypatch.setattr(feeds, "_jobs", {"one": feeds._Job(time.monotonic() + 12), "two": feeds._Job(time.monotonic() + 12)})
    busy = authenticated_client.get("/api/discover/feeds/" + path, headers={"X-API-KEY": "metadata-test-key"}).json
    assert busy["status"] == "unavailable" and busy["retry_after_ms"] == 1000
    assert upstream.calls == []
    feeds._jobs.clear()
    assert get(authenticated_client).json["items"]
    assert authenticated_client.provider_searches == []


def test_late_season_cannot_publish_and_stalled_jobs_count_toward_cap(authenticated_client, upstream, monkeypatch):
    from discover import feeds
    configure(upstream, monkeypatch)
    original = upstream.payload
    started = threading.Event()
    release = threading.Event()
    def payload(url):
        if "/season/" in url:
            started.set()
            assert release.wait(5)
        return original(url)
    upstream.payload = payload
    monkeypatch.setattr(feeds, "CALL_SECONDS", 0.1)
    before = time.monotonic()
    try:
        first = get(authenticated_client).json
        assert started.is_set()
        assert time.monotonic() - before < 1
        assert first["items"] == [] and first["status"] == "unavailable"
        assert len(feeds._jobs) == 1
    finally:
        release.set()
        for job in list(feeds._jobs.values()):
            assert job.done.wait(5)
    assert get(authenticated_client).json["items"] == []


def test_empty_observation_has_honest_coverage_and_dates(authenticated_client, upstream, monkeypatch):
    configure(upstream, monkeypatch)
    upstream.payload = {"results": [], "total_pages": 0}
    result = get(authenticated_client).json
    assert result["status"] == "empty" and result["coverage"]["complete"]
    assert result["fetched_at"] == result["last_success"] == "2026-09-08T12:00:00+00:00"


@pytest.mark.parametrize("today,start", [(date(2026, 1, 1), "2025-12-03"), (date(2024, 3, 1), "2024-02-01")])
def test_calendar_boundaries(authenticated_client, upstream, monkeypatch, today, start):
    from discover import feeds
    configure(upstream, monkeypatch, rows=[episode(401, 1, today.isoformat()), episode(402, 2, start)])
    result = feeds.recent_episodes(today)
    assert result["window"] == {"start": start, "end": today.isoformat()}
    assert len(result["items"]) == 2


def test_empty_library_can_retrieve_the_exact_feed_episode(authenticated_client, upstream, episode_sources, providers, retrieval_database, monkeypatch):
    import sqlalchemy as sa
    from app import database as db
    from discover import feeds
    from provider_hub.protocol import candidate_from_worker
    from subliminal_patch.extensions import provider_registry
    engine, session = retrieval_database
    monkeypatch.setattr(feeds, "_now", lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    episode_sources.records["/trending/tv/week"] = {"results": [{"id": 100, "name": "Northern Light"}], "total_pages": 1}
    episode_sources.records["/tv/100/season/0"] = {"id": 200, "season_number": 0, "episodes": []}
    episode_sources.target.update(seasonNumber=3, number=7)
    before = {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables}
    candidate = candidate_from_worker("discover_recent", {"id": "recent-sub", "language": {"alpha3": "eng"},
                                      "release_info": "Northern.Light.S03E07", "provider_payload": {}})
    name = providers.add("discover_recent", [candidate])
    fetched = []
    content = b"1\n00:00:01,000 --> 00:00:02,000\nExact recent episode\n\n"
    def download(self, subtitle):
        fetched.append(subtitle.worker_id)
        subtitle.content = content
    monkeypatch.setattr(provider_registry[name], "download_subtitle", download)
    records = get(authenticated_client).json["items"]
    item = next(row for row in records if row["id"] == 401)
    detail = fixtures.get(authenticated_client, f"shows/{item['show_id']}/seasons/{item['season']}/episodes/{item['episode']}").json["data"]["episode"]
    assert detail["id"] == item["id"] and detail["air_date"] == item["air_date"]
    assert providers.videos == [] and fetched == []
    response = authenticated_client.post("/api/discover/search", headers={"X-API-KEY": "metadata-test-key"}, json={
        "media_type": "episode", "imdb_id": detail["show_imdb_id"], "language": "eng",
        "title": detail["show_title"], "year": detail["show_year"], "show_id": detail["show_id"],
        "season": detail["target_season"], "episode": detail["target_episode"], "episode_identity": detail})
    assert response.status_code == 200
    row = response.json["results"][0]
    downloaded = authenticated_client.get("/api/discover/download", headers={"X-API-KEY": "metadata-test-key"},
                                         query_string={"result_id": row["id"], "search_id": row["search_id"]})
    assert downloaded.status_code == 200 and downloaded.data == content
    assert fetched == ["recent-sub"]
    assert len(providers.videos) == 1
    video = providers.videos[0][1]
    assert (video.season, video.episode) == (3, 7)
    assert video.tmdb_id == 401
    assert before == {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables}
