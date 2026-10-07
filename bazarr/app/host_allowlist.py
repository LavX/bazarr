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
                          host_is_allowed, normalize_entries, allows_all_hosts)

# The refusal status: the frontend never renders it as the app.
REFUSED_STATUS = 421

# 64 bits of randomness in the approval code: cheap to mint, hopeless to guess.
CODE_BYTES = 8

# The logger that writes the approval code to the console and nowhere else.
CODE_LOGGER_NAME = 'bazarr.host_approval'


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


def _configured_hosts(config):
    hosts = getattr(getattr(config, 'general', None), 'allowed_hosts', None)
    return list(hosts) if isinstance(hosts, (list, tuple)) else []


def _is_api_path(path, config):
    prefix = (getattr(getattr(config, 'general', None), 'base_url', '') or '').rstrip('/') + '/api'
    return path == prefix or path.startswith(prefix + '/')


def host_page(host, message=None, config=settings):
    """The static refusal page, which doubles as the approval form.

    It names the refused hostname and every way to allow it, and carries
    neither the API key nor the application shell: a refused request is
    answered with exactly this page and nothing else.
    """
    shown = html.escape(host or '')
    named = shown if shown else 'a request that carried no hostname'
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
is replaced after each use.</p>
{message_html}
<form method="post" action="{action}">
<input type="hidden" name="host" value="{shown}">
<p><label for="host-approval-code">Approval code</label>
<input id="host-approval-code" type="text" name="code" autocomplete="off"></p>
<p><button type="submit">Allow this hostname</button></p>
</form>
</body>
</html>"""


def host_json(host):
    """The JSON refusal the API and the socket path receive."""
    return json.dumps({
        'code': REFUSED_REASON,
        'host': host,
        'message': 'Bazarr+ does not answer requests for hostnames it does not recognize while no login is '
                   f'configured. Allow the hostname under Settings > General > Allowed Hosts, set '
                   f'{ALLOWED_HOSTS_ENV}, or use the approval code on the refused-host page.',
    })


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

    def announce(self):
        """Write the current code to the console, if the hour allows it."""
        now = self._now()
        with self._lock:
            if self._code is None:
                self._code = secrets.token_hex(CODE_BYTES)
            if self._announced and now - self._announced < HOUR_SECONDS:
                return False
            self._announced = now
            code = self._code
        _console_logger().info(
            'Bazarr+ approval code for refused hostnames: %s. Enter it on the refused-host page to allow '
            'this hostname. It is replaced after each use and appears only here, never in bazarr.log.', code)
        return True

    def verify(self, supplied):
        """Whether this is the current code; using it replaces it."""
        with self._lock:
            code = self._code
        if not supplied or not code:
            return False
        supplied_bytes = str(supplied).strip().encode('utf-8')
        if not secrets.compare_digest(supplied_bytes, code.encode('utf-8')):
            return False
        with self._lock:
            self._code = None
            self._announced = 0.0
        return True


_refusals = BoundedRefusalRecorder()
_approval_code = _ApprovalCode()


def announce_code(config=settings):
    """Announce the approval code at boot, when the check is armed."""
    if not check_active(config):
        return False
    return _approval_code.announce()


def verify_approval_code(supplied):
    """Whether the supplied approval code is the current one."""
    return _approval_code.verify(supplied)


def health_issues(config=settings):
    """The request-host health items for System > Status.

    The wildcard switch is named while it is in force, and refused hostnames
    of the last day are listed so the operator can see what is being turned
    away. With a login configured the check is off and neither can happen.
    """
    if not check_active(config):
        return []
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
    assets and the Socket.IO transport are all behind it, and it reads only
    the host the server hands it: waitress has already replaced it with the
    forwarded host when the peer is general.trusted_proxy, and it ignores
    every forwarded header otherwise. The refusal answers nothing but itself,
    and the approval path stays open for a refused hostname, because
    approving one is what it is for.
    """

    def __init__(self, wsgi_app, config=settings):
        self.wsgi_app = wsgi_app
        self.config = config

    def __call__(self, environ, start_response):
        if not check_active(self.config):
            return self.wsgi_app(environ, start_response)
        if environ.get('PATH_INFO', '') == approval_path(self.config):
            return self.wsgi_app(environ, start_response)
        host = environ.get('HTTP_HOST', '')
        if host_is_allowed(host, _configured_hosts(self.config)):
            return self.wsgi_app(environ, start_response)
        return self._refuse(environ, start_response, host)

    def _refuse(self, environ, start_response, host):
        shown = effective_host(host)
        if _refusals.note(shown):
            logging.warning('BAZARR refused a request for an unrecognized host: %s. Allow it under '
                            'Settings > General > Allowed Hosts, or set %s.', shown, ALLOWED_HOSTS_ENV)
        suppressed = _refusals.take_summary()
        if suppressed:
            logging.info('BAZARR suppressed %d host-refusal log lines in the last hour.', suppressed)
        # Announce the code so a refused hostname has one in the console to
        # use; the announcement itself repeats at most once an hour.
        _approval_code.announce()
        if _is_api_path(environ.get('PATH_INFO', ''), self.config):
            body = host_json(shown)
            content_type = 'application/json'
        else:
            body = host_page(shown, config=self.config)
            content_type = 'text/html; charset=utf-8'
        data = body.encode('utf-8')
        start_response(f'{REFUSED_STATUS} Misdirected Request',
                       [('Content-Type', content_type),
                        ('Content-Length', str(len(data))),
                        ('Cache-Control', 'no-store')])
        return [data]
