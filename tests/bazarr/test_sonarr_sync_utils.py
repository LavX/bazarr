# coding=utf-8
"""Phase 4 INC1 (#156): the Sonarr leaf API fetchers gain an optional
``arr_client``. With no client they run today's exact legacy path; with a
client they route through ArrClient.get(path). Both must produce an identical
URL / headers / timeout / verify for the default instance.
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
        kind="sonarr", ip=settings.sonarr.ip, port=settings.sonarr.port,
        base_url=settings.sonarr.base_url, ssl=settings.sonarr.ssl,
        verify_ssl=get_ssl_verify("sonarr"), api_key=api_key,
        http_timeout=settings.sonarr.http_timeout, http_get=http_get)


def test_get_series_legacy_path_is_unchanged(monkeypatch):
    from app.config import get_ssl_verify, settings
    from sonarr.info import sonarr_headers, url_api_sonarr
    from sonarr.sync import utils

    spy = _SpySession()
    monkeypatch.setattr(utils, "sonarr_session", lambda: spy)

    utils.get_series_from_sonarr_api("KEY", 7)

    call = spy.calls[0]
    assert call["url"] == f"{url_api_sonarr()}series/7"
    assert call["timeout"] == int(settings.sonarr.http_timeout)
    assert call["verify"] == get_ssl_verify("sonarr")
    assert call["headers"] == sonarr_headers("KEY")


def test_get_series_client_path_matches_legacy_url(monkeypatch):
    from app.config import get_ssl_verify, settings
    from sonarr.info import url_api_sonarr
    from sonarr.sync import utils

    captured = {}

    def spy_get(url, headers=None, timeout=None, verify=None):
        captured.update(url=url, headers=headers, timeout=timeout, verify=verify)
        return _Resp()

    client = _mirror_client(spy_get)
    utils.get_series_from_sonarr_api("KEY", 7, arr_client=client)

    assert captured["url"] == f"{url_api_sonarr()}series/7"
    assert captured["timeout"] == int(settings.sonarr.http_timeout)
    assert captured["verify"] == get_ssl_verify("sonarr")
    assert captured["headers"]["X-Api-Key"] == "KEY"


def test_get_episodes_client_path_query_strings(monkeypatch):
    from sonarr.info import url_api_sonarr
    from sonarr.sync import utils

    urls = []

    def spy_get(url, headers=None, timeout=None, verify=None):
        urls.append(url)
        return _Resp()

    client = _mirror_client(spy_get)
    utils.get_episodes_from_sonarr_api("KEY", series_id=9, arr_client=client)
    utils.get_episodes_from_sonarr_api("KEY", episode_id=42, arr_client=client)

    assert urls[0] == f"{url_api_sonarr()}episode?seriesId=9&includeEpisodeFile=true"
    assert urls[1] == f"{url_api_sonarr()}episode/42"


def test_get_tags_handles_connection_error_on_both_paths(monkeypatch):
    import requests

    from sonarr.sync import utils

    class _BoomSession:
        def get(self, *a, **k):
            raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(utils, "sonarr_session", lambda: _BoomSession())
    assert utils.get_tags() == []

    def boom_get(*a, **k):
        raise requests.exceptions.ConnectionError("down")

    assert utils.get_tags(arr_client=_mirror_client(boom_get)) == []
