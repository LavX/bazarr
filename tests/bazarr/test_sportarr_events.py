"""Complete native file reconciliation with colliding owners on both engines."""

import copy
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from test_sportarr_kind_migration import migration_engine  # noqa: F401


@pytest.fixture
def library(migration_engine, monkeypatch):  # noqa: F811
    from app.database import Base, TableArrInstances, TableSportsLeagues
    from sportarr.sync import events

    Base.metadata.create_all(migration_engine)
    session = sessionmaker(bind=migration_engine, expire_on_commit=False)()
    with migration_engine.connect() as connection:
        connection.execute(
            sa.insert(TableArrInstances),
            [
                dict(
                    kind="sportarr",
                    name=str(i),
                    stable_key=str(i),
                    port=1867,
                    path_mappings=json.dumps([["/sports", f"/owner-{i}"]]),
                )
                for i in (1, 2)
            ],
        )
        connection.execute(
            sa.insert(TableSportsLeagues),
            [
                dict(id=50 + i, arr_instance_id=i, sportarrLeagueId=7, title="League")
                for i in (1, 2)
            ],
        )
    monkeypatch.setattr(events, "database", session)
    monkeypatch.setattr(events, "notify", lambda *args: None)
    # Subtitle discovery has its own real-media/ownership integration suite.
    monkeypatch.setattr(events, "refresh_event_files", lambda *args, **kwargs: None)
    yield session, events
    session.close()


def file(file_id=9, part=1, **kwargs):
    return (
        dict(
            id=file_id,
            eventId=8,
            filePath=f"/sports/part-{file_id}.mkv",
            size=100000,
            partNumber=part,
            partName="Main" if part else None,
            exists=True,
            releaseTitle=None,
            quality="WEBDL-1080p",
            codec="H.264",
            audioCodec="AAC",
            languages=["French"],
        )
        | kwargs
    )


def event(files=None, **kwargs):
    return (
        dict(
            id=8,
            leagueId=7,
            title="Card",
            monitored=True,
            eventDate="2026-09-01T12:00:00Z",
            broadcastDate="2026-09-01T00:00:00",
            seasonNumber=None,
            episodeNumber=None,
            hasFile=True,
            files=[file()] if files is None else files,
        )
        | kwargs
    )


def remote(monkeypatch, module, data, *, page_size=1000, change=None):
    calls = []

    def get(path):
        from urllib.parse import parse_qs, urlsplit

        calls.append(path)
        query = parse_qs(urlsplit(path).query)
        assert query["showAll"] == ["true"]
        page = int(query["page"][0])
        if change:
            change()
        size = min(page_size, int(query["pageSize"][0]))
        payload = dict(
            page=page,
            pageSize=size,
            totalRecords=len(data),
            totalPages=(len(data) + size - 1) // size,
            records=data[(page - 1) * size : page * size],
        )
        return SimpleNamespace(status_code=200, json=lambda: payload)

    monkeypatch.setattr(
        module.ArrClientFactory, "from_row", lambda *args: SimpleNamespace(get=get)
    )
    return calls


def rows(session):
    from app.database import TableSportsEvents

    session.expire_all()
    return (
        session.execute(sa.select(TableSportsEvents).order_by(TableSportsEvents.id))
        .scalars()
        .all()
    )


def test_file_identity_swaps_replacements_and_owned_pruning(library, monkeypatch):
    from app.database import TableHistorySports, TableSportsEvents

    session, sync = library
    remote(monkeypatch, sync, [event([file(9, 1), file(10, 2)])])
    a = sync.sync_events(51, 1)
    b = sync.sync_events(52, 2)
    assert len(a) == len(b) == 2 and set(a).isdisjoint(b)
    for row in rows(session):
        session.execute(
            sa.insert(TableHistorySports).values(
                arr_instance_id=row.arr_instance_id,
                league_id=row.league_id,
                event_id=row.id,
            )
        )
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id.in_(a))
        .values(subtitles="[['fr', '/old.fr.srt', 123]]", ffprobe_cache=b"cached")
    )
    remote(monkeypatch, sync, [event([file(9, 2), file(10, 1)])])
    assert set(sync.sync_events(51, 1)) == set(a)
    current = {row.file_id: row for row in rows(session) if row.arr_instance_id == 1}
    assert current[9].id == a[0] and current[9].partNumber == 2
    assert current[10].id == a[1] and current[10].partNumber == 1
    assert current[9].ffprobe_cache == b"cached"
    remote(monkeypatch, sync, [event([file(11, 2), file(10, 1)])])
    assert set(sync.sync_events(51, 1)) == set(a)
    current = {row.file_id: row for row in rows(session) if row.arr_instance_id == 1}
    assert current[11].id == a[0] and current[11].ffprobe_cache is None
    assert current[11].subtitles == "[]" and current[11].sceneName is None
    assert (
        session.execute(
            sa.select(sa.func.count()).select_from(TableHistorySports)
        ).scalar()
        == 4
    )
    remote(monkeypatch, sync, [])
    assert sync.sync_events(51, 1) == []
    assert [row.id for row in rows(session)] == b


def test_complete_pages_null_numbers_and_playable_files(library, monkeypatch):
    session, sync = library
    data = [
        event([file(9, None), file(10, 2, exists=False)]),
        event([file(11, None, eventId=12)], id=12, title="Other"),
        event([file(13, 1, eventId=13, size=20480)], id=13),
    ]
    calls = remote(monkeypatch, sync, data, page_size=1)
    ids = sync.sync_events(51, 1, page_size=1)
    assert len(ids) == 2 and len(calls) == 3
    assert all(
        row.partNumber == 0 and row.season is None and row.episode is None
        for row in rows(session)
    )
    assert rows(session)[0].audio_language == "['French']"


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate-event",
        "duplicate-file",
        "duplicate-part",
        "duplicate-path",
        "wrong-owner",
        "bad-files",
        "bad-size",
        "bad-page",
        "missing-page",
        "changed-total",
        "short-page",
    ],
)
def test_invalid_or_partial_response_cannot_change_rows(library, monkeypatch, defect):
    session, sync = library
    remote(monkeypatch, sync, [event()])
    ids = sync.sync_events(51, 1)
    data = [event(title="Must not save"), event([], id=12)]
    if defect == "duplicate-event":
        data[1] = copy.deepcopy(data[0])
    if defect == "duplicate-file":
        data[0]["files"].append(file(9, 2))
    if defect == "duplicate-part":
        data[0]["files"].append(file(10, 1))
    if defect == "duplicate-path":
        data[0]["files"].append(file(10, 2, filePath="/sports/part-9.mkv"))
    if defect == "wrong-owner":
        data[0]["leagueId"] = 99
    if defect == "bad-files":
        data[0]["files"] = None
    if defect == "bad-size":
        data[0]["files"][0]["size"] = "huge"
    calls = remote(monkeypatch, sync, data, page_size=1)
    factory = sync.ArrClientFactory.from_row

    def wrapped(*args):
        client = factory(*args)
        get = client.get

        def fail(path):
            response = get(path)
            payload = response.json()
            if defect == "bad-page":
                payload["page"] = 0
            if defect == "missing-page" and len(calls) == 2:
                response.status_code = 503
            if defect == "changed-total" and len(calls) == 2:
                payload["totalRecords"] = 3
            if defect == "short-page":
                payload["records"] = []
            return response

        client.get = fail
        return client

    monkeypatch.setattr(sync.ArrClientFactory, "from_row", wrapped)
    with pytest.raises(ValueError):
        sync.sync_events(51, 1, page_size=1)
    assert [row.id for row in rows(session)] == ids
    assert rows(session)[0].title == "Card"


@pytest.mark.parametrize(
    "change", [{"enabled": 0}, {"ip": "changed"}, {"path_mappings": "[]"}]
)
def test_owner_change_during_fetch_rejects_write(library, monkeypatch, change):
    from app.database import TableArrInstances

    session, sync = library
    remote(monkeypatch, sync, [event()])
    ids = sync.sync_events(51, 1)
    remote(
        monkeypatch,
        sync,
        [],
        change=lambda: session.execute(
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 1)
            .values(**change)
        ),
    )
    with pytest.raises(ValueError):
        sync.sync_events(51, 1)
    assert [row.id for row in rows(session)] == ids


def test_mutation_failure_rolls_back_swaps_and_history(library, monkeypatch):
    session, sync = library
    remote(monkeypatch, sync, [event([file(9, 1), file(10, 2)])])
    ids = sync.sync_events(51, 1)
    remote(monkeypatch, sync, [event([file(9, 2), file(10, 1)])])

    def fail(*args):
        raise RuntimeError("injected")

    monkeypatch.setattr(sync, "_prune_events", fail)
    with pytest.raises(RuntimeError):
        sync.sync_events(51, 1)
    assert [(row.id, row.partNumber) for row in rows(session)] == list(zip(ids, [1, 2]))


def test_local_identity_and_mapping_are_explicit(library, monkeypatch):
    from sportarr import identity
    from utilities import path_mappings as mapping
    from app.database import TableArrInstances

    session, sync = library
    monkeypatch.setattr(identity, "database", session)
    remote(monkeypatch, sync, [event()])
    a, b = sync.sync_events(51, 1)[0], sync.sync_events(52, 2)[0]
    context = identity.resolve_event(a, 1)
    assert (
        context.event_id,
        context.league_id,
        context.arr_instance_id,
        context.upstream_event_id,
        context.upstream_league_id,
        context.file_id,
    ) == (a, 51, 1, 8, 7, 9)
    assert (
        context.original_path == "/sports/part-9.mkv"
        and context.mapped_path == "/owner-1/part-9.mkv"
    )
    for local, owner in [(a, 2), (b, 1), (8, 1), (True, 1)]:
        with pytest.raises(ValueError):
            identity.resolve_event(local, owner)
    session.execute(
        sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(enabled=0)
    )
    with pytest.raises(ValueError):
        identity.resolve_event(a)
    assert identity.resolve_event(a, enabled_only=False).arr_instance_id == 1
    assert (
        mapping.apply_sports_mapping("/sports-other/file.mkv", [["/sports", "/local"]])
        == "/sports-other/file.mkv"
    )
    assert (
        mapping.apply_sports_mapping("/other/sports/file.mkv", [["/sports", "/local"]])
        == "/other/sports/file.mkv"
    )
    assert mapping.apply_sports_mapping("/sports/file.mkv", []) == "/sports/file.mkv"


def test_mapping_service_roundtrip_validation_and_legacy_behavior(schema_session):
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    mapping = [["/sports", "/local"]]
    result, status = service.create_instance(
        schema_session, dict(kind="sportarr", name="Sports", path_mappings=mapping)
    )
    assert status == 201 and result["path_mappings"] == mapping
    owner = result["id"]
    assert (
        json.loads(ArrInstanceRepository(schema_session).get(owner).path_mappings)
        == mapping
    )
    assert (
        service.update_instance(schema_session, owner, {"name": "Changed"})[0][
            "path_mappings"
        ]
        == mapping
    )
    for invalid in [
        "",
        {},
        [["", "/local"]],
        [["/a"]],
        [["/a", "/b", "/c"]],
        [[True, "/b"]],
    ]:
        assert (
            service.update_instance(schema_session, owner, {"path_mappings": invalid})[
                1
            ]
            == 400
        )
    assert (
        service.update_instance(schema_session, owner, {"path_mappings": []})[0][
            "path_mappings"
        ]
        == []
    )
    assert (
        service.create_instance(
            schema_session, dict(kind="sonarr", name="TV", path_mappings=mapping)
        )[1]
        == 400
    )


def test_rootfolders_are_complete_owned_and_mapped(library, monkeypatch, tmp_path):
    from app.database import TableArrInstances, TableSportsLeaguesRootfolder
    from sportarr import rootfolder

    session, _ = library
    monkeypatch.setattr(rootfolder, "database", session)
    monkeypatch.setattr(rootfolder, "notify", lambda *args: None)
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(path_mappings=json.dumps([["/sports", str(tmp_path)]]))
    )
    payload = [dict(id=1, path="/sports")]

    def get(path):
        assert path == "/api/rootfolder"
        return SimpleNamespace(status_code=200, json=lambda: payload)

    monkeypatch.setattr(
        rootfolder.ArrClientFactory, "from_row", lambda *a: SimpleNamespace(get=get)
    )
    rootfolder.sync_rootfolders(1)
    rootfolder.sync_rootfolders(2)
    roots = (
        session.execute(
            sa.select(TableSportsLeaguesRootfolder).order_by(
                TableSportsLeaguesRootfolder.id
            )
        )
        .scalars()
        .all()
    )
    assert [(r.arr_instance_id, r.rootfolder_id, r.accessible) for r in roots] == [
        (1, 1, 1),
        (2, 1, 0),
    ]
    payload.append(dict(id=1, path="/bad"))
    with pytest.raises(ValueError):
        rootfolder.sync_rootfolders(1)
    payload.clear()
    rootfolder.sync_rootfolders(1)
    assert session.execute(
        sa.select(TableSportsLeaguesRootfolder.arr_instance_id)
    ).scalars().all() == [2]


def test_event_library_local_ids_pagination_and_disabled_admin_view(
    library, monkeypatch
):
    from app.database import TableArrInstances
    from sportarr.library import list_events, get_event

    session, sync = library
    remote(monkeypatch, sync, [event([file(9, None)])])
    a, b = sync.sync_events(51, 1)[0], sync.sync_events(52, 2)[0]
    response = list_events(session, 51, 1)
    assert response["total"] == 1 and response["data"][0]["id"] == a
    item = response["data"][0]
    assert item["partNumber"] is None and item["hasFile"] is True
    assert item["mapped_path"] == "/owner-1/part-9.mkv" and "ffprobe_cache" not in item
    assert get_event(session, b, 1) is None and get_event(session, 8, 1) is None
    with pytest.raises(ValueError):
        list_events(session, 51, 2)
    assert list_events(session, 51, 1, start=1)["data"] == []
    session.execute(
        sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(enabled=0)
    )
    assert get_event(session, a) is None
    assert get_event(session, a, 1, enabled_only=False)["id"] == a


def test_authenticated_event_and_rootfolder_http_scope(library, monkeypatch):
    import importlib.util
    from pathlib import Path
    import sys
    from types import ModuleType
    from flask import Flask
    from flask_restx import Api
    from app.config import settings

    session, sync = library
    remote(monkeypatch, sync, [event()])
    a, b = sync.sync_events(51, 1)[0], sync.sync_events(52, 2)[0]
    root = Path(__file__).resolve().parents[2] / "bazarr" / "api"
    for name in ("_sports_files_api", "_sports_files_api.sports"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, path in [
        ("_sports_files_api.utils", root / "utils.py"),
        ("_sports_files_api.sports.leagues", root / "sports/leagues.py"),
        ("_sports_files_api.sports.events", root / "sports/events.py"),
    ]:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    monkeypatch.setattr(module, "database", session)
    app = Flask(__name__)
    api = Api(app)
    api.add_namespace(module.api_ns_sports_events, path="/")
    client = app.test_client()
    headers = {"X-API-KEY": settings.auth.apikey}
    for url in [
        f"/sports/events/{a}",
        "/sports/leagues/51/events",
        "/sports/rootfolders",
    ]:
        assert client.get(url).status_code == 401
    assert (
        client.get(f"/sports/events/{a}?arr_instance_id=1", headers=headers).json["id"]
        == a
    )
    assert (
        client.get(f"/sports/events/{a}?arr_instance_id=2", headers=headers).status_code
        == 404
    )
    assert (
        client.get("/sports/events/8?arr_instance_id=1", headers=headers).status_code
        == 404
    )
    result = client.get("/sports/leagues/51/events?arr_instance_id=1", headers=headers)
    assert result.status_code == 200 and result.json["data"][0]["id"] == a
    assert b not in [r["id"] for r in result.json["data"]]
    for query in ["arr_instance_id=2", "arr_instance_id=0", "length=0", "start=-1"]:
        assert (
            client.get(
                "/sports/leagues/51/events?" + query, headers=headers
            ).status_code
            == 400
        )
    assert client.get("/sports/rootfolders", headers=headers).status_code == 400
    assert client.get(
        "/sports/rootfolders?arr_instance_id=1", headers=headers
    ).json == {"data": []}


def test_sync_monitoring_policy_validates_before_filtering(library, monkeypatch):
    from app.database import TableArrInstances

    session, sync = library
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(
            options=json.dumps(
                {"sports_settings": {"sync_only_monitored_events": True}}
            )
        )
    )
    remote(monkeypatch, sync, [event(monitored=False)])
    assert sync.sync_events(51, 1) == []


def test_file_identity_conflict_with_other_league_cannot_mutate(library, monkeypatch):
    from app.database import TableSportsLeagues, TableSportsEvents

    session, sync = library
    session.execute(
        sa.insert(TableSportsLeagues).values(
            id=53, arr_instance_id=1, sportarrLeagueId=10, title="Other"
        )
    )
    session.execute(
        sa.insert(TableSportsEvents).values(
            id=71,
            league_id=53,
            arr_instance_id=1,
            sportarrEventId=18,
            file_id=19,
            title="Other",
            path="/sports/part-9.mkv",
        )
    )
    remote(monkeypatch, sync, [event()])
    with pytest.raises(ValueError):
        sync.sync_events(51, 1)
    assert [r.id for r in rows(session)] == [71]


def test_sports_mapping_boundaries_reverse_and_ambiguous_prefixes():
    from utilities.path_mappings import apply_sports_mapping, validate_sports_mappings

    pairs = [["/sports", "C:\\"], ["/sports/league", "D:\\League"]]
    assert apply_sports_mapping("/sports/file.mkv", pairs) == "C:\\file.mkv"
    assert (
        apply_sports_mapping("/sports/league/main.mkv", pairs) == "D:\\League\\main.mkv"
    )
    assert (
        apply_sports_mapping("D:\\League\\main.mkv", pairs, reverse=True)
        == "/sports/league/main.mkv"
    )
    assert (
        apply_sports_mapping("/anything.mkv", [["/", "/media"]])
        == "/media/anything.mkv"
    )
    for invalid in [[["/a", "/same"], ["/b", "/same/"]], [["/a/", "/x"], ["/a", "/y"]]]:
        with pytest.raises(ValueError):
            validate_sports_mappings(invalid)


def test_large_paged_library_uses_bounded_sql_parameters(library, monkeypatch):
    session, sync = library
    if session.get_bind().dialect.name == "sqlite":
        import sqlite3

        def limit(connection, record):
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)

        sa.event.listen(session.get_bind(), "connect", limit)
    data = [
        event([file(1000 + i, None, eventId=100 + i)], id=100 + i) for i in range(1001)
    ]
    remote(monkeypatch, sync, data)
    ids = sync.sync_events(51, 1)
    assert len(ids) == 1001
    remote(monkeypatch, sync, data[:1])
    assert sync.sync_events(51, 1) == [ids[0]]
    assert len(rows(session)) == 1


@pytest.mark.parametrize("operation", ["events", "roots"])
def test_owner_lock_covers_checked_mutations(library, monkeypatch, operation):
    import threading
    from app.database import TableArrInstances
    from sportarr import rootfolder
    from sportarr.sync import leagues

    session, sync = library
    engine = session.get_bind()
    attempted, release = threading.Event(), threading.Event()
    outcome, errors, threads = {}, [], []
    remote(monkeypatch, sync, [event()])
    original = leagues.require_sportarr

    def competing_edit():
        try:
            with engine.connect() as connection:
                if engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("PRAGMA busy_timeout=0")
                else:
                    connection.exec_driver_sql("SET lock_timeout='100ms'")
                statement = (
                    sa.update(TableArrInstances)
                    .where(TableArrInstances.id == 1)
                    .values(enabled=0)
                )
                try:
                    connection.execute(statement)
                    outcome["first"] = "committed"
                except sa.exc.OperationalError:
                    connection.rollback()
                    outcome["first"] = "blocked"
                attempted.set()
                assert release.wait(5)
                connection.execute(statement)
                outcome["second"] = "committed"
        except BaseException as exc:
            errors.append(exc)
            attempted.set()

    def checked(transaction, owner):
        row = original(transaction, owner)
        if transaction is not session:
            if engine.dialect.name == "sqlite":
                assert (
                    transaction.connection().connection.driver_connection.in_transaction
                )
            thread = threading.Thread(target=competing_edit)
            threads.append(thread)
            thread.start()
            assert attempted.wait(5)
        return row

    monkeypatch.setattr(leagues, "require_sportarr", checked)
    if operation == "roots":
        monkeypatch.setattr(rootfolder, "database", session)
        monkeypatch.setattr(rootfolder, "notify", lambda *args: None)
        monkeypatch.setattr(
            rootfolder.ArrClientFactory,
            "from_row",
            lambda *args: SimpleNamespace(
                get=lambda path: SimpleNamespace(
                    status_code=200, json=lambda: [dict(id=1, path="/sports")]
                )
            ),
        )
    try:
        if operation == "events":
            sync.sync_events(51, 1)
        else:
            rootfolder.sync_rootfolders(1)
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert not errors
    assert outcome == {"first": "blocked", "second": "committed"}


def test_same_path_metadata_upgrade_invalidates_probe_cache(library, monkeypatch):
    from app.database import TableSportsEvents

    session, sync = library
    remote(monkeypatch, sync, [event()])
    local = sync.sync_events(51, 1)[0]
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id == local)
        .values(ffprobe_cache=b"old")
    )
    remote(monkeypatch, sync, [event([file(quality="WEBDL-2160p")])])
    assert sync.sync_events(51, 1) == [local]
    assert rows(session)[0].ffprobe_cache is None


@pytest.mark.parametrize("field", ["eventDate", "broadcastDate", "externalId"])
def test_parser_rejects_malformed_metadata_before_mutation(field):
    from sportarr.parser import parse_events

    with pytest.raises(ValueError):
        parse_events([event(**{field: {"invalid": "metadata"}})], 7)


def test_path_mapping_http_contract_preserves_and_rejects_null(
    schema_session, monkeypatch
):
    from flask import Flask
    from flask_restx import Api
    from app.config import settings
    from arr_instances import service
    import secret_store
    from test_sportarr_instances import _load_endpoints

    endpoints = _load_endpoints(monkeypatch)
    monkeypatch.setattr(endpoints, "database", schema_session)
    monkeypatch.setattr(service, "refresh_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(secret_store, "persist_master_key", lambda: None)
    app = Flask(__name__)
    api = Api(app)
    api.add_namespace(endpoints.api_ns_system_arr_instances, path="/")
    client = app.test_client()
    headers = {"X-API-KEY": settings.auth.apikey}
    result = client.post(
        "/system/arr-instances",
        headers=headers,
        json=dict(
            kind="sportarr", name="Sports", path_mappings=[["/sports", "/local"]]
        ),
    )
    assert result.status_code == 201 and result.json["path_mappings"] == [
        ["/sports", "/local"]
    ]
    url = "/system/arr-instances/" + str(result.json["id"])
    result = client.patch(url, headers=headers, json={"name": "Changed"})
    assert result.status_code == 200 and result.json["path_mappings"] == [
        ["/sports", "/local"]
    ]
    for invalid in [None, {}, "bad", [["", "/local"]]]:
        response = client.patch(url, headers=headers, json={"path_mappings": invalid})
        assert response.status_code == 400
    assert (
        client.patch(url, headers=headers, json={"path_mappings": []}).json[
            "path_mappings"
        ]
        == []
    )


def test_replacement_and_renumbering_reserves_stable_paths_and_history(
    library, monkeypatch
):
    from app.database import TableHistorySports

    session, sync = library
    remote(monkeypatch, sync, [event([file(9, 1), file(10, 2)])])
    a, b = sync.sync_events(51, 1), sync.sync_events(52, 2)
    for owner, league, ids in [(1, 51, a), (2, 52, b)]:
        for local in ids:
            session.execute(
                sa.insert(TableHistorySports).values(
                    arr_instance_id=owner, league_id=league, event_id=local
                )
            )
    before = session.execute(
        sa.select(
            TableHistorySports.arr_instance_id, TableHistorySports.event_id
        ).order_by(TableHistorySports.id)
    ).all()
    remote(
        monkeypatch,
        sync,
        [
            event(
                [
                    file(11, 2, filePath="/sports/part-9.mkv"),
                    file(12, 1, filePath="/sports/part-10.mkv"),
                ]
            )
        ],
    )
    assert sync.sync_events(51, 1) == a
    current = [row for row in rows(session) if row.arr_instance_id == 1]
    assert [(row.id, row.file_id, row.partNumber) for row in current] == [
        (a[0], 11, 2),
        (a[1], 12, 1),
    ]
    assert (
        session.execute(
            sa.select(
                TableHistorySports.arr_instance_id, TableHistorySports.event_id
            ).order_by(TableHistorySports.id)
        ).all()
        == before
    )
    assert [row.id for row in rows(session) if row.arr_instance_id == 2] == b


def test_stable_path_reservation_precedes_positional_replacement(library, monkeypatch):
    session, sync = library
    remote(monkeypatch, sync, [event([file(9, 1), file(10, 2)])])
    previous = sync.sync_events(51, 1)
    # The first row must not steal the second row's unique stable path match.
    remote(
        monkeypatch,
        sync,
        [event([file(11, 1), file(12, 2, filePath="/sports/part-9.mkv")])],
    )
    current = sync.sync_events(51, 1)
    assert current[1] == previous[0]
    assert current[0] not in previous


def test_ambiguous_stored_paths_are_rejected_before_mutation(library, monkeypatch):
    from app.database import TableSportsEvents

    session, sync = library
    remote(monkeypatch, sync, [event([file(9, 1), file(10, 2)])])
    previous = sync.sync_events(51, 1)
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id.in_(previous))
        .values(path="/sports/shared.mkv")
    )
    remote(monkeypatch, sync, [event([file(11, 3, filePath="/sports/shared.mkv")])])
    with pytest.raises(ValueError):
        sync.sync_events(51, 1)
    assert [(row.id, row.partNumber) for row in rows(session)] == list(
        zip(previous, [1, 2])
    )


@pytest.mark.parametrize(
    "source,target",
    [("/sports", "/"), ("/sports", "C:\\"), ("/", "/sports"), ("C:\\", "/sports")],
)
def test_exact_root_mapping_preserves_absolute_root(source, target):
    from utilities.path_mappings import apply_sports_mapping

    assert apply_sports_mapping(source, [[source, target]]) == target
    assert apply_sports_mapping(target, [[source, target]], reverse=True) == source


def test_rootfolder_exact_mapping_checks_the_actual_root(library, monkeypatch):
    from app.database import TableArrInstances, TableSportsLeaguesRootfolder
    from sportarr import rootfolder

    session, _ = library
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(path_mappings=json.dumps([["/sports", "/"]]))
    )
    monkeypatch.setattr(rootfolder, "database", session)
    monkeypatch.setattr(rootfolder, "notify", lambda *args: None)
    monkeypatch.setattr(
        rootfolder.ArrClientFactory,
        "from_row",
        lambda *args: SimpleNamespace(
            get=lambda path: SimpleNamespace(
                status_code=200, json=lambda: [dict(id=1, path="/sports")]
            )
        ),
    )
    rootfolder.sync_rootfolders(1)
    row = session.execute(sa.select(TableSportsLeaguesRootfolder)).scalar_one()
    import os

    assert row.accessible == int(os.path.isdir("/") and os.access("/", os.W_OK))
    assert rootfolder.list_rootfolders(session, 1)["data"][0]["mapped_path"] == "/"
