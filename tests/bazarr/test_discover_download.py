"""Exact Discover attachments through the real authenticated API and provider pool."""
import pytest

import test_discover_search as search_fixtures

authenticated_client = search_fixtures.authenticated_client
providers = search_fixtures.providers
retrieval_database = search_fixtures.retrieval_database
post = search_fixtures.post


FULL_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nFull dialogue\n\n"
FORCED_SRT = b"1\n00:00:03,000 --> 00:00:04,000\nForced translation\n\n"
CONTEXT = {"media_type": "episode", "imdb_id": "tt0903747", "title": "Breaking Bad",
           "year": 2008, "season": 2, "episode": 1, "language": "eng", "manual_confirmed": True}


@pytest.fixture
def choices(providers, monkeypatch):
    from provider_hub.protocol import candidate_from_worker
    from subliminal_patch.extensions import provider_registry

    fetched = []
    candidates = [candidate_from_worker("discover_download", {
        "id": key, "language": {"alpha3": "eng", "forced": forced},
        "release_info": f"Breaking.Bad.S02E01.{key}", "provider_payload": {},
    }) for key, forced in [("full", False), ("forced", True)]]
    name = providers.add("discover_download", candidates)

    def download(self, subtitle):
        fetched.append(subtitle.worker_id)
        subtitle.content = FORCED_SRT if subtitle.worker_id == "forced" else FULL_SRT

    monkeypatch.setattr(provider_registry[name], "download_subtitle", download)
    return candidates, fetched


def get(client, row, **kwargs):
    return client.get("/api/discover/download", query_string={"result_id": row["id"],
                      "search_id": row["search_id"], **kwargs}, headers={"X-API-KEY": "discover-test-key"})


def test_exact_forced_attachment_without_library_or_hub_mutation(
    authenticated_client, choices, retrieval_database, monkeypatch,
):
    import sqlalchemy as sa
    from compat import service, meter
    from app import database as db

    engine, session = retrieval_database
    before = {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables}
    media_and_library_writes = []

    def forbidden(*args, **kwargs):
        media_and_library_writes.append(args)
        raise AssertionError("Unexpected library or metering access")

    for name in ["_lookup_library_metadata", "_parse_video_from_library", "search_local", "download"]:
        monkeypatch.setattr(service, name, forbidden)
    monkeypatch.setattr(meter, "record", forbidden)
    snapshot = post(authenticated_client, CONTEXT).json
    chosen = next(row for row in snapshot["results"] if row["scope"] == "forced")
    sql = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record_sql)
    try:
        download = get(authenticated_client, chosen)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record_sql)
    assert download.status_code == 200
    assert b"-->" in download.data
    assert download.data == FORCED_SRT
    assert "attachment" in download.headers["Content-Disposition"]
    assert "Breaking_Bad.S02E01.en.forced.srt" in download.headers["Content-Disposition"]
    assert download.mimetype == "application/x-subrip"
    assert download.headers["Cache-Control"] == "no-store"
    assert choices[1] == ["forced"]
    assert media_and_library_writes == [] and sql == []
    assert {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables} == before


def test_download_requires_ui_authorization(authenticated_client, choices):
    row = post(authenticated_client, CONTEXT).json["results"][0]
    for headers in [{}, {"X-API-KEY": "wrong"}]:
        response = authenticated_client.get("/api/discover/download", query_string={
            "result_id": row["id"], "search_id": row["search_id"]}, headers=headers)
        assert response.status_code == 401
    assert choices[1] == []


@pytest.mark.parametrize("invalid", ["unknown", "unicode", "missing", "extra", "oversized", "url", "wrong-search"])
def test_rejects_bad_handles_without_fetch(authenticated_client, choices, invalid):
    row = post(authenticated_client, CONTEXT).json["results"][0]
    parts = row["id"].split(".")
    bad = {"unknown": "d1.999999." + parts[2], "unicode": f"d1.{parts[1]}.árvíztűrő",
           "missing": ".".join(parts[:2]), "extra": row["id"] + ".more",
           "oversized": "d1.1." + "x" * 10000, "url": "http://127.0.0.1/sub.srt",
           "wrong-search": row["id"]}[invalid]
    response = get(authenticated_client, {**row, "id": bad}, **({"search_id": "different"} if invalid == "wrong-search" else {}))
    assert response.status_code == 410
    assert response.json["reason"] == "result_expired"
    assert response.json["recoverable"] is True
    assert choices[1] == []


@pytest.mark.parametrize("missing", ["result_id", "search_id"])
def test_missing_identity_never_fetches(authenticated_client, choices, missing):
    row = post(authenticated_client, CONTEXT).json["results"][0]
    params = {"result_id": row["id"], "search_id": row["search_id"]}
    params.pop(missing)
    response = authenticated_client.get("/api/discover/download", query_string=params,
                                        headers={"X-API-KEY": "discover-test-key"})
    assert response.status_code == 410
    assert choices[1] == []


@pytest.mark.parametrize("invalidated", ["expired", "restart"])
def test_invalidated_handles_recover_only_with_fresh_search(authenticated_client, choices, monkeypatch, invalidated):
    from compat import file_id_store
    row = post(authenticated_client, CONTEXT).json["results"][0]
    if invalidated == "restart":
        file_id_store.reset_store()
        file_id_store.get_store().put({"subtitle": choices[0][1]}, 300)
    else:
        store = file_id_store.get_store()
        key = int(row["id"].split(".")[1])
        expiry, record = store._store[key]
        store._store[key] = (0, record)
    assert get(authenticated_client, row).status_code == 410
    assert choices[1] == []
    fresh = post(authenticated_client, {**CONTEXT, "refresh": True}).json["results"][0]
    assert fresh["id"] != row["id"]
    assert get(authenticated_client, fresh).data == FULL_SRT


def test_retained_row_uses_original_search_after_provider_refresh_failure(authenticated_client, choices, providers, monkeypatch):
    from subliminal.exceptions import AuthenticationError
    first = post(authenticated_client, CONTEXT).json

    def fail(*args):
        raise AuthenticationError("fixture")

    monkeypatch.setattr(providers.pool["discover_download"], "list_subtitles", fail)
    refreshed = post(authenticated_client, {**CONTEXT, "refresh": True}).json
    row = refreshed["results"][1]
    assert row["search_id"] == first["search_id"] != refreshed["search_id"]
    assert row["checked_at"] == first["results"][1]["checked_at"]
    assert refreshed["status"] == "partial" and row["stale"]
    assert get(authenticated_client, row).data == FORCED_SRT


@pytest.mark.parametrize("payload", [b"", b"<html>Error</html>", b"not subtitles", b"1\n00:00:01,000 --> 00:00:02,000\n\n"])
def test_invalid_or_empty_provider_content_is_not_an_attachment(authenticated_client, choices, providers, monkeypatch, payload):
    row = post(authenticated_client, CONTEXT).json["results"][0]

    def download(subtitle):
        subtitle.content = payload

    monkeypatch.setattr(providers.pool["discover_download"], "download_subtitle", download)
    response = get(authenticated_client, row)
    assert response.status_code == 502
    assert response.json["reason"] == "download_failed"
    assert "Content-Disposition" not in response.headers


@pytest.mark.parametrize("post_redirect", [False, True])
def test_existing_outbound_guards_remain_in_download_path(authenticated_client, choices, providers, monkeypatch, post_redirect):
    from compat import service
    from utilities.url_guard import UnsafeURLError
    row = post(authenticated_client, CONTEXT).json["results"][0]
    subtitle = choices[0][0]
    subtitle.download_link = "https://safe.example/sub.srt" if post_redirect else "HTTP://127.0.0.1/sub.srt"
    guards = []

    def reject(url):
        guards.append(url)
        raise UnsafeURLError("fixture")

    if post_redirect:
        monkeypatch.setattr(service, "resolve_safe_url", lambda url: url)
        original = providers.pool["discover_download"].download_subtitle

        def redirected(sub):
            original(sub)
            sub.download_link = "http://127.0.0.1/redirect.srt"

        monkeypatch.setattr(providers.pool["discover_download"], "download_subtitle", redirected)
        monkeypatch.setattr(service, "assert_safe_outbound", reject)
    else:
        monkeypatch.setattr(service, "resolve_safe_url", reject)
    response = get(authenticated_client, row)
    assert response.status_code == 502
    assert len(guards) == 1
    assert choices[1] == (["full"] if post_redirect else [])


def test_filename_sanitizes_title_and_preserves_exact_episode(authenticated_client, choices):
    snapshot = post(authenticated_client, {**CONTEXT, "title": '../../Bad: "Title"/name Header'}).json
    response = get(authenticated_client, snapshot["results"][1])
    assert response.status_code == 200
    disposition = response.headers["Content-Disposition"]
    assert "/" not in disposition and "\\" not in disposition and "\r" not in disposition and "\n" not in disposition
    assert "S02E01.en.forced.srt" in disposition


@pytest.mark.parametrize("content", [
    b"WEBVTT\n\n00:00:03.000 --> 00:00:04.000\nForced translation\n\n",
    FORCED_SRT.decode().encode("utf-16"),
])
def test_provider_text_formats_normalize_to_valid_utf8_srt(authenticated_client, choices, providers, monkeypatch, content):
    import pysubs2
    row = post(authenticated_client, CONTEXT).json["results"][1]

    def download(subtitle):
        subtitle.content = content

    monkeypatch.setattr(providers.pool["discover_download"], "download_subtitle", download)
    response = get(authenticated_client, row)
    assert response.status_code == 200
    parsed = pysubs2.SSAFile.from_string(response.data.decode("utf-8"), format_="srt")
    assert [(event.start, event.end, event.plaintext) for event in parsed] == [(3000, 4000, "Forced translation")]
    assert not response.data.startswith(b"WEBVTT")


def test_provider_missing_file_does_not_claim_handle_expiry(authenticated_client, choices, providers, monkeypatch):
    from compat import service
    row = post(authenticated_client, CONTEXT).json["results"][0]

    def missing(subtitle):
        raise FileNotFoundError("fixture upstream file missing")

    monkeypatch.setattr(service, "_fetch_subtitle_bytes", missing)
    response = get(authenticated_client, row)
    assert response.status_code == 502
    assert response.json["reason"] == "download_failed"


def test_movie_filename_has_exact_title_year_and_language(authenticated_client, choices):
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093",
                                         "title": "The Matrix", "year": 1999, "language": "eng"})
    download = get(authenticated_client, response.json["results"][0])
    assert download.status_code == 200
    assert 'The_Matrix.1999.en.full.srt' in download.headers["Content-Disposition"]


@pytest.mark.parametrize("query", ["Example.Movie.2024.1080p.WEB-DL", '../../Bad: "Title"/name Header', "電影"])
def test_raw_attachment_and_expiry_recovery_keep_query_without_identity(
    authenticated_client, choices, retrieval_database, monkeypatch, query,
):
    import sqlalchemy as sa
    from app import database as db
    from compat import file_id_store, service
    from werkzeug.utils import secure_filename

    engine, session = retrieval_database
    before = {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables}

    def forbidden(*args, **kwargs):
        pytest.fail("Raw attachment touched the library or a refiner")

    for name in ("_lookup_library_metadata", "_parse_video_from_library", "_refine_from_imdb", "download"):
        monkeypatch.setattr(service, name, forbidden)
    payload = {"mode": "release", "query": query, "language": "eng"}
    response = post(authenticated_client, payload)
    assert response.status_code == 200
    snapshot = response.json
    row = snapshot["results"][1]
    wrong = get(authenticated_client, row, search_id="identified-search")
    assert wrong.status_code == 410 and choices[1] == []
    download = get(authenticated_client, row)
    assert download.status_code == 200 and download.data == FORCED_SRT
    name = secure_filename(query)[:120].strip("._") or "release-query"
    assert f'{name}.en.forced.srt' in download.headers["Content-Disposition"]
    assert "tt" not in snapshot["context"] and "imdb_id" not in snapshot["context"]
    file_id_store.reset_store()
    assert get(authenticated_client, row).status_code == 410
    fresh = post(authenticated_client, {**payload, "refresh": True}).json
    assert fresh["context"] == snapshot["context"]
    assert fresh["results"][1]["id"] != row["id"]
    assert get(authenticated_client, fresh["results"][1]).data == FORCED_SRT
    assert {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables} == before


resolved_episode = search_fixtures.resolved_episode


def test_resolved_nonlibrary_episode_download_retains_original_source_and_exact_row(
    authenticated_client, choices, resolved_episode, retrieval_database,
):
    import sqlalchemy as sa
    from app import database as db
    from discover.handles import resolve_result
    engine, session = retrieval_database
    before = {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables}
    snapshot = post(authenticated_client, resolved_episode.context).json
    row = snapshot["results"][1]
    assert snapshot["context"]["episode_identity"]["air_date"] == "2026-09-01"
    assert resolve_result(row["id"])["context"]["episode_identity"]["id"] == 401
    download = get(authenticated_client, row)
    assert download.status_code == 200 and download.data == FORCED_SRT
    assert "Northern_Light.S02E01.en.forced.srt" in download.headers["Content-Disposition"]
    assert {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables} == before
