# coding=utf-8
"""Per-instance Sonarr, Radarr and Sportarr HTTP client.

Builds a client from an arr_instances row (or raw params, for a pre-save
connection test). Mirrors the existing ``url_sonarr()`` URL shape and the
``X-Api-Key`` header so behaviour matches the single-instance path. The HTTP
getter is injectable so the connection test is unit-testable without network.
"""
from constants import HEADERS

from .repository import VALID_KINDS

_EMPTY_PORTS = (None, "", 0)


class ArrClient:
    def __init__(self, *, kind, ip, port, base_url="/", ssl=False,
                 verify_ssl=False, api_key="", http_timeout=60, http_get=None,
                 http_post=None):
        if kind not in VALID_KINDS:
            raise ValueError("invalid instance kind")
        self.kind = kind
        self.ip = ip
        self.port = port
        self.ssl = bool(ssl)
        self.verify_ssl = bool(verify_ssl)
        self.api_key = api_key
        self.http_timeout = http_timeout
        self._base_url_raw = base_url
        self._http_get = http_get  # None -> use the kind's shared session pool
        self._http_post = http_post

    def base_url(self):
        protocol = "https" if self.ssl else "http"
        base = self._base_url_raw or "/"
        if not base.startswith("/"):
            base = f"/{base}"
        if base.endswith("/"):
            base = base[:-1]
        port = "" if self.port in _EMPTY_PORTS else f":{self.port}"
        return f"{protocol}://{self.ip}{port}{base}"

    def _headers(self):
        return {**HEADERS, "X-Api-Key": self.api_key}

    def _session(self):
        # Reuse the kind's shared session pool (same retry config the legacy
        # sync path uses); imported lazily to avoid an import-time dependency.
        if self.kind == "radarr":
            from radarr.http_session import radarr_session
            return radarr_session()
        if self.kind == "sonarr":
            from sonarr.http_session import sonarr_session
            return sonarr_session()
        if self.kind == "sportarr":
            from sportarr.http_session import sportarr_session
            return sportarr_session()
        raise ValueError("invalid instance kind")

    def _session_get(self, url, **kwargs):
        return self._session().get(url, **kwargs)

    def get(self, path):
        """GET an absolute API path (e.g. '/api/v3/series/1') against this
        instance and return the raw requests.Response. Mirrors the legacy
        url_*() + shared-session call so default-instance behaviour is identical.
        """
        getter = self._http_get or self._session_get
        return getter(
            f"{self.base_url()}{path}",
            headers=self._headers(),
            timeout=int(self.http_timeout),
            verify=self.verify_ssl,
            **({"allow_redirects": False} if self.kind == "sportarr" else {}),
        )

    def _session_post(self, url, **kwargs):
        return self._session().post(url, **kwargs)

    def post(self, path, json=None):
        """POST to an absolute API path against this instance and return the raw
        requests.Response. Mirrors the legacy session.post call."""
        poster = self._http_post or self._session_post
        return poster(
            f"{self.base_url()}{path}",
            json=json,
            headers=self._headers(),
            timeout=int(self.http_timeout),
            verify=self.verify_ssl,
            **({"allow_redirects": False} if self.kind == "sportarr" else {}),
        )

    def test_connection(self):
        """Probe the kind's authenticated status endpoint; return a result dict."""
        try:
            path = "/api/system/status" if self.kind == "sportarr" else "/api/v3/system/status"
            resp = self.get(path)
        except Exception as exc:
            message = "Could not connect to the Sportarr instance" if self.kind == "sportarr" else str(exc)
            return {"ok": False, "error": "connection_failed", "message": message}

        status_code = getattr(resp, "status_code", None)
        if status_code == 401:
            return {"ok": False, "error": "unauthorized",
                    "message": "Invalid API key"}
        if isinstance(status_code, int) and status_code >= 400:
            return {"ok": False, "error": "http_error",
                    "message": f"HTTP {status_code}"}
        if self.kind == "sportarr" and status_code != 200:
            return {"ok": False, "error": "http_error",
                    "message": "Unexpected HTTP status from the Sportarr instance"}
        try:
            data = resp.json()
        except Exception:
            return {"ok": False, "error": "bad_response",
                    "message": "Non-JSON response from the instance"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "bad_response",
                    "message": "Unexpected response shape"}
        if self.kind == "sportarr" and (
                data.get("appName") != "Sportarr"
                or not isinstance(data.get("version"), str)
                or not data["version"].strip()):
            return {"ok": False, "error": "bad_response",
                    "message": "Expected Sportarr system status with a version"}
        return {"ok": True, "version": data.get("version"),
                "app_name": data.get("appName")}


class ArrClientFactory:
    """Builds ArrClient objects from instance rows or raw params."""

    def __init__(self, repository=None):
        self._repo = repository

    def for_instance(self, instance_id, *, http_get=None):
        if self._repo is None:
            raise ValueError("factory has no repository")
        row = self._repo.get(instance_id)
        if row is None:
            return None
        return self.from_row(row, http_get=http_get)

    def from_row(self, row, *, http_get=None):
        from secret_store import decrypt_secret

        try:
            api_key = decrypt_secret(row.api_key or "")
        except ValueError:
            # The stored key can't be decrypted (master key rotated/changed).
            # Build a client with no key - the call fails auth cleanly - rather
            # than letting the exception crash the whole sync/SignalR fan-out.
            import logging
            logging.error(
                "Cannot decrypt API key for %s instance id=%s (master key changed?); "
                "re-enter the key in Settings.", row.kind, getattr(row, "id", "?"))
            api_key = ""

        return ArrClient(
            kind=row.kind, ip=row.ip, port=row.port, base_url=row.base_url,
            ssl=bool(row.ssl), verify_ssl=bool(row.verify_ssl),
            api_key=api_key,
            http_timeout=row.http_timeout, http_get=http_get,
        )

    def from_params(self, *, kind, ip, port=None, base_url="/", ssl=False,
                    verify_ssl=False, api_key="", http_timeout=60, http_get=None):
        if kind == "sportarr" and port is None:
            port = 1867
        return ArrClient(
            kind=kind, ip=ip, port=port, base_url=base_url, ssl=ssl,
            verify_ssl=verify_ssl, api_key=api_key, http_timeout=http_timeout,
            http_get=http_get,
        )
