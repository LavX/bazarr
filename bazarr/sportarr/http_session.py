# coding=utf-8
"""Shared Sportarr request pool with per-request authentication."""
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session: requests.Session | None = None
_session_lock = threading.Lock()


class _SportarrSession(requests.Session):
    def __init__(self):
        super().__init__()
        # Inherited proxies and netrc authentication must not redirect or
        # replace an instance's explicitly configured connection and API key.
        self.trust_env = False
        # The API key never follows a redirect: a server that answers with one
        # is not the instance the key belongs to, and the next hop is never
        # contacted. Belt to the per-request refusal below, so a caller that
        # forgets it cannot reopen the path.
        self.max_redirects = 0

    def request(self, method, url, **kwargs):
        # requests can forward X-Api-Key to another origin on redirects.
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


def sportarr_session() -> requests.Session:
    """Reuse connections; callers supply API keys and TLS settings per request."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                session = _SportarrSession()
                adapter = HTTPAdapter(
                    pool_connections=20,
                    pool_maxsize=50,
                    max_retries=Retry(total=3, backoff_factor=0.3,
                                      status_forcelist=(502, 503, 504)),
                )
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                _session = session
    return _session
