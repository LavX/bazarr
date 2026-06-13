# coding=utf-8

import os
import requests
import logging

from app.config import settings, get_ssl_verify
from utilities.path_mappings import path_mappings
from app.database import TableMoviesRootfolder, TableMovies, database, delete, update, insert, select
from arr_instances.resolution import default_instance_id
from radarr.http_session import radarr_session
from radarr.info import radarr_headers, url_api_radarr


def get_radarr_rootfolder(arr_instance_id=None, arr_client=None):
    apikey_radarr = settings.radarr.apikey
    radarr_rootfolder = []

    try:
        if arr_client is not None:
            rootfolder = arr_client.get("/api/v3/rootfolder")
        else:
            rootfolder = radarr_session().get(f"{url_api_radarr()}rootfolder", timeout=int(settings.radarr.http_timeout),
                                              verify=get_ssl_verify('radarr'), headers=radarr_headers(apikey_radarr))
    except requests.exceptions.ConnectionError:
        logging.exception("BAZARR Error trying to get rootfolder from Radarr. Connection Error.")
        return []
    except requests.exceptions.Timeout:
        logging.exception("BAZARR Error trying to get rootfolder from Radarr. Timeout Error.")
        return []
    except requests.exceptions.RequestException:
        logging.exception("BAZARR Error trying to get rootfolder from Radarr.")
        return []
    else:
        # Resolve the owning instance: the explicit one when instance-scoped,
        # else the enabled default. owner=None on a pre-backfill install leaves
        # the ownership/split columns NULL (legacy behaviour).
        owner = arr_instance_id if arr_instance_id is not None \
            else default_instance_id(database, 'radarr')

        # Scope the existing-movies lookup and the rootfolder table to the
        # owning instance when one is supplied; with no instance, the statements
        # are unscoped exactly as before (default-instance behaviour unchanged).
        movies_paths_stmt = select(TableMovies.path)
        db_rootfolder_stmt = select(TableMoviesRootfolder.id, TableMoviesRootfolder.path)
        if arr_instance_id is not None:
            movies_paths_stmt = movies_paths_stmt.where(TableMovies.arr_instance_id == arr_instance_id)
            db_rootfolder_stmt = db_rootfolder_stmt.where(TableMoviesRootfolder.arr_instance_id == arr_instance_id)

        for folder in rootfolder.json():
            if any(item.path.startswith(folder['path']) for item in database.execute(movies_paths_stmt).all()):
                radarr_rootfolder.append({'id': folder['id'], 'path': folder['path']})  # noqa: PERF401
        db_rootfolder = database.execute(db_rootfolder_stmt).all()
        rootfolder_to_remove = [x for x in db_rootfolder if not
                                next((item for item in radarr_rootfolder if item['id'] == x.id), False)]
        rootfolder_to_update = [x for x in radarr_rootfolder if
                                next((item for item in db_rootfolder if item.id == x['id']), False)]
        rootfolder_to_insert = [x for x in radarr_rootfolder if not
                                next((item for item in db_rootfolder if item.id == x['id']), False)]

        for item in rootfolder_to_remove:
            stmt = delete(TableMoviesRootfolder).where(TableMoviesRootfolder.id == item.id)
            if arr_instance_id is not None:
                stmt = stmt.where(TableMoviesRootfolder.arr_instance_id == arr_instance_id)
            database.execute(stmt)
        for item in rootfolder_to_update:
            stmt = (update(TableMoviesRootfolder).values(path=item['path'])
                    .where(TableMoviesRootfolder.id == item['id']))
            if arr_instance_id is not None:
                stmt = stmt.where(TableMoviesRootfolder.arr_instance_id == arr_instance_id)
            database.execute(stmt)
        for item in rootfolder_to_insert:
            # Stamp ownership + the upstream id on every insert (default path
            # included) so new rootfolders are owned, closing the same
            # NULL-accumulation gap INC4 closed for media. local_rootfolder_id is
            # the autoincrement PK and MUST NOT be forced to the upstream id:
            # rootfolder id=1 is the universal default in every Radarr, so two
            # instances would collide on the PK and abort the second one's sync.
            # Let it autoincrement; upstream_rootfolder_id (unique per instance)
            # carries the server-side id.
            values = {'id': item['id'], 'path': item['path']}
            if owner is not None:
                values.update(arr_instance_id=owner,
                              upstream_rootfolder_id=item['id'])
            database.execute(insert(TableMoviesRootfolder).values(**values))


def check_radarr_rootfolder(arr_instance_id=None, arr_client=None):
    # Route the rootfolder fetch + ownership through the same instance the sync
    # is running for. With no instance/client this is today's default-instance
    # path (scalar settings + unscoped), byte-identical. (#156)
    get_radarr_rootfolder(arr_instance_id=arr_instance_id, arr_client=arr_client)
    # Only re-check the rootfolders owned by this instance: the accessibility
    # update keys on the upstream rootfolder id, which collides across instances,
    # so an unscoped write would clobber another instance's rows.
    rootfolder_stmt = select(TableMoviesRootfolder.id, TableMoviesRootfolder.path)
    if arr_instance_id is not None:
        rootfolder_stmt = rootfolder_stmt.where(TableMoviesRootfolder.arr_instance_id == arr_instance_id)
    rootfolder = database.execute(rootfolder_stmt).all()
    for item in rootfolder:
        root_path = item.path
        if not root_path.endswith(('/', '\\')):
            if root_path.startswith('/'):
                root_path += '/'
            else:
                root_path += '\\'
        mapped_path = path_mappings.path_replace_movie(root_path)
        if not os.path.isdir(mapped_path):
            accessible, error = 0, ('This Radarr root directory does not seem to be accessible by Bazarr. '
                                    'Please check path mapping or if directory/drive is online.')
        # Try os.access() first (fast, no disk I/O); fall back to an actual write
        # test only if os.access() fails (e.g. NFS mounts).
        elif os.access(mapped_path, os.W_OK):
            accessible, error = 1, ''
        else:
            try:
                test_file = os.path.join(mapped_path, '.bazarr_write_test')
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
            except Exception as e:
                accessible, error = 0, f"There's an issue with this Radarr root directory: {repr(e)}"
            else:
                accessible, error = 1, ''
        stmt = (update(TableMoviesRootfolder)
                .values(accessible=accessible, error=error)
                .where(TableMoviesRootfolder.id == item.id))
        if arr_instance_id is not None:
            stmt = stmt.where(TableMoviesRootfolder.arr_instance_id == arr_instance_id)
        database.execute(stmt)
