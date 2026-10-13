# coding=utf-8
"""TLS verification for the Plex calls that connect with the account settings.

The added-date updates, the library updates and the item refresh all connect
through ``get_plex_server``, which used to pass ``verify=False`` whatever the
user chose. Their certificate check now follows the "Verify SSL" checkbox of
the Plex instance the account owns, and without one the legacy setting, which
is off unless someone turned it on.

The webhook handlers of the same account panel are tested at the end, on the
same signed-in account.
"""

import logging
from collections import OrderedDict
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _Item:
    title = 'Metropolis'

    def edit(self, **_updates):
        pass

    def refresh(self):
        pass

    def episode(self, season, episode):
        return self


class _Section:
    def getGuid(self, *_args, **_kwargs):
        return _Item()

    def update(self):
        pass


class _Server:
    """Just enough PlexServer for the account calls, and no network."""

    def __init__(self, baseurl, token, session):
        self.baseurl, self.token, self.session = baseurl, token, session
        self.library = SimpleNamespace(section=lambda _name: _Section())


@pytest.fixture
def built(monkeypatch):
    """Every PlexServer the module constructs, from an empty pool."""
    from plex import operations
    servers = []

    def construct(baseurl, token, session):
        servers.append(_Server(baseurl, token, session))
        return servers[-1]

    monkeypatch.setattr(operations, 'PlexServer', construct)
    monkeypatch.setattr(operations, '_plex_cache', OrderedDict())
    return servers


@pytest.fixture
def account(schema_session, monkeypatch):
    """A Plex account signed in with an API key, and no instance recorded yet."""
    from app.config import settings
    monkeypatch.setattr('app.database.database', schema_session)
    for key, value in dict(auth_method='apikey', ip='plex.example', port=32400, ssl=True,
                           apikey='synthetic-plex-key', token='', instance_id='',
                           verify_ssl=False, movie_library=['Movies'],
                           series_library=['Shows'], sports_library=['Sports']).items():
        monkeypatch.setitem(settings.plex, key, value)
    monkeypatch.setitem(settings.general, 'use_plex', True)
    return settings


@pytest.fixture
def repo(schema_session):
    from media_servers.repository import MediaServerInstanceRepository
    return MediaServerInstanceRepository(schema_session)


def _plex_row(repo, *, verify_ssl, name='Plex'):
    return repo.create(kind='plex', name=name, url='https://plex.example:32400',
                       api_key='synthetic-plex-key', enabled=True, verify_ssl=verify_ssl)


def _own(account, row):
    account.plex.instance_id = row.id


def _verified(built):
    return [server.session.verify for server in built]


def test_the_account_instance_with_verify_ssl_on_gets_a_verifying_session(account, repo, built):
    from plex.operations import get_plex_server
    _own(account, _plex_row(repo, verify_ssl=True))
    server = get_plex_server()
    assert server.session.verify is True
    assert server.baseurl == 'https://plex.example:32400'


def test_the_account_instance_with_verify_ssl_off_stays_unverified(account, repo, built):
    """The instance is the user's current choice, so it outranks the legacy setting."""
    from plex.operations import get_plex_server
    account.plex.verify_ssl = True
    _own(account, _plex_row(repo, verify_ssl=False))
    assert get_plex_server().session.verify is False


@pytest.mark.parametrize('recorded', ['', 'deleted'])
@pytest.mark.parametrize('legacy', [True, False])
def test_without_an_account_instance_the_legacy_setting_decides(account, built, recorded, legacy):
    """Never recorded, or recorded and then deleted: there is no checkbox to read."""
    from plex.operations import get_plex_server
    account.plex.instance_id = str(uuid4()) if recorded == 'deleted' else ''
    account.plex.verify_ssl = legacy
    assert get_plex_server().session.verify is legacy


def test_without_an_instance_or_a_legacy_setting_tls_is_not_verified(account, built, monkeypatch):
    from plex.operations import get_plex_server
    monkeypatch.delitem(account.plex, 'verify_ssl')
    assert get_plex_server().session.verify is False


@pytest.mark.parametrize('owned', [True, False])
def test_a_second_plex_instance_does_not_decide_for_the_account(account, repo, built, owned):
    """A Plex server someone added by hand is its own, and so is its checkbox.

    The hand-added row is the one that sorts first, so reading whichever Plex
    row comes first instead of the recorded one fails every time, not only
    when the random ids happen to fall that way.
    """
    from plex.operations import get_plex_server
    hand_added, account_row = sorted((_plex_row(repo, verify_ssl=False, name='One'),
                                      _plex_row(repo, verify_ssl=False, name='Two')),
                                     key=lambda row: row.id)
    repo.update(hand_added.id, verify_ssl=not owned)
    repo.update(account_row.id, verify_ssl=owned)
    _own(account, account_row)
    assert get_plex_server().session.verify is owned


def test_an_instance_id_naming_another_kind_is_not_the_account_instance(account, repo, built):
    """Only a Plex row speaks for the Plex account; anything else falls back."""
    from plex.operations import get_plex_server
    emby = repo.create(kind='emby', name='Den', url='https://emby.example',
                       api_key='synthetic-emby-key', enabled=False, verify_ssl=True)
    account.plex.instance_id = emby.id
    assert get_plex_server().session.verify is False


def _added_date_movie():
    from plex import operations
    operations.plex_set_movie_added_date_now(SimpleNamespace(imdbId='tt0017136'))


def _added_date_episode():
    from plex import operations
    operations.plex_set_episode_added_date_now(
        SimpleNamespace(imdbId='tt0017136', season=1, episode=2))


def _library_update():
    from plex import operations
    operations.plex_update_library(True)


def _sports_library_update():
    from plex import operations
    operations.plex_update_sports_library()


def _item_refresh():
    from plex import operations
    operations.plex_refresh_item('tt0017136', False, season=1, episode=2)


ACCOUNT_CALLS = [_added_date_movie, _added_date_episode, _library_update,
                 _sports_library_update, _item_refresh]


@pytest.mark.parametrize('verify_ssl', [True, False])
@pytest.mark.parametrize('call', ACCOUNT_CALLS, ids=lambda call: call.__name__.strip('_'))
def test_every_account_call_connects_the_way_the_instance_says(account, repo, built,
                                                                call, verify_ssl):
    _own(account, _plex_row(repo, verify_ssl=verify_ssl))
    call()
    assert _verified(built) == [verify_ssl]


def test_ticking_verify_ssl_replaces_the_pooled_unverified_connection(account, repo, built):
    """The pool is keyed on the flag, so the old unverified session is not reused."""
    from plex.operations import get_plex_server
    row = _plex_row(repo, verify_ssl=False)
    _own(account, row)
    before = get_plex_server()
    assert get_plex_server() is before

    repo.update(row.id, verify_ssl=True)
    after = get_plex_server()
    assert after is not before
    assert _verified(built) == [False, True]


@pytest.mark.parametrize('legacy', [False, True])
def test_an_upgraded_install_keeps_the_choice_it_already_had(account, schema_session, built,
                                                             monkeypatch, legacy):
    """The account's row is created from the legacy setting on the first start.

    So an install that never turned verification on is still unverified after
    the upgrade, and one that did turn it on is now actually verified.
    """
    from media_servers import dispatcher
    from media_servers.plex_account import apply_plex_account
    from plex.operations import get_plex_server
    monkeypatch.setattr(dispatcher, '_configuration', None)
    account.plex.verify_ssl = legacy
    row, created = apply_plex_account(schema_session, account)
    assert created and account.plex.instance_id == row.id
    assert get_plex_server().session.verify is legacy


def test_a_database_error_is_not_taken_to_mean_unverified(account, built, monkeypatch):
    from plex.operations import get_plex_server

    class Unreadable:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError('synthetic database failure')

    monkeypatch.setattr('app.database.database', Unreadable())
    account.plex.instance_id = str(uuid4())
    with pytest.raises(RuntimeError, match='synthetic database failure'):
        get_plex_server()
    assert built == []


# --- The account panel's own requests ----------------------------------------
#
# The server picker, the connection test, the library listing with its counts
# and locations, and the Autopulse path lookup all talk to the account's server
# with plain requests calls, which read the legacy setting directly.

class _Response:
    status_code = 200
    headers = {'content-type': 'application/json'}
    text = ''

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def _plex_answer(url):
    """What plex.tv and the Plex server say to each call, just enough to reach the next."""
    if url.startswith('https://plex.tv/'):
        return [{'name': 'Attic', 'clientIdentifier': 'machine-abc', 'provides': 'server',
                 'owned': True, 'connections': [{'uri': 'https://plex.example:32400'}]}]
    if url.endswith('/library/sections'):
        return {'MediaContainer': {'Directory': [{'key': '1', 'title': 'Movies', 'type': 'movie'}]}}
    if url.endswith('/library/sections/1'):
        return {'MediaContainer': {'Directory': [{'Location': [{'path': '/movies'}]}]}}
    return {'MediaContainer': {'size': 3}}


@pytest.fixture
def plex_requests(monkeypatch):
    """The verify flag of every request sent to the Plex server itself."""
    import requests
    sent = []

    def get(url, **kwargs):
        if not url.startswith('https://plex.tv/'):
            sent.append(kwargs.get('verify'))
        return _Response(_plex_answer(url))

    monkeypatch.setattr(requests, 'get', get)
    return sent


def _server_picker():
    from flask import Flask
    from api.plex.oauth import PlexServers
    with Flask(__name__).test_request_context('/plex/oauth/servers'):
        return PlexServers.get.__wrapped__(PlexServers())


def _connection_test():
    from flask import Flask
    from api.plex.oauth import PlexTestConnection
    with Flask(__name__).test_request_context('/plex/test-connection', method='POST',
                                              json={'uri': 'https://plex.example:32400'}):
        return PlexTestConnection.post.__wrapped__(PlexTestConnection())


def _library_listing():
    from flask import Flask
    from api.plex.oauth import PlexLibraries
    with Flask(__name__).test_request_context('/plex/oauth/libraries'):
        return PlexLibraries.get.__wrapped__(PlexLibraries())


def _autopulse_paths():
    from utilities.plex_utils import get_plex_libraries_with_paths
    return get_plex_libraries_with_paths()


PANEL_CALLS = {_server_picker: 1, _connection_test: 1, _library_listing: 3, _autopulse_paths: 2}


@pytest.fixture
def oauth_account(account, monkeypatch):
    """The same account signed in through OAuth with a server picked."""
    import api.plex.oauth  # noqa: F401  (imports the API before the checks run)
    for key, value in dict(auth_method='oauth', token='synthetic-plex-token',
                           server_machine_id='machine-abc',
                           server_url='https://plex.example:32400').items():
        monkeypatch.setitem(account.plex, key, value)
    return account


@pytest.mark.parametrize('verify_ssl', [True, False])
@pytest.mark.parametrize('call', list(PANEL_CALLS), ids=lambda call: call.__name__.strip('_'))
def test_every_panel_request_verifies_the_way_the_instance_says(oauth_account, repo,
                                                                 plex_requests, call, verify_ssl):
    """The legacy setting says the opposite, so reading it instead fails every case."""
    oauth_account.plex.verify_ssl = not verify_ssl
    _own(oauth_account, _plex_row(repo, verify_ssl=verify_ssl))
    result = call()
    assert plex_requests == [verify_ssl] * PANEL_CALLS[call]
    assert result not in ({'data': []}, {'movie_paths': [], 'series_paths': []})


def _autopulse_config():
    from flask import Flask
    from api.plex.oauth import PlexAutopulseConfig
    with Flask(__name__).test_request_context('/plex/autopulse/config'):
        return PlexAutopulseConfig.get.__wrapped__(PlexAutopulseConfig())


@pytest.mark.parametrize('call, message', [
    (_connection_test, 'Sign in to Plex to test the connection.'),
    (_autopulse_config, 'Sign in to Plex to generate an Autopulse configuration.'),
], ids=['connection test', 'autopulse config'])
def test_the_panel_asks_for_a_plex_sign_in_when_bazarr_holds_no_plex_token(oauth_account, plex_requests,
                                                                           monkeypatch, call, message):
    """Both answered 401, and the web client reads any 401 as its own Bazarr
    session ending: it signs the reader out and goes back to the login page,
    when only Plex is signed out. A 409 with a code the Plex panel reads asks
    for a Plex sign-in instead, and nothing is sent anywhere without a token."""
    monkeypatch.setitem(oauth_account.plex, 'token', '')
    assert call() == ({'error': message, 'error_code': 'sign_in_required'}, 409)
    assert plex_requests == []


@pytest.mark.parametrize('legacy', [True, False])
def test_picking_the_first_server_follows_the_legacy_setting(oauth_account, plex_requests, legacy):
    """Before a server is picked there is no instance row, so no checkbox to read."""
    oauth_account.plex.verify_ssl = legacy
    assert _server_picker()['data'][0]['name'] == 'Attic'
    assert plex_requests == [legacy]


def test_the_api_key_connection_test_accepts_a_bracketed_ipv6_origin(account, monkeypatch):
    """The trusted origin under the API key method is read from the configured
    address the same way the submitted one is normalized, so a bracketed IPv6
    literal is the same server on both sides rather than one only its brackets
    keep apart from the address the request typed."""
    import requests

    from flask import Flask

    from api.plex.oauth import PlexTestConnection
    for key, value in dict(ip='[fe80::1]', port=32400, ssl=False).items():
        monkeypatch.setitem(account.plex, key, value)
    sent = []

    def get(url, **_kwargs):
        sent.append(url)
        return _Response({'MediaContainer': {'size': 1}})

    monkeypatch.setattr(requests, 'get', get)

    with Flask(__name__).test_request_context('/plex/test-connection', method='POST',
                                              json={'uri': 'http://[fe80::1]:32400'}):
        result = PlexTestConnection.post.__wrapped__(PlexTestConnection())

    assert result == {'success': True}
    assert sent == ['http://[fe80::1]:32400/identity'], \
        'the configured bracketed address was the trusted one'


def test_the_api_key_connection_test_answers_a_database_error_in_its_snapshot(
        account, monkeypatch):
    """The snapshot read under the account lock is part of the connection
    test, so a database error while it reads the account's instance is a
    failure the route answers, not an exception that escapes its handler."""
    import requests

    from flask import Flask

    from api.plex.oauth import PlexTestConnection

    class Unreadable:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError('synthetic database failure')

    monkeypatch.setattr('app.database.database', Unreadable())
    account.plex.instance_id = str(uuid4())

    def get(url, **_kwargs):
        return _Response({'MediaContainer': {'size': 1}})

    monkeypatch.setattr(requests, 'get', get)

    with Flask(__name__).test_request_context('/plex/test-connection', method='POST',
                                              json={'uri': 'https://plex.example:32400'}):
        result = PlexTestConnection.post.__wrapped__(PlexTestConnection())

    assert result == {'success': False, 'error': 'synthetic database failure'}


def test_a_replacement_between_the_address_and_the_key_reads_cannot_cross_the_pair(
        account, built, monkeypatch):
    """The API key connection reads its method, its address, its credential
    and its TLS choice as one snapshot under the account lock, the one every
    account save holds, so an address-and-key replacement landing while the
    connection is being built cannot pair one account's address with
    another's key: the connection carries the snapshot's own credential to
    its own address, or the replaced pair once the replacement landed before
    the snapshot. The unfixed order read the address and the key separately,
    so the replaced key reached the address it replaced."""
    from media_servers import plex_account
    from plex import operations
    from plex.operations import get_plex_server

    landed = []

    def land_the_replacement():
        # The replacement lands inside the window wherever this head leaves
        # one: at the fixed head the entry into the account lock runs it
        # before the snapshot is taken, and at the unfixed head the key read
        # runs it between the address read and its own.
        if landed:
            return
        landed.append(True)
        account.plex.ip = 'moved.example'
        account.plex.apikey = 'moved-key'

    real_lock = plex_account.ACCOUNT_LOCK

    class _PausedLock:
        def __enter__(self):
            land_the_replacement()
            return real_lock.__enter__()

        def __exit__(self, *_exception):
            return real_lock.__exit__(*_exception)

    monkeypatch.setattr(plex_account, 'ACCOUNT_LOCK', _PausedLock())

    real_settings = operations.settings
    real_section = real_settings.plex
    real_get = real_section.get

    class _SectionProxy:
        def __getattr__(self, name):
            return getattr(real_section, name)

        def get(self, key, default=None):
            if key == 'apikey':
                land_the_replacement()
            return real_get(key, default)

    class _SettingsProxy:
        plex = _SectionProxy()

        def __getattr__(self, name):
            return getattr(real_settings, name)

    monkeypatch.setattr(operations, 'settings', _SettingsProxy())

    server = get_plex_server()

    assert (server.baseurl, server.token) in (
        ('https://plex.example:32400', 'synthetic-plex-key'),
        ('https://moved.example:32400', 'moved-key')), \
        'the connection paired one account\'s address with another account\'s key'


@pytest.mark.parametrize('call', [_connection_test, _library_listing, _autopulse_paths],
                         ids=lambda call: call.__name__.strip('_'))
def test_an_account_transition_landing_at_the_tls_read_cannot_weaken_the_send(
        oauth_account, repo, plex_requests, monkeypatch, call):
    """An allowed account transition can clear the token and turn certificate
    verification off. The three panel sends used to read the TLS setting
    separately, after the verification captured the token, so a transition
    landing between the two sent the captured OAuth token with the weaker
    setting the transition left behind. The fixed reads capture the TLS
    setting before the verification, the same coherent capture the
    credential and the destination are part of, so the account the
    transition leaves is the one the listing confirms and nothing is sent.
    """
    import api.plex.oauth  # the module attribute the routes read
    from plex import operations

    row = _plex_row(repo, verify_ssl=True)
    _own(oauth_account, row)
    real = operations.plex_account_verify_ssl
    landed = []

    def transition():
        # The transition lands wherever this head reads the TLS setting: at
        # the unfixed head that is after the token was captured and verified,
        # and at the fixed head it is before the verification, so the account
        # the transition leaves is the one the listing has to answer for.
        if not landed:
            landed.append(True)
            monkeypatch.setitem(oauth_account.plex, 'token', '')
            repo.update(row.id, verify_ssl=False)
        return real()

    monkeypatch.setattr(operations, 'plex_account_verify_ssl', transition)
    monkeypatch.setattr(api.plex.oauth, 'plex_account_verify_ssl', transition)

    call()

    assert plex_requests == [], \
        'the captured token was sent after the account changed, or with the weaker TLS'


@pytest.mark.parametrize('call', [_connection_test, _library_listing, _autopulse_paths],
                         ids=lambda call: call.__name__.strip('_'))
def test_a_replacement_between_the_tls_read_and_the_credential_cannot_go_out_under_it(
        oauth_account, repo, plex_requests, monkeypatch, call):
    """The replacement interleaving: the choice is captured while certificate
    verification is off, the account token is replaced while verification is
    turned on, and the send then carries the replacement token under the
    disabled choice. The TLS read and the credential capture are one snapshot
    under the account lock and the snapshot is confirmed before any send, so
    nothing is sent: neither the replacement under the weaker choice it
    replaced nor the captured token after the account left it behind."""
    from media_servers import plex_account

    row = _plex_row(repo, verify_ssl=False)
    _own(oauth_account, row)
    plex_account.forget_plex_tv_answer()
    landed = []

    def land_the_replacement():
        # The replacement lands wherever this head captures the credential:
        # at the unfixed head the TLS choice was read on its own before the
        # verification, so the token the verification returns is the
        # replacement while the choice the send carries is still the disabled
        # one. At the fixed head the same landing is inside the verification
        # too, but the snapshot was captured before it and the confirm before
        # the send refuses the pair.
        if landed:
            return
        landed.append(True)
        monkeypatch.setitem(oauth_account.plex, 'token', 'replacement-plex-token')
        repo.update(row.id, verify_ssl=True)

    real = plex_account.verified_server_addresses

    def verified(settings, **kwargs):
        land_the_replacement()
        return real(settings, **kwargs)

    monkeypatch.setattr(plex_account, 'verified_server_addresses', verified)

    call()

    assert plex_requests == [], \
        'the replacement token was sent, or sent under the choice it replaced'


# --- The Autopulse configuration, built from a verification result -----------

def test_the_autopulse_config_refuses_an_address_plex_tv_does_not_list(
        oauth_account, plex_requests, monkeypatch):
    """The generated configuration pairs the account token with a server
    address, so it is built only from a verification result: an address plex.tv
    does not list for the signed-in account refuses generation, and the token
    is never embedded next to an address nobody verified."""
    from media_servers import plex_account
    from utilities import autopulse_webhook
    from utilities.autopulse_webhook import generate_autopulse_config
    monkeypatch.setitem(oauth_account.plex, 'server_url', 'https://unlisted.example:32400')
    templates = []
    monkeypatch.setattr(autopulse_webhook, '_get_autopulse_template',
                        lambda: templates.append(True) or
                        {'config': 'url = "{url}" token = "{token}"', 'version': '1'})
    plex_account.forget_plex_tv_answer()

    assert generate_autopulse_config() is None
    assert templates == [], 'no template was asked for without a verified address'
    assert plex_requests == [], 'the token was sent nowhere'


def test_the_autopulse_config_refuses_while_the_provider_is_unreachable(
        oauth_account, plex_requests, monkeypatch):
    """With Autopulse itself not answering, there is no template to build the
    configuration from, and none is returned."""
    from media_servers import plex_account
    from utilities import autopulse_webhook
    from utilities.autopulse_webhook import generate_autopulse_config
    plex_account.forget_plex_tv_answer()
    monkeypatch.setattr(autopulse_webhook, '_get_autopulse_template', lambda: None)

    assert generate_autopulse_config() is None


def test_the_autopulse_config_embeds_the_uri_plex_tv_listed(oauth_account, plex_requests,
                                                           monkeypatch):
    """A stored spelling of a listed origin gives way to the URI plex.tv
    returned, and the credential embedded with it is the one the listing was
    verified with."""
    from media_servers import plex_account
    from utilities import autopulse_webhook
    from utilities.autopulse_webhook import generate_autopulse_config
    monkeypatch.setitem(oauth_account.plex, 'server_url', 'https://plex.example:32400/legacy')
    monkeypatch.setattr(autopulse_webhook, '_get_autopulse_template',
                        lambda: {'config': 'url = "{url}" token = "{token}"', 'version': '1'})
    plex_account.forget_plex_tv_answer()

    config = generate_autopulse_config()

    assert config is not None
    assert 'url = "https://plex.example:32400"' in config['config_yaml']
    assert 'url = "https://plex.example:32400/legacy"' not in config['config_yaml']
    assert 'token = "synthetic-plex-token"' in config['config_yaml']


# --- The webhook handlers, against a faked plex.tv account -------------------
#
# Webhooks are a Plex Pass feature. plex.tv refuses the webhooks endpoint to
# any other account with a 403 (code 1043), and the settings page lists the
# account's webhooks on every visit.

_PLEX_PASS_REFUSAL = ('(403) forbidden; https://plex.tv/api/v2/user/webhooks '
                      '{"errors":[{"code":1043,"message":"This action is not available for this user",'
                      '"status":403}]}')
# plexapi asks for no JSON, so plex.tv may answer the same refusal in XML.
_PLEX_PASS_REFUSAL_XML = ('(403) forbidden; https://plex.tv/api/v2/user/webhooks '
                          '<?xml version="1.0" encoding="UTF-8"?> <errors>   <error code="1043" '
                          'message="This action is not available for this user" status="403"/> </errors> ')
# A 403 for any other reason, which is not a Plex Pass answer.
_OTHER_REFUSAL = ('(403) forbidden; https://plex.tv/api/v2/user/webhooks '
                  '{"errors":[{"code":1003,"message":"Access denied","status":403}]}')
_WEBHOOK = 'https://bazarr.example/api/webhooks/plex?apikey=synthetic&instance=Bazarr'


def _plex_tv_error(message):
    """What plexapi raises for a plex.tv refusal such as a 403 or a 500."""
    from plexapi.exceptions import BadRequest
    return BadRequest(message)


class _PlexAccount:
    """A plex.tv account as MyPlexAccount reads it, with no network behind it."""

    def __init__(self, *, plex_pass, refusal=None, webhooks=()):
        self.subscriptionActive = plex_pass
        self.subscriptionFeatures = ['webhooks'] if plex_pass else []
        self.subscriptionPlan = 'lifetime' if plex_pass else None
        self.refusal = refusal
        self.urls = list(webhooks)
        self.asked = []

    def _answer(self, call):
        self.asked.append(call)
        if self.refusal is not None:
            raise self.refusal
        return list(self.urls)

    def webhooks(self):
        return self._answer('webhooks')

    def addWebhook(self, url):
        self.urls.append(url)
        return self._answer('addWebhook')

    def deleteWebhook(self, url):
        self.urls.remove(url)
        return self._answer('deleteWebhook')


@pytest.fixture
def plex_tv(oauth_account, monkeypatch):
    """The account plex.tv answers with, set by each test."""
    from api.plex import oauth
    state = SimpleNamespace(account=None, settings=oauth_account)
    monkeypatch.setattr('plexapi.myplex.MyPlexAccount', lambda token: state.account)
    monkeypatch.setattr(oauth, '_update_plexapi_headers', lambda: None)
    # Creating a webhook builds its callback address from these two.
    monkeypatch.setitem(oauth_account.auth, 'apikey', 'synthetic-bazarr-key')
    monkeypatch.setitem(oauth_account.general, 'hostname', 'bazarr.example')
    return state


def _webhook_call(name, body=None):
    """One webhook handler, past the API-key check, as (body, status)."""
    from flask import Flask
    from api.plex import oauth
    resource = getattr(oauth, name)
    method = 'get' if name == 'PlexWebhookList' else 'post'
    if body is None and name == 'PlexWebhookDelete':
        body = {'webhook_url': _WEBHOOK}
    with Flask(__name__).test_request_context('/plex/webhook', method=method.upper(), json=body):
        answer = getattr(resource, method).__wrapped__(resource())
    return answer if isinstance(answer, tuple) else (answer, 200)


def _loud(caplog, level):
    return [record for record in caplog.records if record.levelno >= level]


def test_listing_webhooks_without_plex_pass_reports_the_plan_instead_of_failing(plex_tv, caplog):
    """The listing asked plex.tv anyway and turned its refusal into an ERROR
    line and a 502 on every visit, so the page showed a failure instead of
    the Plex Pass notice it already has."""
    plex_tv.account = _PlexAccount(plex_pass=False, refusal=_plex_tv_error(_PLEX_PASS_REFUSAL))
    with caplog.at_level(logging.DEBUG):
        body, status = _webhook_call('PlexWebhookList')
    assert status == 200
    assert body == {'data': {'webhooks': [], 'count': 0, 'plexPassSubscription': {
        'active': False, 'has_webhooks_feature': False, 'plan': None}}}
    assert plex_tv.account.asked == []
    assert _loud(caplog, logging.WARNING) == []


@pytest.mark.parametrize('refusal', [_PLEX_PASS_REFUSAL, _PLEX_PASS_REFUSAL_XML], ids=['json', 'xml'])
def test_a_plex_pass_refusal_from_plex_tv_reads_as_no_webhooks_feature(plex_tv, caplog, refusal):
    """The account lists the feature, yet plex.tv refuses: the same answer as
    an account without it, not a failed listing. The two disagree, so the log
    at its default level says so."""
    plex_tv.account = _PlexAccount(plex_pass=True, refusal=_plex_tv_error(refusal))
    with caplog.at_level(logging.DEBUG):
        body, status = _webhook_call('PlexWebhookList')
    assert status == 200
    assert body == {'data': {'webhooks': [], 'count': 0, 'plexPassSubscription': {
        'active': True, 'has_webhooks_feature': False, 'plan': 'lifetime'}}}
    assert plex_tv.account.asked == ['webhooks']
    assert [record.levelno for record in _loud(caplog, logging.INFO)] == [logging.INFO]


def test_a_plex_pass_account_still_lists_its_webhooks(plex_tv):
    plex_tv.account = _PlexAccount(plex_pass=True, webhooks=[_WEBHOOK])
    body, status = _webhook_call('PlexWebhookList')
    assert status == 200
    assert body == {'data': {'webhooks': [{'url': _WEBHOOK}], 'count': 1, 'plexPassSubscription': {
        'active': True, 'has_webhooks_feature': True, 'plan': 'lifetime'}}}


@pytest.mark.parametrize('failure', [
    '(500) internal_server_error; https://plex.tv/api/v2/user/webhooks ',
    _OTHER_REFUSAL,
], ids=['500', 'other 403'])
def test_any_other_plex_tv_failure_still_fails_the_listing(plex_tv, caplog, failure):
    plex_tv.account = _PlexAccount(plex_pass=True, refusal=_plex_tv_error(failure))
    with caplog.at_level(logging.DEBUG):
        _body, status = _webhook_call('PlexWebhookList')
    assert status == 502
    assert [record.levelno for record in _loud(caplog, logging.WARNING)] == [logging.ERROR]


@pytest.mark.parametrize('plex_account', [
    lambda: _PlexAccount(plex_pass=False),
    lambda: _PlexAccount(plex_pass=True, refusal=_plex_tv_error(_PLEX_PASS_REFUSAL)),
], ids=['the account says so', 'plex.tv says so'])
def test_creating_a_webhook_without_plex_pass_says_it_needs_plex_pass(plex_tv, caplog, plex_account):
    """The account check already answered this. A refusal from plex.tv itself
    was logged as an ERROR and answered 502, a Plex outage to the page."""
    plex_tv.account = plex_account()
    with caplog.at_level(logging.DEBUG):
        body, status = _webhook_call('PlexWebhookCreate')
    assert status == 403
    assert 'requires Plex Pass' in body['error']
    assert _loud(caplog, logging.ERROR) == []


def test_removing_a_webhook_plex_tv_refuses_says_it_needs_plex_pass(plex_tv, caplog):
    plex_tv.account = _PlexAccount(plex_pass=True, refusal=_plex_tv_error(_PLEX_PASS_REFUSAL))
    with caplog.at_level(logging.DEBUG):
        body, status = _webhook_call('PlexWebhookDelete')
    assert status == 403
    assert 'requires Plex Pass' in body['error']
    assert _loud(caplog, logging.ERROR) == []


@pytest.mark.parametrize('handler', ['PlexWebhookCreate', 'PlexWebhookDelete'])
def test_a_403_for_another_reason_is_not_called_a_plex_pass_refusal(plex_tv, caplog, handler):
    """Every call in these handlers goes to plex.tv, the sign-in included, and
    a 403 there can mean something else than a missing Plex Pass."""
    plex_tv.account = _PlexAccount(plex_pass=True, refusal=_plex_tv_error(_OTHER_REFUSAL))
    with caplog.at_level(logging.DEBUG):
        body, status = _webhook_call(handler)
    assert status == 502
    assert 'Plex Pass' not in body['error']
    assert [record.levelno for record in _loud(caplog, logging.WARNING)] == [logging.ERROR]


def test_a_plex_pass_account_adds_its_webhook(plex_tv, monkeypatch):
    monkeypatch.setitem(plex_tv.settings.general, 'base_url', '')
    monkeypatch.setitem(plex_tv.settings.general, 'instance_name', 'Living Room')
    plex_tv.account = _PlexAccount(plex_pass=True)
    body, status = _webhook_call('PlexWebhookCreate')
    webhook = 'http://bazarr.example/api/webhooks/plex?apikey=synthetic-bazarr-key&instance=Living+Room'
    assert status == 200
    assert body == {'data': {'success': True, 'message': 'Webhook created successfully',
                             'webhook_url': webhook, 'total_webhooks': 1}}
    assert plex_tv.account.asked == ['webhooks', 'addWebhook']
    assert plex_tv.account.urls == [webhook]


def test_a_plex_pass_account_removes_its_webhook(plex_tv):
    plex_tv.account = _PlexAccount(plex_pass=True, webhooks=[_WEBHOOK])
    body, status = _webhook_call('PlexWebhookDelete')
    assert status == 200
    assert body == {'data': {'success': True, 'message': 'Webhook deleted successfully'}}
    assert plex_tv.account.asked == ['webhooks', 'deleteWebhook']
    assert plex_tv.account.urls == []


@pytest.mark.parametrize('handler', ['PlexWebhookList', 'PlexWebhookCreate', 'PlexWebhookDelete'])
def test_a_missing_plex_token_asks_for_a_plex_sign_in_not_a_bazarr_login(plex_tv, monkeypatch, handler):
    """Each handler raised UnauthorizedError inside the try whose catch-all
    turned it into a 502. A 401 is no better: the web client reads any 401 as
    its own Bazarr session ending and goes back to the login page, when only
    Plex is signed out. So it is a 409 with a code the Plex panel reads."""
    monkeypatch.setitem(plex_tv.settings.plex, 'token', '')
    plex_tv.account = _PlexAccount(plex_pass=True)
    body, status = _webhook_call(handler)
    assert status == 409
    assert body['error_code'] == 'sign_in_required'
    assert 'Sign in to Plex' in body['error']
    assert plex_tv.account.asked == []


def test_removing_a_webhook_without_naming_one_is_a_bad_request(plex_tv):
    """The argument check ran inside the same catch-all, so its 400 became a 502."""
    from werkzeug.exceptions import BadRequest
    plex_tv.account = _PlexAccount(plex_pass=True, webhooks=[_WEBHOOK])
    with pytest.raises(BadRequest):
        _webhook_call('PlexWebhookDelete', body={})
    assert plex_tv.account.asked == []


def test_verification_leaves_the_account_lock_free_for_a_transition(oauth_account, built, monkeypatch):
    """The plex.tv verification is a network ask, and no lock may be held
    across it: an account transition trying the lock while a connection is
    being verified must get through, the same way it gets through while no
    connection is being built. The unfixed order held the account lock across
    the ask, so sign-ins and instance saves waited on a connection they never
    asked for."""
    import threading

    from media_servers import plex_account
    from plex.operations import get_plex_server

    real_lock = plex_account.ACCOUNT_LOCK
    taken = threading.Event()

    def transition():
        if real_lock.acquire(timeout=3):
            real_lock.release()
            taken.set()

    def verified(_settings, **_kwargs):
        thread = threading.Thread(target=transition)
        thread.start()
        thread.join(5)
        return ['https://plex.example:32400'], 'ok', 'synthetic-plex-token'

    monkeypatch.setattr(plex_account, 'verified_server_addresses', verified)

    server = get_plex_server()

    assert taken.is_set(), 'the account lock was free while the address was being verified'
    assert server.baseurl == 'https://plex.example:32400'
    assert server.token == 'synthetic-plex-token'


def test_a_sign_in_landing_during_verification_does_not_connect_with_the_verified_answer(
        oauth_account, built, monkeypatch):
    """The verification's answer belongs to the account it was asked about, so
    the captured account is confirmed against what is stored before anything
    connects with the answer: a sign-in landing while plex.tv was being asked
    stops the connection rather than letting the older account's listing carry
    its credential. The unfixed order had nothing to confirm against, and
    connected with the replaced account none the wiser."""
    from media_servers import plex_account
    from plex.operations import get_plex_server

    def verified(_settings, **_kwargs):
        # A sign-in lands while the address is being verified.
        oauth_account.plex.token = 'replacement-token'
        return ['https://plex.example:32400'], 'ok', 'synthetic-plex-token'

    monkeypatch.setattr(plex_account, 'verified_server_addresses', verified)

    with pytest.raises(ValueError, match='account changed'):
        get_plex_server()
    assert built == [], 'no connection was built on the replaced account\'s listing'
