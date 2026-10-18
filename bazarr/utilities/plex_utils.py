# coding=utf-8

import logging
import requests
from app.config import settings


def get_plex_libraries_with_paths():
    """
    Get Plex library sections with their location paths.
    Returns a dictionary with movie_paths and series_paths arrays.
    
    This utility requires OAuth authentication and is used for Autopulse integration.
    """
    try:
        # Only works with OAuth authentication
        if settings.plex.get('auth_method') != 'oauth':
            return {'movie_paths': [], 'series_paths': []}

        # The account token goes only to an address plex.tv lists for the
        # signed-in account, and to the URI plex.tv listed for it, never to the
        # stored string on its word. Without a verified address nothing is sent,
        # and the credential the listing was verified with is the one this
        # path sends, so a sign-in landing between the two cannot swap it.
        # The credential, the destination and the TLS choice are one coherent
        # capture: the credential and the choice are read together under the
        # account lock, the one every account save holds, and the destination
        # is the listing the plex.tv ask captures for that same credential and
        # confirms under the same lock, so the selection the account stores
        # moves only through it. The ask runs outside the lock, and the
        # captured credential and choice are confirmed before any send, so an
        # account transition landing between the capture and the send can
        # neither carry its replacement token under the weaker choice the
        # capture read nor send the captured token once the account left it
        # behind.
        from plex.operations import plex_account_verify_ssl
        from media_servers.plex_account import (ACCOUNT_LOCK, account_credential,
                                                verified_server_addresses)
        with ACCOUNT_LOCK:
            verify = plex_account_verify_ssl()
            captured_token = account_credential(settings.plex)
        server_addresses, status, verified_token = verified_server_addresses(settings)
        if status != 'ok' or not server_addresses or not verified_token:
            return {'movie_paths': [], 'series_paths': []}
        with ACCOUNT_LOCK:
            # The captured account, confirmed against what is stored now: a
            # credential or a choice that moved while plex.tv was being asked
            # is re-read on the next call instead of being sent what an older
            # account was captured with.
            if (account_credential(settings.plex) != captured_token
                    or plex_account_verify_ssl() != verify):
                return {'movie_paths': [], 'series_paths': []}

        # Get library sections from the first listed connection that answers.
        response = None
        server_url = None
        for address in server_addresses:
            try:
                candidate = requests.get(
                    f"{address}/library/sections",
                    headers={'X-Plex-Token': verified_token, 'Accept': 'application/json'},
                    timeout=5,
                    verify=verify,
                    allow_redirects=False,
                )
                if candidate.status_code == 200:
                    response, server_url = candidate, address
                    break
            except Exception:
                logging.debug('BAZARR could not reach a listed Plex connection')
        if response is None:
            return {'movie_paths': [], 'series_paths': []}

        data = response.json()
        if 'MediaContainer' not in data or 'Directory' not in data['MediaContainer']:
            return {'movie_paths': [], 'series_paths': []}

        movie_paths = []
        series_paths = []

        # Process library sections to extract paths
        for section in data['MediaContainer']['Directory']:
            section_type = section.get('type')
            section_key = section.get('key')
            if section_type in ['movie', 'show']:
                locations = _get_library_locations(server_url, section_key, verified_token, verify)
                if section_type == 'movie':
                    movie_paths.extend(locations)
                elif section_type == 'show':
                    series_paths.extend(locations)
        
        return {
            'movie_paths': movie_paths,
            'series_paths': series_paths
        }
        
    except Exception as e:
        logging.debug(f"BAZARR could not fetch Plex library paths: {e}")  # noqa: G004
        return {'movie_paths': [], 'series_paths': []}


def _get_library_locations(server_url, section_key, token, verify):
    """Get the locations for a specific Plex library section."""
    try:
        response = requests.get(
            f"{server_url}/library/sections/{section_key}",
            headers={'X-Plex-Token': token, 'Accept': 'application/json'},
            timeout=5,
            verify=verify,
            allow_redirects=False,
        )
        
        if response.status_code == 200:
            data = response.json()
            if 'MediaContainer' in data and 'Directory' in data['MediaContainer']:
                directories = data['MediaContainer']['Directory']
                if directories and len(directories) > 0:
                    directory = directories[0]
                    locations = directory.get('Location', [])
                    if isinstance(locations, list):
                        return [loc.get('path', '') for loc in locations if loc.get('path')]
                    elif isinstance(locations, dict):
                        path = locations.get('path', '')
                        return [path] if path else []
        return []
    except Exception as e:
        logging.debug(f"Failed to get locations for library section {section_key}: {e}")  # noqa: G004
        return []
