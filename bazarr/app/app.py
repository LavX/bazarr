# coding=utf-8

import logging
import os

from datetime import timedelta

from flask import Flask, redirect, request, Request
from flask.sessions import SecureCookieSessionInterface

from flask_compress import Compress
from flask_cors import CORS
from flask_socketio import SocketIO

from waitress.proxy_headers import proxy_headers_middleware

from utilities.security_guards import api_key_matches

from .auth import is_session_authenticated
from .auth.generation import before_save_cleanup, open_session_cleanup
from .database import database
from .get_args import args
from .config import settings, base_url
from .host_allowlist import HostAllowlistMiddleware, RAW_HOST_ENVIRON, front_refusal_notice
from .host_policy import APPROVAL_SEGMENT

socketio = SocketIO()

# docker/supervisor.py starts the backend with a per-boot token in this
# variable and polls /api/system/status until an answer carries the token back
# in SUPERVISOR_TOKEN_HEADER. On a network namespace shared by two instances
# that is how a supervisor tells its own backend from a neighbour's.
SUPERVISOR_TOKEN_ENV = 'BAZARR_SUPERVISOR_TOKEN'
SUPERVISOR_TOKEN_HEADER = 'X-Bazarr-Supervisor-Token'
# The supervisor also sets the address the backend binds, which is loopback.
BACKEND_HOST_ENV = 'BAZARR_BACKEND_HOST'


def supervisor_token():
    """The token of the supervisor that started this process, or None."""
    return os.environ.get(SUPERVISOR_TOKEN_ENV, '').strip() or None


class SupervisorTokenMiddleware:
    """Name this backend to its supervisor on every response.

    The supervisor's proxy refuses any answer without the token, so this sits
    in front of the whole WSGI stack: Flask-SocketIO answers the event stream
    from its own middleware, where Flask's response hooks never run. The proxy
    strips the header again, and without a supervisor there is nothing to add.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        token = supervisor_token()
        if not token:
            return self.wsgi_app(environ, start_response)

        def stamped_start_response(status, headers, exc_info=None):
            headers = [(name, value) for name, value in headers
                       if name.lower() != SUPERVISOR_TOKEN_HEADER.lower()]
            headers.append((SUPERVISOR_TOKEN_HEADER, token))
            return start_response(status, headers, exc_info)

        return self.wsgi_app(environ, stamped_start_response)


class CustomRequest(Request):
    def __init__(self, *args, **kwargs):
        super(CustomRequest, self).__init__(*args, **kwargs)
        # required to increase form-data size before returning a 413
        self.max_form_parts = 10000


class SchemeAwareSessionInterface(SecureCookieSessionInterface):
    """Decide the session cookie's Secure flag per request.

    Flask reads SESSION_COOKIE_SECURE once, from config, which forces a single
    answer for an application that is reached both ways: marking the cookie
    Secure breaks a plain-http LAN install, and leaving it unmarked sends the
    cookie in the clear for everyone behind HTTPS. Under the 'auto' policy the
    flag follows the scheme of the request the cookie is being set on, so both
    work. 'always' and 'never' are the explicit overrides, and 'always' is what
    an HTTPS proxy this instance cannot detect needs.

    The scheme seen here is the one the forwarded-header middleware in
    create_app derived from X-Forwarded-Proto, which it passes on only for
    the one address general.trusted_proxy names.
    """

    def get_cookie_secure(self, app):
        policy = app.config.get('SESSION_COOKIE_SECURE_POLICY', 'auto')
        if policy == 'always':
            return True
        if policy == 'never':
            return False
        return bool(request and request.is_secure)

    def open_session(self, app, request):
        """Refuse a cookie from a revoked generation instead of carrying it on.

        Without this a stale cookie is still re-signed on every request that
        carries it, which is how a read already in flight at logout handed the
        browser a fresh copy of the session it had just deleted. The gate
        refuses such a cookie anyway; this also keeps its contents away from
        every handler and stops it being renewed. It is not deleted either:
        see open_session_cleanup.
        """
        return open_session_cleanup(super().open_session(app, request))

    def save_session(self, app, session, response):
        if not before_save_cleanup(session):
            # A stale session sends no cookie header, neither a renewal nor a
            # deletion (see before_save_cleanup). The Vary header that a
            # session read earns is still owed.
            if session.accessed:
                response.vary.add("Cookie")
            return None
        return super().save_session(app, session, response)


def configure_session_cookie(app, config=settings):
    """Apply the session cookie policy to an app.

    SameSite=Lax is set explicitly rather than left to the browser default:
    the value decides whether the cookie survives a top-level redirect back
    into Bazarr, which is what an external identity provider returning to a
    callback looks like, while still not riding along on a cross-site POST.
    """
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE_POLICY'] = config.auth.cookie_secure
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=config.auth.session_lifetime_days)
    app.session_interface = SchemeAwareSessionInterface()
    return app


def trusted_proxy_value(config=settings):
    """Render general.trusted_proxy for the forwarded-header middleware.

    One address, not a list: waitress compares the peer against this value
    exactly (`remote_peer == trusted_proxy` in its proxy_headers middleware),
    so a comma-separated list would match no one and silently strip every
    forwarded header, including the supervisor hop that worked before.

    None rather than an empty string, because waitress reads an empty string
    as an address that matches nothing.
    """
    value = str(config.general.trusted_proxy or '').strip()
    return value or None


# Waitress spools a request body to disk before any route sees it, and its own
# default ceiling is 1 GiB. The largest upload a route accepts is a 150 MiB
# subtitle file, and each upload route refuses its own excess, so this is the
# backstop for every other path, including a request that never authenticates.
# In the image, docker/supervisor.py refuses a body at this ceiling in front of
# it, because waitress's own refusal would not reach the page.
MAX_REQUEST_BODY_SIZE = 256 * 1024 * 1024  # 256 MiB


def waitress_server_options():
    """The waitress options beyond host and port, in one place.

    ``trusted_proxy`` is deliberately absent: the forwarded headers are
    parsed inside create_app, against the trust rule of the application
    itself. ``clear_untrusted_proxy_headers`` is off for the same reason:
    with no proxy options, that flag is on by default and makes waitress
    wrap the app in its own proxy middleware with no trusted proxy, which
    strips every forwarded header before the app's own parsing runs; a
    request the operator's proxy forwarded in good faith would then be
    judged by its proxied hostname alone, and the forwarded scheme, address
    and host would never arrive. The app clears the headers of peers it
    does not trust itself, so nothing is left unfiltered.
    """
    return {
        'max_request_body_size': MAX_REQUEST_BODY_SIZE,
        'clear_untrusted_proxy_headers': False,
    }


def cors_is_enabled(config=settings):
    """Read the cors.enabled flag.

    This used to be `settings.get('cors', 'enabled')`, which reads like "the
    enabled key of the cors section" but is dynaconf's get(key, default): it
    returned the whole `cors` section and fell back to the string 'enabled'.
    Either way the result was a non-empty truthy object, so CORS was on for
    everyone and the setting did nothing.
    """
    return bool(config.cors.enabled)


def socket_allowed_origins(config=settings):
    """Which origins may open the event socket.

    The socket was configured with a wildcard, which is the same defect the
    HTTP CORS switch had and worse in effect: engineio reflects the caller's
    origin and adds the credentials header, so any page in any tab could open
    a long poll and read the whole live event stream.

    None is engineio's same-origin mode: it builds the allowed origin from the
    request's own scheme and host, honouring X-Forwarded-Proto and
    X-Forwarded-Host, so a reverse-proxied install keeps working. The wildcard
    returns only when the operator turns CORS on deliberately, which is the
    same switch the HTTP routes obey.
    """
    return '*' if cors_is_enabled(config) else None


@socketio.on('connect')
def _authorize_socket_connection(auth=None):
    """Refuse a socket connection that cannot prove who it is.

    There was no connect handler at all, so the gate that protects the rest of
    the application did not exist here: anyone who could reach the port could
    subscribe and watch every library and task event, with no cookie and no API
    key. Returning False rejects the connection.

    Two credentials are accepted, because no single one covers every
    configuration. The session cookie rides along on the handshake under form
    login. The API key, which the client sends in the connect payload, is what
    the rest of the API always requires and is the only thing available with
    basic auth (where nothing guarantees the browser attaches the header to a
    background XHR) or with no login mode configured. Requiring one of the two
    in every mode also closes the case where auth.type is None and the event
    stream, which carries media names, was readable by anything that could
    reach the port while /api was not.

    Everything is wrapped, because python-socketio joins the client to the
    namespace before calling this handler and only catches
    ConnectionRefusedError around it. Any other exception escaping would leave
    the client in the room receiving events, so an error here has to mean
    "refuse", not "carry on".
    """
    try:
        if settings.auth.type == 'form' and is_session_authenticated():
            return True

        if settings.auth.type == 'basic':
            # Imported here: utilities.helper reads app.config at import time,
            # and pulling it in at module scope makes this module part of that
            # cycle.
            from utilities.helper import check_credentials

            credentials = request.authorization
            if credentials and check_credentials(credentials.username, credentials.password, request,
                                                 log_success=False):
                return True

        supplied = (auth or {}).get('apiKey') if isinstance(auth, dict) else None
        return api_key_matches(supplied, settings.auth.apikey)
    except Exception:
        logging.exception('Socket authorization failed, refusing the connection')
        return False


def create_app():
    # Flask Setup
    app = Flask(__name__)
    app.request_class = CustomRequest
    app.config['COMPRESS_ALGORITHM'] = 'gzip'
    Compress(app)
    app.wsgi_app = ReverseProxied(app.wsgi_app)

    app.config["SECRET_KEY"] = settings.general.flask_secret_key
    configure_session_cookie(app)
    app.config['JSONIFY_PRETTYPRINT_REGULAR'] = True
    app.config['JSON_AS_ASCII'] = False

    app.config['RESTX_MASK_SWAGGER'] = False

    if cors_is_enabled():
        CORS(app)

    if args.dev:
        app.config["DEBUG"] = True
    else:
        app.config["DEBUG"] = False

    from engineio.async_drivers import threading  # noqa: F401
    socketio.init_app(app, path=f'{base_url.rstrip("/")}/api/socket.io',
                      cors_allowed_origins=socket_allowed_origins(),
                      async_mode='threading', allow_upgrades=False, transports='polling', engineio_logger=False)
    # The request-host gate wraps the whole WSGI stack, the Socket.IO
    # transport included, and sits inside the supervisor token wrapper so a
    # refusal still carries the token the Docker supervisor relays. The
    # forwarded headers are parsed outside it: the shim below keeps the Host
    # header the request arrived with, and proxy_headers_middleware then
    # replaces environ['HTTP_HOST'] when the peer is general.trusted_proxy,
    # so the gate judges the name it was given and the name it was handed at
    # once. A peer that is not the trusted proxy has its forwarded headers
    # stripped first, as before.
    app.wsgi_app = HostAllowlistMiddleware(app.wsgi_app)
    app.wsgi_app = proxy_headers_middleware(
        app.wsgi_app,
        trusted_proxy=trusted_proxy_value(),
        trusted_proxy_count=1,
        trusted_proxy_headers={'x-forwarded-host', 'x-forwarded-proto', 'x-forwarded-for'},
        clear_untrusted=True,
        log_untrusted=False)
    app.wsgi_app = _raw_host_shim(app.wsgi_app)
    # Outermost, so it also covers what Flask-SocketIO's middleware answers.
    app.wsgi_app = SupervisorTokenMiddleware(app.wsgi_app)

    # The front listener in front of this backend reports the hostnames its
    # own request-host check refused here, authenticated with the token it
    # started this process with.
    app.add_url_rule(f'{base_url}/_supervisor/host_refusal', 'front_refusal_notice',
                     front_refusal_notice, methods=['POST'])
    if base_url.rstrip('/'):
        # A proxy that strips the install's base_url prefix leaves the
        # approval form at the root, so the same submission is served there
        # too; the blueprint below registers the prefixed path.
        from .ui import approve_refused_host

        app.add_url_rule(f'/{APPROVAL_SEGMENT}', 'approve_refused_host_at_root',
                         approve_refused_host, methods=['POST'])

    @app.errorhandler(404)
    def page_not_found(_):
        return redirect(base_url, code=302)

    # This hook ensures that a connection is opened to handle any queries
    # generated by the request.
    @app.before_request
    def _db_connect():
        database.begin()

    # This hook ensures that the connection is closed when we've finished
    # processing the request.
    @app.teardown_request
    def _db_close(exc):
        database.close()

    return app


def _raw_host_shim(wsgi_app):
    """Keep the Host header a request arrived with, before it is replaced.

    proxy_headers_middleware overwrites environ['HTTP_HOST'] with the
    forwarded host of a trusted proxy, and the request-host gate wants both
    values, so this runs first and stores the original.
    """
    def shim(environ, start_response):
        environ[RAW_HOST_ENVIRON] = environ.get('HTTP_HOST', '')
        return wsgi_app(environ, start_response)
    return shim


class ReverseProxied(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        scheme = environ.get('HTTP_X_FORWARDED_PROTO')
        if scheme:
            environ['wsgi.url_scheme'] = scheme
        return self.app(environ, start_response)
