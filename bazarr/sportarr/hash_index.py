"""Durable recording hashes, built by background jobs and read by Hub requests."""

import hashlib
import json
import os
import stat
import struct
import unicodedata

from sqlalchemy import select

from app.config import settings
from app.database import database, TableSportsEvents, TableSportsFileIndex
from sportarr.connection import check_cancelled, connection_identity, owner_sync_lock, revalidate
from sportarr.db import sports_transaction
from sportarr.sync.leagues import require_sportarr
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings


class SportsIndexPending(RuntimeError):
    """The recording index cannot yet prove a deterministic selection."""


class _RecordingChanged(ValueError):
    """A physical-file race, distinct from owner/configuration cancellation."""


def basename(value):
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFC", value.replace("\\", "/").rsplit("/", 1)[-1].strip()).casefold()


def connection_fingerprint(instance):
    return hashlib.sha256(repr((connection_identity(instance), read_sports_mappings(instance.path_mappings))).encode()).hexdigest()


def file_stat(value):
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def recording_hash(path, expected, cancel=None):
    """Two bounded reads from one verified regular-file descriptor."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or file_stat(before) != expected:
            raise _RecordingChanged("Sports recording changed during indexing")
        value = before.st_size
        blocks = [stream.read(min(65536, before.st_size))]
        check_cancelled(cancel)
        if before.st_size > 65536:
            stream.seek(max(0, before.st_size - 65536))
            blocks.append(stream.read(65536))
        for block in blocks:
            value += sum(item[0] for item in struct.iter_unpack("<Q", block[:len(block) // 8 * 8]))
        if file_stat(os.fstat(stream.fileno())) != expected:
            raise _RecordingChanged("Sports recording changed during indexing")
    check_cancelled(cancel)
    return f"{value & 0xFFFFFFFFFFFFFFFF:016x}"


def _verify_file(path, physical_path, expected):
    try:
        unchanged = file_stat(os.stat(path)) == expected and os.path.realpath(path) == physical_path
    except OSError as exc:
        raise _RecordingChanged("Sports recording became unavailable during indexing") from exc
    if not unchanged:
        raise _RecordingChanged("Sports recording changed during indexing")


def refresh_recording(event_id, arr_instance_id, *, session=None, cancel=None):
    """Return False only for a physical race; authority changes still abort."""
    session = database if session is None else session
    instance = require_sportarr(session, arr_instance_id)
    expected = connection_identity(instance)
    fingerprint = connection_fingerprint(instance)
    row = session.get(TableSportsEvents, event_id, populate_existing=True)
    if row is None or row.arr_instance_id != arr_instance_id:
        return
    identity = (row.file_id, row.path, row.sceneName)
    mapped = apply_sports_mapping(row.path, read_sports_mappings(instance.path_mappings))
    values = dict(event_id=event_id, arr_instance_id=arr_instance_id,
                  file_id=row.file_id, original_path=row.path, scene_name=row.sceneName,
                  connection=fingerprint, original_name=basename(row.path),
                  mapped_name=basename(mapped), release_name=basename(row.sceneName),
                  physical_path=os.path.realpath(mapped), stamp=None, moviehash=None)
    cached = session.get(TableSportsFileIndex, event_id, populate_existing=True)
    unstable = False
    try:
        check_cancelled(cancel)
        physical = os.stat(mapped)
        if not stat.S_ISREG(physical.st_mode):
            raise OSError("Sports recording is not a regular file")
        stamp = file_stat(physical)
        values['stamp'] = json.dumps(stamp)
        same = (cached is not None and cached.file_id == row.file_id and cached.original_path == row.path
                and cached.connection == fingerprint and cached.physical_path == values['physical_path']
                and cached.stamp == values['stamp'] and cached.moviehash is not None)
        values['moviehash'] = cached.moviehash if same else recording_hash(values['physical_path'], stamp, cancel)
        _verify_file(mapped, values['physical_path'], stamp)
    except _RecordingChanged:
        unstable = True
    except OSError:
        # An unavailable/non-regular recording is a completed negative result.
        # It is retried by the periodic job, but cannot hold other hashes hostage.
        values['stamp'] = values['moviehash'] = None
    check_cancelled(cancel)
    with sports_transaction(session) as transaction:
        current_owner = revalidate(transaction, arr_instance_id, expected, cancel)
        if connection_fingerprint(current_owner) != fingerprint:
            raise ValueError("Sports mapping changed during recording indexing")
        current = transaction.get(TableSportsEvents, event_id, populate_existing=True, with_for_update=True)
        if current is None:
            return
        if current.arr_instance_id != arr_instance_id:
            raise ValueError("Sports recording owner changed during indexing")
        if (current.file_id, current.path, current.sceneName) != identity:
            raise ValueError("Sports recording changed during indexing")
        if not unstable and values['stamp'] is not None:
            try:
                _verify_file(mapped, values['physical_path'], stamp)
            except _RecordingChanged:
                unstable = True
        indexed = transaction.get(TableSportsFileIndex, event_id)
        if unstable:
            # A race leaves the hash unknown. Clear only this recording's stale
            # derived row after proving its owner and metadata still match.
            if indexed is not None:
                transaction.delete(indexed)
        elif indexed is None:
            transaction.add(TableSportsFileIndex(**values))
        else:
            for name, value in values.items():
                setattr(indexed, name, value)
        transaction.flush()
        check_cancelled(cancel)
    return not unstable


def indexing_enabled():
    return settings.general.use_sportarr or (settings.compat_endpoint.enabled and settings.compat_endpoint.serve_local_subs)


class _IndexSignal:
    def __init__(self, parent):
        self.parent = parent

    def is_set(self):
        return not indexing_enabled() or self.parent is not None and self.parent.is_set()


def refresh_recording_index(arr_instance_id, event_ids=None, *, session=None, cancel=None, scheduled=False):
    """Restart-safe keyset scan. No recording bytes are read in request threads."""
    session = database if session is None else session
    if scheduled:
        cancel = _IndexSignal(cancel)
    with owner_sync_lock(arr_instance_id, cancel):
        cursor = 0
        while True:
            check_cancelled(cancel)
            require_sportarr(session, arr_instance_id)
            query = select(TableSportsEvents.id).where(TableSportsEvents.arr_instance_id == arr_instance_id,
                                                       TableSportsEvents.id > cursor)
            if event_ids is not None:
                query = query.where(TableSportsEvents.id.in_(event_ids))
            ids = session.execute(query.order_by(TableSportsEvents.id).limit(200)).scalars().all()
            if not ids:
                return
            for event_id in ids:
                for _ in range(2):
                    check_cancelled(cancel)
                    if refresh_recording(event_id, arr_instance_id, session=session, cancel=cancel) is not False:
                        break
            cursor = ids[-1]
