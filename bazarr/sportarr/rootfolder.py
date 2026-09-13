"""Reconcile root folders and accessibility for one enabled sports owner."""
import os

from sqlalchemy import delete, select

from app.database import database, TableSportsLeaguesRootfolder
from arr_instances.client import ArrClientFactory
from sportarr.connection import connection_identity, owner_sync_lock, revalidate
from sportarr.db import sports_transaction
from sportarr.parser import positive_id
from sportarr.sync.leagues import _get_list, notify, require_sportarr
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings


def sync_rootfolders(arr_instance_id, *, cancel=None, expected_connection=None, http_get=None):
    with owner_sync_lock(arr_instance_id, cancel):
        instance = require_sportarr(database, arr_instance_id)
        expected = connection_identity(instance)
        if expected_connection is not None and expected_connection != expected:
            raise ValueError('Sportarr connection changed during synchronization')
        mapping = read_sports_mappings(instance.path_mappings)
        factory = ArrClientFactory()
        client = factory.from_row(instance, http_get=http_get) if http_get else factory.from_row(instance)
        payload = _get_list(client, '/api/rootfolder')
        ids, paths, parsed = set(), set(), []
        for item in payload:
            if (not isinstance(item, dict) or not positive_id(item.get('id'))
                    or not isinstance(item.get('path'), str) or not item['path'].strip()
                    or item['id'] in ids or item['path'] in paths):
                raise ValueError('Malformed or duplicate Sportarr root folder')
            ids.add(item['id'])
            paths.add(item['path'])
            path = apply_sports_mapping(item['path'], mapping)
            error = ''
            if not os.path.isdir(path):
                error = 'This Sportarr root directory is not accessible. Check its path mapping.'
            elif not os.access(path, os.W_OK):
                error = 'Bazarr cannot write to this directory.'
            parsed.append((item, error))
        with sports_transaction(database) as transaction:
            revalidate(transaction, arr_instance_id, expected, cancel)
            existing = {row.rootfolder_id: row for row in transaction.execute(
                select(TableSportsLeaguesRootfolder).where(
                    TableSportsLeaguesRootfolder.arr_instance_id == arr_instance_id)).scalars()}
            for item, error in parsed:
                row = existing.get(item['id'])
                if row is None:
                    row = TableSportsLeaguesRootfolder(arr_instance_id=arr_instance_id, rootfolder_id=item['id'])
                    transaction.add(row)
                row.path, row.accessible, row.error = item['path'], int(not error), error
            transaction.flush()
            transaction.execute(delete(TableSportsLeaguesRootfolder).where(
                TableSportsLeaguesRootfolder.arr_instance_id == arr_instance_id,
                TableSportsLeaguesRootfolder.rootfolder_id.not_in(ids)))
        notify([])


def list_rootfolders(session, arr_instance_id):
    instance = require_sportarr(session, arr_instance_id)
    mapping = read_sports_mappings(instance.path_mappings)
    return {'data': [row.to_dict() | {'mapped_path': apply_sports_mapping(row.path, mapping)}
                     for row in session.execute(select(TableSportsLeaguesRootfolder).where(
                         TableSportsLeaguesRootfolder.arr_instance_id == arr_instance_id)
                         .order_by(TableSportsLeaguesRootfolder.id)).scalars()]}


def list_rootfolder_paths(session, arr_instance_id):
    """The remote root folder paths known for one owner, in sync order.

    The file browser seeds its initial listing from these: the first thing a
    path-mapping editor asks for is the set of roots Sportarr already knows,
    which is exactly what a mapping is drawn between. Empty when the root
    folders have not been synced yet, so the browser falls back to a live
    filesystem listing.
    """
    return [row for row in session.execute(
        select(TableSportsLeaguesRootfolder.path)
        .where(TableSportsLeaguesRootfolder.arr_instance_id == arr_instance_id)
        .order_by(TableSportsLeaguesRootfolder.id)).scalars().all() if row]
