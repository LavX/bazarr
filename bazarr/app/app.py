# coding=utf-8

from datetime import timedelta

from flask import Flask, redirect, request, Request
from flask.sessions import SecureCookieSessionInterface

from flask_compress import Compress
from flask_cors import CORS
from flask_socketio import SocketIO

from .auth import is_session_authenticated
from .database import database
from .get_args import args
from .config import settings, base_url

socketio = SocketIO()


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

    The scheme seen here is the one ReverseProxied derived from
    X-Forwarded-Proto, which waitress only passes on for an address listed in
    general.trusted_proxies.
    """

    def get_cookie_secure(self, app):
        policy = app.config.get('SESSION_COOKIE_SECURE_POLICY', 'auto')
        if policy == 'always':
            return True
        if policy == 'never':
            return False
        return bool(request and request.is_secure)


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
    """Render general.trusted_proxies for waitress.

    Waitress takes either one address or a comma-separated list. An empty
    setting means "trust nothing", which it spells as None rather than an empty
    string: an empty string would be read as an address, match nothing, and be
    harder to spot.
    """
    entries = [str(entry).strip() for entry in (config.general.trusted_proxies or [])]
    entries = [entry for entry in entries if entry]
    if not entries:
        return None
    return ','.join(entries)


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
    """Refuse an unauthenticated socket connection when a login mode is on.

    The auth argument is the payload flask-socketio hands a connect handler; it
    is unused here because the browser authenticates with the session cookie or
    the basic-auth header the handshake already carries.

    There was no connect handler at all, so the gate that protects the rest of
    the application did not exist here: anyone who could reach the port could
    subscribe and watch every library and task event, with no cookie and no
    API key. Returning False rejects the connection.

    With no login mode configured the socket stays open, which matches what
    every other surface does in that configuration.
    """
    if settings.auth.type == 'form':
        return is_session_authenticated()
    if settings.auth.type == 'basic':
        # Imported here: utilities.helper reads app.config at import time, and
        # pulling it in at module scope makes this module part of that cycle.
        from utilities.helper import check_credentials

        credentials = request.authorization
        return bool(credentials and check_credentials(credentials.username, credentials.password, request,
                                                      log_success=False))
    return True


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


class ReverseProxied(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        scheme = environ.get('HTTP_X_FORWARDED_PROTO')
        if scheme:
            environ['wsgi.url_scheme'] = scheme
        return self.app(environ, start_response)
