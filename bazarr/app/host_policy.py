# coding=utf-8

"""The request-host policy, shared by the web application and the supervisor.

While no login mode is configured, Bazarr+ answers requests only for hostnames
it recognizes: the default set below, the entries of ``general.allowed_hosts``
in config.yaml, and the entries of the ``BAZARR_ALLOWED_HOSTS`` environment
variable. This module holds the rules only, with no application imports, so
docker/supervisor.py can load it straight from the file and apply the same
decision as the backend. It also holds the bounded refusal bookkeeping both
layers share, so neither can record or log more than the agreed ceiling.
"""

import os
import threading
import time
from collections import OrderedDict
from ipaddress import ip_address

# The environment variable that carries extra allowed hosts, comma-separated,
# for an operator who cannot reach the UI or edit config.yaml.
ALLOWED_HOSTS_ENV = 'BAZARR_ALLOWED_HOSTS'

# Local-only suffixes: no public DNS zone can serve a name under them, so a
# request that arrives for one is not a request some other site redirected.
LOCAL_ONLY_SUFFIXES = ('.local', '.lan', '.home.arpa', '.internal', '.localhost')

# The path the refusal page's form posts to, relative to the install's
# base_url. Both layers must leave it open for a refused hostname, because
# approving one is the whole point of the path.
APPROVAL_SEGMENT = 'host-approval'

# The reason code the JSON refusal carries under the API and the socket path.
REFUSED_REASON = 'host_not_allowed'

# A hostname is at most 253 characters long; a longer input cannot be one.
MAX_HOSTNAME_LENGTH = 253

# The refusal record holds the most recent refused hostnames, for the health
# page and the log's one-per-host pace. Everything else about a refusal is
# answered and forgotten.
REFUSED_HOSTS_LIMIT = 50
HOUR_SECONDS = 3600.0
DAY_SECONDS = 86400.0
# Per hour: at most this many per-host refusal lines, then one line an hour
# that counts the suppressed ones.
REFUSAL_LOG_LINES_PER_HOUR = 20


def effective_host(host):
    """The comparable form of a request's host.

    Lowercased, the port stripped, IPv6 brackets removed, and one trailing
    dot dropped, so ``Bazarr.Local:6767`` and ``bazarr.local`` compare equal
    and an operator can paste either form into the allowlist.
    """
    host = str(host or '').strip().lower().rstrip('.')
    if not host:
        return ''
    if host.startswith('['):
        end = host.find(']')
        if end != -1:
            return host[1:end]
        return host
    if host.count(':') == 1:
        name, _, port = host.rpartition(':')
        if port.isdigit() and name:
            return name
    return host


def _is_address(host):
    try:
        ip_address(host)
    except ValueError:
        return False
    return True


def in_default_set(host):
    """True for a hostname that cannot come from a public DNS zone.

    IP literals, ``localhost``, single-label names (``bazarr``, ``nas``) and
    names under a local-only suffix. Everything else needs an entry in the
    configured allowlist. The host must already be in effective form.
    """
    if not host:
        return False
    if host == 'localhost' or _is_address(host):
        return True
    if '.' not in host:
        return True
    return host.endswith(LOCAL_ONLY_SUFFIXES)


def normalize_entries(entries):
    """Configured entries in effective form, blanks and non-strings dropped."""
    if not isinstance(entries, (list, tuple)):
        return []
    normalized = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        host = effective_host(entry)
        if host:
            normalized.append(host)
    return normalized


def entries_from_environ(environ=None):
    """The entries of BAZARR_ALLOWED_HOSTS, normalized."""
    environ = os.environ if environ is None else environ
    raw = environ.get(ALLOWED_HOSTS_ENV, '')
    if not raw:
        return []
    return normalize_entries([part for part in raw.split(',')])


def allows_all_hosts(entries):
    """True when an entry is a lone ``*``, which answers every hostname.

    The switch exists so an operator who cannot make a hostname work has a way
    back; the health page names it, because an install that answers every
    hostname while no login is configured is worth a warning.
    """
    return '*' in entries


def host_matches(host, entry):
    """Whether one allowlist entry answers the host.

    Exact names, or ``*.example.com`` for any depth of labels below the
    domain. The bare domain itself is not covered: an operator who wants both
    adds both.
    """
    if entry.startswith('*.'):
        return host.endswith(entry[1:])
    return host == entry


def host_is_allowed(host, configured_entries, environ=None):
    """Whether the check answers a request for this host.

    ``True`` is also the answer when the allowlist holds the lone ``*``
    switch, because the check is off rather than permissive.
    """
    entries = normalize_entries(configured_entries) + entries_from_environ(environ)
    if allows_all_hosts(entries):
        return True
    host = effective_host(host)
    if in_default_set(host):
        return True
    return any(host_matches(host, entry) for entry in entries)


class BoundedRefusalRecorder:
    """Bookkeeping for refusals, bounded on every axis.

    The record holds the ``REFUSED_HOSTS_LIMIT`` most recent refused hosts with
    the time of their last refusal. A host gets a log line at most once per
    hour, refusal lines are capped at ``REFUSAL_LOG_LINES_PER_HOUR`` per hour
    in total, and past the cap one summary line per hour counts the suppressed
    ones. The caller owns the wording and the channel: ``note`` returns
    whether a per-host line may be written now, and ``take_summary`` returns
    the suppressed count when a summary line is due (and resets it).
    """

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self._hosts = OrderedDict()  # host -> [last_refused, last_log_line]
        self._line_times = []  # times of per-host refusal lines this hour
        self._suppressed = 0
        self._last_summary = 0.0

    def note(self, host):
        """Record a refusal; True when a per-host line may be written now."""
        now = self._now()
        with self._lock:
            record = self._hosts.get(host)
            if record is None:
                self._hosts[host] = [now, 0.0]
                while len(self._hosts) > REFUSED_HOSTS_LIMIT:
                    self._hosts.popitem(last=False)
                record = self._hosts[host]
            record[0] = now
            self._line_times = [stamp for stamp in self._line_times if now - stamp < HOUR_SECONDS]
            within_hour = (now - record[1]) < HOUR_SECONDS
            if within_hour or len(self._line_times) >= REFUSAL_LOG_LINES_PER_HOUR:
                self._suppressed += 1
                return False
            record[1] = now
            self._line_times.append(now)
            return True

    def take_summary(self):
        """The suppressed count when a summary line is due, else 0."""
        now = self._now()
        with self._lock:
            if not self._suppressed:
                return 0
            if self._last_summary and now - self._last_summary < HOUR_SECONDS:
                return 0
            suppressed = self._suppressed
            self._suppressed = 0
            self._last_summary = now
            return suppressed

    def recent(self, within_seconds):
        """The refused hosts of the window, oldest first."""
        now = self._now()
        with self._lock:
            return [host for host, record in self._hosts.items()
                    if now - record[0] < within_seconds]
