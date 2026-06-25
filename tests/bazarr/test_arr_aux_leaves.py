# coding=utf-8
"""Phase 4 INC2 (#156): auxiliary Sonarr/Radarr leaves (filesystem browse +
notify/command) gain an optional ``arr_client``. With no client they run the
legacy path unchanged; with a client they route through ArrClient.get/post.
"""


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {"ok": True}
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _sonarr_client(http_get=None, http_post=None, api_key="KEY"):
    from app.config import get_ssl_verify, settings
    from arr_instances.client import ArrClient

    return ArrClient(
        kind="sonarr", ip=settings.sonarr.ip, port=settings.sonarr.port,
        base_url=settings.sonarr.base_url, ssl=settings.sonarr.ssl,
        verify_ssl=get_ssl_verify("sonarr"), api_key=api_key,
        http_timeout=settings.sonarr.http_timeout,
        http_get=http_get, http_post=http_post)


def test_browse_sonarr_filesystem_legacy_path(monkeypatch):
    from app.config import settings
    from sonarr import filesystem
    from sonarr.info import url_api_sonarr

    calls = []

    class _S:
        def get(self, url, timeout=None, verify=None, headers=None):
            calls.append({"url": url, "timeout": timeout})
            return _Resp()

    monkeypatch.setattr(filesystem, "sonarr_session", lambda: _S())
    filesystem.browse_sonarr_filesystem("/tv")

    assert calls[0]["url"] == (f"{url_api_sonarr()}filesystem?path=/tv"
                               "&allowFoldersWithoutTrailingSlashes=true&includeFiles=false")
    assert calls[0]["timeout"] == int(settings.sonarr.http_timeout)


def test_browse_sonarr_filesystem_client_path():
    from sonarr import filesystem
    from sonarr.info import url_api_sonarr

    captured = {}

    def spy_get(url, headers=None, timeout=None, verify=None):
        captured["url"] = url
        return _Resp()

    filesystem.browse_sonarr_filesystem("/tv", arr_client=_sonarr_client(http_get=spy_get))
    assert captured["url"] == (f"{url_api_sonarr()}filesystem?path=/tv"
                               "&allowFoldersWithoutTrailingSlashes=true&includeFiles=false")


def test_notify_sonarr_legacy_posts_rescan_command(monkeypatch):
    from sonarr import notify
    from sonarr.info import url_api_sonarr

    calls = []

    class _S:
        def post(self, url, json=None, timeout=None, verify=None, headers=None):
            calls.append({"url": url, "json": json})
            return _Resp()

    monkeypatch.setattr(notify, "sonarr_session", lambda: _S())
    notify.notify_sonarr(7)

    assert calls[0]["url"] == f"{url_api_sonarr()}command"
    assert calls[0]["json"] == {"name": "RescanSeries", "seriesId": 7}


def test_notify_sonarr_client_path_posts_rescan_command():
    from sonarr import notify

    captured = {}

    def spy_post(url, json=None, headers=None, timeout=None, verify=None):
        captured.update(url=url, json=json, headers=headers)
        return _Resp()

    notify.notify_sonarr(7, arr_client=_sonarr_client(http_post=spy_post))

    assert captured["url"].endswith("/api/v3/command")
    assert captured["json"] == {"name": "RescanSeries", "seriesId": 7}
    assert captured["headers"]["X-Api-Key"] == "KEY"


def test_notify_radarr_client_path_posts_rescan_movie():
    from app.config import get_ssl_verify, settings
    from arr_instances.client import ArrClient
    from radarr import notify

    captured = {}

    def spy_post(url, json=None, headers=None, timeout=None, verify=None):
        captured.update(url=url, json=json)
        return _Resp()

    client = ArrClient(
        kind="radarr", ip=settings.radarr.ip, port=settings.radarr.port,
        base_url=settings.radarr.base_url, ssl=settings.radarr.ssl,
        verify_ssl=get_ssl_verify("radarr"), api_key="K",
        http_timeout=settings.radarr.http_timeout, http_post=spy_post)
    notify.notify_radarr(11, arr_client=client)

    assert captured["url"].endswith("/api/v3/command")
    assert captured["json"] == {"name": "RescanMovie", "movieId": 11}
