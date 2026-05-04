from __future__ import annotations
import ipaddress
import socket
import threading
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

class UnsafeURLError(ValueError):
    """Outbound URL is rejected by the SSRF guard."""

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_TLD_SUFFIXES = (".local", ".internal")

_walker_session: requests.Session | None = None
_walker_lock = threading.Lock()


def _get_walker_session() -> requests.Session:
    """Lazy module-level Session for redirect pre-flight HEADs. No retries:
    a transient 5xx during pre-flight should NOT trigger multiple HEAD
    probes against a possibly-malicious target."""
    global _walker_session
    if _walker_session is None:
        with _walker_lock:
            if _walker_session is None:
                s = requests.Session()
                adapter = HTTPAdapter(max_retries=0)
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                _walker_session = s
    return _walker_session


def assert_safe_outbound(url: str) -> None:
    """Raise UnsafeURLError if `url` targets loopback, link-local, RFC1918,
    reserved, or non-http(s) destinations.

    Must run BEFORE requests.get. Callers that follow redirects MUST re-invoke
    this function on the final URL (after redirect resolution) to prevent
    redirect-based bypasses. When DNS returns multiple addresses, ALL of them
    are validated; a single unsafe address rejects the URL.
    """
    if not url or "\x00" in url:
        raise UnsafeURLError("null byte or empty url")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parsed.scheme!r} not allowed")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("missing host")
    host_l = host.lower()
    if host_l in ("localhost",) or any(host_l.endswith(s) for s in _BLOCKED_TLD_SUFFIXES):
        raise UnsafeURLError(f"host {host!r} is reserved")

    # Build the candidate IP list: literal or all DNS results.
    ips = []
    try:
        ips.append(ipaddress.ip_address(host))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise UnsafeURLError(f"DNS resolution failed for {host!r}")
        seen = set()
        for info in infos:
            addr = info[4][0]
            if addr in seen:
                continue
            seen.add(addr)
            try:
                ips.append(ipaddress.ip_address(addr))
            except ValueError:
                # Unparseable address — treat as unsafe
                raise UnsafeURLError(f"unresolvable DNS entry {addr!r}")

    if not ips:
        raise UnsafeURLError(f"no IPs resolved for {host!r}")

    for ip in ips:
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise UnsafeURLError(f"destination IP {ip} is not a public address")


def resolve_safe_url(url: str, max_redirects: int = 5, timeout: float = 10.0) -> str:
    """Walk the redirect chain via HEAD with SSRF validation at every hop.

    The provider's HTTP client follows redirects automatically inside
    `download_subtitle()`, so a public advertised URL that 30x-redirects to
    127.0.0.1 or 169.254.169.254 would fetch the private target with no
    per-hop SSRF check. This pre-flight walks the chain ourselves with
    `allow_redirects=False`, validates each Location, and returns the final
    URL once a non-3xx status is seen.

    Behaviour:
      - input URL is validated by assert_safe_outbound first
      - HEAD failure (connection error, timeout, etc.) returns the input URL
        unchanged: cannot probe further, but the initial guard already passed
      - 30x without Location header is treated as unsafe (raises)
      - relative Location values are resolved via urljoin against the current
        URL before validation
      - exceeding `max_redirects` raises UnsafeURLError
      - any unsafe hop raises UnsafeURLError
    """
    assert_safe_outbound(url)
    current = url
    session = _get_walker_session()
    for _ in range(max_redirects):
        try:
            response = session.head(
                current, allow_redirects=False, timeout=timeout
            )
        except requests.RequestException:
            # HEAD probe failed entirely. Cannot walk further.
            # Caller's existing post-download guard remains as defense
            # in depth.
            return current
        status = response.status_code
        if status < 300 or status >= 400:
            return current
        location = response.headers.get("Location")
        if not location:
            raise UnsafeURLError(
                f"redirect at {current!r} missing Location header"
            )
        next_url = urljoin(current, location)
        assert_safe_outbound(next_url)
        current = next_url
    raise UnsafeURLError(
        f"too many redirects (>{max_redirects}) starting at {url!r}"
    )
