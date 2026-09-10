# coding=utf-8

from media_servers.http import MediaServerError

from .client import SiloClient


def silo_test_connection(url: str, apikey: str, verify_ssl: bool = True) -> dict:
    try:
        with SiloClient(url, apikey, verify_ssl) as client:
            return client.test_connection()
    except MediaServerError as error:
        return {"success": False, "error_code": error.code}
    except Exception:
        return {"success": False, "error_code": "connection_error"}


def silo_get_libraries(url: str, apikey: str, verify_ssl: bool = True) -> dict:
    try:
        with SiloClient(url, apikey, verify_ssl) as client:
            return {"data": client.get_libraries(), "error_code": None}
    except MediaServerError as error:
        return {"data": [], "error_code": error.code}
    except Exception:
        return {"data": [], "error_code": "connection_error"}
