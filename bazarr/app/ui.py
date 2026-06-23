# coding=utf-8

import os
import ipaddress
import socket
import time
import requests
import mimetypes

from flask import (request, abort, render_template, Response, session, send_file, stream_with_context, Blueprint,
                   redirect)
from functools import wraps
from urllib.parse import unquote, urlparse

from constants import HEADERS
from literals import FILE_LOG
from sonarr.info import url_api_sonarr
from radarr.info import url_api_radarr
from utilities.helper import check_credentials
from utilities.central import get_log_file_path
from utilities.security_guards import api_key_matches

from .config import settings, base_url, get_ssl_verify
from .database import database, System
from .get_args import args

frontend_build_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'frontend', 'build')

ui_bp = Blueprint('ui', __name__,
                  template_folder=frontend_build_path,
                  static_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'frontend',
                                             'build', 'assets'),
                  static_url_path='/assets')

if os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'frontend', 'build',
                               'images')):
    static_directory = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'frontend', 'build',
                                    'images')
else:
    static_directory = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'frontend', 'public',
                                    'images')
static_bp = Blueprint('images', __name__, static_folder=static_directory, static_url_path='/images')

ui_bp.register_blueprint(static_bp)

mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('font/woff2', '.woff2')
mimetypes.add_type('image/svg+xml', '.svg')
mimetypes.add_type('image/png', '.png')
mimetypes.add_type('image/x-icon', '.ico')
mimetypes.add_type('application/manifest+json', '.webmanifest')

pwa_assets = ['registerSW.js', 'manifest.webmanifest', 'sw.js']


def check_login(actual_method):
    @wraps(actual_method)
    def wrapper(*args, **kwargs):
        if settings.auth.type == 'basic':
            auth = request.authorization
            if not (auth and
                    check_credentials(request.authorization.username, request.authorization.password, request)):
                return ('Unauthorized', 401, {
                    'WWW-Authenticate': 'Basic realm="Login Required"'
                })
        elif settings.auth.type == 'form':
            if 'logged_in' not in session:
                return abort(401)
        return actual_method(*args, **kwargs)
    return wrapper


def _require_proxy_api_key():
    """The /test connection proxies forward a server-side request to a
    user-supplied host (an SSRF surface). Require the global API key so they are
    never reachable unauthenticated, independent of settings.auth.type (which is
    None by default). The frontend already sends X-API-KEY on every request."""
    provided = request.headers.get('X-API-KEY') or request.args.get('apikey')
    if not api_key_matches(provided, settings.auth.apikey):
        abort(401)


@ui_bp.route('/', defaults={'path': ''})
@ui_bp.route('/<path:path>')
def catch_all(path):
    if path.startswith('login') and settings.auth.type not in ['basic', 'form']:
        # login page has been accessed when no authentication is enabled
        return redirect(base_url or "/", code=302)

    # PWA Assets are returned from frontend root folder.
    # Uses the CodeQL-documented "GOOD" pattern from py/path-injection:
    # normpath(join(base, name)) + startswith(base). Pre-rejects absolute
    # paths because os.path.join(base, '/abs') would silently drop the base.
    if path in pwa_assets or path.startswith('workbox-'):
        if os.path.isabs(path):
            return abort(403)
        fullpath = os.path.normpath(os.path.join(frontend_build_path, path))
        if not fullpath.startswith(frontend_build_path):
            return abort(403)
        return send_file(fullpath)

    auth = True
    if settings.auth.type == 'basic':
        auth = request.authorization
        if not (auth and check_credentials(request.authorization.username, request.authorization.password, request,
                                           log_success=False)):
            return ('Unauthorized', 401, {
                'WWW-Authenticate': 'Basic realm="Login Required"'
            })
    elif settings.auth.type == 'form':
        if 'logged_in' not in session or not session['logged_in']:
            auth = False

    try:
        updated = database.scalar(System.updated)
    except Exception:
        updated = '0'

    try:
        configured = database.scalar(System.configured)
    except Exception:
        configured = '0'

    inject = dict()

    if not path.startswith('api/'):
        inject["baseUrl"] = base_url
        inject["canUpdate"] = not args.no_update
        inject["hasUpdate"] = updated != '0'
        inject["isConfigured"] = configured != '0'

        if auth:
            inject["apiKey"] = settings.auth.apikey

    template_url = base_url
    if not template_url.endswith("/"):
        template_url += "/"

    return render_template("index.html", BAZARR_SERVER_INJECT=inject, baseUrl=template_url)


@ui_bp.route('/' + FILE_LOG)
@check_login
def download_log():
    return send_file(get_log_file_path(), max_age=0, as_attachment=True)


def _instance_image_url(kind, url):
    """Build the (image_url, verify_ssl) for the cover proxy.

    With an ``arr_instance_id`` query arg (#156) the cover is fetched from that
    instance's Sonarr/Radarr (base url + decrypted key from its saved row);
    without it the scalar/default path is used, byte-identical to before.
    Returns (None, None) for an unknown/disabled instance.
    """
    from flask import request
    arr_instance_id = request.args.get('arr_instance_id', type=int)
    if arr_instance_id is None:
        if kind == 'sonarr':
            base = settings.sonarr.base_url
            return f'{url_api_sonarr()}{url.lstrip(base)}?apikey={settings.sonarr.apikey}', get_ssl_verify('sonarr')
        base = settings.radarr.base_url
        return f'{url_api_radarr()}{url.lstrip(base)}?apikey={settings.radarr.apikey}', get_ssl_verify('radarr')

    from arr_instances.resolution import client_for_instance
    from app.database import database as _db
    client = client_for_instance(_db, arr_instance_id)
    if client is None or client.kind != kind:
        return None, None
    # Strip the instance's own base_url prefix from the stored path so it is not
    # duplicated; then fetch from the instance's /api/v3 cover endpoint.
    inst_base = (getattr(client, '_base_url_raw', '') or '').strip('/')
    path = url[len(inst_base):].lstrip('/') if inst_base and url.startswith(inst_base) else url
    return f'{client.base_url()}/api/v3/{path}?apikey={client.api_key}', client.verify_ssl


@ui_bp.route('/images/series/<path:url>', methods=['GET'])
@check_login
def series_images(url):
    url = url.strip("/")
    url_image, verify = _instance_image_url('sonarr', url)
    if url_image is None:
        return '', 404
    url_image = url_image.replace('poster-250', 'poster-500')
    try:
        req = requests.get(url_image, stream=True, timeout=15, verify=verify, headers=HEADERS)
    except Exception:
        return '', 404
    else:
        return Response(stream_with_context(req.iter_content(2048)), content_type=req.headers['content-type'])


@ui_bp.route('/images/movies/<path:url>', methods=['GET'])
@check_login
def movies_images(url):
    url = url.strip("/")
    url_image, verify = _instance_image_url('radarr', url)
    if url_image is None:
        return '', 404
    try:
        req = requests.get(url_image, stream=True, timeout=15, verify=verify, headers=HEADERS)
    except Exception:
        return '', 404
    else:
        return Response(stream_with_context(req.iter_content(2048)), content_type=req.headers['content-type'])


# --- Cinematic login backdrops (pre-auth) --------------------------------
#
# The login page renders before authentication, so it can't use the auth-gated
# /images/... library proxy. Like Overseerr/Jellyseerr, we show TMDB trending
# backdrops instead: public catalog art, never the user's own library, so there
# is no private data on the pre-auth surface and no per-item proxying. The TMDB
# key stays server-side; the browser only ever receives public image.tmdb.org
# URLs, which it loads directly.

# Built-in read-only TMDB v3 API key (Overseerr/Jellyseerr ship one the same
# way). Override at runtime with the BAZARR_TMDB_API_KEY environment variable.
# Empty until provided; with no key the endpoint returns an empty list and the
# login screen uses its gradient fallback.
_TMDB_BUILTIN_API_KEY = ''
_TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/original'
_TMDB_TRENDING_URL = 'https://api.themoviedb.org/3/trending/all/week'
# How many backdrops the login screen rotates through, and how long the TMDB
# response is cached so repeated pre-auth hits don't hammer the API.
_BACKDROP_SAMPLE_CAP = 12
_BACKDROP_CACHE_TTL = 6 * 3600
_backdrop_cache = {'at': 0.0, 'urls': []}


def _tmdb_api_key():
    """Resolve the TMDB key: env override first, then the built-in default."""
    return os.environ.get('BAZARR_TMDB_API_KEY', '').strip() or _TMDB_BUILTIN_API_KEY


def _fetch_tmdb_backdrops():
    """Trending backdrop image URLs from TMDB.

    Returns an empty list on any failure (no key, network error, unexpected
    payload) so the caller degrades cleanly to the gradient fallback.
    """
    key = _tmdb_api_key()
    if not key:
        return []
    try:
        resp = requests.get(_TMDB_TRENDING_URL, params={'api_key': key},
                            timeout=10, headers=HEADERS)
        resp.raise_for_status()
        results = resp.json().get('results', [])
    except Exception:
        return []
    urls = [f'{_TMDB_IMAGE_BASE}{r["backdrop_path"]}'
            for r in results
            if isinstance(r, dict) and r.get('backdrop_path')]
    return urls[:_BACKDROP_SAMPLE_CAP]


@ui_bp.route('/system/backdrops', methods=['GET'])
def login_backdrops():
    """Trending TMDB backdrop URLs for the cinematic login screen.

    Unauthenticated by design, but safe: the response is public TMDB catalog art
    (image.tmdb.org URLs), never the user's library, and the TMDB key stays
    server-side. Cached for several hours so repeated login-page hits don't call
    the TMDB API each time. Empty list (no key / failure) lets the frontend fall
    back to a plain gradient.
    """
    now = time.monotonic()
    cached = _backdrop_cache['urls']
    if not cached or now - _backdrop_cache['at'] > _BACKDROP_CACHE_TTL:
        fresh = _fetch_tmdb_backdrops()
        if fresh:
            _backdrop_cache['urls'] = fresh
            _backdrop_cache['at'] = now
            cached = fresh
    return {'backdrops': cached}


@ui_bp.route('/system/backup/download/<path:filename>', methods=['GET'])
@check_login
def backup_download(filename):
    backup_folder = os.path.realpath(settings.backup.folder)
    fullpath = os.path.realpath(os.path.join(backup_folder, filename))
    # Trailing-separator containment so a sibling dir sharing the prefix
    # (e.g. /config/backup-evil) cannot pass; realpath also defeats `..`/symlinks.
    if fullpath != backup_folder and not fullpath.startswith(backup_folder + os.sep):
        return '', 404
    return send_file(fullpath, max_age=0, as_attachment=True)


@ui_bp.route('/api/swaggerui/static/<path:filename>', methods=['GET'])
def swaggerui_static(filename):
    basepath = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'libs', 'flask_restx',
                            'static')
    fullpath = os.path.realpath(os.path.join(basepath, filename))
    # Use startswith to prevent path traversal
    if not fullpath.startswith(os.path.realpath(basepath) + os.sep):
        return '', 404
    else:
        return send_file(fullpath)


def _resolve_and_validate(url_str):
    """Resolve DNS once and validate resolved IPs. Pick a safe address to pin to.
    Returns (resolved_ip, hostname, parsed) or raises ValueError."""
    parsed = urlparse(url_str)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("No hostname in URL")
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    addrs = socket.getaddrinfo(hostname, port)
    if not addrs:
        raise ValueError("DNS resolution returned no results")
    # Find a safe (non-link-local, non-loopback) address to pin to.
    # Dual-stack hosts may resolve to both private LAN and link-local IPv6.
    safe_ip = None
    for _, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_link_local and not ip.is_loopback:
            if safe_ip is None:
                safe_ip = sockaddr[0]
    if safe_ip is None:
        raise ValueError("All resolved addresses are link-local or loopback")
    return safe_ip, hostname, parsed


def _resolve_and_validate_constrained(url_str):
    """Relaxed twin of _resolve_and_validate for the connection-test path.

    Caller hard-codes the request path to /api/system/status or
    /api/v3/system/status, so an authenticated UI session can only probe
    whether a Sonarr/Radarr-shaped service is listening at the user's
    typed host:port. The bounded surface justifies allowing loopback,
    link-local, and private targets that the strict guard rejects.

    Loopback supports bare-metal installs where Bazarr+ and Sonarr both
    bind to localhost. IPv6 link-local (fe80::/10) is the default for
    every IPv6 NIC on a single-host install. IPv4 link-local
    (169.254.0.0/16) is the DHCP fallback. None of these are valid
    rejection targets once the path is locked.

    The IP-class guard is reduced to a sanity filter: only multicast
    and unspecified addresses are rejected because those cannot host a
    TCP service. Everything else passes.

    Returns (resolved_ips, hostname, parsed) where `resolved_ips` is a
    de-duplicated list of every safe address from the DNS query in the
    order returned by getaddrinfo. The list lets the caller fall back
    to the next address when a connection is refused, which matters on
    dual-stack hosts where a hostname resolves to both IPv6 and IPv4 but
    only one of the two has the service actually listening (the typical
    'localhost -> ::1 first, but only 127.0.0.1 binds' case).

    Raises ValueError if no usable address was returned.
    """
    parsed = urlparse(url_str)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("No hostname in URL")
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    addrs = socket.getaddrinfo(hostname, port)
    if not addrs:
        raise ValueError("DNS resolution returned no results")
    seen = set()
    resolved_ips = []
    for _, _, _, _, sockaddr in addrs:
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        ip = ipaddress.ip_address(ip_str)
        if ip.is_multicast or ip.is_unspecified:
            continue
        resolved_ips.append(ip_str)
    if not resolved_ips:
        raise ValueError(
            "No usable address resolved (multicast and unspecified are not valid TCP targets)"
        )
    return resolved_ips, hostname, parsed


# Each entry pins:
#   paths: tuple of upstream status endpoints to probe in order. First 200
#          wins. Sonarr/Radarr accept a v1 legacy + v3 path; Whisper-ASR
#          exposes a single /status endpoint.
#   apikey_required: True for Sonarr/Radarr (X-Api-Key required upstream);
#                    False for Whisper which has no API-key concept.
#   has_verify_ssl_setting: True only if `settings.<service>.verify_ssl`
#                           exists. get_ssl_verify is consulted in that
#                           case; otherwise we default verify=True so
#                           public Whisper instances over HTTPS still
#                           validate certificates.
_TEST_SERVICES = {
    'sonarr':    {'paths': ('/api/system/status', '/api/v3/system/status'),
                  'apikey_required': True,
                  'has_verify_ssl_setting': True},
    'radarr':    {'paths': ('/api/system/status', '/api/v3/system/status'),
                  'apikey_required': True,
                  'has_verify_ssl_setting': True},
    'whisperai': {'paths': ('/status',),
                  'apikey_required': False,
                  'has_verify_ssl_setting': False},
}


def _validate_test_base_url(base):
    """Reject obviously hostile or nonsensical inputs. Allow reverse-proxy
    base paths like /sonarr but refuse anything that smuggles a different
    request via .., query string, or fragment.
    """
    parsed = urlparse(base)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError('unsupported protocol')
    if not parsed.hostname:
        raise ValueError('missing host')
    if parsed.query or parsed.fragment:
        raise ValueError('query strings and fragments are not allowed in url')
    if '..' in (parsed.path or ''):
        raise ValueError('relative path segments are not allowed in url')
    return parsed


def _format_host_header(hostname, original_port, scheme):
    """Build an RFC 7230 §5.4-compliant Host header value.

    IPv6 literals MUST be bracketed in the Host header (`[::1]:8989`,
    not `::1:8989`) because the colon is otherwise ambiguous with the
    port separator. urlparse(...).hostname strips brackets, so we have
    to put them back when emitting the header.

    Port is included only when non-default for the scheme, matching the
    convention urllib3 follows when it generates Host headers itself.
    """
    is_ipv6 = ':' in hostname
    host = f'[{hostname}]' if is_ipv6 else hostname
    default_port = 443 if scheme == 'https' else 80
    if original_port and original_port != default_port:
        return f'{host}:{original_port}'
    return host


def _build_request_url(base_parsed, status_path, resolved_ip, hostname, pin):
    """Construct the (request_url, request_headers) pair for one probe.

    When `pin=True`, the URL is rewritten to use `resolved_ip` in the
    netloc and a `Host:` header is set so DNS rebinding between resolve
    and connect cannot redirect the request. This mode is correct for
    HTTP and for HTTPS with TLS verification disabled.

    When `pin=False`, the original hostname is preserved so urllib3 can
    set SNI to the hostname and validate the TLS certificate against
    it. This mode is correct for HTTPS with verify_ssl enabled. Pinning
    in that case would replace the hostname with an IP literal in the
    URL, urllib3 would set SNI to the IP, the server's cert (issued for
    e.g. sonarr.example.com) would not match the IP, and TLS hostname
    validation would fail every legitimate test.
    """
    base_path = (base_parsed.path or '').rstrip('/')
    test_path = base_path + status_path
    test_url = base_parsed._replace(path=test_path).geturl()
    headers = dict(HEADERS)
    if not pin:
        return test_url, headers
    parsed = urlparse(test_url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    pinned_netloc = (f'[{resolved_ip}]:{port}' if ':' in resolved_ip
                     else f'{resolved_ip}:{port}')
    pinned_url = parsed._replace(netloc=pinned_netloc).geturl()
    headers['Host'] = _format_host_header(hostname, parsed.port, parsed.scheme)
    return pinned_url, headers


@ui_bp.route('/test/<service>', methods=['GET'])
@check_login
def proxy_service(service):
    """Constrained connection tester for Sonarr/Radarr.

    Frontend supplies only base URL and apikey via query params; backend
    appends the known /api/[v3/]system/status path itself, so the surface
    available to an authenticated UI session is bounded to a status probe
    against the user-typed host:port. Loopback and link-local are allowed
    targets here (bare-metal installs, single-NIC IPv6 hosts) because the
    locked path makes the bounded surface safe.

    See LavX/bazarr#92 for the original report and rationale.
    """
    _require_proxy_api_key()
    if service not in _TEST_SERVICES:
        return dict(status=False, error='unsupported service', code=0)
    config = _TEST_SERVICES[service]
    base = (request.args.get('url') or '').strip()
    apikey = (request.args.get('apikey') or '').strip()
    if not base:
        return dict(status=False, error='missing url', code=0)
    if config['apikey_required'] and not apikey:
        return dict(status=False, error='missing apikey', code=0)

    try:
        base_parsed = _validate_test_base_url(base)
    except ValueError as e:
        return dict(status=False, error=f'Request blocked: {e}', code=0)

    try:
        resolved_ips, hostname, _ = _resolve_and_validate_constrained(
            base_parsed.geturl()
        )
    except (ValueError, socket.gaierror) as e:
        return dict(status=False, error=f'Request blocked: {e}', code=0)

    verify = (get_ssl_verify(service)
              if config['has_verify_ssl_setting'] else True)
    # Pin to a resolved IP only when TLS hostname validation is NOT
    # going to do the work for us. For HTTPS + verify=True, the cert's
    # SAN/CN check provides equivalent DNS-rebinding mitigation: an
    # attacker who poisons DNS still cannot mint a valid cert for the
    # hostname. Pinning in that case would replace the hostname with
    # the IP literal in the URL, SNI would be set to the IP, and a
    # legitimate cert installation would fail TLS validation.
    pin_to_ip = not (base_parsed.scheme == 'https' and verify is True)
    # When pinning is off, dual-stack fallback is unnecessary: urllib3
    # already iterates DNS-resolved addresses internally during the
    # actual GET. Iterate only the first usable IP slot in that case;
    # `_build_request_url(pin=False)` ignores it.
    candidate_ips = resolved_ips if pin_to_ip else resolved_ips[:1]

    last_response_code = 0
    last_error = None
    last_connection_error = None
    reachable_ip = None
    # Outer loop: each safe IP returned by DNS, in original order. Inner
    # loop: each status path. Fall through to the next IP on
    # ConnectionError so dual-stack hosts where one address family is
    # not actually listening (e.g. localhost -> ::1 first but only
    # 127.0.0.1 binds) still produce a successful test.
    for resolved_ip in candidate_ips:
        if reachable_ip and reachable_ip != resolved_ip:
            # Already proven a different IP is reachable; do not
            # cross-probe additional addresses.
            break
        for status_path in config['paths']:
            request_url, request_headers = _build_request_url(
                base_parsed, status_path, resolved_ip, hostname, pin_to_ip
            )
            if apikey:
                request_headers['X-Api-Key'] = apikey
            try:
                result = requests.get(request_url, allow_redirects=False,
                                      verify=verify,
                                      timeout=5, headers=request_headers)
            except requests.ConnectionError as e:
                last_connection_error = repr(e)
                # Cannot reach this IP; try the next one. Skip the
                # remaining status paths for this IP.
                break
            except Exception as e:
                return dict(status=False, error=repr(e))
            reachable_ip = resolved_ip
            last_response_code = result.status_code
            if result.status_code == 200:
                try:
                    version = result.json()['version']
                    return dict(status=True, version=version,
                                code=result.status_code)
                except Exception:
                    last_error = 'Error Occurred. Check your settings.'
                    continue
            elif result.status_code == 401:
                return dict(status=False,
                            error='Access Denied. Check API key.',
                            code=result.status_code)
            elif result.status_code == 404:
                last_error = 'Cannot get version. Maybe unsupported legacy API call?'
                continue
            elif 300 <= result.status_code <= 399:
                return dict(status=False, error='Wrong URL Base.',
                            code=result.status_code)
            else:
                # result.raise_for_status() raises on 4xx/5xx
                # rather than returning a value, so wrapping it in dict()
                # made the route propagate an HTTPError to Flask and
                # surface as 500 to the frontend. Return a structured
                # error string instead so the UI can show the upstream
                # status code on transient failures.
                return dict(status=False,
                            error=f'Upstream returned status {result.status_code}',
                            code=result.status_code)
    if reachable_ip is None and last_connection_error is not None:
        return dict(status=False,
                    error=f'Cannot connect: {last_connection_error}', code=0)
    return dict(status=False,
                error=last_error or 'Cannot reach Sonarr/Radarr at the configured URL.',
                code=last_response_code)


@ui_bp.route('/test', methods=['GET'])
@ui_bp.route('/test/<protocol>/<path:url>', methods=['GET'])
@check_login
def proxy(protocol, url):
    _require_proxy_api_key()
    if protocol.lower() not in ['http', 'https']:
        return dict(status=False, error='Unsupported protocol', code=0)
    url = f'{protocol}://{unquote(url)}'
    try:
        resolved_ip, hostname, parsed = _resolve_and_validate(url)
    except (ValueError, socket.gaierror) as e:
        return dict(status=False, error=f'Request blocked: {e}', code=0)
    # Pin request to resolved IP to prevent DNS rebinding
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    pinned_netloc = f'{resolved_ip}:{port}'
    pinned_url = parsed._replace(netloc=pinned_netloc).geturl()
    pinned_headers = dict(HEADERS)
    pinned_headers['Host'] = hostname
    params = request.args
    try:
        result = requests.get(pinned_url, params, allow_redirects=False, verify=False, timeout=5, headers=pinned_headers)
    except Exception as e:
        return dict(status=False, error=repr(e))
    else:
        if result.status_code == 200:
            try:
                version = result.json()['version']
                return dict(status=True, version=version, code=result.status_code)
            except Exception:
                return dict(status=False, error='Error Occurred. Check your settings.', code=result.status_code)
        elif result.status_code == 401:
            return dict(status=False, error='Access Denied. Check API key.', code=result.status_code)
        elif result.status_code == 404:
            return dict(status=False, error='Cannot get version. Maybe unsupported legacy API call?',
                        code=result.status_code)
        elif 300 <= result.status_code <= 399:
            return dict(status=False, error='Wrong URL Base.', code=result.status_code)
        else:
            return dict(status=False, error=result.raise_for_status(), code=result.status_code)
