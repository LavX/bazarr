"""Ask an owner's Sportarr to rescan its library after a subtitle write."""

import logging
from contextlib import ContextDecorator
from contextvars import ContextVar
from threading import Thread


def _rescan_request(owner, *, client_factory=None):
    """Perform one whole-library rescan request for the owner."""
    from app.database import database
    from arr_instances.client import ArrClientFactory
    from sportarr.sync.leagues import require_sportarr

    instance = require_sportarr(database, owner)
    factory = client_factory if client_factory is not None else ArrClientFactory()
    client = factory.from_row(instance)
    response = client.post('/api/library/rescan')
    if response.status_code != 200:
        logging.warning(
            "Sportarr rescan answered HTTP %s for owner %s",
            response.status_code, owner,
        )


class _RescanBatch:
    """Owners a batch has asked to rescan, merged on exit."""

    def __init__(self):
        self.owners = set()
        self.media_servers = {}


_batch = ContextVar('sportarr_rescan_batch', default=None)


class rescan_batch(ContextDecorator):
    """Coalesce whole-library rescans across one operation and its nested work.

    A wanted or league download can publish several events of one owner in a
    single operation, and every publication asks for a rescan. POST
    /api/library/rescan is untargeted: one rescan covers every event the
    operation just wrote, so the operation is the unit the request is
    deduplicated against.
    """

    def __enter__(self):
        self._token = _batch.set(_RescanBatch()) if _batch.get() is None else None
        return self

    def _recreate_cm(self):
        return type(self)()

    def __exit__(self, *_exc):
        if self._token is None:
            return False
        batch = _batch.get()
        _batch.reset(self._token)
        # Published files still need refreshes when subsequent work fails or
        # is cancelled. Per-file dispatcher notifications are never deferred.
        for refresh in batch.media_servers:
            _refresh_media_server(refresh)
        if batch.owners:
            Thread(
                target=_rescan_batch_dispatch,
                args=(tuple(batch.owners),),
                name="sportarr-library-rescan", daemon=True,
            ).start()
        return False


def _refresh_media_server(refresh):
    try:
        refresh()
    except Exception:
        logging.exception('Could not refresh a sports media-server library')


def request_media_server_refresh(refresh):
    """Run a configured server's library refresh once at the operation boundary."""
    batch = _batch.get()
    if batch is None:
        _refresh_media_server(refresh)
    else:
        batch.media_servers[refresh] = None


def _rescan_batch_dispatch(owners):
    for owner in owners:
        try:
            _rescan_request(owner)
        except Exception:
            logging.exception(
                "Could not notify Sportarr to rescan its library for owner %s",
                owner,
            )


def notify_rescan(arr_instance_id, *, client_factory=None):
    """Request one whole-library rescan on the owner's Sportarr, non-blocking.

    POST /api/library/rescan walks every configured root folder and takes
    around a second on a real library, so the request never runs on the
    caller's thread. Inside a ``rescan_batch`` the request is recorded and a
    single request is sent per affected owner when the batch completes;
    outside one, the request is sent immediately. Either way it is
    fire-and-forget: a rescan that fails is logged, never raised, because the
    subtitle write has already succeeded and must not be reported as failed
    for it.
    """
    batch = _batch.get()
    if batch is not None:
        batch.owners.add(arr_instance_id)
        return

    def run():
        try:
            _rescan_request(arr_instance_id, client_factory=client_factory)
        except Exception:
            logging.exception(
                "Could not notify Sportarr to rescan its library for owner %s",
                arr_instance_id,
            )

    Thread(target=run, name='sportarr-library-rescan', daemon=True).start()
