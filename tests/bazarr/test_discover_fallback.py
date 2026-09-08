"""OMDB fallback preserves primary capability and source identity."""
import json
from types import SimpleNamespace

import pytest

import test_discover_search as retrieval_fixtures
import test_discover_download as download_fixtures
import test_discover_metadata as fixtures
import test_discover_library as library_fixtures

upstream = fixtures.upstream
authenticated_client = fixtures.authenticated_client
library_database = library_fixtures.library_database


@pytest.fixture(autouse=True)
def fallback_config(monkeypatch):
    from app.config import settings
    from discover import metadata
    from subliminal_patch.refiners import omdb
    monkeypatch.setattr(metadata, "_omdb_current", None)
    monkeypatch.setattr(settings.discover, "tmdb_access_token", "")
    monkeypatch.setattr(omdb, "_resolve_omdb_apikey", lambda: None)


def test_omdb_fallback_preserves_primary_setup_and_returns_sparse_identity(authenticated_client, library_database, monkeypatch):
    from app.config import settings
    from discover import metadata
    from subliminal_patch.refiners import omdb
    settings.discover.tmdb_access_token = ""
    monkeypatch.setattr(omdb, "_resolve_omdb_apikey", lambda: "synthetic-omdb-key")
    calls = []
    def transport(config, params, deadline=None):
        calls.append(params)
        return {"Response": "True", "Search": [{"Title": "Example", "Year": "N/A", "Type": "movie",
                                                 "imdbID": "tt0000042", "Poster": "N/A"}], "totalResults": "1"}
    monkeypatch.setattr(metadata, "_omdb_request", transport, raising=False)
    response = fixtures.get(authenticated_client, "search?q=Example&type=movie&source=all")
    assert response.status_code == 200
    data = response.json["data"]
    assert data["primary"]["status"] == "unconfigured"
    assert data["items"][0]["source_id"] == "omdb:movie:tt0000042"
    assert data["items"][0]["year"] is None
    assert calls == [{"s": "example", "type": "movie", "page": 1}]
    assert authenticated_client.provider_searches == []
    assert "synthetic-omdb-key" not in json.dumps(data)


@pytest.fixture
def omdb_transport(monkeypatch):
    from discover import metadata
    from subliminal_patch.refiners import omdb
    monkeypatch.setattr(omdb, "_resolve_omdb_apikey", lambda: "synthetic-omdb-key")
    state = SimpleNamespace(calls=[], error=None, raw={"Response": "True", "Search": [
        {"Title": "Example", "Type": "movie", "imdbID": "tt0000042", "Year": "2024"}], "totalResults": "1"})
    def request(config, params, deadline=None):
        state.calls.append(params)
        if state.error:
            raise state.error
        return state.raw
    monkeypatch.setattr(metadata, "_omdb_request", request)
    return state


def test_fallback_order_keeps_primary_outage(library_database, monkeypatch, omdb_transport):
    from discover import metadata
    from app.config import settings
    settings.discover.tmdb_access_token = "synthetic-tmdb"
    monkeypatch.setattr(metadata, "_request", lambda *a, **kw: {"results": [{"id": 42, "title": "Primary"}]})
    assert metadata.candidates("Example")["data"]["items"][0]["source"] == "tmdb"
    assert omdb_transport.calls == []
    settings.discover.tmdb_access_token = "synthetic-tmdb-outage"
    def fail(*args, **kwargs):
        raise metadata.UpstreamFailure()
    monkeypatch.setattr(metadata, "_request", fail)
    data = metadata.candidates("Example")["data"]
    assert data["primary"]["status"] == "unavailable"
    assert data["source"] == "omdb"
    assert len(omdb_transport.calls) == 1


def test_omdb_cache_isolated_from_locale_region_language_and_rotations(monkeypatch, omdb_transport):
    from app.config import settings
    from discover import metadata
    from subliminal_patch.refiners import omdb
    first = metadata.omdb_search("Example")["data"]
    monkeypatch.setattr(settings.discover, "locale", "hu-HU")
    monkeypatch.setattr(settings.discover, "region", "HU", raising=False)
    second = metadata.omdb_search("Example")["data"]
    assert second["status"] == "cached"
    assert second["fallback_revision"] == first["fallback_revision"]
    assert second["locale"] == ""
    assert len(omdb_transport.calls) == 1
    monkeypatch.setattr(omdb, "_resolve_omdb_apikey", lambda: "rotated-synthetic-key")
    rotated = metadata.omdb_search("Example")["data"]
    assert rotated["status"] == "available"
    assert rotated["fallback_revision"] != first["fallback_revision"]
    assert len(omdb_transport.calls) == 2
    assert all(set(params) == {"s", "type", "page"} for params in omdb_transport.calls)


def test_omdb_stale_timeout_retains_original_time_then_expires(monkeypatch, omdb_transport):
    from discover import metadata
    now = [100.0]
    monkeypatch.setattr(metadata.time, "monotonic", lambda: now[0])
    first = metadata.omdb_search("Example")["data"]
    now[0] += 301
    omdb_transport.error = metadata.UpstreamFailure()
    stale = metadata.omdb_search("Example")["data"]
    assert stale["status"] == "cached" and stale["service_status"] == "unavailable"
    assert stale["fetched_at"] == first["fetched_at"]
    now[0] = 3700
    assert metadata.omdb_search("Example")["data"]["items"] == []


def test_omdb_duplicates_sparse_fields_and_page_limit(omdb_transport):
    from discover import metadata
    omdb_transport.raw["Search"] *= 20
    omdb_transport.raw["Search"][0]["Year"] = "2024 garbage"
    omdb_transport.raw["totalResults"] = "20000"
    data = metadata.omdb_search("Example")["data"]
    assert len(data["items"]) == 1 and data["truncated"] is True
    assert data["items"][0]["year"] is None
    assert len(omdb_transport.calls) == 1


@pytest.mark.parametrize("field,value", [("imdbID", "42"), ("imdbID", "tt123"), ("Type", "series"), ("Title", "N/A")])
def test_omdb_malformed_identity_never_adopted(omdb_transport, field, value):
    from discover import metadata
    omdb_transport.raw["Search"][0][field] = value
    data = metadata.omdb_search("Example")["data"]
    assert data["status"] == "unavailable" and data["items"] == []


def test_obsolete_omdb_request_cannot_publish(monkeypatch, omdb_transport):
    from discover import metadata
    from subliminal_patch.refiners import omdb
    def rotate(config, params, deadline=None):
        monkeypatch.setattr(omdb, "_resolve_omdb_apikey", lambda: "new-synthetic-key")
        return omdb_transport.raw
    monkeypatch.setattr(metadata, "_omdb_request", rotate)
    data = metadata.omdb_search("Example")["data"]
    assert data["status"] == "unavailable" and data["items"] == []


def test_private_https_transport_never_debug_logs_query_credential(monkeypatch, capsys, caplog):
    import http.client
    from io import BytesIO
    from discover import metadata
    from urllib.parse import parse_qs, urlsplit
    payload = json.dumps({"Response": "True", "Title": "Example", "imdbID": "tt0000042", "Type": "movie"}).encode()
    sent = []
    class Socket:
        def sendall(self, data):
            sent.append(data)
        def makefile(self, *args):
            return BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        def settimeout(self, value):
            pass
        def close(self):
            pass
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", lambda self: setattr(self, "sock", Socket()))
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
    caplog.set_level("DEBUG")
    config = metadata.Configuration("synthetic-query-credential", "", "debug-test")
    result = metadata._omdb_request(config, {"i": "tt0000042"})
    target = b"".join(sent).split(b" ")[1].decode()
    assert parse_qs(urlsplit(target).query)["apikey"] == [config.token]
    assert result["imdbID"] == "tt0000042"
    captured = capsys.readouterr()
    assert config.token not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("body,status", [({"Response": "False", "Error": "Invalid API key!"}, "authentication_failed"),
                                         ({"Response": "False", "Error": "Request limit reached!"}, "unavailable"),
                                         ({"Response": "True", "Title": "synthetic-query-credential"}, "unavailable")])
def test_transport_error_envelopes_are_sanitized(monkeypatch, body, status):
    import http.client
    from io import BytesIO
    from discover import metadata
    payload = json.dumps(body).encode()
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs):
            self.response = BytesIO(payload)
        def set_debuglevel(self, value):
            assert value == 0
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return SimpleNamespace(status=200, getheader=lambda _: None, read1=self.response.read)
        def close(self):
            pass
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    with pytest.raises(metadata.UpstreamFailure) as caught:
        metadata._omdb_request(metadata.Configuration("synthetic-query-credential", "", "transport-" + status), {"s": "Example"})
    assert str(caught.value) == status


def test_supported_show_identity_requires_explicit_manual_numbers(library_database, omdb_transport):
    from discover import metadata
    omdb_transport.raw = {"Response": "True", "Title": "Example series", "Type": "series", "imdbID": "tt0000042", "Year": "2020–2024"}
    data = metadata.title_details("tt0000042", "show", "omdb")["data"]
    assert data["item"]["id"] == "tt0000042"
    assert data["item"]["seasons"] is None
    assert data["item"]["tvdb_id"] is None
    assert data["item"]["year"] == 2020
    assert "episode_identity" not in data["item"]


providers = retrieval_fixtures.providers
choices = download_fixtures.choices


@pytest.mark.parametrize("kind", ["movie", "show"])
def test_fallback_selection_continues_real_explicit_search_and_exact_attachment(
    authenticated_client, library_database, omdb_transport, choices, kind,
):
    import sqlalchemy as sa
    from app.database import Base
    _, session = library_database
    before = {table.name: session.execute(sa.select(table)).all() for table in Base.metadata.sorted_tables}
    omdb_transport.raw = {"Response": "True", "Title": "Example", "Type": "series" if kind == "show" else "movie",
                          "imdbID": "tt0000042", "Year": "2024"}
    endpoint = "shows" if kind == "show" else "movies"
    response = fixtures.get(authenticated_client, endpoint + "/tt0000042?source=omdb")
    assert response.status_code == 200
    item = response.json["data"]["item"]
    assert choices[1] == []
    payload = {"media_type": "episode" if kind == "show" else "movie", "imdb_id": item["imdb_id"],
               "title": item["title"], "year": item["year"], "language": "eng"}
    if kind == "show":
        payload.update(season=2, episode=1, manual_confirmed=True)
    searched = authenticated_client.post("/api/discover/search", json=payload, headers={"X-API-KEY": "metadata-test-key"})
    assert searched.status_code == 200
    row = next(row for row in searched.json["results"] if row["scope"] == "forced")
    downloaded = authenticated_client.get("/api/discover/download", query_string={"result_id": row["id"], "search_id": row["search_id"]},
                                         headers={"X-API-KEY": "metadata-test-key"})
    assert downloaded.status_code == 200
    assert downloaded.data == download_fixtures.FORCED_SRT
    assert "attachment" in downloaded.headers["Content-Disposition"]
    assert choices[1] == ["forced"]
    assert {table.name: session.execute(sa.select(table)).all() for table in Base.metadata.sorted_tables} == before


def test_omdb_caller_wait_is_bounded_without_late_cache_publication(monkeypatch):
    import http.client
    import threading
    from discover import metadata
    from subliminal_patch.refiners import omdb
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs):
            pass
        def set_debuglevel(self, value):
            pass
        def request(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
        def getresponse(self):
            return SimpleNamespace(status=503)
        def close(self):
            closed.set()
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(omdb, "_resolve_omdb_apikey", lambda: "timeout-synthetic-key")
    monkeypatch.setattr(metadata, "OMDB_WAIT_SECONDS", .03)
    result = metadata.omdb_search("Example")["data"]
    assert entered.is_set()
    assert result["status"] == "unavailable" and result["items"] == []
    assert result["failure_reason"] == "timeout"
    release.set()
    assert closed.wait(3)
    from dogpile.cache.api import NO_VALUE
    assert metadata._omdb_cache.get(("omdb", result["fallback_revision"], ("search", "movie", "example"))) is NO_VALUE


def test_omdb_search_cache_and_media_namespaces_are_bounded(omdb_transport):
    from discover import metadata
    for index in range(260):
        assert metadata.omdb_search(f"Example {index}")["data"]["status"] == "available"
    assert metadata._omdb_cache.backend._cache.currsize <= 256
    assert metadata.omdb_search("Example 0")["data"]["status"] == "available"
    omdb_transport.raw["Search"][0]["Type"] = "series"
    show = metadata.omdb_search("Example 259", "show")["data"]
    assert show["status"] == "available"
    assert show["items"][0]["source_id"] == "omdb:show:tt0000042"


@pytest.mark.parametrize("query", ["source=all&source=tmdb&q=Example", "source=remote&q=Example", "source=all&q=Example&type=episode", "source=tmdb&q=Example&local_q=Example", "source=all&q=Example&local_q=A&local_q=B"])
def test_source_query_admission_rejects_ambiguous_parameters(authenticated_client, upstream, query):
    response = fixtures.get(authenticated_client, "search?" + query)
    assert response.status_code == 400
    assert upstream.calls == []
    assert authenticated_client.provider_searches == []


def test_omdb_queued_request_succeeds_with_remaining_budget(monkeypatch, omdb_transport):
    import threading
    from discover import metadata
    config = metadata.omdb_configuration()
    key = ("omdb", config.revision, ("search", "movie", "example"))
    lock = metadata._fetch_locks[hash(key) % len(metadata._fetch_locks)]
    monkeypatch.setattr(metadata, "OMDB_WAIT_SECONDS", 1)
    lock.acquire()
    release = threading.Timer(.02, lock.release)
    release.start()
    try:
        data = metadata.omdb_search("Example")["data"]
    finally:
        release.join(1)
    assert data["status"] == "available" and len(omdb_transport.calls) == 1
    assert "failure_reason" not in data
    assert metadata.omdb_search("Example")["data"]["status"] == "cached"
    assert len(omdb_transport.calls) == 1


def test_tmdb_work_on_same_stripe_cannot_hold_omdb_caller(monkeypatch, omdb_transport):
    import threading
    from app.config import settings
    from discover import metadata
    monkeypatch.setattr(settings.discover, "tmdb_access_token", "synthetic-stripe-primary")
    primary, fallback = metadata.configuration(), metadata.omdb_configuration()
    stripe = hash(("omdb", fallback.revision, ("search", "movie", "example"))) % len(metadata._fetch_locks)
    operation = next(("shared-stripe", i) for i in range(10000)
                     if hash((metadata.SOURCE, primary.revision, primary.locale, ("shared-stripe", i)))
                     % len(metadata._fetch_locks) == stripe)
    entered, release = threading.Event(), threading.Event()
    primary_result = []
    def load():
        entered.set()
        assert release.wait(2)
        return {"items": []}
    worker = threading.Thread(target=lambda: primary_result.append(metadata._cached(primary, operation, load)))
    worker.start()
    try:
        assert entered.wait(1)
        monkeypatch.setattr(metadata, "OMDB_WAIT_SECONDS", .02)
        result = metadata.omdb_search("Example")["data"]
        assert result["status"] == "unavailable" and result["failure_reason"] == "timeout"
        assert omdb_transport.calls == []
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert primary_result[0][1] == "available"


@pytest.mark.parametrize("elapsed,available", [(11.999, True), (12, False), (12.001, False)])
@pytest.mark.parametrize("stage", ["transport", "publication", "return"])
def test_omdb_deadline_covers_loading_publication_and_return(monkeypatch, omdb_transport, elapsed, available, stage):
    from discover import metadata
    from dogpile.cache.api import NO_VALUE
    now = [100.0]
    monkeypatch.setattr(metadata.time, "monotonic", lambda: now[0])
    snapshot = metadata._omdb_snapshot
    def checked_snapshot(deadline):
        result = snapshot(deadline)
        if stage == "return" and metadata._omdb_cache.get(key) is not NO_VALUE:
            now[0] = 100 + elapsed
        return result
    def transport(config, params, deadline=None):
        assert deadline == 112
        if stage == "transport":
            now[0] = 100 + elapsed
        return omdb_transport.raw
    publish = metadata._omdb_cache.set
    def delayed_publish(cache_key, value):
        publish(cache_key, value)
        if stage == "publication":
            now[0] = 100 + elapsed
    config = metadata.omdb_configuration()
    key = ("omdb", config.revision, ("search", "movie", "example"))
    monkeypatch.setattr(metadata, "_omdb_request", transport)
    monkeypatch.setattr(metadata, "_omdb_snapshot", checked_snapshot)
    monkeypatch.setattr(metadata._omdb_cache, "set", delayed_publish)
    result = metadata.omdb_search("Example")["data"]
    assert result["status"] == ("available" if available else "unavailable")
    if available:
        assert result["items"] and metadata._omdb_cache.get(key) is not NO_VALUE
    else:
        assert result["items"] == [] and result["failure_reason"] == "timeout"
        assert metadata._omdb_cache.get(key) is NO_VALUE


@pytest.mark.parametrize("reason", ["quota", "timeout"])
def test_combined_failure_reason_preserves_primary_and_local_candidates(
    authenticated_client, library_database, monkeypatch, omdb_transport, reason,
):
    from app.config import settings
    from app.database import TableMovies
    from discover import metadata
    _, session = library_database
    session.add(TableMovies(id=1, radarrId=1, arr_instance_id=1, title="Example local", path="/private/local",
                            imdbId="tt0000099", tmdbId="99"))
    session.commit()
    monkeypatch.setattr(settings.discover, "tmdb_access_token", "synthetic-primary-outage")
    def primary_failure(*args, **kwargs):
        raise metadata.UpstreamFailure()
    monkeypatch.setattr(metadata, "_request", primary_failure)
    omdb_transport.error = metadata.OMDBFailure(reason)
    response = fixtures.get(authenticated_client, "search?q=Example&type=movie&source=all")
    assert response.status_code == 200
    data = response.json["data"]
    assert data["primary"]["status"] == "unavailable"
    assert data["fallback"]["failure_reason"] == reason
    assert data["items"][0]["source"] == "local"
    assert data["items"][0]["copies"][0]["local_id"] == 1
    assert authenticated_client.provider_searches == []


def test_unknown_sources_cannot_reuse_numeric_tmdb_detail_routes(authenticated_client):
    for path in ["movies/42?source=all", "movies/tt0000042?source=tmdb", "movies/tt0000042?source=local", "shows/42?source=local&source=tmdb"]:
        assert fixtures.get(authenticated_client, path).status_code == 400


@pytest.mark.parametrize("stale_age", [None, 301, 3600])
def test_omdb_cache_stripe_wait_is_within_caller_budget(monkeypatch, omdb_transport, stale_age):
    import threading
    import time
    from discover import metadata
    config = metadata.omdb_configuration()
    operation = ("search", "movie", "example")
    key = ("omdb", config.revision, operation)
    if stale_age is not None:
        first = metadata.omdb_search("Example")["data"]
        entry = metadata._omdb_cache.get(key)
        entry["time"] = time.monotonic() - stale_age
        metadata._omdb_cache.set(key, entry)
    before_calls = len(omdb_transport.calls)
    lock = metadata._fetch_locks[hash(key) % len(metadata._fetch_locks)]
    monkeypatch.setattr(metadata, "OMDB_WAIT_SECONDS", .02)
    finished = threading.Event()
    result = {}
    def fetch():
        try:
            result.update(metadata.omdb_search("Example")["data"])
        finally:
            finished.set()
    lock.acquire()
    thread = threading.Thread(target=fetch)
    try:
        thread.start()
        completed = finished.wait(.1)
    finally:
        lock.release()
        thread.join(1)
    assert completed, "OMDB request waited beyond its caller budget behind another cache operation"
    assert not thread.is_alive()
    assert len(omdb_transport.calls) == before_calls
    assert result["failure_reason"] == "timeout"
    if stale_age == 301:
        assert result["status"] == "cached" and result["fetched_at"] == first["fetched_at"]
    else:
        assert result["status"] == "unavailable" and result["items"] == []


@pytest.mark.parametrize("failure", ["http429", "quota_payload", "socket_timeout"])
@pytest.mark.parametrize("primed", [False, True])
def test_safe_quota_and_timeout_reasons_survive_authenticated_api_and_cache(
    authenticated_client, library_database, monkeypatch, failure, primed,
):
    import http.client
    from io import BytesIO
    from discover import metadata
    from subliminal_patch.refiners import omdb
    monkeypatch.setattr(omdb, "_resolve_omdb_apikey", lambda: "synthetic-reason-key")
    failed = [False]
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs):
            self.content = BytesIO(json.dumps({"Response": "False", "Error": "Request limit reached!"} if failed[0] else {
                "Response": "True", "Title": "Example", "Type": "movie", "imdbID": "tt0000042"}).encode())
        def set_debuglevel(self, value):
            pass
        def request(self, *args, **kwargs):
            if failed[0] and failure == "socket_timeout":
                raise TimeoutError("synthetic-reason-key must not escape")
        def getresponse(self):
            return SimpleNamespace(status=429 if failed[0] and failure == "http429" else 200,
                                   getheader=lambda _: None, read1=self.content.read)
        def close(self):
            pass
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    path = "movies/tt0000042?source=omdb"
    if primed:
        first = fixtures.get(authenticated_client, path).json["data"]
        key = ("omdb", first["fallback_revision"], ("details", "movie", "tt0000042"))
        entry = metadata._omdb_cache.get(key)
        entry["time"] -= 301
        metadata._omdb_cache.set(key, entry)
    failed[0] = True
    response = fixtures.get(authenticated_client, path)
    assert response.status_code == 200
    result = response.json["data"]
    reason = "timeout" if failure == "socket_timeout" else "quota"
    assert result["failure_reason"] == reason
    assert result["status"] == ("cached" if primed else "unavailable")
    assert "synthetic-reason-key" not in response.get_data(as_text=True)
    if primed:
        assert result["fetched_at"] == first["fetched_at"]
        assert result["service_status"] == "unavailable"
    assert authenticated_client.provider_searches == []


@pytest.mark.parametrize("kind", ["movie", "show"])
@pytest.mark.parametrize("ordering", [(41, 42, None), (None, 42, 41), (41, None, 42)])
@pytest.mark.parametrize("consistent", [False, True])
def test_authenticated_composition_keeps_full_conflict_evidence_and_final_ownership(
    authenticated_client, library_database, monkeypatch, kind, ordering, consistent,
):
    import sqlalchemy as sa
    from app.database import Base, TableShows, TableEpisodes
    from discover import metadata
    engine, session = library_database
    actual = tuple(41 if consistent and value == 42 else value for value in ordering)
    for identity, external in enumerate(actual, 1):
        title = "Example partial" if external is None else "Other title"
        if kind == "movie":
            library_fixtures.add_movie(session, identity, identity, imdb="tt0000001",
                                       tmdb=str(external) if external else "invalid", title=title)
        else:
            session.add(TableShows(id=identity, arr_instance_id=identity, sonarrSeriesId=7, title=title,
                                   imdbId="tt0000001", tvdbId=external, path=f"/private/{identity}"))
    session.flush()
    if kind == "show":
        for identity in range(1, 4):
            session.add(TableEpisodes(id=identity, series_id=identity, arr_instance_id=identity,
                                     sonarrSeriesId=7, sonarrEpisodeId=7, season=1, episode=identity,
                                     title="Episode", path=f"/private/episode-{identity}"))
        session.add(TableEpisodes(id=4, series_id=1, arr_instance_id=99, sonarrSeriesId=7,
                                 sonarrEpisodeId=7, season=1, episode=4, title="Wrong owner", path="/private/wrong"))
    # An equal identity in the other media namespace must never contribute.
    if kind == "movie":
        session.add(TableShows(id=99, arr_instance_id=99, sonarrSeriesId=7, title="Example opposite",
                               imdbId="tt0000001", tvdbId=41, path="/private/opposite"))
    else:
        library_fixtures.add_movie(session, 99, 99, imdb="tt0000001", tmdb="41", title="Example opposite")
    session.commit()
    external_key = "tmdb_id" if kind == "movie" else "tvdb_id"
    remote = {"source": "tmdb", "source_id": f"tmdb:{kind}:41", "id": 41, "media_type": kind,
              "imdb_id": "tt0000001", external_key: 41, "title": "Example catalog", "year": 2024,
              "overview": "", "poster_url": None, "backdrop_url": None, "mapping_status": "resolved"}
    def catalog(query):
        return {"data": {"status": "available", "source": "tmdb", "configured": True, "message": "Available",
                         "items": [dict(remote)]}}
    monkeypatch.setattr(metadata, "search_movies" if kind == "movie" else "search_shows", catalog)
    before = {table.name: session.execute(sa.select(table)).all() for table in Base.metadata.sorted_tables}
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        response = fixtures.get(authenticated_client, f"search?q=Example&type={kind}&source=all")
        assert response.status_code == 200
        items = response.json["data"]["items"]
        partial = ordering.index(None) + 1
        expected = {f"tmdb:{kind}:41": {1, 2, 3}} if consistent else {
            f"tmdb:{kind}:41": {ordering.index(41) + 1}, f"local:{kind}:{partial}": {partial}}
        assert {item["source_id"]: {copy["local_id"] for copy in item["copies"]} for item in items} == expected
        for item in items:
            copies = expected[item["source_id"]]
            assert {copy["arr_instance_id"] for copy in item["copies"]} == copies
            assert item[external_key] == (None if item["source"] == "local" and not consistent else 41)
            if kind == "show":
                assert item["ownership"]["episode_count"] == sum(copy["episode_count"] for copy in item["copies"]) == len(copies)
                assert item["ownership"]["selected_episode_owned"] is None
                assert item["ownership"]["complete_series"] is None
        for identity, external in enumerate(actual, 1):
            details = fixtures.get(authenticated_client, f"{'movies' if kind == 'movie' else 'shows'}/{identity}?source=local")
            assert details.status_code == 200
            item = details.json["data"]["item"]
            assert {copy["local_id"] for copy in item["copies"]} == ({1, 2, 3} if consistent else {identity})
            assert item[external_key] == (41 if consistent else external)
        assert authenticated_client.provider_searches == []
        assert "private" not in response.get_data(as_text=True)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert {table.name: session.execute(sa.select(table)).all() for table in Base.metadata.sorted_tables} == before
