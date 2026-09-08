"""Owned sports files through the unchanged first-party Hub request protocol."""

import hashlib
import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from flask import Flask

HASH = "0000000000020000"  # 128 KiB of zero bytes, OpenSubtitles size-plus-words.
OTHER_HASH = "0000000000020001"  # The first little-endian word is one.


@pytest.fixture
def library(compat_db, tmp_path, monkeypatch):
    from app.config import settings
    from app.database import (
        database,
        delete,
        insert,
        TableArrInstances,
        TableSportsLeagues,
        TableSportsEvents,
        TableMovies,
        TableHistorySports,
    )
    from compat import cache, file_id_store, service
    from compat.routes import compat_bp

    for name, value in {
        "serve_local_subs": True,
        "search_rate_limit_enabled": True,
    }.items():
        monkeypatch.setattr(settings.compat_endpoint, name, value)
    monkeypatch.setattr(settings.general, "subfolder", "current")
    monkeypatch.setattr(settings.general, "subfolder_custom", "")
    monkeypatch.setattr(settings.general, "base_url", "")
    # Provider discovery is the external boundary. Keep real service/cache/fanout,
    # database, auth, language selection, file allocation and streaming code.
    monkeypatch.setattr(service, "get_providers_sorted", lambda: [])
    service.reset_compat_pool()
    cache.invalidate_all()
    file_id_store.reset_store()

    for owner, stable, default in [(101, "alpha", 0), (202, "zulu", 1)]:
        directory = tmp_path / str(owner)
        directory.mkdir()
        database.execute(
            insert(TableArrInstances).values(
                id=owner,
                kind="sportarr",
                name=stable,
                stable_key=stable,
                port=1867,
                enabled=1,
                is_default=default,
                path_mappings=json.dumps([["/recordings", str(directory)]]),
            )
        )
        database.execute(
            insert(TableSportsLeagues).values(
                id=owner, arr_instance_id=owner, sportarrLeagueId=31, title="League"
            )
        )
        name = "Alpha.Final.2026.mkv" if owner == 101 else "Zulu.Final.2026.mkv"
        (directory / name).write_bytes(b"\0" * 131072)
        subs = []
        for language in ("en", "hu"):
            subtitle = directory / name.replace(".mkv", f".{language}.srt")
            subtitle.write_bytes(
                f"1\n00:00:00,000 --> 00:00:01,000\n{owner} {language}\n".encode()
            )
            subs.append([language, "/recordings/" + subtitle.name])
        database.execute(
            insert(TableSportsEvents).values(
                id=owner * 10 + 1,
                arr_instance_id=owner,
                league_id=owner,
                sportarrLeagueId=31,
                sportarrEventId=51,
                file_id=77,
                path="/recordings/" + name,
                title=f"Final {owner}",
                sceneName=name.replace(".mkv", ".Release.mkv"),
                eventDate="2026-09-08",
                subtitles=repr(subs),
            )
        )
        # Indexed action 6 is not a provider-blacklist release.
        database.execute(
            insert(TableHistorySports).values(
                arr_instance_id=owner,
                league_id=owner,
                event_id=owner * 10 + 1,
                action=6,
                subtitles_path=subs[0][1],
                video_path="/recordings/" + name,
            )
        )

    native = tmp_path / "native"
    native.mkdir()
    (native / "Native.2020.mkv").write_bytes(b"\x02" + b"\0" * 131071)
    (native / "Native.2020.en.srt").write_bytes(
        b"1\n00:00:00,000 --> 00:00:01,000\nNative\n"
    )
    database.execute(
        insert(TableMovies).values(
            id=909,
            radarrId=909,
            tmdbId=909,
            title="Native",
            year="2020",
            imdbId="tt0000909",
            path=str(native / "Native.2020.mkv"),
            subtitles=repr([["en", str(native / "Native.2020.en.srt")]]),
        )
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(compat_bp, url_prefix="/api/v1")
    yield SimpleNamespace(client=app.test_client(), db=database, root=tmp_path)
    for table in (TableHistorySports, TableSportsEvents, TableSportsLeagues):
        database.execute(delete(table).where(table.arr_instance_id.in_([101, 202])))
    database.execute(
        delete(TableArrInstances).where(TableArrInstances.id.in_([101, 202]))
    )
    database.execute(delete(TableMovies).where(TableMovies.id == 909))
    cache.invalidate_all()
    file_id_store.reset_store()
    service.reset_compat_pool()


def key(**fields):
    from compat import keyring

    return keyring.create("sports-contract", tier="free", **fields)


def search(library, token, **query):
    return library.client.get(
        "/api/v1/subtitles",
        headers={"Api-Key": token},
        query_string={"languages": "en", **query},
    )


def entries(response):
    assert response.status_code == 200
    return response.get_json()["data"]


def file_id(response):
    data = entries(response)
    assert len(data) == 1, "Expected exactly one owned local subtitle"
    value = data[0]["attributes"]["files"][0]["file_id"]
    assert type(value) is int
    return value


def login(library, token, body=None):
    response = library.client.post(
        "/api/v1/login", headers={"Api-Key": token}, json=body or {}
    )
    assert response.status_code == 200
    return response.get_json()["token"]


def download(library, token, fid, jwt=None):
    return library.client.post(
        "/api/v1/download",
        json={"file_id": fid, "sub_format": "srt"},
        headers={
            "Api-Key": token,
            "Authorization": f"Bearer {jwt or login(library, token)}",
        },
    )


def link(response):
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body["remaining"], int)
    assert isinstance(body["remaining_downloads"], int)
    assert body["reset_time_utc"]
    url = urlsplit(body["link"])
    assert url.netloc in ("", "localhost")
    assert url.path.startswith("/api/v1/download/stream/")
    return url.path


def assert_stream(library, path, owner=202, language="en", token=None):
    response = library.client.get(path, headers={"Api-Key": token} if token else {})
    assert response.status_code == 200
    expected = f"1\n00:00:00,000 --> 00:00:01,000\n{owner} {language}\n".encode()
    assert len(response.data) > 0
    assert hashlib.sha256(response.data).digest() == hashlib.sha256(expected).digest()
    assert response.mimetype == "application/x-subrip"


@pytest.mark.parametrize("shape", ["jellyfin-hash", "vlsub-filename"])
def test_first_party_search_link_stream_and_meter(library, shape):
    from compat import meter

    kid, token = key(allowed_providers=["local"])
    query = (
        {"moviehash": HASH, "moviebytesize": 131072, "moviehash_match": "only"}
        if shape == "jellyfin-hash"
        else {"query": "Zulu.Final.2026.mkv"}
    )
    response = search(library, token, **query)
    fid = file_id(response)
    attrs = entries(response)[0]["attributes"]
    assert attrs["moviehash_match"] is (shape == "jellyfin-hash")
    assert attrs["feature_details"] == {
        "feature_type": "Movie",
        "imdb_id": 0,
        "season_number": 0,
        "episode_number": 0,
        "title": "Final 202",
        "movie_name": "2026 - Final 202",
        "year": 2026,
    }
    jwt = login(
        library, token, {"username": "bazarr", "password": "bazarr-placeholder"}
    )
    path = link(download(library, token, fid, jwt))
    assert_stream(library, path, token=token)
    assert meter.window_sum(kid, "search", "hour") == 1
    assert meter.window_sum(kid, "download", "hour") == 1
    assert file_id(search(library, token, **query)) == fid
    assert meter.window_sum(kid, "search", "hour") == 2
    assert meter.window_sum(kid, "download", "hour") == 1


@pytest.mark.parametrize(
    "query,expected,matched",
    [
        ({"moviehash": HASH}, 202, True),
        ({"moviehash": HASH, "query": "Alpha.Final.2026.mkv"}, 101, True),
        ({"query": r"C:\\Client\\  zULu.Final.2026.MKV  "}, 202, False),
        ({"query": "Zulu.Final.2026.Release.mkv"}, 202, False),
        ({"moviehash": OTHER_HASH, "query": "Zulu.Final.2026.mkv"}, 202, False),
    ],
)
def test_hash_filename_and_duplicate_ordering(library, query, expected, matched):
    _, token = key(allowed_providers=["local"])
    response = search(library, token, **query)
    assert entries(response)[0]["attributes"]["moviehash_match"] is matched
    assert_stream(
        library, link(download(library, token, file_id(response))), owner=expected
    )


def test_true_hash_outranks_exact_filename(library):
    (library.root / "101/Alpha.Final.2026.mkv").write_bytes(b"\x01" + b"\0" * 131071)
    _, token = key(allowed_providers=["local"])
    response = search(library, token, moviehash=OTHER_HASH, query="Zulu.Final.2026.mkv")
    assert_stream(library, link(download(library, token, file_id(response))), owner=101)


@pytest.mark.parametrize(
    "query",
    [
        {"moviehash": OTHER_HASH, "moviehash_match": "only"},
        {
            "moviehash": OTHER_HASH,
            "moviehash_match": "only",
            "query": "Zulu.Final.2026.mkv",
        },
        {"query": "Final"},
        {"query": "Unrelated.Event.2026.mkv"},
        {"imdb_id": "tt1234567"},
    ],
)
def test_strict_miss_and_fuzzy_or_id_only_do_not_choose_sports(library, query):
    _, token = key(allowed_providers=["local"])
    assert entries(search(library, token, **query)) == []


@pytest.mark.parametrize("hash_value", ["", "z" * 16, "a" * 15, "a" * 17])
def test_hash_only_admission_requires_valid_hash(library, hash_value):
    _, token = key(allowed_providers=["local"])
    assert search(library, token, moviehash=hash_value).status_code == 400


def test_native_imdb_precedence_is_unchanged(library):
    _, token = key(allowed_providers=["local"])
    response = search(library, token, imdb_id="909", query="Zulu.Final.2026.mkv")
    attrs = entries(response)[0]["attributes"]
    assert attrs["feature_details"]["imdb_id"] == 909
    response = library.client.get(link(download(library, token, file_id(response))))
    assert b"Native" in response.data


def test_disabled_default_and_stable_owner_order_refresh_cached_search(library):
    from app.database import update, TableArrInstances

    _, token = key(allowed_providers=["local"])
    initial = file_id(search(library, token, moviehash=HASH))
    library.db.execute(
        update(TableArrInstances)
        .where(TableArrInstances.id == 202)
        .values(enabled=0, is_default=0)
    )
    current = search(library, token, moviehash=HASH)
    assert file_id(current) != initial
    assert_stream(library, link(download(library, token, file_id(current))), owner=101)
    library.db.execute(
        update(TableArrInstances).where(TableArrInstances.id == 202).values(enabled=1)
    )
    assert_stream(
        library,
        link(download(library, token, file_id(search(library, token, moviehash=HASH)))),
        owner=101,
    )
    library.db.execute(
        update(TableArrInstances)
        .where(TableArrInstances.id.in_([101, 202]))
        .values(enabled=0)
    )
    assert entries(search(library, token, moviehash=HASH)) == []


def test_language_selection_is_scoped(library):
    _, token = key(allowed_providers=["local"])
    response = search(library, token, moviehash=HASH, languages="hu")
    assert entries(response)[0]["attributes"]["language"] == "hu"
    assert_stream(
        library, link(download(library, token, file_id(response))), language="hu"
    )
    assert entries(search(library, token, moviehash=HASH, languages="fr")) == []


@pytest.mark.parametrize(
    "fields",
    [
        {"excluded_providers": ["local"]},
        {"allowed_providers": ["opensubtitlescom"]},
        {"excluded_providers": ["local"], "allowed_providers": ["local"]},
    ],
)
@pytest.mark.parametrize(
    "query", [{"moviehash": HASH}, {"imdb_id": "909"}], ids=["sports", "native"]
)
def test_provider_denial_cannot_be_overridden_on_search_or_download(
    library, fields, query
):
    from compat import meter

    _, allowed = key(allowed_providers=["local"])
    fid = file_id(search(library, allowed, **query))
    kid, denied = key(**fields)
    assert entries(search(library, denied, **query, only_providers="local")) == []
    response = download(library, denied, fid)
    assert response.status_code == 404
    assert meter.window_sum(kid, "search", "hour") == 1
    assert meter.window_sum(kid, "download", "hour") == 0


def test_empty_key_allowlist_is_unrestricted_but_empty_request_selects_nothing(library):
    from compat import meter

    kid, token = key(allowed_providers=[])
    file_id(search(library, token, moviehash=HASH))
    assert entries(search(library, token, moviehash=HASH, only_providers="")) == []
    assert meter.window_sum(kid, "search", "hour") == 2


def test_cached_search_and_download_limits_use_real_usage_rows(library):
    from app.database import TableCompatUsage, select
    from compat import meter

    kid, token = key(
        allowed_providers=["local"],
        custom_limits={"search": {"hour": 2}, "download": {"hour": 1}},
    )
    fid = file_id(search(library, token, moviehash=HASH))
    assert file_id(search(library, token, moviehash=HASH)) == fid
    throttled = search(library, token, moviehash=HASH)
    assert throttled.status_code == 429
    assert throttled.headers["x-reason"] == "throttled"
    assert throttled.headers["X-RateLimit-Limit"] == "2"
    assert throttled.headers["Retry-After"]
    assert_stream(library, link(download(library, token, fid)))
    throttled = download(library, token, fid)
    assert throttled.status_code == 406
    assert throttled.headers["x-reason"] == "throttled"
    assert meter.window_sum(kid, "search", "hour") == 2
    assert meter.window_sum(kid, "download", "hour") == 1
    rows = (
        library.db.execute(
            select(TableCompatUsage).where(TableCompatUsage.key_id == kid)
        )
        .scalars()
        .all()
    )
    assert {(row.kind, row.count, row.blocked) for row in rows} == {
        ("search", 2, 1),
        ("download", 1, 1),
    }


def test_disabled_deleted_unknown_keys_and_mismatched_jwt(library):
    from compat import keyring, meter

    kid, token = key(allowed_providers=["local"])
    fid = file_id(search(library, token, moviehash=HASH))
    _, other = key(allowed_providers=["local"])
    assert download(library, other, fid, login(library, token)).status_code == 401
    for mutate in (lambda: keyring.update(kid, enabled=0), lambda: keyring.delete(kid)):
        mutate()
        assert search(library, token, moviehash=HASH).status_code == 403
    assert search(library, "unknown", moviehash=HASH).status_code == 403
    assert (
        library.client.get(
            "/api/v1/subtitles", query_string={"moviehash": HASH}
        ).status_code
        == 403
    )
    assert meter.window_sum(kid, "search", "hour") == 1
    assert meter.window_sum(kid, "download", "hour") == 0


@pytest.mark.parametrize(
    "first,second", [("sports", "sports"), ("native", "sports"), ("sports", "native")]
)
def test_old_signed_link_cannot_acquire_reused_entry(library, first, second):
    from compat import cache, file_id_store

    _, token = key(allowed_providers=["local"])
    query_a = (
        {"query": "Alpha.Final.2026.mkv"} if first == "sports" else {"imdb_id": "909"}
    )
    fid = file_id(search(library, token, **query_a))
    old_path = link(download(library, token, fid))
    cache.invalidate_all()
    file_id_store.reset_store()  # Simulate a process restart with persistent signing secrets.
    query_b = (
        {"query": "Zulu.Final.2026.mkv"} if second == "sports" else {"imdb_id": "909"}
    )
    reused = file_id(search(library, token, **query_b))
    assert reused == fid
    rejected = library.client.get(old_path)
    assert rejected.status_code in (404, 410)
    if second == "sports":
        assert_stream(library, link(download(library, token, reused)))
    else:
        current = library.client.get(link(download(library, token, reused)))
        assert current.status_code == 200 and b"Native" in current.data


@pytest.mark.parametrize(
    "change",
    [
        "disable",
        "delete",
        "owner",
        "path",
        "file_id",
        "mapping",
        "video",
        "subtitle",
        "subtitle_removed",
        "unindexed",
        "subtitle_symlink",
    ],
)
def test_signed_sports_link_revalidates_current_owner_and_file(library, change):
    from app.database import (
        update,
        delete,
        TableArrInstances,
        TableSportsEvents,
        TableHistorySports,
    )

    _, token = key(allowed_providers=["local"])
    fid = file_id(search(library, token, moviehash=HASH))
    path = link(download(library, token, fid))
    video = library.root / "202/Zulu.Final.2026.mkv"
    subtitle = library.root / "202/Zulu.Final.2026.en.srt"
    if change == "disable":
        library.db.execute(
            update(TableArrInstances)
            .where(TableArrInstances.id == 202)
            .values(enabled=0, is_default=0)
        )
    elif change == "delete":
        library.db.execute(
            delete(TableSportsEvents).where(TableSportsEvents.id == 2021)
        )
    elif change == "owner":
        library.db.execute(
            delete(TableHistorySports).where(TableHistorySports.event_id == 2021)
        )
        library.db.execute(
            update(TableSportsEvents)
            .where(TableSportsEvents.id == 2021)
            .values(arr_instance_id=101, league_id=101, sportarrEventId=52, file_id=78)
        )
    elif change in ("path", "file_id", "unindexed"):
        values = (
            {"path": "/recordings/Rebound.mkv"}
            if change == "path"
            else ({"file_id": 999} if change == "file_id" else {"subtitles": "[]"})
        )
        library.db.execute(
            update(TableSportsEvents)
            .where(TableSportsEvents.id == 2021)
            .values(**values)
        )
    elif change == "mapping":
        library.db.execute(
            update(TableArrInstances)
            .where(TableArrInstances.id == 202)
            .values(
                path_mappings=json.dumps([["/recordings", str(library.root / "101")]])
            )
        )
    elif change in ("video", "subtitle"):
        target = video if change == "video" else subtitle
        before = target.stat()
        replacement = target.with_suffix(".replacement")
        replacement.write_bytes(target.read_bytes())
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(target)
    elif change == "subtitle_removed":
        subtitle.unlink()
    elif change == "subtitle_symlink":
        subtitle.unlink()
        subtitle.symlink_to(library.root / "101/Alpha.Final.2026.en.srt")
    assert library.client.get(path).status_code in (404, 410)


def test_disabling_key_does_not_revoke_issued_sports_capability(library):
    from compat import keyring

    kid, token = key(allowed_providers=["local"])
    path = link(
        download(library, token, file_id(search(library, token, moviehash=HASH)))
    )
    keyring.update(kid, enabled=0)
    keyring.delete(kid)
    assert_stream(library, path)


def test_shared_output_owner_and_oversized_subtitle_are_not_served(library):
    from app.database import update, TableSportsEvents, TableMovies

    _, token = key(allowed_providers=["local"])
    file_id(search(library, token, query="Zulu.Final.2026.mkv"))
    # Another real media row now claims the same physical output stem.
    library.db.execute(
        update(TableMovies)
        .where(TableMovies.id == 909)
        .values(path=str(library.root / "202/Zulu.Final.2026.mp4"))
    )
    assert entries(search(library, token, query="Zulu.Final.2026.mkv")) == []
    library.db.execute(
        update(TableMovies)
        .where(TableMovies.id == 909)
        .values(path="/unavailable/native.mkv")
    )
    subtitle = library.root / "202/Zulu.Final.2026.en.srt"
    with subtitle.open("wb") as stream:
        stream.truncate(5 * 1024 * 1024 + 1)
    library.db.execute(
        update(TableSportsEvents)
        .where(TableSportsEvents.id == 2021)
        .values(subtitles=repr([["en", "/recordings/" + subtitle.name]]))
    )
    assert entries(search(library, token, query="Zulu.Final.2026.mkv")) == []


def test_replaced_video_cannot_reuse_stale_hash(library):
    _, token = key(allowed_providers=["local"])
    file_id(
        search(
            library,
            token,
            moviehash=HASH,
            moviehash_match="only",
            query="Zulu.Final.2026.mkv",
        )
    )
    for owner, name in ((101, "Alpha"), (202, "Zulu")):
        video = library.root / f"{owner}/{name}.Final.2026.mkv"
        before = video.stat()
        replacement = video.with_suffix(".replacement")
        replacement.write_bytes(b"\x01" + b"\0" * 131071)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(video)
    assert entries(search(library, token, moviehash=HASH, moviehash_match="only")) == []
    response = search(library, token, moviehash=OTHER_HASH, moviehash_match="only")
    assert entries(response)[0]["attributes"]["moviehash_match"] is True
    assert_stream(library, link(download(library, token, file_id(response))))


def test_equal_hashes_within_owner_use_stable_local_event_id(library):
    from app.database import insert, TableSportsEvents

    _, token = key(allowed_providers=["local"])
    (library.root / "202/Earlier.Final.2026.mkv").write_bytes(b"\0" * 131072)
    content = b"1\n00:00:00,000 --> 00:00:01,000\nEarlier local event\n"
    (library.root / "202/Earlier.Final.2026.en.srt").write_bytes(content)
    library.db.execute(
        insert(TableSportsEvents).values(
            id=2020,
            arr_instance_id=202,
            league_id=202,
            sportarrLeagueId=31,
            sportarrEventId=52,
            file_id=78,
            path="/recordings/Earlier.Final.2026.mkv",
            title="Earlier local event",
            subtitles=repr([["en", "/recordings/Earlier.Final.2026.en.srt"]]),
        )
    )
    response = search(library, token, moviehash=HASH)
    assert (
        entries(response)[0]["attributes"]["feature_details"]["title"]
        == "Earlier local event"
    )
    stream = library.client.get(link(download(library, token, file_id(response))))
    assert stream.status_code == 200 and stream.data == content


def test_read_only_lookup_does_not_create_configured_subtitle_directory(
    library, monkeypatch
):
    from app.config import settings

    destination = library.root / "not-created-by-search"
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(destination))
    _, token = key(allowed_providers=["local"])
    assert_stream(
        library,
        link(download(library, token, file_id(search(library, token, moviehash=HASH)))),
    )
    assert not destination.exists()


def test_sports_signed_binding_contains_identity_but_no_disk_paths(library):
    from compat import auth

    _, token = key(allowed_providers=["local"])
    fid = file_id(search(library, token, moviehash=HASH))
    path = link(download(library, token, fid))
    valid, payload = auth.parse_file_stream_token(path.rsplit("/", 1)[-1])
    assert valid
    assert payload["sports"]["event_id"] == 2021
    assert payload["sports"]["arr_instance_id"] == 202
    assert len(payload["sports"]["file"]) == 64
    assert "/recordings" not in json.dumps(payload)
    assert str(library.root) not in json.dumps(payload)
    assert_stream(library, path)


def test_native_signed_payload_bytes_remain_unchanged(monkeypatch):
    import base64
    from compat import auth

    monkeypatch.setattr(auth.time, "time", lambda: 2000000000)
    token = auth.mint_file_stream_token(42)
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert raw[:-33] == b'{"exp":2000000300,"fid":42,"t":"s"}'


def test_hash_match_cannot_survive_replacement_before_context_resolution(
    library, monkeypatch
):
    from compat import local_subs, sports

    hash_file = sports._hash_file

    def replace_after_hash(path, stamp):
        value = hash_file(path, stamp)
        with open(path, "r+b") as stream:
            stream.write(b"\x01")
        return value

    monkeypatch.setattr(sports, "_hash_file", replace_after_hash)
    result = local_subs.search_local(
        None, None, None, "movie", ["en"], moviehash=HASH, moviehash_match="only"
    )
    assert result == []


@pytest.mark.parametrize("failure", ["unreadable", "stat-race", "hash-change"])
def test_unavailable_neighbor_does_not_hide_healthy_sports_match(
    library, monkeypatch, failure
):
    import builtins
    from compat import sports, meter

    sports._hash_file.cache_clear()
    neighbor = library.root / "101/Alpha.Final.2026.mkv"
    if failure == "unreadable":
        original = builtins.open

        def unavailable(path, *args, **kwargs):
            if str(path) == str(neighbor):
                raise PermissionError("Fixture neighbor is not readable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(sports.local, "open", unavailable, raising=False)
    elif failure == "stat-race":
        original = os.stat
        calls = 0

        def unavailable(path, *args, **kwargs):
            nonlocal calls
            if str(path) == str(neighbor):
                calls += 1
                if calls % 2 == 0:
                    raise OSError("Fixture neighbor disappeared after isfile")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(sports.os, "stat", unavailable)
    else:
        original = sports._hash_file

        def unavailable(path, stamp):
            value = original(path, stamp)
            if str(path) == str(neighbor):
                raise ValueError("Fixture neighbor changed during hashing")
            return value

        monkeypatch.setattr(sports, "_hash_file", unavailable)

    kid, token = key(allowed_providers=["local"])
    query = dict(moviehash=HASH, query="Zulu.Final.2026.mkv", moviehash_match="only")
    response = search(library, token, **query)
    fid = file_id(response)
    assert entries(response)[0]["attributes"]["moviehash_match"] is True
    assert_stream(library, link(download(library, token, fid)))
    assert file_id(search(library, token, **query)) == fid
    assert meter.window_sum(kid, "search", "hour") == 2
    assert meter.window_sum(kid, "download", "hour") == 1


@pytest.mark.parametrize("warm", [False, True], ids=["cold-creator", "warm-hit"])
def test_owner_change_during_cache_lookup_cannot_poison_restored_default(
    library, monkeypatch, warm
):
    from app.database import update, TableArrInstances
    from compat import sports, meter

    kid, token = key(allowed_providers=["local"])
    if warm:
        file_id(search(library, token, moviehash=HASH))
    original = sports.resolve_for_request
    changed = False

    def disable_selected_owner(*args, **kwargs):
        nonlocal changed
        match = original(*args, **kwargs)
        if not changed:
            changed = True
            assert match.context.arr_instance_id == 202
            library.db.execute(
                update(TableArrInstances)
                .where(TableArrInstances.id == 202)
                .values(enabled=0, is_default=0)
            )
        return match

    monkeypatch.setattr(sports, "resolve_for_request", disable_selected_owner)
    response = search(library, token, moviehash=HASH)
    assert entries(response)[0]["attributes"]["feature_details"]["title"] == "Final 101"
    assert_stream(library, link(download(library, token, file_id(response))), owner=101)
    library.db.execute(
        update(TableArrInstances)
        .where(TableArrInstances.id == 202)
        .values(enabled=1, is_default=1)
    )
    restored = search(library, token, moviehash=HASH)
    assert entries(restored)[0]["attributes"]["feature_details"]["title"] == "Final 202"
    assert_stream(library, link(download(library, token, file_id(restored))))
    assert meter.window_sum(kid, "search", "hour") == 2 + int(warm)
    assert meter.window_sum(kid, "download", "hour") == 2


def test_sports_appearing_during_creation_cannot_poison_non_sports_cache(
    library, monkeypatch
):
    from app.database import update, TableArrInstances
    from compat import sports, meter

    library.db.execute(
        update(TableArrInstances)
        .where(TableArrInstances.id.in_([101, 202]))
        .values(enabled=0, is_default=0)
    )
    original = sports.resolve_for_request
    changed = False

    def enable_owner(*args, **kwargs):
        nonlocal changed
        match = original(*args, **kwargs)
        if not changed:
            changed = True
            assert match is None
            library.db.execute(
                update(TableArrInstances)
                .where(TableArrInstances.id == 202)
                .values(enabled=1, is_default=1)
            )
        return match

    monkeypatch.setattr(sports, "resolve_for_request", enable_owner)
    kid, token = key(allowed_providers=["local"])
    found = search(library, token, moviehash=HASH)
    assert_stream(library, link(download(library, token, file_id(found))))
    library.db.execute(
        update(TableArrInstances)
        .where(TableArrInstances.id == 202)
        .values(enabled=0, is_default=0)
    )
    assert entries(search(library, token, moviehash=HASH)) == []
    assert meter.window_sum(kid, "search", "hour") == 2


def test_continuously_changing_sports_selection_stops_without_caching(
    library, monkeypatch
):
    from app.database import update, TableArrInstances
    from compat import sports, meter

    original = sports.resolve_for_request
    observations = []

    def change_default(*args, **kwargs):
        match = original(*args, **kwargs)
        observations.append(match.context.arr_instance_id)
        next_owner = 101 if match.context.arr_instance_id == 202 else 202
        library.db.execute(
            update(TableArrInstances)
            .where(TableArrInstances.id.in_([101, 202]))
            .values(is_default=0)
        )
        library.db.execute(
            update(TableArrInstances)
            .where(TableArrInstances.id == next_owner)
            .values(is_default=1)
        )
        return match

    monkeypatch.setattr(sports, "resolve_for_request", change_default)
    kid, token = key(allowed_providers=["local"])
    response = search(library, token, moviehash=HASH)
    assert response.status_code == 503
    assert len(observations) <= 6
    assert meter.window_sum(kid, "search", "hour") == 1
    monkeypatch.setattr(sports, "resolve_for_request", original)
    restored = search(library, token, moviehash=HASH)
    fid = file_id(restored)
    assert_stream(
        library,
        link(download(library, token, fid)),
        owner=101 if observations[-1] == 202 else 202,
    )
    assert meter.window_sum(kid, "search", "hour") == 2


def test_selection_change_after_fanout_does_not_spend_provider_budget_twice(
    library, monkeypatch
):
    from app.database import update, TableArrInstances
    from compat import service, meter

    original = service.list_all_subtitles_parallel
    fanouts = 0

    def change_after_fanout(*args, **kwargs):
        nonlocal fanouts
        result = original(*args, **kwargs)
        fanouts += 1
        if fanouts == 1:
            library.db.execute(
                update(TableArrInstances)
                .where(TableArrInstances.id == 202)
                .values(enabled=0, is_default=0)
            )
        return result

    monkeypatch.setattr(service, "list_all_subtitles_parallel", change_after_fanout)
    kid, token = key(allowed_providers=["local"])
    response = search(library, token, moviehash=HASH)
    assert response.status_code == 503
    assert fanouts == 1
    assert meter.window_sum(kid, "search", "hour") == 1
    assert meter.window_sum(kid, "download", "hour") == 0
    library.db.execute(
        update(TableArrInstances)
        .where(TableArrInstances.id == 202)
        .values(enabled=1, is_default=1)
    )
    restored = search(library, token, moviehash=HASH)
    assert_stream(library, link(download(library, token, file_id(restored))))
    assert fanouts == 2
    assert meter.window_sum(kid, "search", "hour") == 2


@pytest.mark.parametrize("operation", ["isfile", "realpath", "stat"])
def test_filename_only_ignores_unrelated_storage_before_any_io(
    library, monkeypatch, operation
):
    from compat import sports

    original = getattr(sports.os.path if operation != "stat" else sports.os, operation)
    target = sports.os.path if operation != "stat" else sports.os

    def guarded(path, *args, **kwargs):
        if "Alpha.Final" in str(path):
            pytest.fail("Filename-only search touched an unrelated sports recording")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(target, operation, guarded)
    _, token = key(allowed_providers=["local"])
    for _ in range(2):
        fid = file_id(search(library, token, query="Zulu.Final.2026.mkv"))
        assert_stream(library, link(download(library, token, fid)))


@pytest.mark.parametrize("policy", ["relative", "absolute"])
@pytest.mark.parametrize("location", ["video", "custom"])
def test_indexed_discovery_roots_search_link_and_stream(
    library, monkeypatch, policy, location
):
    from app.config import settings
    from app.database import TableSportsEvents, update

    video = library.root / "202" / "Zulu.Final.2026.en.srt"
    custom = (
        library.root / "202" / "subs"
        if policy == "relative"
        else library.root / "separate-output"
    )
    monkeypatch.setattr(settings.general, "subfolder", policy)
    monkeypatch.setattr(
        settings.general,
        "subfolder_custom",
        "subs" if policy == "relative" else str(custom),
    )
    if location == "custom":
        custom.mkdir()
        selected = custom / video.name
        video.rename(selected)
        library.db.execute(
            update(TableSportsEvents)
            .where(TableSportsEvents.id == 2021)
            .values(subtitles=repr([["en", str(selected)]]))
        )
    _, token = key(allowed_providers=["local"])
    fid = file_id(search(library, token, query="Zulu.Final.2026.mkv"))
    assert_stream(library, link(download(library, token, fid)))
    if location == "video":
        assert not custom.exists()


def test_hub_with_many_unrelated_windows_subtitle_records(library):
    from app.database import TableMovies, insert

    for index in range(140):
        library.db.execute(
            insert(TableMovies).values(
                id=1000 + index,
                radarrId=1000 + index,
                tmdbId=str(index),
                title="Unrelated",
                path=f"C:\\Movies\\movie-{index}.mkv",
                subtitles=repr([["en", f"C:\\Movies\\movie-{index}.en.srt", 10]]),
            )
        )
    _, token = key(allowed_providers=["local"])
    fid = file_id(search(library, token, query="Zulu.Final.2026.mkv"))
    assert_stream(library, link(download(library, token, fid)))
