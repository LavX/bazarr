# coding=utf-8
"""Browse a Sportarr server's file system through the owning instance client."""

import logging

import requests


def browse_sportarr_filesystem(path='', arr_client=None):
    """List directories on the owning Sportarr server, or None on transport error.

    Sportarr names its API routes without the ``/api/v3`` prefix Sonarr uses
    (``/api/system/status``, ``/api/rootfolder``, ``/api/tag``), so the
    filesystem route is ``/api/filesystem`` rather than ``/api/v3/filesystem``.
    The client carries the per-instance connection details and decrypted key;
    there is no scalar ``settings.sportarr`` fallback like Sonarr and Radarr
    have, so a missing client is refused rather than probing a default server.
    """
    if arr_client is None:
        logging.warning(
            "BAZARR Skipping Sportarr filesystem browse: no instance client")
        return None
    suffix = f"filesystem?path={path}&allowFoldersWithoutTrailingSlashes=true&includeFiles=false"
    try:
        r = arr_client.get(f"/api/{suffix}")
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        logging.exception("BAZARR Error trying to get filesystem from Sportarr. Http error.")
        return
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get filesystem from Sportarr. Connection Error.")
        return
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get filesystem from Sportarr. Timeout Error.")
        return
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get filesystem from Sportarr.")
        return

    return r.json()