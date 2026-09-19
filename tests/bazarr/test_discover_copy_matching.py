"""Explicit, ownership-safe local-copy matching for Discover.

Every case here runs the real authenticated API, the real provider pool and the
real database on both supported engines. A copy is only ever adopted because a
reader chose it: nothing in these tests lets a title, an IMDb id or a single
library row select a file on its own.
"""
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import test_discover_search as search_fixtures

authenticated_client = search_fixtures.authenticated_client
providers = search_fixtures.providers
post = search_fixtures.post

MOVIE = {"media_type": "movie", "imdb_id": "tt0133093", "title": "The Matrix",
         "year": 1999, "language": "eng"}
EPISODE = {"media_type": "episode", "imdb_id": "tt0903747", "title": "Breaking Bad",
           "year": 2008, "season": 2, "episode": 1, "language": "eng",
           "manual_confirmed": True}
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nChosen copy dialogue\n\n"


@pytest.fixture(params=["sqlite", "postgresql"])
def copy_database(request, monkeypatch):
    """A disposable library on both engines, with the ORM session and the Core
    engine pointed at the same throwaway data."""
    from app import database as db

    cleanup = None
    schema = "discover_" + uuid.uuid4().hex
    if request.param == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.fail("BAZARR_PG_TEST_URL is required for the PostgreSQL acceptance lane")
        cleanup = sa.create_engine(url)
        with cleanup.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        engine = sa.create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    try:
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            monkeypatch.setattr(db, "engine", engine)
            monkeypatch.setattr(db, "database", session)
            yield engine, session
    finally:
        engine.dispose()
        if cleanup is not None:
            with cleanup.begin() as connection:
                connection.execute(sa.schema.DropSchema(schema, cascade=True))
            cleanup.dispose()


@pytest.fixture
def hashing(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings.general, "skip_hashing", False)
    monkeypatch.setattr(settings.general, "path_mappings", [])
    monkeypatch.setattr(settings.general, "path_mappings_movie", [])
    from utilities.path_mappings import path_mappings
    path_mappings.update()
    return settings


def media_file(directory, name, marker, size=11 * 1024 * 1024):
    """A sparse file large enough for the established hashing threshold."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with open(path, "wb") as handle:
        handle.write(marker)
        handle.truncate(size)
    return path


def replace_content(path, marker):
    """Replace bytes in place while holding size and timestamps constant.

    This is what an archive restore, a preserving copy and several remux tools
    produce, and it is the case metadata alone cannot see. A test that lets the
    modification time move proves nothing about the content hash.
    """
    before = os.stat(path)
    with open(path, "r+b") as handle:
        handle.write(marker)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(path)
    assert after.st_size == before.st_size, "the replacement must not change size"
    assert after.st_mtime_ns == before.st_mtime_ns, "the replacement must not change mtime"
    return before


def opensubtitles_hash(path):
    """The upstream digest without subliminal's per-path process memo."""
    from subliminal_patch.hashes import hash_opensubtitles
    return getattr(hash_opensubtitles, "__wrapped__", hash_opensubtitles)(path)


def add_instance(session, instance_id, name, kind="radarr", mappings=None):
    from app.database import TableArrInstances
    session.add(TableArrInstances(id=instance_id, kind=kind, stable_key=f"key-{instance_id}",
                                  name=name, enabled=1, is_default=1 if instance_id == 1 else 0,
                                  ip="127.0.0.1", port=7878 + instance_id, base_url="/",
                                  api_key="unused", path_mappings=mappings))


def add_movie(session, local_id, owner, path, *, imdb="tt0133093", scene=None, title="The Matrix",
              file_id=None, fmt="Web", resolution="1080p", video_codec="H.264", size=None):
    from app.database import TableMovies
    session.add(TableMovies(id=local_id, arr_instance_id=owner, radarrId=local_id, imdbId=imdb,
                            tmdbId=str(600 + local_id), title=title, year="1999", path=str(path),
                            sceneName=scene, format=fmt, resolution=resolution,
                            video_codec=video_codec, audio_codec="DTS",
                            movie_file_id=file_id if file_id is not None else local_id,
                            file_size=size))


def add_episode(session, local_id, owner, series_local_id, path, *, season=2, episode=1,
                imdb="tt0903747", scene=None, series_title="Breaking Bad"):
    from app.database import TableEpisodes, TableShows
    if session.get(TableShows, series_local_id) is None:
        session.add(TableShows(id=series_local_id, arr_instance_id=owner, sonarrSeriesId=series_local_id,
                               imdbId=imdb, tvdbId=81189, title=series_title,
                               path=str(os.path.dirname(str(path)))))
        session.flush()
    session.add(TableEpisodes(id=local_id, series_id=series_local_id, arr_instance_id=owner,
                              sonarrEpisodeId=local_id, sonarrSeriesId=series_local_id,
                              season=season, episode=episode, title="Seven Thirty-Seven",
                              path=str(path), sceneName=scene, format="Bluray",
                              resolution="720p", video_codec="H.264", audio_codec="AC3",
                              episode_file_id=local_id))


def copies(client, **params):
    return client.get("/api/discover/copies", query_string=params,
                      headers={"X-API-KEY": "discover-test-key"})


def catalog(name, results):
    from provider_hub.protocol import candidate_from_worker
    return [candidate_from_worker(name, {
        "id": key, "language": {"alpha3": "eng"}, "release_info": release,
        "provider_payload": {},
    }) for key, release in results]


@pytest.fixture
def catalog_provider(providers, monkeypatch):
    from subliminal_patch.extensions import provider_registry
    fetched = []
    candidates = catalog("discover_copy", [
        ("web", "The.Matrix.1999.1080p.WEB.H264-GRP"),
        ("bluray", "The.Matrix.1999.2160p.BluRay.x265-OTHER"),
    ])
    name = providers.add("discover_copy", candidates)

    def download(self, subtitle):
        fetched.append(subtitle.worker_id)
        subtitle.content = SRT

    monkeypatch.setattr(provider_registry[name], "download_subtitle", download)
    return candidates, fetched


def searched(providers):
    return [getattr(video, "name", None) for _, video, _ in providers.videos]


# --- offering exact copies -------------------------------------------------


def test_offers_exact_movie_copies_with_owner_identity_and_release(
    authenticated_client, copy_database, hashing, tmp_path,
):
    engine, session = copy_database
    add_instance(session, 1, "Radarr HD")
    add_instance(session, 2, "Radarr 4K")
    add_movie(session, 5, 1, media_file(tmp_path / "hd", "matrix.hd.mkv", b"HD"),
              scene="The.Matrix.1999.1080p.WEB.H264-GRP")
    add_movie(session, 8, 2, media_file(tmp_path / "uhd", "matrix.uhd.mkv", b"UHD"),
              scene="The.Matrix.1999.2160p.BluRay.x265-OTHER", resolution="2160p")
    session.commit()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        response = copies(authenticated_client, media_type="movie", imdb_id="tt0133093")
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    items = response.json["items"]
    assert [item["local_id"] for item in items] == [5, 8]
    assert [item["arr_instance_id"] for item in items] == [1, 2]
    assert [item["instance_name"] for item in items] == ["Radarr HD", "Radarr 4K"]
    assert [item["release"] for item in items] == [
        "The.Matrix.1999.1080p.WEB.H264-GRP", "The.Matrix.1999.2160p.BluRay.x265-OTHER"]
    assert [item["resolution"] for item in items] == ["1080p", "2160p"]
    assert len({item["copy_id"] for item in items}) == 2
    assert all(item["selectable"] for item in items)
    assert response.json["truncated"] is False
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_show_ownership_never_supplies_an_absent_episode(
    authenticated_client, copy_database, hashing, tmp_path,
):
    _, session = copy_database
    add_instance(session, 1, "Sonarr", kind="sonarr")
    add_episode(session, 3, 1, 30, media_file(tmp_path / "bb", "s02e01.mkv", b"E1"))
    session.commit()
    present = copies(authenticated_client, media_type="episode", imdb_id="tt0903747",
                     season="2", episode="1")
    assert [item["local_id"] for item in present.json["items"]] == [3]
    assert present.json["items"][0]["series_local_id"] == 30
    absent = copies(authenticated_client, media_type="episode", imdb_id="tt0903747",
                    season="9", episode="4")
    assert absent.status_code == 200
    assert absent.json["items"] == []
    # The show is owned; the episode is not. That difference has to be visible.
    assert absent.json["owning_titles"] == 1


def test_copies_require_authorization_and_an_exact_target(authenticated_client, copy_database, hashing):
    assert authenticated_client.get("/api/discover/copies").status_code == 401
    for params in [{}, {"media_type": "movie"}, {"media_type": "movie", "imdb_id": "matrix"},
                   {"media_type": "episode", "imdb_id": "tt0903747"},
                   {"media_type": "episode", "imdb_id": "tt0903747", "season": "2"},
                   {"media_type": "release", "imdb_id": "tt0903747"}]:
        assert copies(authenticated_client, **params).status_code == 400


# --- default is title only -------------------------------------------------


def test_a_single_existing_copy_is_never_adopted_automatically(
    authenticated_client, providers, copy_database, hashing, tmp_path,
):
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    add_movie(session, 5, 1, media_file(tmp_path / "hd", "matrix.hd.mkv", b"HD"),
              scene="The.Matrix.1999.1080p.WEB.H264-GRP")
    session.commit()
    providers.add("discover_copy", catalog("discover_copy", [("web", "The.Matrix.1999.1080p")]))
    response = post(authenticated_client, MOVIE)
    assert response.status_code == 200
    assert "copy_id" not in response.json["context"]
    assert "file_revision" not in response.json["context"]
    video = providers.videos[0][1]
    assert not getattr(video, "name", "")
    assert getattr(video, "size", None) is None
    assert not getattr(video, "hashes", {})
    assert getattr(video, "resolution", None) is None
    assert response.json["results"][0]["copy_compatibility"] is None


# --- explicit selection isolates files, caches and results -----------------


def test_each_chosen_copy_supplies_only_its_own_file_facts(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    add_instance(session, 2, "Radarr 4K")
    file_a = media_file(tmp_path / "hd", "The.Matrix.1999.1080p.WEB.H264-GRP.mkv", b"HD")
    file_b = media_file(tmp_path / "uhd", "The.Matrix.1999.2160p.BluRay.x265-OTHER.mkv", b"UHD")
    add_movie(session, 5, 1, file_a, scene="The.Matrix.1999.1080p.WEB.H264-GRP")
    add_movie(session, 8, 2, file_b, scene="The.Matrix.1999.2160p.BluRay.x265-OTHER",
              resolution="2160p", fmt="Bluray")
    session.commit()
    offered = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"]
    copy_a, copy_b = offered[0]["copy_id"], offered[1]["copy_id"]

    first = post(authenticated_client, {**MOVIE, "copy_id": copy_a}).json
    second = post(authenticated_client, {**MOVIE, "copy_id": copy_b}).json
    assert first["context"]["copy_id"] != second["context"]["copy_id"]
    assert first["context"]["file_revision"] != second["context"]["file_revision"]
    assert searched(providers) == [str(file_a), str(file_b)]
    assert first["search_id"] != second["search_id"]
    assert first["context"]["copy"]["instance_name"] == "Radarr HD"
    assert second["context"]["copy"]["resolution"] == "2160p"
    # The chosen copy supplies release facts; identity stays the confirmed target.
    video_a, video_b = providers.videos[0][1], providers.videos[1][1]
    assert (video_a.resolution, video_b.resolution) == ("1080p", "2160p")
    assert video_a.imdb_id == video_b.imdb_id == "tt0133093"
    assert video_a.hashes and video_a.hashes != video_b.hashes
    assert video_a.size == os.path.getsize(file_a)
    # No private path ever reaches the client.
    assert str(tmp_path) not in json.dumps(first) + json.dumps(second)


def test_results_separate_known_matches_conflicts_and_unknown(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    add_movie(session, 5, 1, media_file(tmp_path / "hd", "The.Matrix.1999.1080p.WEB.H264-GRP.mkv", b"HD"),
              scene="The.Matrix.1999.1080p.WEB.H264-GRP")
    session.commit()
    copy_id = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]["copy_id"]
    snapshot = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    rows = {row["release"]: row["copy_compatibility"] for row in snapshot["results"]}
    same = rows["The.Matrix.1999.1080p.WEB.H264-GRP"]
    other = rows["The.Matrix.1999.2160p.BluRay.x265-OTHER"]
    assert same["resolution"] == "match" and same["release_group"] == "match"
    assert other["resolution"] == "conflict" and other["source"] == "conflict"
    assert same["audio_codec"] == "unknown" and other["audio_codec"] == "unknown"


def test_download_names_its_own_release_provider_and_scope(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    add_movie(session, 5, 1, media_file(tmp_path / "hd", "matrix.mkv", b"HD"),
              scene="The.Matrix.1999.1080p.WEB.H264-GRP")
    session.commit()
    copy_id = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]["copy_id"]
    snapshot = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    chosen = snapshot["results"][1]
    response = authenticated_client.get("/api/discover/download", query_string={
        "result_id": chosen["id"], "search_id": chosen["search_id"]},
        headers={"X-API-KEY": "discover-test-key"})
    assert response.status_code == 200
    assert response.data == SRT
    disposition = response.headers["Content-Disposition"]
    assert "The.Matrix.1999.2160p.BluRay.x265-OTHER" in disposition
    assert "discover_copy" in disposition
    assert disposition.endswith('.en.srt')
    assert catalog_provider[1] == ["bluray"]


# --- ownership, collisions and recovery ------------------------------------


def test_upstream_id_collisions_never_switch_to_another_instance_file(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    add_instance(session, 2, "Radarr 4K")
    file_a = media_file(tmp_path / "hd", "matrix.hd.mkv", b"HD")
    file_b = media_file(tmp_path / "uhd", "matrix.uhd.mkv", b"UHD")
    # Same upstream Radarr id and same IMDb id in both instances.
    add_movie(session, 5, 1, file_a, file_id=99)
    add_movie(session, 8, 2, file_b, file_id=99)
    session.execute(sa.update(sa.table("table_movies", sa.column("id"), sa.column("radarrId")))
                    .where(sa.column("id") == 8).values(radarrId=5))
    session.commit()
    offered = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"]
    chosen = next(item for item in offered if item["arr_instance_id"] == 2)
    post(authenticated_client, {**MOVIE, "copy_id": chosen["copy_id"]})
    assert searched(providers) == [str(file_b)]
    # A copy id naming the other instance for the same local row must not resolve.
    forged = chosen["copy_id"].rsplit(".", 1)[0] + ".1"
    rejected = post(authenticated_client, {**MOVIE, "copy_id": forged})
    assert rejected.status_code == 409
    assert rejected.json["reason"] == "copy_unavailable"
    assert searched(providers) == [str(file_b)]


def test_reused_local_id_for_a_different_title_requires_recovery(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    from app.database import TableMovies
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    add_movie(session, 5, 1, media_file(tmp_path / "hd", "matrix.mkv", b"HD"))
    session.commit()
    copy_id = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]["copy_id"]
    assert post(authenticated_client, {**MOVIE, "copy_id": copy_id}).status_code == 200
    # SQLite reuses a deleted local id. The same number is now another film.
    session.query(TableMovies).filter_by(id=5).delete()
    session.commit()
    add_movie(session, 5, 1, media_file(tmp_path / "other", "other.mkv", b"OT"),
              imdb="tt1375666", title="Inception")
    session.commit()
    stale = post(authenticated_client, {**MOVIE, "copy_id": copy_id})
    assert stale.status_code == 409
    assert stale.json["reason"] == "copy_unavailable"
    assert searched(providers) == [str(tmp_path / "hd" / "matrix.mkv")]


def test_deleted_or_moved_copy_recovers_instead_of_substituting(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    add_instance(session, 2, "Radarr 4K")
    file_a = media_file(tmp_path / "hd", "matrix.hd.mkv", b"HD")
    media_file(tmp_path / "uhd", "matrix.uhd.mkv", b"UHD")
    add_movie(session, 5, 1, file_a)
    add_movie(session, 8, 2, tmp_path / "uhd" / "matrix.uhd.mkv")
    session.commit()
    copy_id = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]["copy_id"]
    os.remove(file_a)
    response = post(authenticated_client, {**MOVIE, "copy_id": copy_id})
    assert response.status_code == 409
    assert response.json["reason"] == "copy_unavailable"
    assert response.json["recoverable"] is True
    # Never quietly search the other instance's file instead.
    assert providers.videos == []


def test_changed_instance_path_mapping_invalidates_the_selection(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path, monkeypatch,
):
    from app.database import TableArrInstances
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    remote = "/remote/movies/matrix.mkv"
    local = media_file(tmp_path / "hd", "matrix.mkv", b"HD")
    add_movie(session, 5, 1, remote)
    session.commit()
    session.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 1)
                    .values(path_mappings=json.dumps([["/remote/movies", str(tmp_path / "hd")]])))
    session.commit()
    copy_id = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]["copy_id"]
    first = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    assert searched(providers) == [str(local)]
    moved = media_file(tmp_path / "moved", "matrix.mkv", b"MV")
    session.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 1)
                    .values(path_mappings=json.dumps([["/remote/movies", str(tmp_path / "moved")]])))
    session.commit()
    second = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    assert second["context"]["file_revision"] != first["context"]["file_revision"]
    assert searched(providers) == [str(local), str(moved)]


def test_different_editions_of_one_title_stay_separate_copies(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    theatrical = media_file(tmp_path / "a", "The.Matrix.1999.Theatrical.Cut.1080p.mkv", b"TH")
    extended = media_file(tmp_path / "b", "The.Matrix.1999.Extended.Cut.1080p.mkv", b"EX")
    add_movie(session, 5, 1, theatrical, scene="The.Matrix.1999.Theatrical.Cut.1080p.WEB-GRP")
    add_movie(session, 6, 1, extended, scene="The.Matrix.1999.Extended.Cut.1080p.WEB-GRP")
    session.commit()
    offered = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"]
    assert len(offered) == 2
    assert offered[0]["copy_id"] != offered[1]["copy_id"]
    first = post(authenticated_client, {**MOVIE, "copy_id": offered[0]["copy_id"]}).json
    second = post(authenticated_client, {**MOVIE, "copy_id": offered[1]["copy_id"]}).json
    assert first["context"]["file_revision"] != second["context"]["file_revision"]
    assert providers.videos[0][1].edition != providers.videos[1][1].edition


def test_a_changed_file_retires_the_earlier_result_handles(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    """Size and timestamps are held constant, so only the content hash can see this."""
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    path = media_file(tmp_path / "hd", "matrix.mkv", b"HD")
    add_movie(session, 5, 1, path)
    session.commit()
    copy_id = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]["copy_id"]
    first = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    row = first["results"][0]

    def download(chosen):
        return authenticated_client.get("/api/discover/download", query_string={
            "result_id": chosen["id"], "search_id": chosen["search_id"]},
            headers={"X-API-KEY": "discover-test-key"})

    assert download(row).status_code == 200
    assert providers.videos[0][1].hashes["opensubtitles"] == opensubtitles_hash(path)
    replace_content(path, b"REPLACED")
    expired = download(row)
    assert expired.status_code == 410
    assert expired.json["reason"] == "result_expired"
    second = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    assert second["context"]["file_revision"] != first["context"]["file_revision"]
    assert second["results"][0]["id"] != row["id"]
    # The hash sent to hash-capable providers is the file the reader has now,
    # not the one a process-lifetime memo remembers.
    current = opensubtitles_hash(path)
    assert providers.videos[1][1].hashes["opensubtitles"] == current
    assert providers.videos[0][1].hashes["opensubtitles"] != current


def test_metadata_alone_cannot_see_a_preserving_replacement(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path, monkeypatch,
):
    """Isolate what the hash contributes, by removing it.

    With hashing disabled the same replacement is invisible, which is the honest
    limit of the fallback and the proof that the case above is carried by the
    content hash rather than by an incidental timestamp change.
    """
    from app.config import settings
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    path = media_file(tmp_path / "hd", "matrix.mkv", b"HD")
    add_movie(session, 5, 1, path)
    session.commit()
    monkeypatch.setattr(settings.general, "skip_hashing", True)
    copy_id = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]["copy_id"]
    before = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    assert not providers.videos[0][1].hashes
    replace_content(path, b"REPLACED")
    after = post(authenticated_client, {**MOVIE, "copy_id": copy_id, "refresh": True}).json
    assert after["context"]["file_revision"] == before["context"]["file_revision"]
    # Turn hashing back on, take a fresh baseline with it, and replace again.
    # Now the only thing that can move the revision is the content hash.
    monkeypatch.setattr(settings.general, "skip_hashing", False)
    hashed = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    replace_content(path, b"AGAIN!!!")
    detected = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    assert detected["context"]["file_revision"] != hashed["context"]["file_revision"]


def test_the_content_hash_is_not_a_per_path_process_constant(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    """subliminal memoizes its hash on the path alone for the life of the process.

    Reading through that memo would pay for 128 KiB and observe nothing, so this
    pins the observation to the file rather than to the path.
    """
    from discover.library import _copy_hash
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    path = media_file(tmp_path / "hd", "matrix.mkv", b"HD")
    add_movie(session, 5, 1, path)
    session.commit()
    first = _copy_hash(str(path), os.path.getsize(path))
    assert first == opensubtitles_hash(path)
    replace_content(path, b"REPLACED")
    second = _copy_hash(str(path), os.path.getsize(path))
    assert second != first
    assert second == opensubtitles_hash(path)
    # The shared memo is left as it was for every other caller.
    from subliminal_patch.hashes import hash_opensubtitles
    assert hasattr(hash_opensubtitles, "cache_clear")


def test_episode_copies_resolve_within_their_own_owner(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    _, session = copy_database
    add_instance(session, 1, "Sonarr HD", kind="sonarr")
    add_instance(session, 2, "Sonarr 4K", kind="sonarr")
    file_a = media_file(tmp_path / "hd", "Breaking.Bad.S02E01.720p.mkv", b"HD")
    file_b = media_file(tmp_path / "uhd", "Breaking.Bad.S02E01.2160p.mkv", b"UHD")
    add_episode(session, 3, 1, 30, file_a, scene="Breaking.Bad.S02E01.720p.BluRay-GRP")
    add_episode(session, 4, 2, 40, file_b, scene="Breaking.Bad.S02E01.2160p.WEB-OTHER")
    session.commit()
    offered = copies(authenticated_client, media_type="episode", imdb_id="tt0903747",
                     season="2", episode="1").json["items"]
    assert [(item["local_id"], item["arr_instance_id"], item["series_local_id"])
            for item in offered] == [(3, 1, 30), (4, 2, 40)]
    snapshot = post(authenticated_client, {**EPISODE, "copy_id": offered[1]["copy_id"]}).json
    assert searched(providers) == [str(file_b)]
    video = providers.videos[0][1]
    assert (video.season, video.episode) == (2, 1)
    assert video.series_imdb_id == "tt0903747"
    assert snapshot["context"]["copy"]["series_local_id"] == 40


def test_copy_matching_never_writes_rows_or_settings(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path,
):
    from app import database as db
    from app.config import settings
    engine, session = copy_database
    add_instance(session, 1, "Radarr HD")
    path = media_file(tmp_path / "hd", "matrix.mkv", b"HD")
    add_movie(session, 5, 1, path)
    session.commit()
    before = {table.name: session.execute(sa.select(table)).all()
              for table in db.Base.metadata.sorted_tables}
    settings_before = json.dumps(settings.as_dict(), sort_keys=True, default=str)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        copy_id = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]["copy_id"]
        row = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json["results"][0]
        authenticated_client.get("/api/discover/download", query_string={
            "result_id": row["id"], "search_id": row["search_id"]},
            headers={"X-API-KEY": "discover-test-key"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements, "the copy path must actually reach the database"
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    # Bounded: one offer, plus copy resolution per search and per delivery.
    assert len(statements) <= 24
    assert {table.name: session.execute(sa.select(table)).all()
            for table in db.Base.metadata.sorted_tables} == before
    assert json.dumps(settings.as_dict(), sort_keys=True, default=str) == settings_before
    assert os.path.getsize(path) == 11 * 1024 * 1024


def test_unowned_rows_are_disclosed_but_never_selectable(
    authenticated_client, providers, copy_database, hashing, tmp_path,
):
    _, session = copy_database
    add_movie(session, 5, None, media_file(tmp_path / "legacy", "matrix.mkv", b"LG"))
    session.commit()
    item = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]
    assert item["arr_instance_id"] is None
    assert item["selectable"] is False
    assert item["unavailable_reason"] == "owner_unknown"
    rejected = post(authenticated_client, {**MOVIE, "copy_id": item["copy_id"]})
    assert rejected.status_code == 409
    assert providers.videos == []


def test_skipped_hashing_still_yields_a_physical_revision(
    authenticated_client, providers, copy_database, hashing, catalog_provider, tmp_path, monkeypatch,
):
    from app.config import settings
    _, session = copy_database
    add_instance(session, 1, "Radarr HD")
    path = media_file(tmp_path / "hd", "matrix.mkv", b"HD")
    add_movie(session, 5, 1, path)
    session.commit()
    monkeypatch.setattr(settings.general, "skip_hashing", True)
    copy_id = copies(authenticated_client, media_type="movie", imdb_id="tt0133093").json["items"][0]["copy_id"]
    first = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    assert not getattr(providers.videos[0][1], "hashes", {})
    with open(path, "r+b") as handle:
        handle.truncate(12 * 1024 * 1024)
    second = post(authenticated_client, {**MOVIE, "copy_id": copy_id}).json
    assert second["context"]["file_revision"] != first["context"]["file_revision"]
