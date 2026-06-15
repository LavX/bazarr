# coding=utf-8
"""Phase 4 INC1 (#156): the Radarr leaf API fetchers gain an optional
``arr_client``, mirroring the Sonarr conversion. Also locks the fix for the
confirmed bug where get_history_from_radarr_api read settings.sonarr.http_timeout.
"""


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else [{"id": 1}]
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _SpySession:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None, verify=None, headers=None):
        self.calls.append(
            {"url": url, "timeout": timeout, "verify": verify, "headers": headers})
        return _Resp()


def _mirror_client(http_get, api_key="KEY"):
    from app.config import get_ssl_verify, settings
    from arr_instances.client import ArrClient

    return ArrClient(
        kind="radarr", ip=settings.radarr.ip, port=settings.radarr.port,
        base_url=settings.radarr.base_url, ssl=settings.radarr.ssl,
        verify_ssl=get_ssl_verify("radarr"), api_key=api_key,
        http_timeout=settings.radarr.http_timeout, http_get=http_get)


def test_get_movies_client_path_targets_v3(monkeypatch):
    from radarr.sync import utils

    captured = {}

    def spy_get(url, headers=None, timeout=None, verify=None):
        captured.update(url=url, headers=headers, timeout=timeout, verify=verify)
        return _Resp()

    client = _mirror_client(spy_get)
    utils.get_movies_from_radarr_api("KEY", 5, arr_client=client)

    assert captured["url"].endswith("/api/v3/movie/5")
    assert captured["headers"]["X-Api-Key"] == "KEY"


def test_get_history_legacy_uses_radarr_http_timeout(monkeypatch):
    # Regression: the legacy path used settings.sonarr.http_timeout by mistake.
    from app.config import settings
    from radarr.sync import utils

    monkeypatch.setattr(settings.sonarr, "http_timeout", 999)
    monkeypatch.setattr(settings.radarr, "http_timeout", 45)

    spy = _SpySession()
    monkeypatch.setattr(utils, "radarr_session", lambda: spy)

    utils.get_history_from_radarr_api("KEY", 3)

    assert spy.calls[0]["timeout"] == 45  # radarr, not sonarr's 999


def test_get_movies_legacy_path_unchanged(monkeypatch):
    from app.config import get_ssl_verify, settings
    from radarr.info import radarr_headers, url_api_radarr
    from radarr.sync import utils

    spy = _SpySession()
    monkeypatch.setattr(utils, "radarr_session", lambda: spy)

    utils.get_movies_from_radarr_api("KEY", 5)

    call = spy.calls[0]
    assert call["url"] == f"{url_api_radarr()}movie/5"
    assert call["timeout"] == int(settings.radarr.http_timeout)
    assert call["verify"] == get_ssl_verify("radarr")
    assert call["headers"] == radarr_headers("KEY")
