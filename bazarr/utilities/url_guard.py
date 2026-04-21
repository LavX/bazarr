from __future__ import annotations
import ipaddress
import socket
from urllib.parse import urlparse

class UnsafeURLError(ValueError):
    """Outbound URL is rejected by the SSRF guard."""

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_TLD_SUFFIXES = (".local", ".internal")


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
