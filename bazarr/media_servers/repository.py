# coding=utf-8
"""Destination persistence and the encrypted credential boundary."""

import json
from contextlib import contextmanager
from uuid import UUID, uuid4

from sqlalchemy import select

from app.database import TableMediaServerInstances
from .http import MediaServerError
from .instances import ConnectionSnapshot, validate_connection, validate_fields


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

    def values(self, row, *, decrypt_key=True):
        return dict(kind=row.kind, name=row.name, enabled=bool(row.enabled), url=row.url,
                    verify_ssl=bool(row.verify_ssl), api_key=self.get_decrypted_api_key(row.id) if decrypt_key else '',
                    path_mappings=json.loads(row.path_mappings))

    def create(self, kind, name, **fields):
        values = dict(kind=kind, name=name, enabled=False, url='', verify_ssl=True, api_key='', path_mappings=[])
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
                                           path_mappings=json.dumps(values['path_mappings']), revision=1)
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
        validate_connection(values)
        encrypted = _encrypted(values['api_key']) if fields.get('api_key') else row.api_key
        if fields.get('clear_api_key'):
            encrypted = ''
        with atomic(self.session):
            for key in ('name', 'url', 'enabled', 'verify_ssl'):
                setattr(row, key, int(values[key]) if key in {'enabled', 'verify_ssl'} else values[key])
            row.api_key = encrypted
            row.path_mappings = json.dumps(values['path_mappings'])
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

    def snapshot(self, instance_id, settings):
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
                                     api_key=key, path_mappings=mappings))
        except MediaServerError as exc:
            error = error or exc.code
        return ConnectionSnapshot(row.id, row.kind, row.name, bool(row.enabled),
                                  getattr(settings.general, 'use_' + row.kind) is True,
                                  row.url, key, bool(row.verify_ssl),
                                  tuple(tuple(sorted(item.items())) for item in mappings), row.revision, error)

    def snapshots(self, settings, *, kinds=None):
        return [self.snapshot(row.id, settings) for row in self.list() if kinds is None or row.kind in kinds]


def to_safe_dict(row):
    return dict(id=row.id, kind=row.kind, name=row.name, enabled=bool(row.enabled), url=row.url,
                verify_ssl=bool(row.verify_ssl), api_key_set=bool(row.api_key),
                path_mappings=json.loads(row.path_mappings))
