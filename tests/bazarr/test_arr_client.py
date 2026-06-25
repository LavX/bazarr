# coding=utf-8
"""ArrClient + ArrClientFactory + connection-test service (#156).

Builds a per-instance Sonarr/Radarr HTTP client from an instance row (or raw
params for a pre-save "Test" button), mirroring the existing url_sonarr() shape
and X-Api-Key header. The HTTP getter is injectable so these tests never touch
the network.

Plan: docs/superpowers/plans/2026-05-27-multiple-arr-instances-final.md (Phase 2).
"""


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _make_client(**overrides):
    from arr_instances.client import ArrClient

    params = dict(kind="sonarr", ip="10.0.0.5", port=8989, base_url="/",
                  ssl=False, verify_ssl=False, api_key="key", http_timeout=60)
    params.update(overrides)
    return ArrClient(**params)


def test_base_url_http_with_port():
    client = _make_client(ssl=False, ip="10.0.0.5", port=8989, base_url="/")
    assert client.base_url() == "http://10.0.0.5:8989"


def test_base_url_https_and_base_path():
    client = _make_client(ssl=True, ip="sonarr.local", port=443, base_url="/sonarr/")
    assert client.base_url() == "https://sonarr.local:443/sonarr"


def test_test_connection_success_returns_version():
    captured = {}

    def fake_get(url, headers=None, timeout=None, verify=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp(200, {"version": "4.0.0.123", "appName": "Sonarr"})

    client = _make_client(http_get=fake_get, api_key="my-key")
    result = client.test_connection()

    assert result["ok"] is True
    assert result["version"] == "4.0.0.123"
    assert captured["url"] == "http://10.0.0.5:8989/api/v3/system/status"
    assert captured["headers"]["X-Api-Key"] == "my-key"


def test_test_connection_unauthorized():
    client = _make_client(http_get=lambda *a, **k: _FakeResp(401))
    result = client.test_connection()
    assert result["ok"] is False
    assert result["error"] == "unauthorized"


def test_test_connection_network_error():
    def boom(*a, **k):
        raise OSError("no route to host")

    client = _make_client(http_get=boom)
    result = client.test_connection()
    assert result["ok"] is False
    assert result["error"] == "connection_failed"


def test_factory_from_row_decrypts_key(schema_session):
    from arr_instances.client import ArrClientFactory
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    inst = repo.create("radarr", "Main", api_key="radarr-secret", ip="1.2.3.4", port=7878)

    captured = {}

    def fake_get(url, headers=None, timeout=None, verify=None):
        captured["headers"] = headers
        return _FakeResp(200, {"version": "5.0.0"})

    client = ArrClientFactory(repo).for_instance(inst.id, http_get=fake_get)
    assert client.base_url() == "http://1.2.3.4:7878"
    client.test_connection()
    # the decrypted key is what reaches the upstream header
    assert captured["headers"]["X-Api-Key"] == "radarr-secret"


def test_client_for_instance_skips_disabled(schema_session):
    # The single-item sync entry points guard `if arr_client is None` to "skip
    # disabled" instances. client_for_instance must honour that contract and
    # return None for a disabled instance, while the connection-test path
    # (enabled_only=False) can still reach it.
    from arr_instances.repository import ArrInstanceRepository
    from arr_instances.resolution import client_for_instance

    repo = ArrInstanceRepository(schema_session)
    on = repo.create("sonarr", "On", api_key="K", enabled=True)
    off = repo.create("sonarr", "Off", api_key="K", enabled=False)
    schema_session.flush()

    assert client_for_instance(schema_session, on.id) is not None
    assert client_for_instance(schema_session, off.id) is None
    assert client_for_instance(schema_session, off.id, enabled_only=False) is not None
    assert client_for_instance(schema_session, 999999) is None


def test_service_test_connection_invalid_kind_returns_400():
    from arr_instances import service

    body, status = service.test_connection({"kind": "plex"})
    assert status == 400


def test_service_test_connection_ok():
    from arr_instances import service

    def fake_get(url, headers=None, timeout=None, verify=None):
        return _FakeResp(200, {"version": "4.0.0"})

    body, status = service.test_connection(
        {"kind": "sonarr", "ip": "10.0.0.9", "port": 8989, "api_key": "k"},
        http_get=fake_get)
    assert status == 200
    assert body["ok"] is True
    assert body["version"] == "4.0.0"


def test_service_test_connection_for_instance_uses_stored_key(schema_session):
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    inst = ArrInstanceRepository(schema_session).create(
        "sonarr", "Main", api_key="stored-secret", ip="1.2.3.4", port=8989)

    captured = {}

    def fake_get(url, headers=None, timeout=None, verify=None):
        captured.update(url=url, headers=headers)
        return _FakeResp(200, {"version": "4.0.0"})

    body, status = service.test_connection_for_instance(
        schema_session, inst.id, http_get=fake_get)

    assert status == 200 and body["ok"] is True and body["version"] == "4.0.0"
    # the decrypted STORED key is what reaches the instance, not a body param
    assert captured["headers"]["X-Api-Key"] == "stored-secret"
    assert captured["url"].startswith("http://1.2.3.4:8989")


def test_service_test_connection_for_instance_404(schema_session):
    from arr_instances import service

    body, status = service.test_connection_for_instance(schema_session, 99999)
    assert status == 404


def test_service_test_connection_for_instance_applies_overrides_with_stored_key(schema_session):
    # edit-form "keep current key" + edited host: stored key, body connection override
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    inst = ArrInstanceRepository(schema_session).create(
        "sonarr", "Main", api_key="stored-secret", ip="1.2.3.4", port=8989)

    captured = {}

    def fake_get(url, headers=None, timeout=None, verify=None):
        captured.update(url=url, headers=headers)
        return _FakeResp(200, {"version": "4.0.0"})

    service.test_connection_for_instance(
        schema_session, inst.id, args={"ip": "9.9.9.9", "port": 9000}, http_get=fake_get)

    assert captured["url"].startswith("http://9.9.9.9:9000")
    assert captured["headers"]["X-Api-Key"] == "stored-secret"
