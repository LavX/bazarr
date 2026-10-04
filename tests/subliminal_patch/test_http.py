import logging

import brotli
from requests_mock import ANY
from subliminal.cache import region
from subliminal_patch.http import CFSession, RetryingCFSession, RetryingSession


def test_cf_session_brotli_response(requests_mock):
    region.configure("dogpile.cache.memory", replace_existing_backend=True)
    raw_html = b"<!DOCTYPE html><html><body>Test LegendasDivx Content</body></html>"
    compressed_html = brotli.compress(raw_html)

    requests_mock.get(
        "https://www.legendasdivx.pt/forum/ucp.php?mode=login",
        content=compressed_html,
        headers={"Content-Encoding": "br"},
    )

    session = CFSession()
    resp = session.get("https://www.legendasdivx.pt/forum/ucp.php?mode=login")
    assert resp.status_code == 200
    assert resp.content == raw_html
    assert "Test LegendasDivx" in resp.text


def test_retrying_cf_session_brotli_response(requests_mock):
    region.configure("dogpile.cache.memory", replace_existing_backend=True)
    raw_html = b"<!DOCTYPE html><html><body>Test RetryingCFSession Brotli</body></html>"
    compressed_html = brotli.compress(raw_html)

    requests_mock.get(
        "https://www.legendasdivx.pt/forum/ucp.php?mode=login",
        content=compressed_html,
        headers={"Content-Encoding": "br"},
    )

    session = RetryingCFSession()
    resp = session.get("https://www.legendasdivx.pt/forum/ucp.php?mode=login")
    assert resp.status_code == 200
    assert resp.content == raw_html
    assert "Test RetryingCFSession" in resp.text


def test_retrying_session_logs_its_proxy_once_without_credentials(requests_mock, monkeypatch, caplog):
    """The proxy is announced when the session is built and never per request:
    the proxy URL carries the operator's credentials and a request URL can carry
    a provider's API key, so only the stripped proxy address is ever logged."""
    monkeypatch.setenv("SZ_HTTP_PROXY", "http://proxyuser:proxy-pass@proxy.lan:3128")
    requests_mock.get(ANY, text="ok")

    with caplog.at_level(logging.DEBUG, logger="subliminal_patch.http"):
        session = RetryingSession()
        session.get("https://apiuser:api-pass@api.example.test/search?apikey=query-secret&q=x")
        session.get("https://api.example.test/other")

    lines = [r.getMessage() for r in caplog.records if "Using proxy" in r.getMessage()]
    assert lines == ["Using proxy http://proxy.lan:3128"]
