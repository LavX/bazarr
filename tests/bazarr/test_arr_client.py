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
