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
# What the Plex section ships with when nobody has configured it. Composing a
# URL out of these says nothing about where a server is, so it must never
# replace an address a destination already has.
_DEFAULT_ADDRESS = ('127.0.0.1', 32400, False)


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


def has_real_address(section):
    """Whether the account points somewhere, or just at the shipped defaults.

    Logging out resets ip, port and auth_method, so the next startup would
    otherwise compose http://127.0.0.1:32400 and write localhost over the
    address the destination actually had.
    """
    if getattr(section, 'auth_method', 'apikey') == 'oauth':
        return bool(getattr(section, 'server_url', ''))
    try:
        address = (getattr(section, 'ip', ''), int(getattr(section, 'port', 32400)),
                   bool(getattr(section, 'ssl', False)))
    except (TypeError, ValueError):
        return False
    return bool(address[0]) and address != _DEFAULT_ADDRESS


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

    Recorded by id rather than inferred, and never guessed. A sign-in adopts no
    row the account did not create: when the recorded id matches nothing,
    because disconnecting the destination deleted it, or because the account has
    never had one, the caller creates a fresh row instead of taking whichever
    Plex instance happens to be first. Falling back to that one handed the
    account a sibling somebody added by hand, and the next sign-in wrote its own
    URL and token over that server's.
    """
    saved = getattr(settings.plex, 'instance_id', '') or ''
    if not saved:
        return None
    return next((row for row in repo.list('plex') if row.id == saved), None)


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
    by the next startup. The startup import wants the failure instead, so it
    calls :func:`apply_plex_account`.
    """
    try:
        return apply_plex_account(session, settings, signed_in=signed_in,
                                  signed_out=signed_out, persist=persist)[0]
    except Exception:
        session.rollback()
        logging.warning('BAZARR could not carry the Plex account onto its destination; '
                        'retrying on the next account change or restart')
        logging.debug('Plex destination sync failed', exc_info=True)
        return None


def apply_plex_account(session, settings, *, signed_in=False, signed_out=False, persist=None):
    """The same work, raising, as ``(row, created)``.

    The startup import owns its own failure handling and has to tell a database
    error apart from an account with nothing configured, because only the first
    should leave the kind unstamped and blocked.
    """
    repo = MediaServerInstanceRepository(session)
    row = _account_row(repo, settings)
    url, credential = account_url(settings.plex), account_credential(settings.plex)
    created = False
    if row is None:
        if signed_out or not credential or not url:
            # Nothing to connect with or nowhere to connect to, so there is no
            # destination yet. Signing in comes before picking a server, which
            # is the normal order of the OAuth flow; the picker is the next step
            # and it calls back here with both.
            return None, False
        # The library scoping and the per-type opt-ins are seeded from the
        # scalars this once, because the account panel is where they were
        # configured. After that the instance form owns them, so a later
        # account change carries the connection and leaves them alone.
        row = repo.create(kind='plex', name=ACCOUNT_NAME, url=url, api_key=credential,
                          verify_ssl=bool(getattr(settings.plex, 'verify_ssl', False)),
                          enabled=getattr(settings.general, 'use_plex', False) is True,
                          **_seed(settings))
        created = True
    elif signed_out:
        repo.update(row.id, enabled=False, clear_api_key=True)
    else:
        fields = {}
        # A URL the account has not got yet is not a URL to write: signing in
        # again after a sign-out keeps the address the row already has until the
        # picker supplies a new one, and the shipped ip and port are not an
        # address at all.
        if url and has_real_address(settings.plex):
            fields['url'] = url
        # An unchanged credential is not resent, so a server switch cannot blank
        # the token by carrying an empty field along with the URL.
        if credential:
            fields['api_key'] = credential
        # Completing a sign-in is the user saying to use Plex, so it switches
        # the destination on outright rather than mirroring a scalar that the
        # handler may not have set.
        if signed_in and (fields.get('url') or row.url):
            fields['enabled'] = True
        if fields:
            repo.update(row.id, **fields)
    # Recorded even when nothing else changed, so the row the account just made
    # is the row it finds next time rather than one it has to guess at.
    recorded = getattr(settings.plex, 'instance_id', '') != row.id
    settings.plex.instance_id = row.id
    session.commit()
    if recorded and persist is not None:
        # The row is already saved, so a config write that fails is a lost
        # shortcut, not a lost destination: the owner falls back to the only
        # Plex row until the next transition records it again. Never worth
        # failing the reconcile, which would block the kind.
        try:
            persist()
        except Exception:
            logging.warning('BAZARR saved the Plex destination but could not record which '
                            'row the account owns; it will be recorded again on the next change')
            logging.debug('Recording the Plex destination owner failed', exc_info=True)
    _publish(session, settings, row.id)
    return row, created


def _publish(session, settings, instance_id):
    """Hand the saved row to the running workers, as an instance save does.

    Only to workers that exist. Building the shared configuration from here
    would freeze it at whatever import markers happen to be recorded at that
    instant, and during the startup import that is a partial set: the kind being
    synced is stamped after its step, and the kinds after it in the loop have
    not run. The singleton is cached for the life of the process, so it would
    block those kinds until the next restart. Left alone, it is built on first
    use, after every marker is in place.

    Best effort otherwise too: the row is what survives a restart, and a worker
    that never heard about this change picks it up from the database next boot
    rather than the account transition failing over it.
    """
    try:
        from . import dispatcher
        if dispatcher._configuration is None:
            return
        configuration = dispatcher.get_native_configuration()
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
