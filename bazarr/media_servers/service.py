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
        if libraries and row.kind != 'silo':
            raise MediaServerError('invalid_kind')
        key = '' if body.get('clear_api_key') else body.get('api_key') or repo.get_decrypted_api_key(instance_id)
        url, verify_ssl = body.get('url', row.url), body.get('verify_ssl', bool(row.verify_ssl))
        validate_server_url(url)
        if not key or not key.strip():
            raise MediaServerError('missing_credentials')
    except MediaServerError as error:
        return ({'data': [], 'error_code': error.code} if libraries else
                {'success': False, 'error_code': error.code}), 400
    if row.kind == 'emby':
        from emby.operations import emby_test_connection
        return emby_test_connection(url, key, verify_ssl), 200
    from silo.operations import silo_get_libraries, silo_test_connection
    return (silo_get_libraries if libraries else silo_test_connection)(url, key, verify_ssl), 200
