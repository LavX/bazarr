# coding=utf-8
"""Phase 4 INC3 (#156): rootfolder sync stamps arr_instance_id and scopes its
lookups/writes by it when an instance is supplied; the default (no-instance)
path inserts unscoped exactly as before.
"""
from sqlalchemy import insert, select


class _Resp:
    def __init__(self, folders):
        self._folders = folders

    def json(self):
        return self._folders


def _client(folders, kind="sonarr"):
    from app.config import get_ssl_verify, settings
    from arr_instances.client import ArrClient

    scalar = getattr(settings, kind)

    def http_get(url, headers=None, timeout=None, verify=None):
        return _Resp(folders)

    return ArrClient(
        kind=kind, ip=scalar.ip, port=scalar.port, base_url=scalar.base_url,
        ssl=scalar.ssl, verify_ssl=get_ssl_verify(kind), api_key="K",
        http_timeout=scalar.http_timeout, http_get=http_get)


def test_default_path_inserts_rootfolder_without_instance(schema_session, monkeypatch):
    from app.database import TableShows, TableShowsRootfolder
    from sonarr import rootfolder

    monkeypatch.setattr(rootfolder, "database", schema_session)
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/show", title="S"))

    rootfolder.get_sonarr_rootfolder(arr_client=_client([{"id": 1, "path": "/tv"}]))

    row = schema_session.execute(select(TableShowsRootfolder)).scalar_one()
    assert row.id == 1 and row.path == "/tv"
    assert row.arr_instance_id is None  # default path does not stamp


def test_instance_path_stamps_rootfolder(schema_session, monkeypatch):
    from app.database import TableShows, TableShowsRootfolder
    from sonarr import rootfolder

    monkeypatch.setattr(rootfolder, "database", schema_session)
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/anime/show", title="S", arr_instance_id=5))

    rootfolder.get_sonarr_rootfolder(
        arr_instance_id=5, arr_client=_client([{"id": 1, "path": "/anime"}]))

    row = schema_session.execute(select(TableShowsRootfolder)).scalar_one()
    assert row.arr_instance_id == 5
    assert row.upstream_rootfolder_id == 1
    assert row.local_rootfolder_id == 1


def test_instance_lookup_ignores_other_instances_shows(schema_session, monkeypatch):
    from app.database import TableShows, TableShowsRootfolder
    from sonarr import rootfolder

    monkeypatch.setattr(rootfolder, "database", schema_session)
    # only an instance-9 show exists under /other; instance 5 owns nothing there
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=2, path="/other/show", title="O", arr_instance_id=9))

    rootfolder.get_sonarr_rootfolder(
        arr_instance_id=5, arr_client=_client([{"id": 1, "path": "/other"}]))

    rows = schema_session.execute(
        select(TableShowsRootfolder).where(TableShowsRootfolder.arr_instance_id == 5)
    ).scalars().all()
    assert rows == []


def test_radarr_instance_path_stamps_rootfolder(schema_session, monkeypatch):
    from app.database import TableMovies, TableMoviesRootfolder
    from radarr import rootfolder

    monkeypatch.setattr(rootfolder, "database", schema_session)
    schema_session.execute(insert(TableMovies).values(
        radarrId=1, path="/4k/movie.mkv", title="M", tmdbId="1", arr_instance_id=8))

    rootfolder.get_radarr_rootfolder(
        arr_instance_id=8, arr_client=_client([{"id": 1, "path": "/4k"}], kind="radarr"))

    row = schema_session.execute(select(TableMoviesRootfolder)).scalar_one()
    assert row.arr_instance_id == 8
    assert row.upstream_rootfolder_id == 1
