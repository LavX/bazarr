# coding=utf-8

import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session: requests.Session | None = None
_session_lock = threading.Lock()


def sonarr_session() -> requests.Session:
    """Lazily-initialized module-level requests.Session for all Sonarr API
    calls.

    Reusing a single Session lets urllib3 keep its connection pool alive
    across calls, so the TCP+TLS handshake happens once per (scheme, host,
    port) instead of once per request. On a 1000-show library sync this
    saves ~1000 handshakes.

    The pool is keyed inside urllib3 by (scheme, host, port), so a URL
    change from the user's settings is picked up automatically: stale
    pool entries are evicted by urllib3 over time, no manual rebuild
    needed. ``verify=`` and ``headers=`` are still passed per-call by
    the caller because those values are settings-dependent and change
    at runtime.

    The retry policy is intentionally narrow: only retry the canonical
    transient gateway statuses (502/503/504), never 4xx, to preserve the
    existing no-retry-on-client-error behaviour of the call sites.
    """
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=20,
                    pool_maxsize=50,
                    max_retries=Retry(
                        total=3,
                        backoff_factor=0.3,
                        status_forcelist=(502, 503, 504),
                    ),
                )
                s.mount('http://', adapter)
                s.mount('https://', adapter)
                _session = s
    return _session
