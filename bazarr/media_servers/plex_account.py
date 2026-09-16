# coding=utf-8
"""The Plex account surface owning the Plex destination row.

Plex is the one kind whose connection is not typed into the instance form. The
OAuth flow hands back a token, the server picker hands back a URL, and both land
in the scalar settings the account panel still reads. The row the refresh
dispatcher works from has to follow them, or a user who signs in gets a
destination with no credential and no refreshes, and no field to paste one into.

So every account transition calls through here: sign-in, an API key saved by
hand, a server selected, the automatic apikey-to-OAuth migration, and sign-out.
The row is the account's, identified by ``settings.plex.instance_id``; a second
Plex instance someone adds by hand is their own and is never touched.
"""

import logging

from .repository import MediaServerInstanceRepository

ACCOUNT_NAME = 'Plex'


def account_url(section):
    """Whichever endpoint the configured authentication method points at."""
    if getattr(section, 'auth_method', 'apikey') == 'oauth':
        url = getattr(section, 'server_url', '')
        return url if isinstance(url, str) else ''
    ip = getattr(section, 'ip', '')
    if not isinstance(ip, str) or not ip:
        return ''
    try:
        port = int(getattr(section, 'port', 32400))
    except (TypeError, ValueError):
        return ''
    return f"{'https' if getattr(section, 'ssl', False) else 'http'}://{ip}:{port}"


def account_credential(section):
    """The token or the API key, whichever the account is authenticating with."""
    oauth = getattr(section, 'auth_method', 'apikey') == 'oauth'
    value = getattr(section, 'token' if oauth else 'apikey', '')
    return value if isinstance(value, str) else ''


def _seed(settings):
    """The refresh scoping the account panel held, for a row being created."""
    from .backfill import _legacy_values
    try:
        values = _legacy_values(settings, 'plex')
    except Exception:
        return {}
    return {key: values[key] for key in ('refresh_movies', 'refresh_episodes', 'options')}


def _account_row(repo, settings):
    """The row this account owns, or nothing when it has never had one.

    Recorded by id rather than inferred, so adding a second Plex instance by
    hand cannot silently hand the account someone else's row.
    """
    rows = repo.list('plex')
    saved = getattr(settings.plex, 'instance_id', '') or ''
    return next((row for row in rows if row.id == saved), rows[0] if rows else None)


def sync_plex_instance(session, settings, *, signed_in=False, signed_out=False, persist=None):
    """Carry the account's URL and credential onto its destination row.

    Idempotent: startup and every account transition call it, and it creates the
    row only when the account has something to connect with. ``signed_out``
    clears the credential and switches the row off rather than deleting it, so a
    user who signs back in keeps the libraries and toggles they chose.

    What it carries onto an existing row is the connection and nothing else.
    ``verify_ssl`` is written only when the row is created; on an ordinary
    reconcile it is the user's own instance setting, and rewriting it from the
    scalars would undo a change they made at the next startup. ``enabled`` is
    the same, with two exceptions that are the account speaking rather than the
    reconcile: ``signed_out`` switches the destination off, and ``signed_in``
    switches it back on, because a user completing a sign-in is saying to use
    Plex. Switching servers while signed in is neither, and leaves it alone.

    ``persist`` writes the config once the owner id changes, so the row this
    account owns survives a restart instead of being re-guessed from whichever
    Plex row happens to sort first.

    Never raises into a caller that was doing something else: an account change
    that could not reach the database is logged and retried by the next one, and
    by the next startup.
    """
    try:
        repo = MediaServerInstanceRepository(session)
        row = _account_row(repo, settings)
        url, credential = account_url(settings.plex), account_credential(settings.plex)
        verify_ssl = bool(getattr(settings.plex, 'verify_ssl', False))
        enabled = getattr(settings.general, 'use_plex', False) is True
        if row is None:
            if signed_out or not credential or not url:
                # Nothing to connect with or nowhere to connect to, so there is
                # no destination yet. Signing in comes before picking a server,
                # which is the normal order of the OAuth flow; the picker is the
                # next step and it calls back here with both.
                return None
            # The library scoping and the per-type opt-ins are seeded from the
            # scalars this once, because the account panel is where they were
            # configured. After that the instance form owns them, so a later
            # account change carries the connection and leaves them alone.
            row = repo.create(kind='plex', name=ACCOUNT_NAME, url=url, api_key=credential,
                              verify_ssl=verify_ssl, enabled=enabled, **_seed(settings))
        elif signed_out:
            repo.update(row.id, enabled=False, clear_api_key=True)
        else:
            fields = {}
            # A URL the account has not got yet is not a URL to write: signing
            # in again after a sign-out keeps the address the row already has
            # until the picker supplies a new one.
            if url:
                fields['url'] = url
            # An unchanged credential is not resent, so a server switch cannot
            # blank the token by carrying an empty field along with the URL.
            if credential:
                fields['api_key'] = credential
            if signed_in and (url or row.url):
                fields['enabled'] = enabled
            if not fields:
                return row
            repo.update(row.id, **fields)
        recorded = getattr(settings.plex, 'instance_id', '') != row.id
        settings.plex.instance_id = row.id
        session.commit()
        if recorded and persist is not None:
            persist()
        _publish(session, settings, row.id)
        return row
    except Exception:
        session.rollback()
        logging.warning('BAZARR could not carry the Plex account onto its destination; '
                        'retrying on the next account change or restart')
        logging.debug('Plex destination sync failed', exc_info=True)
        return None


def _publish(session, settings, instance_id):
    """Hand the saved row to the running workers, as an instance save does.

    Best effort on purpose: the row is what survives a restart, and a worker
    that never heard about this change picks it up from the database next boot
    rather than the account transition failing over it.
    """
    try:
        from .dispatcher import get_native_configuration
        configuration = get_native_configuration()
        snapshot = MediaServerInstanceRepository(session).snapshot(instance_id, settings)
        if snapshot is not None:
            with configuration.lock:
                configuration.publish(snapshot)
    except Exception:
        logging.debug('BAZARR could not publish the Plex account destination to the '
                      'running workers', exc_info=True)


def sync_plex_account(*, signed_in=False, signed_out=False):
    """The request-side entry point, on the app's own session.

    The account handlers write the config before calling here, so the owner id
    this records needs a write of its own to reach disk.
    """
    from app.config import settings, write_config
    from app.database import database
    return sync_plex_instance(database, settings, signed_in=signed_in,
                              signed_out=signed_out, persist=write_config)
