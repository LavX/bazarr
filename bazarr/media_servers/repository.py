# coding=utf-8
"""Destination persistence and the encrypted credential boundary."""

import json
import logging
from contextlib import contextmanager
from uuid import UUID, uuid4

from sqlalchemy import select

from app.database import TableMediaServerInstances
from .http import MediaServerError
from .instances import (ConnectionSnapshot, TOGGLES, normalized_external_url, parse_options,
                        validate_connection, validate_fields, validate_options)


@contextmanager
def atomic(session, *, durable=False):
    """Use a real transaction before SAVEPOINT, including AUTOCOMMIT engines.

    SQLite's legacy transaction mode and PostgreSQL AUTOCOMMIT do not start a
    transaction for SAVEPOINT on our behalf. Inspect the driver transaction,
    then explicitly own BEGIN/COMMIT only when no transaction exists yet.
    """
    connection = session.connection()
    driver = connection.connection.driver_connection
    if connection.dialect.name == 'sqlite':
        active = driver.in_transaction
    else:
        active = driver.info.transaction_status != 0
    own = not active
    if own:
        connection.exec_driver_sql('BEGIN')
    try:
        with session.begin_nested():
            yield
        if own:
            connection.exec_driver_sql('COMMIT')
        elif durable:
            session.commit()
    except BaseException:
        if own:
            connection.exec_driver_sql('ROLLBACK')
        session.expire_all()
        raise


def _valid_id(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _encrypted(value):
    from secret_store import encrypt_secret, persist_master_key
    if value:
        persist_master_key()
    return encrypt_secret(value)


class MediaServerInstanceRepository:
    def __init__(self, session):
        self.session = session

    def get(self, instance_id):
        if not _valid_id(instance_id):
            return None
        return self.session.get(TableMediaServerInstances, instance_id, populate_existing=True)

    def list(self, kind=None):
        statement = select(TableMediaServerInstances).execution_options(populate_existing=True)
        if kind is not None:
            statement = statement.where(TableMediaServerInstances.kind == kind)
        return list(self.session.execute(statement.order_by(TableMediaServerInstances.kind,
                                                           TableMediaServerInstances.id)).scalars())

    def get_decrypted_api_key(self, instance_id):
        from secret_store import decrypt_secret
        row = self.get(instance_id)
        if row is None:
            return None
        try:
            return decrypt_secret(row.api_key)
        except ValueError:
            raise MediaServerError('missing_credentials') from None

    def snapshot_api_key(self, row):
        """The row's own stored key as plaintext, read from the row itself.

        The probe routes decide on the row they fetched, and the credential
        one sends is that same snapshot's, never a fresh read: an
        address-and-key replacement landing after the fetch cannot pair the
        new key with the address the request already had.
        """
        from secret_store import decrypt_secret
        try:
            return decrypt_secret(row.api_key)
        except ValueError:
            raise MediaServerError('missing_credentials') from None

    def retained_api_key(self, instance_id):
        """The saved key as plaintext, or nothing when it can no longer be read.

        A database restored without its master key leaves ciphertext that
        decrypts to nothing usable. That is a destination holding no
        credentials, not a request the user is forbidden to make: the enabled
        checks below still refuse to run it, and turning it off has to stay
        possible.
        """
        try:
            return self.get_decrypted_api_key(instance_id)
        except MediaServerError:
            return ''

    def values(self, row, *, decrypt_key=True):
        return dict(kind=row.kind, name=row.name, enabled=bool(row.enabled), url=row.url,
                    verify_ssl=bool(row.verify_ssl), api_key=self.retained_api_key(row.id) if decrypt_key else '',
                    path_mappings=json.loads(row.path_mappings),
                    refresh_movies=bool(row.refresh_movies), refresh_episodes=bool(row.refresh_episodes),
                    options=parse_options(row.options), external_url=row.external_url or '')

    def create(self, kind, name, **fields):
        values = dict(kind=kind, name=name, enabled=False, url='', verify_ssl=True, api_key='',
                      path_mappings=[], refresh_movies=True, refresh_episodes=True, options={},
                      external_url='')
        values.update(fields)
        validate_fields(dict(kind=kind, name=name, **fields), create=True)
        values.pop('clear_api_key', None)
        validate_connection(values)
        return self.import_values(values)

    def import_values(self, values):
        """Legacy structurally valid drafts may be incomplete; keep every value."""
        encrypted = _encrypted(values['api_key'])
        with atomic(self.session):
            row = TableMediaServerInstances(id=str(uuid4()), kind=values['kind'], name=values['name'],
                                           enabled=int(values['enabled']), url=values['url'],
                                           verify_ssl=int(values['verify_ssl']), api_key=encrypted,
                                           path_mappings=json.dumps(values['path_mappings']),
                                           refresh_movies=int(values.get('refresh_movies', True)),
                                           refresh_episodes=int(values.get('refresh_episodes', True)),
                                           options=json.dumps(values.get('options') or {}),
                                           external_url=normalized_external_url(values.get('external_url')),
                                           revision=1)
            self.session.add(row)
            self.session.flush()
        return row

    def update(self, instance_id, **fields):
        validate_fields(fields)
        row = self.get(instance_id)
        if row is None:
            return None
        values = self.values(row, decrypt_key=not (fields.get('api_key') or fields.get('clear_api_key')))
        if fields.get('clear_api_key'):
            values['api_key'] = ''
        values.update({key: value for key, value in fields.items()
                       if key != 'clear_api_key' and (key != 'api_key' or value)})
        if 'options' in fields:
            # An update carries no kind, so the closed key set is checked here,
            # against the kind the row already has.
            validate_options(row.kind, fields['options'])
        try:
            validate_connection(values)
        except MediaServerError:
            # Retained values only have to hold up while the destination is on.
            # A key that no longer decrypts, or a legacy import that never had a
            # URL, would otherwise make the destination impossible to switch
            # off: every field this request actually submits was validated by
            # validate_fields above, so nothing new gets in this way.
            if values['enabled']:
                raise
        encrypted = _encrypted(values['api_key']) if fields.get('api_key') else row.api_key
        if fields.get('clear_api_key'):
            encrypted = ''
        with atomic(self.session):
            for key in ('name', 'url', 'enabled', 'verify_ssl', *TOGGLES):
                setattr(row, key, (int(values[key]) if key in {'enabled', 'verify_ssl', *TOGGLES}
                                   else values[key]))
            row.api_key = encrypted
            row.path_mappings = json.dumps(values['path_mappings'])
            row.options = json.dumps(values.get('options') or {})
            if 'external_url' in fields:
                # Only a submitted value is rewritten, so a stored one is never
                # re-judged by an edit that did not touch it.
                row.external_url = normalized_external_url(fields['external_url'])
            row.revision += 1
            self.session.flush()
        return row

    def delete(self, instance_id):
        row = self.get(instance_id)
        if row is None:
            return False
        with atomic(self.session):
            self.session.delete(row)
            self.session.flush()
        return True

    def snapshot(self, instance_id, settings, *, hold_account_row=False):
        row = self.get(instance_id)
        if row is None:
            return None
        error = None
        try:
            key = self.get_decrypted_api_key(row.id)
        except MediaServerError as exc:
            key, error = '', exc.code
        mappings = json.loads(row.path_mappings)
        try:
            validate_connection(dict(kind=row.kind, name=row.name, enabled=bool(row.enabled), url=row.url,
                                     api_key=key, path_mappings=mappings, options=parse_options(row.options)))
        except MediaServerError as exc:
            error = error or exc.code
        # The refresh workers send the signed-in account's token to this row's
        # address, so the account row reaches them only while plex.tv lists
        # that address for the credential the row would actually send, and
        # only with the URI plex.tv listed for it rather than the stored
        # spelling. The row is the account's by the credential it holds, not
        # by any setting a save can write, so a one-field save switching the
        # method or emptying the recorded owner cannot lift the rule; the row
        # and the settings can disagree while a token replacement has not
        # carried onto the row yet, and that mismatch holds the refresh rather
        # than sending a credential plex.tv was never asked about, and so does
        # an empty one. ``sendable`` is kept apart from ``enabled``, which stays
        # what the user switched, so a held refresh reads as held and not as a
        # destination somebody disabled. ``hold_account_row`` skips the
        # consult, which can reach plex.tv: a caller that must not hold its
        # lock for a network ask gets the account's row held instead, and the
        # read path asks plex.tv outside that lock and releases the row once
        # the answer says the credential may be sent; a row held that way is
        # waiting rather than refused, so it is not the warning's to say.
        sendable = True
        url = row.url
        if row.kind == 'plex':
            section = getattr(settings, 'plex', None)
            if section is not None:
                from .plex_account import account_credential, account_owned_row, account_row_uri
                if account_owned_row(section, row, key or None):
                    settings_credential = account_credential(section)
                    listed = (None if hold_account_row else
                              account_row_uri(settings, row.url, credential=key or None))
                    sendable = (bool(row.api_key) and bool(settings_credential)
                                and (key or '') == settings_credential
                                and listed is not None)
                    if sendable:
                        url = listed
                    if not sendable and not hold_account_row:
                        logging.info('BAZARR is holding the Plex account refreshes while plex.tv '
                                     'does not list the saved server address for the signed-in '
                                     'account; choose the server again on the Plex card')
        return ConnectionSnapshot(row.id, row.kind, row.name, bool(row.enabled),
                                  getattr(settings.general, 'use_' + row.kind) is True,
                                  url, key, bool(row.verify_ssl),
                                  tuple(tuple(sorted(item.items())) for item in mappings),
                                  bool(row.refresh_movies), bool(row.refresh_episodes),
                                  json.dumps(parse_options(row.options)), row.revision, error,
                                  sendable=sendable, external_url=row.external_url or '')

    def snapshots(self, settings, *, kinds=None, hold_account_row=False):
        return [self.snapshot(row.id, settings, hold_account_row=hold_account_row)
                for row in self.list() if kinds is None or row.kind in kinds]


def to_safe_dict(row):
    return dict(id=row.id, kind=row.kind, name=row.name, enabled=bool(row.enabled), url=row.url,
                verify_ssl=bool(row.verify_ssl), api_key_set=bool(row.api_key),
                path_mappings=json.loads(row.path_mappings),
                refresh_movies=bool(row.refresh_movies), refresh_episodes=bool(row.refresh_episodes),
                options=parse_options(row.options), external_url=row.external_url or '')
