# coding=utf-8
"""Validated instance operations, publishing only durable saved snapshots."""

from .http import MediaServerError
from .instances import VALID_KINDS, validate_fields
from .repository import MediaServerInstanceRepository, atomic, to_safe_dict


def list_instances(session, kind=None):
    if kind is not None and kind not in VALID_KINDS:
        return {'error_code': 'invalid_kind'}, 400
    return {'data': [to_safe_dict(row) for row in MediaServerInstanceRepository(session).list(kind)]}, 200


def get_instance(session, instance_id):
    row = MediaServerInstanceRepository(session).get(instance_id)
    return (to_safe_dict(row), 200) if row else ({'error_code': 'not_found'}, 404)


def _write(session, operation, instance_id=None, body=None):
    from app.config import settings
    from .dispatcher import get_native_configuration
    repo = MediaServerInstanceRepository(session)
    try:
        if operation != 'delete':
            validate_fields(body, create=operation == 'create')
        configuration = get_native_configuration()
        # No staged changes are visible to workers, and failed writes retain
        # the old revision. Other destination operations resume unchanged.
        with configuration.lock:
            with atomic(session, durable=True):
                if operation == 'create':
                    if 'name' not in body or 'url' not in body:
                        raise MediaServerError('invalid_settings')
                    row = repo.create(**body)
                    instance_id = row.id
                elif operation == 'update':
                    row = repo.update(instance_id, **body)
                    if row is None:
                        return {'error_code': 'not_found'}, 404
                else:
                    if not repo.delete(instance_id):
                        return {'error_code': 'not_found'}, 404
                snapshot = repo.snapshot(instance_id, settings) if operation != 'delete' else None
                result = to_safe_dict(row) if snapshot else None
            if snapshot:
                configuration.publish(snapshot)
            else:
                configuration.delete(instance_id)
        return (None, 204) if operation == 'delete' else (result, 201 if operation == 'create' else 200)
    except MediaServerError as error:
        session.rollback()
        return {'error_code': error.code}, 400
    except Exception:
        session.rollback()
        return {'error_code': 'persistence_failed'}, 500


def create_instance(session, body):
    return _write(session, 'create', body=body)


def update_instance(session, instance_id, body):
    return _write(session, 'update', instance_id, body)


def delete_instance(session, instance_id):
    return _write(session, 'delete', instance_id)


def probe_instance(session, instance_id, body, *, libraries=False):
    from .http import validate_server_url
    repo = MediaServerInstanceRepository(session)
    row = repo.get(instance_id)
    if row is None:
        return {'error_code': 'not_found'}, 404
    try:
        validate_fields(body, probe=True)
        if libraries and row.kind == 'emby':
            raise MediaServerError('invalid_kind')
        key = '' if body.get('clear_api_key') else body.get('api_key') or repo.get_decrypted_api_key(instance_id)
        url, verify_ssl = body.get('url', row.url), body.get('verify_ssl', bool(row.verify_ssl))
        validate_server_url(url)
        if not key or not key.strip():
            raise MediaServerError('missing_credentials')
    except MediaServerError as error:
        return ({'data': [], 'error_code': error.code} if libraries else
                {'success': False, 'error_code': error.code}), 400
    result = _probe(row.kind, url, key, verify_ssl, libraries)
    # A test that used exactly what is saved has just learned this
    # destination's version, so the status page gets it without a probe of its
    # own. A test of unsaved edits describes some other connection and is not
    # recorded against the saved instance.
    if not libraries and not set(body or {}) & {'url', 'api_key', 'clear_api_key', 'verify_ssl'}:
        from .versions import record
        record(row.id, row.revision, result)
    return result, 200


def probe_connection(body, *, libraries=False):
    """Probe connection settings that have not been saved as an instance yet.

    One route for every kind, because which server is being added is a value in
    the request, not a different endpoint. The per-kind endpoints that predate
    the destination layer still answer for compatibility.
    """
    from .http import parse_verify_ssl, validate_server_url
    empty = {'data': [], 'error_code': None} if libraries else {'success': False}
    try:
        if not isinstance(body, dict) or set(body) - {'kind', 'url', 'apikey', 'verify_ssl'}:
            raise MediaServerError('invalid_settings')
        kind = body.get('kind')
        if kind not in VALID_KINDS:
            raise MediaServerError('invalid_kind')
        url, key = body.get('url'), body.get('apikey')
        if not all(isinstance(value, str) and value.strip() for value in (url, key)):
            raise MediaServerError('missing_credentials')
        validate_server_url(url)
        verify_ssl = parse_verify_ssl(body.get('verify_ssl', True))
        if libraries and kind == 'emby':
            raise MediaServerError('invalid_kind')
    except MediaServerError as error:
        return dict(empty, error_code=error.code), 400
    return _probe(kind, url, key, verify_ssl, libraries), 200


def _probe(kind, url, key, verify_ssl, libraries):
    if kind == 'emby':
        from emby.operations import emby_test_connection
        return emby_test_connection(url, key, verify_ssl)
    if kind == 'jellyfin':
        return _jellyfin_probe(url, key, verify_ssl, libraries)
    if kind == 'plex':
        return _plex_probe(url, key, verify_ssl, libraries)
    from silo.operations import silo_get_libraries, silo_test_connection
    return (silo_get_libraries if libraries else silo_test_connection)(url, key, verify_ssl)


def _jellyfin_probe(url, key, verify_ssl, libraries):
    """Jellyfin's own probe, in the shape the destination endpoints return.

    The singleton's callers expect `libraries`/`error_code`; the destination
    endpoints answer `data`/`error_code`, the same shape Silo already returns,
    so one library picker serves every kind.
    """
    from jellyfin.operations import jellyfin_get_libraries, jellyfin_test_connection
    if not libraries:
        return jellyfin_test_connection(url, key, verify_ssl=verify_ssl)
    result = jellyfin_get_libraries(url, key, verify_ssl=verify_ssl, include_all=True)
    return {'data': [{'id': row['id'], 'name': row['name'], 'type': row['type']}
                     for row in result['libraries']], 'error_code': result['error_code']}


def _plex_probe(url, key, verify_ssl, libraries):
    """Plex sections, addressed by name because that is what Plex refreshes by."""
    from plex.operations import plex_server_for, plex_server_identity
    types = {'movie': 'movies', 'show': 'series'}
    try:
        server = plex_server_for(url, key, bool(verify_ssl))
        # A section carries no id Plex will refresh by, so its title is the
        # handle, which is also what the scalar settings stored.
        data = [{'id': section.title, 'name': section.title,
                 'type': types.get(section.type, section.type)}
                for section in server.library.sections()]
        # The pooled client's own friendlyName and version were read when it
        # was built and are never re-read, so reporting them from the object
        # would pin an upgraded Plex to its old version forever. Ask the
        # server, over the connection that client already holds.
        name, version = ('', '') if libraries else plex_server_identity(url, key, bool(verify_ssl))
    except Exception:
        return ({'data': [], 'error_code': 'connection_error'} if libraries
                else {'success': False, 'error_code': 'connection_error'})
    if libraries:
        return {'data': data, 'error_code': None}
    return {'success': True, 'server_name': name, 'version': version}
