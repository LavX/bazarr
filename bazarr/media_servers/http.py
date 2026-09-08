# coding=utf-8
"""Bounded authenticated HTTP without proxy or redirect credential forwarding."""

import json as json_module
from urllib.parse import urlsplit, urlunsplit

import requests
from urllib3.exceptions import ReadTimeoutError


class MediaServerError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_server_url(url: str) -> str:
    if not isinstance(url, str) or not url or any(char.isspace() or ord(char) < 32 for char in url):
        raise MediaServerError("invalid_url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "?" in url or "#" in url or "\\" in url):
            raise ValueError
        parsed.port
    except ValueError:
        raise MediaServerError("invalid_url") from None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def parse_verify_ssl(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise MediaServerError("invalid_verify_ssl")


class MediaServerHTTP:
    RESPONSE_LIMIT = 2 * 1024 * 1024

    def __init__(self, url: str, *, verify_ssl: bool = True, headers=None):
        self.url = validate_server_url(url)
        self.verify_ssl = parse_verify_ssl(verify_ssl)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Accept": "application/json", **(headers or {})})

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _request(self, method, path, *, params=None, json=None, success_statuses=(200,)):
        # API paths are local to this validated base, including any proxy prefix.
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise MediaServerError("invalid_url")
        try:
            with self.session.request(method, self.url + path, params=params, json=json,
                                      timeout=(3, 10), verify=self.verify_ssl,
                                      allow_redirects=False, stream=True) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise MediaServerError("redirect_denied")
                if status not in success_statuses:
                    code = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(status)
                    raise MediaServerError(code or ("server_error" if status >= 500 else "request_rejected"))
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        size = int(length)
                    except ValueError:
                        raise MediaServerError("invalid_response") from None
                    if size < 0:
                        raise MediaServerError("invalid_response")
                    if size > self.RESPONSE_LIMIT:
                        raise MediaServerError("response_too_large")
                body = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if len(body) + len(chunk) > self.RESPONSE_LIMIT:
                        raise MediaServerError("response_too_large")
                    body.extend(chunk)
                return bytes(body)
        except requests.exceptions.SSLError:
            raise MediaServerError("tls_error") from None
        except requests.exceptions.Timeout:
            raise MediaServerError("timeout") from None
        except requests.exceptions.ConnectionError as error:
            # Requests wraps urllib3 read timeouts raised during iter_content.
            code = "timeout" if error.args and isinstance(error.args[0], ReadTimeoutError) else "connection_error"
            raise MediaServerError(code) from None
        except requests.exceptions.RequestException:
            raise MediaServerError("connection_error") from None

    def request_json(self, method, path, *, params=None, json=None, success_statuses=(200,)):
        body = self._request(method, path, params=params, json=json, success_statuses=success_statuses)
        try:
            return json_module.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise MediaServerError("invalid_response") from None

    def request_empty(self, method, path, *, params=None, json=None, success_statuses=(200, 204)):
        body = self._request(method, path, params=params, json=json, success_statuses=success_statuses)
        if body:
            raise MediaServerError("invalid_response")
