# coding=utf-8
"""Validated instance operations, publishing only durable saved snapshots."""

from .http import MediaServerError
from .instances import VALID_KINDS, validate_fields
from .repository import MediaServerInstanceRepository, atomic, to_safe_dict

# A stored key is never used with a destination the request changed: moving a
# destination means typing its key again.
REENTER_KEY = 'Re-enter the key to use a different address'
ACCOUNT_ROW_MESSAGE = ('This Plex instance follows the signed-in account, so its address '
                       'is chosen on the Plex card')
ACCOUNT_VERIFY_OFF_MESSAGE = ('This Plex instance follows the signed-in account, so certificate '
                              'verification cannot be turned off for it')
UNLISTED_MESSAGE = 'Plex does not list this server address for the signed-in account'


def _destination_changed(body, row):
    """Whether the request moves the destination away from its stored address.

    ``url`` decides where the stored key is sent, and so does ``verify_ssl``:
    turning certificate verification off hands the key to whatever answers in
    place of the server. Turning it back on only makes the connection harder to
    sit in front of, so it is not a move. A field the request leaves out is not
    a change.
    """
    url = body.get('url')
    if url is not None and url != row.url:
        return True
    verify_ssl = body.get('verify_ssl')
    return verify_ssl is not None and bool(row.verify_ssl) and not bool(verify_ssl)


def _account_row(session, row):
    """Whether this Plex row carries the signed-in account's credential.

    The same condition the snapshot check reads, so a probe or a save cannot
    stop being about the account's row merely because a dropped selection
    emptied the stored address or a settings save switched the method: the
    row still carries the account's token, so the rule that holds it to the
    plex.tv listing keeps applying to it.
    """
    from app.config import settings

    from .plex_account import account_owned_row
    if row.kind != 'plex':
        return False
    credential = MediaServerInstanceRepository(session).retained_api_key(row.id)
    return account_owned_row(settings.plex, row, credential)


def _refuse_account_row_destination(session, row, body, *, probe, account_owned=None):
    """The destination rule for the Plex row the signed-in account owns.

    That row carries the account token, which nobody can type again, so its
    address is the one plex.tv listed for the account and only the picker moves
    it: a request that would probe or save a different address for it is
    refused, and even the address as saved is probed only while plex.tv still
    lists it for the credential the row holds. Turning certificate verification
    off for it is refused too, for the same reason; turning it back on is the
    user hardening the connection and is allowed. The caller may hand in the
    ownership decision it already made, so a probe that captures the row's
    credential from that decision cannot decide a second time after the
    capture: an account transition landing in between cannot flip the rule
    mid-probe.
    """
    from app.config import settings

    from .plex_account import account_row_sendable
    if account_owned is None:
        account_owned = _account_row(session, row)
    if not account_owned:
        return
    url = body.get('url') if isinstance(body, dict) else None
    verify_ssl = body.get('verify_ssl') if isinstance(body, dict) else None
    if url is not None and url != row.url:
        raise MediaServerError('account_owned')
    if verify_ssl is not None and bool(row.verify_ssl) and not bool(verify_ssl):
        raise MediaServerError('account_verify_off')
    if probe and not (body.get('api_key') or body.get('clear_api_key')):
        credential = MediaServerInstanceRepository(session).get_decrypted_api_key(row.id)
        if not account_row_sendable(settings, row.url, credential=credential):
            raise MediaServerError('server_not_listed')


def _shown(row):
    """The row as the API shows it, including whether the Plex account owns it."""
    from app.config import settings
    from .plex_account import owns_row
    return {**to_safe_dict(row), 'account_owned': owns_row(settings, row.id)}


def list_instances(session, kind=None):
    if kind is not None and kind not in VALID_KINDS:
        return {'error_code': 'invalid_kind'}, 400
    return {'data': [_shown(row) for row in MediaServerInstanceRepository(session).list(kind)]}, 200


def get_instance(session, instance_id):
    row = MediaServerInstanceRepository(session).get(instance_id)
    return (_shown(row), 200) if row else ({'error_code': 'not_found'}, 404)


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
                # The snapshot is built with the account row held rather than
                # consulting plex.tv for it here: the consult can outlast the
                # write boundaries this save holds, the account and config
                # locks above it, the configuration lock and the database
                # transaction, so a name-only save would block unrelated
                # account transitions for the whole ask. The dispatcher's
                # read path asks plex.tv outside every one of those
                # boundaries and releases the row once the answer says the
                # credential may be sent, the same way the startup publishes
                # its rows.
                snapshot = (repo.snapshot(instance_id, settings, hold_account_row=True)
                            if operation != 'delete' else None)
                result = _shown(row) if snapshot else None
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
    # A save follows the same rule as a test: moving the destination without
    # the key typed again is refused rather than silently keeping the key.
    # The check and the write share the boundary the settings save commits
    # under, the account and config locks it holds, and the row is re-read
    # inside it: a save that paused between its check and its write cannot
    # land a move a second save's stored key enables, because the re-read
    # sees the key the second save stored in the window, and the refusal
    # names the same re-enter message the sequential path returns.
    from discover.metadata import CONFIG_LOCK
    from media_servers.plex_account import ACCOUNT_LOCK
    with ACCOUNT_LOCK, CONFIG_LOCK:
        if isinstance(body, dict):
            row = MediaServerInstanceRepository(session).get(instance_id)
            if row is not None:
                from .instances import validate_fields
                try:
                    # The validation error wins: a body that cannot be saved anyway
                    # is answered as invalid rather than as a destination refusal.
                    validate_fields(body)
                except MediaServerError as error:
                    return {'error_code': error.code}, 400
                try:
                    _refuse_account_row_destination(session, row, body, probe=False)
                except MediaServerError as error:
                    # The account's destination moves only on the Plex card, and
                    # it is the account's row that is being refused, not the save.
                    if error.code == 'account_owned':
                        return ({'error_code': 'account_owned',
                                 'message': ACCOUNT_ROW_MESSAGE}, 409)
                    if error.code == 'account_verify_off':
                        return ({'error_code': 'account_verify_off',
                                 'message': ACCOUNT_VERIFY_OFF_MESSAGE}, 409)
                    return ({'error_code': error.code, 'message': UNLISTED_MESSAGE}, 400)
                if (row.api_key and _destination_changed(body, row)
                        and not (body.get('api_key') or body.get('clear_api_key'))):
                    return {'error_code': 'reenter_key', 'message': REENTER_KEY}, 400
        return _write(session, 'update', instance_id, body)


def delete_instance(session, instance_id):
    from app.config import settings
    from .plex_account import owns_row
    if owns_row(settings, instance_id):
        # The next startup would rebuild it from the signed-in account, so it is
        # removed by disconnecting from Plex rather than by a delete.
        return {'error_code': 'account_owned'}, 409
    return _write(session, 'delete', instance_id)


def probe_instance(session, instance_id, body, *, libraries=False):
    from .http import validate_server_url
    repo = MediaServerInstanceRepository(session)
    row = repo.get(instance_id)
    if row is None:
        return {'error_code': 'not_found'}, 404
    try:
        validate_fields(body, probe=True)
        # One ownership decision carries the whole probe: the rule, the
        # credential capture and the plex.tv resolution all follow the one
        # decision made before any of them, so an account transition landing
        # between the capture and the send can neither send the captured token
        # to the stored spelling of a listed origin nor wave a plain row's
        # credential through the listing rule. The unfixed order decided again
        # after the capture, and a transition flipped the second decision.
        account_owned = _account_row(session, row)
        # The stored key is used only with the destination as it is saved: a
        # probe of a moved address needs the key typed again, and this route is
        # what the card's Test and the edit form's "Keep current key" mode call.
        # The account's own row is additionally held to the plex.tv listing:
        # its credential is the sign-in token, which nobody can type again.
        _refuse_account_row_destination(session, row, body, probe=True, account_owned=account_owned)
        if row.api_key and _destination_changed(body, row) and not body.get('api_key'):
            raise MediaServerError('reenter_key')
        if libraries and row.kind == 'emby':
            raise MediaServerError('invalid_kind')
        # The credential the probe sends comes from the row snapshot the
        # comparison used, never a fresh read: an address-and-key replacement
        # landing after the row was fetched cannot pair its key with the
        # address the request already had.
        key = '' if body.get('clear_api_key') else body.get('api_key') or repo.snapshot_api_key(row)
        url, verify_ssl = body.get('url', row.url), body.get('verify_ssl', bool(row.verify_ssl))
        validate_server_url(url)
        if not key or not key.strip():
            raise MediaServerError('missing_credentials')
        if account_owned:
            # The account token goes to the URI plex.tv listed for the row's
            # origin, never to the stored spelling of it; an origin plex.tv
            # does not list answers below.
            from app.config import settings

            from .plex_account import account_row_uri
            resolved = account_row_uri(settings, url, credential=key or None)
            if resolved is None:
                raise MediaServerError('server_not_listed')
            url = resolved
    except MediaServerError as error:
        refusal = ({'data': [], 'error_code': error.code} if libraries else
                   {'success': False, 'error_code': error.code})
        messages = {'reenter_key': REENTER_KEY,
                    'account_owned': ACCOUNT_ROW_MESSAGE,
                    'account_verify_off': ACCOUNT_VERIFY_OFF_MESSAGE,
                    'server_not_listed': UNLISTED_MESSAGE}
        if error.code in messages:
            refusal['message'] = messages[error.code]
        return refusal, 400
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
