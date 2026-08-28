import brotli
from subliminal.cache import region
from subliminal_patch.http import CFSession, RetryingCFSession


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
