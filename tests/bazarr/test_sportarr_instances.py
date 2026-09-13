"""Sportarr registration and authenticated, owner-specific connection handling."""
import json
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import sqlalchemy as sa


def test_sportarr_defaults_and_stable_keys_are_independent(schema_session):
    from arr_instances.repository import ArrInstanceRepository, to_safe_dict

    repo = ArrInstanceRepository(schema_session)
    sonarr = repo.create("sonarr", "Main")
    radarr = repo.create("radarr", "Main")
    first = repo.create("sportarr", "Main", api_key="fixture-secret")
    second = repo.create("sportarr", "Main", api_key="other-fixture-secret")
    assert first.port == 1867
    assert first.stable_key == sonarr.stable_key == radarr.stable_key == "main"
    assert second.stable_key == "main-2"
    assert repo.get_default("sportarr").id == first.id
    repo.set_default(second.id)
    assert repo.get_default("sportarr").id == second.id
    assert first.is_default == 0
    repo.update(second.id, is_default=False)
    assert repo.get_default("sportarr").id == first.id
    repo.update(first.id, enabled=False)
    assert repo.get_default("sportarr").id == second.id
    repo.update(first.id, enabled=True)
    repo.delete(second.id)
    assert repo.get_default("sportarr").id == first.id
    assert repo.get_default("sonarr").id == sonarr.id
    assert repo.get_default("radarr").id == radarr.id
    assert first.api_key.startswith("enc:v1:")
    assert "fixture-secret" not in first.api_key
    assert repo.get_decrypted_api_key(first.id) == "fixture-secret"
    assert to_safe_dict(first)["api_key_set"] is True
    assert "fixture-secret" not in str(to_safe_dict(first))
    assert "api_key" not in to_safe_dict(first)


def _client(**overrides):
    from arr_instances.client import ArrClient

    params = dict(kind="sportarr", ip="127.0.0.1", port=1867,
                  api_key="fixture-secret", http_timeout=2)
    return ArrClient(**(params | overrides))


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


def test_sportarr_probe_uses_native_endpoint_and_connection_settings():
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        return _Response({"appName": "Sportarr", "version": "4.1.6.1116"})

    result = _client(ip="sports.local", port=8443, base_url="sports/", ssl=True,
                     verify_ssl=True, http_timeout=19, http_get=get).test_connection()
    assert result == {"ok": True, "app_name": "Sportarr", "version": "4.1.6.1116"}
    assert len(requests) == 1
    url, kwargs = requests[0]
    assert url == "https://sports.local:8443/sports/api/system/status"
    assert kwargs["headers"]["X-Api-Key"] == "fixture-secret"
    assert kwargs["timeout"] == 19
    assert kwargs["verify"] is True
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("payload", [None, [], "Sportarr", {},
    {"status": "healthy"}, {"appName": "Sonarr", "version": "4.1.6"},
    {"appName": "Sportarr"}, {"appName": "Sportarr", "version": ""},
    {"appName": "Sportarr", "version": "  "},
    {"appName": "Sportarr", "version": 416},
    {"appName": "Sportarr", "version": {"secret": "fixture-secret"}}])
def test_sportarr_probe_rejects_health_or_malformed_status(payload):
    result = _client(http_get=lambda *a, **k: _Response(payload)).test_connection()
    assert result["ok"] is False
    assert result["error"] == "bad_response"
    assert "fixture-secret" not in str(result)


@pytest.mark.parametrize("status,error", [(401, "unauthorized"), (403, "http_error"),
                                         (500, "http_error"), (302, "http_error")])
def test_sportarr_probe_http_errors_do_not_echo_body(status, error):
    result = _client(http_get=lambda *a, **k: _Response(
        {"appName": "Sportarr", "version": "fixture-secret"}, status)).test_connection()
    assert result["ok"] is False
    assert result["error"] == error
    assert "fixture-secret" not in str(result)


def test_sportarr_probe_does_not_echo_credential_bearing_exception():
    def get(*args, **kwargs):
        raise OSError("failed requesting http://fixture-secret@host/?api_key=fixture-secret")

    result = _client(http_get=get).test_connection()
    assert result["error"] == "connection_failed"
    assert "fixture-secret" not in str(result)


@contextmanager
def _server(respond):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("X-Api-Key")))
            status, payload, headers = respond(self.path, self.headers)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, received
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def _native_status(path, headers):
    if path == "/api/health":
        return 200, {"status": "healthy"}, {}
    if path != "/api/system/status":
        return 404, {}, {}
    if headers.get("X-Api-Key") != "fixture-secret":
        return 401, {}, {}
    return 200, {"appName": "Sportarr", "version": "4.1.6.1116"}, {}


def test_native_auth_checks_each_instance_key_without_proxy_or_session_fallback(monkeypatch):
    from sonarr.http_session import sonarr_session
    from radarr.http_session import radarr_session

    def wrong_pool(*args, **kwargs):
        pytest.fail("Sportarr used a Sonarr/Radarr session")

    monkeypatch.setattr(sonarr_session(), "get", wrong_pool)
    monkeypatch.setattr(radarr_session(), "get", wrong_pool)
    with _server(lambda *a: (502, {}, {})) as (proxy_port, proxy_calls):
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("NO_PROXY", "")
        with _server(_native_status) as (port, received):
            assert _client(port=port).test_connection()["ok"] is True
            assert _client(port=port, api_key="wrong-key").test_connection()["error"] == "unauthorized"
            assert _client(port=port).test_connection()["ok"] is True
        assert received == [("/api/system/status", "fixture-secret"),
                            ("/api/system/status", "wrong-key"),
                            ("/api/system/status", "fixture-secret")]
        assert proxy_calls == []


@pytest.mark.parametrize("method", ["get", "post", "session"])
def test_authenticated_sportarr_requests_do_not_follow_redirects(method):
    with _server(_native_status) as (target_port, target_calls):
        location = f"http://127.0.0.1:{target_port}/api/system/status"
        with _server(lambda *a: (307, {}, {"Location": location})) as (port, received):
            if method == "session":
                from sportarr.http_session import sportarr_session
                response = sportarr_session().get(
                    f"http://127.0.0.1:{port}/api/system/status", timeout=2,
                    headers={"X-Api-Key": "fixture-secret"}, allow_redirects=True)
            else:
                response = getattr(_client(port=port), method)("/api/system/status")
            assert response.status_code == 307
            assert received == [("/api/system/status", "fixture-secret")]
        assert target_calls == []


def test_unknown_kind_does_not_fall_back_to_sonarr(monkeypatch):
    from sonarr.http_session import sonarr_session

    def wrong_pool(*args, **kwargs):
        pytest.fail("Unknown kind used the Sonarr session")

    monkeypatch.setattr(sonarr_session(), "get", wrong_pool)
    with pytest.raises(ValueError, match="kind"):
        _client(kind="unknown").get("/api/system/status")


def test_sportarr_unsaved_test_defaults_to_native_port():
    from arr_instances import service

    requests = []

    def get(url, **kwargs):
        requests.append(url)
        return _Response({"appName": "Sportarr", "version": "4.1.6.1116"})

    body, status = service.test_connection({"kind": "sportarr"}, http_get=get)
    assert status == 200
    assert body["ok"] is True
    assert requests == ["http://127.0.0.1:1867/api/system/status"]


def test_sportarr_profile_application_does_not_touch_movies(schema_session):
    from app.database import TableLanguagesProfiles, TableMovies
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    schema_session.execute(sa.insert(TableLanguagesProfiles).values(profileId=1, name="English", items="[]"))
    repo = ArrInstanceRepository(schema_session)
    instance = repo.create("sportarr", "Sports", options=json.dumps({
        "media_defaults": {"default_enabled": True, "default_profile": 1}}))
    radarr = repo.create("radarr", "Movies")
    schema_session.execute(sa.insert(TableMovies).values(
        arr_instance_id=instance.id, radarrId=7, tmdbId="1", path="/sports/event.mkv", title="Event"))
    schema_session.execute(sa.insert(TableMovies).values(
        arr_instance_id=radarr.id, radarrId=7, tmdbId="2", path="/movies/movie.mkv", title="Movie"))
    body, status = service.apply_default_profile(schema_session, instance.id)
    assert status == 200
    assert body["updated"] == 0
    assert schema_session.execute(sa.select(TableMovies.profileId).order_by(TableMovies.id)).scalars().all() == [None, None]


def test_sportarr_runtime_refresh_has_no_scalar_or_legacy_scheduler_effects(monkeypatch, scheduler_runtime):
    from app import config, scheduler, signalr_client
    from arr_instances import service
    from sportarr import scheduler as sports_scheduler
    refreshes = []
    monkeypatch.setattr(sports_scheduler, "refresh_sports_runtime", lambda: refreshes.append(True))

    def unexpected(*args, **kwargs):
        pytest.fail("Sportarr registration changed legacy runtime state")

    monkeypatch.setattr(config, "write_config", unexpected)
    monkeypatch.setattr(scheduler.scheduler, "update_configurable_tasks", unexpected)
    monkeypatch.setattr(signalr_client, "restart_sonarr_signalr", unexpected)
    monkeypatch.setattr(signalr_client, "restart_radarr_signalr", unexpected)
    service.refresh_runtime("sportarr", instance_id=1)
    service.refresh_runtime("sportarr", instance_id=1, removed=True)
    assert refreshes == [True, True]


def _load_endpoints(monkeypatch):
    # Load the actual wrappers and authentication without importing api.__init__,
    # which starts unrelated release and announcement network checks.
    api_path = Path(__file__).resolve().parents[2] / "bazarr" / "api"
    for name in ("_sportarr_test_api", "_sportarr_test_api.system"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, path in (("_sportarr_test_api.utils", api_path / "utils.py"),
                       ("_sportarr_test_api.system.arr_instances", api_path / "system" / "arr_instances.py")):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    return module


def test_sportarr_crud_through_existing_http_wrappers(schema_session, monkeypatch, scheduler_runtime):
    from flask import Flask
    from flask_restx import Api
    from app.config import settings
    import secret_store
    from arr_instances.repository import ArrInstanceRepository

    endpoints = _load_endpoints(monkeypatch)
    monkeypatch.setattr(endpoints, "database", schema_session)
    monkeypatch.setattr(secret_store, "persist_master_key", lambda: None)
    app = Flask(__name__)
    api = Api(app)
    api.add_namespace(endpoints.api_ns_system_arr_instances, path="/")
    client = app.test_client()
    headers = {"X-API-KEY": settings.auth.apikey}
    response = client.post("/system/arr-instances", headers=headers, json={
        "kind": "sportarr", "name": "Sports", "api_key": "fixture-secret"})
    assert response.status_code == 201
    instance_id = response.json["id"]
    assert response.json["port"] == 1867
    assert "fixture-secret" not in response.get_data(as_text=True)
    with _server(_native_status) as (port, received):
        response = client.post(f"/system/arr-instances/{instance_id}/test", headers=headers,
                               json={"port": port})
        assert response.status_code == 200
        assert response.json == {"ok": True, "version": "4.1.6.1116", "app_name": "Sportarr"}
        assert received == [("/api/system/status", "fixture-secret")]
        response = client.post("/system/arr-instances/test", headers=headers,
                               json={"kind": "sportarr", "port": port, "api_key": "wrong-key"})
        assert response.status_code == 200
        assert response.json["error"] == "unauthorized"
    response = client.patch(f"/system/arr-instances/{instance_id}", headers=headers,
                            json={"name": "Updated", "enabled": False})
    assert response.status_code == 200
    assert response.json["enabled"] is False
    assert response.json["name"] == "Updated"
    assert ArrInstanceRepository(schema_session).get_decrypted_api_key(instance_id) == "fixture-secret"
    response = client.delete(f"/system/arr-instances/{instance_id}", headers=headers)
    assert response.status_code == 204
    assert ArrInstanceRepository(schema_session).get(instance_id) is None


def test_sportarr_queued_profile_reindex_does_not_use_movie_indexer(monkeypatch, schema_session):
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository
    from app.database import TableSportsLeagues, TableSportsEvents
    from subtitles.indexer import movies, sports

    owner = ArrInstanceRepository(schema_session).create('sportarr', 'Sports')
    schema_session.execute(sa.insert(TableSportsLeagues).values(
        id=7, arr_instance_id=owner.id, sportarrLeagueId=7, title='League'))
    schema_session.execute(sa.insert(TableSportsEvents).values(
        id=61, arr_instance_id=owner.id, league_id=7, sportarrEventId=8, file_id=9,
        title='Event', path='/sports/event.mkv', subtitles='[]', missing_subtitles="['de']"))
    monkeypatch.setattr(sports, 'database', schema_session)
    monkeypatch.setattr(sports, 'notify', lambda *a: None)

    def unexpected(*args, **kwargs):
        pytest.fail("Sportarr default profile work reached the movie indexer")

    monkeypatch.setattr(movies, "list_missing_subtitles_movies", unexpected)
    monkeypatch.setattr(service, "event_stream", unexpected)
    service.reindex_after_default_profile("sportarr", [7], arr_instance_id=owner.id)
    assert schema_session.execute(sa.select(TableSportsEvents.missing_subtitles)).scalar_one() == '[]'


def test_sportarr_malformed_json_is_a_redacted_error():
    class MalformedResponse:
        status_code = 200

        def json(self):
            raise ValueError("fixture-secret response body")

    result = _client(http_get=lambda *a, **k: MalformedResponse()).test_connection()
    assert result["error"] == "bad_response"
    assert "fixture-secret" not in str(result)
