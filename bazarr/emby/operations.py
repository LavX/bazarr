# coding=utf-8

from .client import EmbyClient
from media_servers.http import MediaServerError


def emby_test_connection(url: str, apikey: str, verify_ssl: bool = True) -> dict:
    try:
        with EmbyClient(url, apikey, verify_ssl) as client:
            return client.test_connection()
    except MediaServerError as error:
        return {"success": False, "error_code": error.code}
    except Exception:
        return {"success": False, "error_code": "connection_error"}
