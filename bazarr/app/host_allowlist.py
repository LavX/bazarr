# coding=utf-8

"""The web application's request-host gate.

This module owns the backend half of the check: the WSGI middleware in front
of Flask, the refusal page and the JSON refusal, the approval code that lets
an operator allow a hostname without editing files, and the health items. The
policy itself (which hostnames are recognized) lives in host_policy.py, which
docker/supervisor.py also loads, so both layers apply the same rules.

The approval code lives in memory only and is written to the process console
through a logger of its own, with propagation off: every other log line also
reaches bazarr.log, which is readable over HTTP, so the code never may. The
refusal page tells the operator where to find it.
"""

import html
import json
import logging
import secrets
import threading
import time

from .config import settings
from .host_policy import (ALLOWED_HOSTS_ENV, APPROVAL_SEGMENT, DAY_SECONDS, HOUR_SECONDS,
                          REFUSED_REASON, BoundedRefusalRecorder, effective_host, entries_from_environ,
                          normalize_entries, allows_all_hosts, refused_hostnames)

# The refusal status: the frontend never renders it as the app.
REFUSED_STATUS = 421

# 64 bits of randomness in the approval code: cheap to mint, hopeless to guess.
CODE_BYTES = 8

# The logger that writes the approval code to the console and nowhere else.
CODE_LOGGER_NAME = 'bazarr.host_approval'

# Where the shim in app.py keeps the Host header the request arrived with,
# before the forwarded-header middleware replaces environ['HTTP_HOST'].
RAW_HOST_ENVIRON = 'bazarr.raw_host'


def check_active(config=settings):
    """The check runs only while no login mode is configured.

    With form or basic login a request that gets this far has presented a
    valid credential, and the gate the check closes is not the one that
    matters.
    """
    auth_type = getattr(getattr(config, 'auth', None), 'type', None)
    return auth_type not in ('form', 'basic')


def approval_path(config=settings):
    """The full path of the approval endpoint, under the install's base_url."""
    configured = (getattr(getattr(config, 'general', None), 'base_url', '') or '').rstrip('/')
    return f"{configured}/{APPROVAL_SEGMENT}"


def approval_paths(config=settings):
    """Every path the approval form is served at.

    The install's base_url prefixes one of them, and a proxy that strips the
    prefix leaves the other at the root, so the form is submitted at both.
    """
    return approval_path(config), f"/{APPROVAL_SEGMENT}"


def _configured_hosts(config):
    hosts = getattr(getattr(config, 'general', None), 'allowed_hosts', None)
    return list(hosts) if isinstance(hosts, (list, tuple)) else []


def _is_api_path(path, config):
    prefix = (getattr(getattr(config, 'general', None), 'base_url', '') or '').rstrip('/') + '/api'
    return path == prefix or path.startswith(prefix + '/')


def host_page(host, *, also=None, message=None, config=settings):
    """The static refusal page, which doubles as the approval form.

    It names the refused hostname and every way to allow it, and it is the
    entire response: nothing from the application or its settings is rendered
    into it. The form names the first refused hostname, so it is always a
    hostname that has to be allowed; a request that carried two refused
    hostnames names the second one as well. ``also`` and ``message`` are
    keyword-only so an error message can never land in the forwarded-hostname
    slot: the recovery pages answer with their problem in the error
    paragraph, not rendered as a second hostname.
    """
    shown = html.escape(host or '')
    named = shown if shown else 'a request that carried no hostname'
    if also:
        named += f' (and its forwarded hostname {html.escape(also)})'
    action = html.escape(approval_path(config))
    message_html = f'<p class="error">{html.escape(message)}</p>' if message else ''
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bazarr+ does not recognize this address</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; }}
code {{ background: #f0f0f0; padding: 0 0.2rem; }}
.error {{ color: #b00020; }}
</style>
</head>
<body>
<h1>Bazarr+ does not recognize this address</h1>
<p>While no login is configured, Bazarr+ only answers requests for hostnames it
recognizes. This one arrived for <code>{named}</code>.</p>
<p>To answer it, add <code>{named}</code> to <code>general.allowed_hosts</code>
in config.yaml (a file edited by hand applies after a restart), or set
<code>{ALLOWED_HOSTS_ENV}</code> in the environment, or enter the one-time
approval code below. The code is printed to the console Bazarr+ runs in:
the terminal, <code>docker logs</code> for a container, or
<code>journalctl</code> for a service. It never appears in bazarr.log, and it
is replaced after each use and by every restart: use the most recent line.</p>
{message_html}
<form method="post" action="{action}">
<input type="hidden" name="host" value="{shown}">
<p><label for="host-approval-code">Approval code</label>
<input id="host-approval-code" type="text" name="code" autocomplete="off"></p>
<p><button type="submit">Allow this hostname</button></p>
</form>
</body>
</html>"""


def host_json(host, also=None):
    """The JSON refusal the API and the socket path receive."""
    body = {
        'code': REFUSED_REASON,
        'host': host,
        'message': 'Bazarr+ does not answer requests for hostnames it does not recognize while no login is '
                   f'configured. Allow the hostname under Settings > General > Allowed Hosts, set '
                   f'{ALLOWED_HOSTS_ENV}, or use the approval code on the refused-host page.',
    }
    if also:
        body['also_refused'] = also
    return json.dumps(body)


def _console_logger():
    """The logger for the approval code: console only, never bazarr.log.

    Propagation is off unconditionally: the root logger's file handler is
    what writes bazarr.log, which is readable over HTTP, so no record this
    logger emits may ever reach it.
    """
    logger = logging.getLogger(CODE_LOGGER_NAME)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
        logger.addHandler(handler)
    return logger


class _ApprovalCode:
    """The one current approval code, minted on demand and replaced on use.

    It is announced at boot and then at most once an hour while refusals
    continue, so a refused hostname always has a code in the console to use.
    Every restart mints and announces a new one, which is why the refusal page
    sends the operator to the most recent line.
    """

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self._code = None
        self._announced = 0.0

    def clear(self):
        with self._lock:
            self._code = None
            self._announced = 0.0

    def _announce_locked(self):
        """Write the current code to the console, if the hour allows it.

        The caller holds the lock: announcing and consuming share it, so two
        submissions of the same code cannot both be accepted.
        """
        now = self._now()
        if self._code is None:
            self._code = secrets.token_hex(CODE_BYTES)
        if self._announced and now - self._announced < HOUR_SECONDS:
            return False
        self._announced = now
        _console_logger().info(
            'Bazarr+ approval code for refused hostnames: %s. Enter it on the refused-host page to allow '
            'this hostname. It is replaced after each use and appears only here, never in bazarr.log.',
            self._code)
        return True

    def announce(self):
        """Write the current code to the console, if the hour allows it."""
        with self._lock:
            return self._announce_locked()

    def verify(self, supplied):
        """Whether this is the current code; using it replaces it.

        The comparison, the consumption and the replacement all run under the
        one lock, so a code that is submitted twice in the same moment is
        accepted exactly once, and the replacement is announced straight away:
        the operator who just allowed one hostname has the next code on the
        console already, and the Docker front listener refusals cannot ask
        for a new one themselves.
        """
        with self._lock:
            code = self._code
            if not code or not supplied:
                return False
            supplied_bytes = str(supplied).strip().encode('utf-8')
            if not secrets.compare_digest(supplied_bytes, code.encode('utf-8')):
                return False
            self._code = secrets.token_hex(CODE_BYTES)
            self._announced = 0.0
            self._announce_locked()
            return True


_refusals = BoundedRefusalRecorder()
_approval_code = _ApprovalCode()


def _log_suppressed_summary(summary):
    """One line for the refusals past the hour's log ceiling, by window."""
    count, opened, closed = summary
    opened_at = time.strftime('%H:%M', time.localtime(opened))
    closed_at = time.strftime('%H:%M', time.localtime(closed))
    logging.info('BAZARR suppressed %d host-refusal log lines between %s and %s.',
                 count, opened_at, closed_at)


def _flush_suppressed_summary():
    """Log the due summary, so it is not lost when refusals stop arriving.

    The count is held for a full hour before it is reported; once it is due,
    the next refusal and every visit to System > Status both deliver it.
    """
    summary = _refusals.take_summary()
    if summary:
        _log_suppressed_summary(summary)


def announce_code(config=settings):
    """Announce the approval code at boot, when the check is armed."""
    if not check_active(config):
        return False
    return _approval_code.announce()


def verify_approval_code(supplied):
    """Whether the supplied approval code is the current one."""
    return _approval_code.verify(supplied)


def record_refusal(host, config=settings):
    """Record a refused hostname and keep a code on the console.

    The backend's own refusals call this, and so does the report the front
    listener in front of this backend sends: the hostnames that layer refuses
    never reach this process, so without the report they would never appear on
    the health page and the console code would never be repeated while they
    continue.
    """
    shown = effective_host(host)
    if not shown or not check_active(config):
        return
    if _refusals.note(shown):
        logging.warning('BAZARR refused a request for an unrecognized host: %s. Allow it under '
                        'Settings > General > Allowed Hosts, or set %s.', shown, ALLOWED_HOSTS_ENV)
    _flush_suppressed_summary()
    # Announce the code so a refused hostname has one in the console to
    # use; the announcement itself repeats at most once an hour.
    _approval_code.announce()


def front_refusal_notice(config=settings):
    """The report the front listener in front of this backend sends.

    It carries the token the supervisor started this process with, and the
    sender bounds it to at most one report per hostname per hour, so the work
    here is only the bookkeeping and the announcement.
    """
    from flask import request

    from .app import SUPERVISOR_TOKEN_HEADER, supervisor_token
    expected = supervisor_token()
    supplied = request.headers.get(SUPERVISOR_TOKEN_HEADER, '')
    if not expected or not secrets.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
        return '', 403
    record_refusal(request.form.get('host', ''), config=config)
    return '', 204


def health_issues(config=settings):
    """The request-host health items for System > Status.

    The wildcard switch is named while it is in force, and refused hostnames
    of the last day are listed so the operator can see what is being turned
    away. With a login configured the check is off and neither can happen.
    """
    if not check_active(config):
        return []
    # A summary that came due after the last refusal is logged here, so a
    # stream of refusals that stops does not take its count with it.
    _flush_suppressed_summary()
    issues = []
    entries = normalize_entries(_configured_hosts(config)) + entries_from_environ()
    if allows_all_hosts(entries):
        issues.append({
            'object': 'Allowed hosts',
            'issue': 'Every hostname is answered while no login is configured, because the allowed hosts '
                     'list holds a lone "*". Remove it to answer recognized hostnames only.'})
    refused = _refusals.recent(DAY_SECONDS)
    if refused:
        shown = ', '.join(refused[:3])
        more = f' and {len(refused) - 3} more' if len(refused) > 3 else ''
        issues.append({
            'object': 'Refused request hosts',
            'issue': f'{len(refused)} hostname(s) were refused in the last day: {shown}{more}. Allow a '
                     'hostname under Settings > General > Allowed Hosts, or set '
                     f'{ALLOWED_HOSTS_ENV}, to answer it.'})
    return issues


class HostAllowlistMiddleware:
    """WSGI middleware in front of Flask that refuses unrecognized hosts.

    It sits around the whole WSGI stack, so the API, the shell, static
    assets and the Socket.IO transport are all behind it. The approval paths
    stay open for exactly the one POST that submits the form on the refusal
    page: any other request for them is judged by its hostname like
    everything else.

    The hostnames it judges are the two a forwarded request can carry. The
    shim in app.py keeps the Host header the request arrived with, and the
    forwarded-header middleware that wraps this gate has since replaced
    environ['HTTP_HOST'] when the peer is general.trusted_proxy; both have to
    pass, and the two are one value for every request that carries no trusted
    forwarded host.
    """

    def __init__(self, wsgi_app, config=settings):
        self.wsgi_app = wsgi_app
        self.config = config

    def __call__(self, environ, start_response):
        if not check_active(self.config):
            return self.wsgi_app(environ, start_response)
        if (environ.get('REQUEST_METHOD') == 'POST'
                and environ.get('PATH_INFO', '') in approval_paths(self.config)):
            return self.wsgi_app(environ, start_response)
        entries = _configured_hosts(self.config)
        # The raw host is read by presence, not truthiness: the header the
        # forwarded-header middleware may have replaced must never stand in
        # for a Host header the request did not carry at all.
        raw_host = environ.get(RAW_HOST_ENVIRON)
        if raw_host is None:
            raw_host = environ.get('HTTP_HOST', '')
        host = environ.get('HTTP_HOST', '')
        refused = refused_hostnames(raw_host, host, entries)
        if not refused:
            return self.wsgi_app(environ, start_response)
        return self._refuse(environ, start_response, refused)

    def _refuse(self, environ, start_response, refused):
        # The first refused hostname is the one the client sent, so it is
        # the one the page, the form and the record name; a request that
        # carried two refused hostnames names the forwarded one as well, and
        # both are recorded, each within the bounds of its own entry.
        shown = effective_host(refused[0])
        also = effective_host(refused[1]) if len(refused) > 1 else None
        record_refusal(shown, config=self.config)
        if also:
            record_refusal(also, config=self.config)
        if _is_api_path(environ.get('PATH_INFO', ''), self.config):
            body = host_json(shown, also=also)
            content_type = 'application/json'
        else:
            body = host_page(shown, also=also, config=self.config)
            content_type = 'text/html; charset=utf-8'
        data = body.encode('utf-8')
        start_response(f'{REFUSED_STATUS} Misdirected Request',
                       [('Content-Type', content_type),
                        ('Content-Length', str(len(data))),
                        ('Cache-Control', 'no-store')])
        return [data]
