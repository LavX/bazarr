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
import time

from threading import RLock

from .repository import MediaServerInstanceRepository

ACCOUNT_NAME = 'Plex'

# Every write to the account's own settings, from a sign-in, a sign-out, an
# API key saved by hand or a server picked, runs under this lock, and so does
# the write phase of a verification: a plex.tv answer describes the account it
# was asked about, and the settings it lands on have to be that account's
# still, not whichever account signed in while the answer was in flight.
ACCOUNT_LOCK = RLock()
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


def owns_row(settings, instance_id):
    """Whether this is the row the signed-in Plex account would rebuild.

    The startup import recreates the account's row whenever the account has a
    credential and an address, so deleting it only lasts until the next restart.
    Disconnecting from Plex clears the credential, and after that it deletes
    like any other row.
    """
    saved = getattr(settings.plex, 'instance_id', '') or ''
    return (bool(saved) and saved == instance_id
            and bool(account_credential(settings.plex)) and bool(account_url(settings.plex)))


def account_owned_row(section, row, credential=None):
    """Whether the signed-in account's credential sits on this Plex row.

    Decided by the credential the row holds rather than by any setting a save
    can write: a one-field save can switch ``plex.auth_method`` or empty the
    recorded owner id, and neither changes which credential the row would
    send, so neither may lift the rule that holds it to the plex.tv listing.
    A row holding the stored sign-in token is the account's whatever the
    method says. The recorded owner id stays the owner under the sign-in
    method as well, for the window in which a replacement credential has not
    carried onto the row yet; it is written by the account itself and a
    settings save cannot type it in.
    """
    if getattr(row, 'kind', None) != 'plex':
        return False
    token = getattr(section, 'token', '') or ''
    if isinstance(credential, str) and credential and isinstance(token, str) and token \
            and credential == token:
        return True
    return ((getattr(section, 'instance_id', '') or '') == row.id
            and getattr(section, 'auth_method', 'apikey') == 'oauth')


# --- The plex.tv answer a stored address is trusted with -----------------------

# plex.tv is asked for the signed-in account's own servers the way the server
# listing route asks (`plex.tv/pms/resources`), and the answer is remembered in
# memory only: no settings save can write it. It belongs to the credential and
# the server it was asked about, so any change to the Plex token, the Plex API
# key or `plex.auth_method` drops it and the next use asks again.
PLEX_TV_RESOURCES_URL = 'https://plex.tv/pms/resources'

# A plex.tv answer of 401 or 403: the account has to sign in again. Kept apart
# from an unreachable plex.tv, which is asked again on the next use, so a
# refused account neither wipes its stored selection nor gets a retry.
SIGN_IN_AGAIN = 'sign-in-again'

# How long an unreachable plex.tv is remembered, so reading the account row
# does not block on a network call on every use while plex.tv is down.
UNREACHABLE_CACHE_SECONDS = 60

# (credential key, answer, expiry): one tuple, assigned and read atomically, so
# a worker never reads one account's origins with another account's credential.
# The answer is an (origins, uris) pair, or None while plex.tv cannot be asked.
_plex_tv_answer = None


def forget_plex_tv_answer():
    """Drop the remembered plex.tv answer (tests swap credentials in place)."""
    global _plex_tv_answer
    _plex_tv_answer = None


def credential_key(method, credential, machine_id):
    """What a plex.tv answer belongs to: the credential and the server."""
    return (str(method), str(credential or ''), str(machine_id or ''))


def plex_tv_server_origins(token, machine_id):
    """The connections plex.tv lists for the account's own server.

    ``(origins, uris)``: every origin plex.tv lists for the server, each mapped
    to the exact URI plex.tv returned, which is the only string a credential is
    ever sent to. ``None`` when plex.tv cannot be asked or its answer cannot be
    read: nothing is then sent, nothing is wiped, and the next use asks again.
    :data:`SIGN_IN_AGAIN` when plex.tv answers 401 or 403: nothing is sent or
    wiped either, and the caller asks the user to sign in again.
    """
    import requests

    try:
        response = requests.get(
            PLEX_TV_RESOURCES_URL,
            headers={'X-Plex-Token': token, 'Accept': 'application/json'},
            params={'includeHttps': '1', 'includeRelay': '1'},
            allow_redirects=False, timeout=10)
    except Exception:
        logging.debug('BAZARR could not reach plex.tv to list the account servers', exc_info=True)
        return None
    status = getattr(response, 'status_code', None)
    if status in (401, 403):
        return SIGN_IN_AGAIN
    if status != 200:
        return None
    return _server_origins_from_answer(response, machine_id)


def _server_origins_from_answer(response, machine_id):
    """Read the resources answer the way the server listing route does.

    ``(origins, uris)`` for the account's own server, or ``None`` for an answer
    that cannot be read (a captive portal, a proxy error page): that is plex.tv
    not answering, not plex.tv listing nothing.
    """
    import xml.etree.ElementTree as ET

    from utilities.security_guards import normalized_origin

    devices = []
    try:
        content_type = (getattr(response, 'headers', None) or {}).get('content-type', '')
        if 'application/json' in content_type:
            data = response.json()
            if not isinstance(data, list):
                # A JSON answer that is not a list of resources (a captive
                # portal, a proxy error page) is plex.tv not answering.
                return None
            devices = data
        elif 'xml' in content_type:
            root = ET.fromstring(response.text)
            if root.tag != 'MediaContainer':
                # A parseable document that is not a resource listing (a
                # captive portal, a proxy error page) is plex.tv not
                # answering, not plex.tv listing nothing.
                return None
            devices = [{'clientIdentifier': device.get('clientIdentifier'),
                        'provides': device.get('provides'),
                        'owned': device.get('owned') == '1',
                        'connections': [{'uri': connection.get('uri')}
                                        for connection in device.findall('Connection')]}
                       for device in root.findall('Device')]
        else:
            # An answer in no format this reader knows is plex.tv not
            # answering, not plex.tv listing nothing.
            return None
    except Exception:
        logging.debug('BAZARR could not read the plex.tv server listing', exc_info=True)
        return None
    origins, uris = set(), {}
    for device in devices:
        if (isinstance(device, dict) and device.get('clientIdentifier') == machine_id
                and device.get('provides') == 'server' and device.get('owned')):
            for connection in device.get('connections', []):
                uri = connection.get('uri') if isinstance(connection, dict) else None
                origin = normalized_origin(uri or '')
                if origin and origin not in uris:
                    origins.add(origin)
                    uris[origin] = uri
    return frozenset(origins), uris


def remembered_listed(method, credential, machine_id):
    """The remembered plex.tv answer for this credential and server, or a new ask.

    Nothing is remembered for a refused credential: the next use asks plex.tv
    again, so a fresh sign-in is picked up without a restart. An unreachable
    one is remembered only briefly, so the account row does not block on a
    network call on every use while plex.tv is down.
    """
    global _plex_tv_answer
    key = credential_key(method, credential, machine_id)
    answer = _plex_tv_answer
    if answer is not None and answer[0] == key:
        result, expiry = answer[1], answer[2]
        if result is not None or (expiry or 0) > time.monotonic():
            return result
    result = plex_tv_server_origins(credential, machine_id)
    if result is SIGN_IN_AGAIN:
        return SIGN_IN_AGAIN
    _plex_tv_answer = ((key, result, None) if result is not None
                       else (key, None, time.monotonic() + UNREACHABLE_CACHE_SECONDS))
    return result


def remember_listed_origins(method, credential, machine_id, result):
    """Record the plex.tv answer a selection was just verified against."""
    global _plex_tv_answer
    _plex_tv_answer = (credential_key(method, credential, machine_id), result, None)


def stored_server_addresses(section):
    """The stored selection's addresses, the selected URL first."""
    primary = getattr(section, 'server_url', '') or ''
    addresses = [primary] if primary else []
    connections = getattr(section, 'server_connections', None) or []
    for url in connections:
        if isinstance(url, str) and url and url not in addresses:
            addresses.append(url)
    return addresses


def _persist_selection(section, persist):
    """Write the config after the selection changed, if a writer was given."""
    if persist is not None:
        try:
            persist()
        except Exception:
            logging.warning('BAZARR could not write the changed Plex server selection',
                            exc_info=True)


def _clear_server_selection(section, persist):
    """Drop the stored selection: the card asks the user to choose again."""
    section.server_machine_id = ''
    section.server_name = ''
    section.server_url = ''
    section.server_local = False
    section.server_connections = []
    _persist_selection(section, persist)


def _sync_row_address(settings):
    """Carry a rewritten selection onto the account's destination row.

    The refresh workers, the probe and the status page read the row's address,
    so a stored selection that was rewritten to the URI plex.tv listed takes
    the row with it. Best effort only: a database that cannot be reached logs
    and lets the next account change or startup try again.
    """
    if not (getattr(settings.plex, 'instance_id', '') or ''):
        # No row was recorded yet, so the startup import builds it from the
        # rewritten selection rather than this.
        return None
    try:
        from app.database import database
        return sync_plex_instance(database, settings)
    except Exception:
        logging.debug('BAZARR could not carry the rewritten Plex server address onto '
                      'its destination row', exc_info=True)
        return None


def verified_server_addresses(settings, *, persist=None):
    """The stored Plex server addresses plex.tv lists, with the credential it
    listed them for.

    Returns ``(addresses, status, credential)``. ``status`` is ``'ok'``, or
    ``'unreachable'`` when plex.tv cannot be asked and ``'sign-in'`` when it
    refuses the credential: on both the credential is sent nowhere, the stored
    selection is kept, and the caller says which of the two it was. The
    ``credential`` is the exact one the listing was verified with, so a caller
    that sends it cannot rotate to a different one between the verification and
    the send; senders use it rather than reading the token again.

    No stored string is trusted on its word, and none is sent to either: each
    stored address only says which of the account's connections is meant, and
    what the credential goes to is the URI plex.tv itself listed for that
    connection. The answer is remembered in memory for the life of the
    credential, so this costs one plex.tv call per start; any change to the
    token, the API key or ``plex.auth_method`` drops it and the next use asks
    again. When the selected address is one plex.tv does not list, it is
    dropped, the unlisted fallbacks are dropped with it and the Plex card asks
    the user to choose the server again. A stored selection that normalizes to
    a listed origin but spells it differently is rewritten to the URI plex.tv
    listed, on the settings and on the destination row that reads them.

    The account and its selection are read once and written to only when they
    are still what is stored: the plex.tv ask can outlast a sign-in landing in
    the meantime, and an older account's answer never erases a newer account's
    selection. When the account changes under an ask it is asked again for the
    account there now, and a caller that keeps changing hears plex.tv could
    not be read and sends nothing. An address a save landed meanwhile is kept
    the same way: the selected URL and its connections are rechecked under the
    lock, so an address-only save is not rewritten from the older answer's
    snapshot either.
    """
    from utilities.security_guards import normalized_origin

    section = settings.plex
    for _attempt in range(3):
        stored = stored_server_addresses(section)
        credential = account_credential(section)
        if not stored or not credential:
            return [], 'ok', ''
        method = getattr(section, 'auth_method', 'apikey')
        machine_id = getattr(section, 'server_machine_id', '') or ''
        listed = remembered_listed(method, credential, machine_id)
        if listed is SIGN_IN_AGAIN:
            return [], 'sign-in', credential
        if listed is None:
            return [], 'unreachable', credential
        origins, uris = listed
        with ACCOUNT_LOCK:
            # Synchronized with every account transition, so nothing between
            # the check and the writes can be a sign-in landing instead.
            if (account_credential(section) != credential
                    or (getattr(section, 'server_machine_id', '') or '') != machine_id):
                continue
            if stored_server_addresses(section) != stored:
                # An address-only save changes neither the credential nor the
                # server, so it slips past the check above while the ask is
                # still out: the selection is read again rather than rewritten
                # from the addresses the answer was asked about.
                continue
            kept = [uris[normalized_origin(url)] for url in stored if normalized_origin(url) in uris]
            primary = getattr(section, 'server_url', '') or ''
            primary_origin = normalized_origin(primary)
            if primary and (primary_origin is None or primary_origin not in origins):
                _clear_server_selection(section, persist)
                logging.warning('BAZARR dropped the stored Plex server address plex.tv does not list; '
                                'choose the server again on the Plex card')
                return [], 'ok', ''
            # The URI plex.tv listed is the one string the credential is ever
            # sent to, so a stored spelling of the same origin gives way to it:
            # the settings and the row that reads them keep pointing where
            # plex.tv pointed.
            if primary and primary != uris[primary_origin]:
                section.server_url = uris[primary_origin]
                _persist_selection(section, persist)
                _sync_row_address(settings)
            # A fallback plex.tv does not list is removed with the same write,
            # so it cannot come back as the address a refresh falls over to.
            if kept and kept != list(getattr(section, 'server_connections', None) or []):
                section.server_connections = list(kept)
                _persist_selection(section, persist)
            return kept, 'ok', credential
    logging.warning('BAZARR could not read one Plex account\'s server listing; the account kept '
                    'changing under the asks, so nothing was sent and nothing was wiped')
    return [], 'unreachable', account_credential(section)


def account_row_uri(settings, url, credential=None):
    """The URI plex.tv listed for this row address, or None.

    A pure consult of the per-credential plex.tv answer, for the destination
    row the account owns: it never mutates settings. The credential verified
    against is the one the caller says would be sent, defaulting to the
    account's own, so a row holding something else is never waved through on
    the settings' answer. The returned URI is the string plex.tv itself
    returned, so a legacy spelling of a listed origin does not become the
    address the credential is sent to. None while plex.tv cannot be reached,
    refuses the credential or lists no such origin: the credential is sent
    nowhere then.
    """
    from utilities.security_guards import normalized_origin

    section = settings.plex
    origin = normalized_origin(url)
    if getattr(section, 'auth_method', 'apikey') != 'oauth':
        return None
    if origin is None:
        # An address no parser reads plainly is not one the credential goes to.
        return None
    if credential is None:
        credential = account_credential(section)
    if not credential:
        return None
    machine_id = getattr(section, 'server_machine_id', '') or ''
    listed = remembered_listed('oauth', credential, machine_id)
    if isinstance(listed, tuple) and origin in listed[1]:
        return listed[1][origin]
    return None


def account_row_sendable(settings, url, credential=None):
    """Whether the account credential may be sent to this row address now.

    The row reaches workers and callers only through the URI plex.tv listed
    for it, and only while plex.tv still lists that origin for the credential
    the row would actually send. The ask repeats at most once per short pause
    while plex.tv does not answer.
    """
    return account_row_uri(settings, url, credential=credential) is not None


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
    account owns survives a restart. The id is the only way back to it, so a
    row whose id could not be written is removed again rather than orphaned for
    the next startup to create a second destination beside.

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
        elif row.api_key and not (getattr(settings.plex, 'token', '') or ''):
            # The sign-in the row's credential came from is gone, so the
            # credential is cleared off the row with it rather than left
            # sitting on the destination: a stored token nobody can type again
            # must not wait there for a later change to wave it through. The
            # destination is switched off with it, exactly as a sign-out does.
            fields['enabled'] = False
            fields['clear_api_key'] = True
        # Completing a sign-in is the user saying to use Plex, so it switches
        # the destination on outright rather than mirroring a scalar that the
        # handler may not have set.
        if signed_in and (fields.get('url') or row.url):
            fields['enabled'] = True
        if fields:
            repo.update(row.id, **fields)
    # Recorded even when nothing else changed, so the row the account just made
    # is the row it finds next time rather than one it has to guess at.
    previous = getattr(settings.plex, 'instance_id', '')
    recorded = previous != row.id
    settings.plex.instance_id = row.id
    session.commit()
    if recorded and persist is not None and not _persisted(persist):
        # Ownership is never inferred, so after a restart a row whose id never
        # reached disk belongs to nobody and the account would create another.
        # The new row goes, and the next change or startup tries again.
        settings.plex.instance_id = previous
        if created:
            repo.delete(row.id)
            session.commit()
        raise RuntimeError('the Plex destination owner could not be saved')
    _publish(session, settings, row.id)
    return row, created


def _persisted(persist):
    """Whether the config write went through. ``write_config`` reports a
    failed write by returning False rather than raising."""
    try:
        return persist() is not False
    except Exception:
        logging.debug('Recording the Plex destination owner failed', exc_info=True)
        return False


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
