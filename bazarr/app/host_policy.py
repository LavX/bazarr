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
import re
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
    host = str(host or '').strip().lower()
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
            host = name
    # The port goes first, then one trailing dot: the dot is the zone root
    # ("bazarr.example.com."), and "name.:8443" otherwise keeps its dot and
    # never matches the entry an operator pasted.
    if host.endswith('.'):
        host = host[:-1]
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
    """Configured entries in effective form, blanks and non-strings dropped.

    A single string is the comma-separated form: a config.yaml edited by hand
    names one hostname most naturally as a scalar, and it reads back as that
    one entry the same way the environment variable does.
    """
    if isinstance(entries, str):
        entries = entries.split(',')
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


def refused_hostnames(raw, effective, configured_entries, environ=None):
    """The hostnames of a request the allowlist does not admit, in order.

    A request from a trusted proxy carries two of them. The one the client
    sent is judged first, so when only one of the two fails it is the failing
    one that is named, and when both fail they both are. A request that
    carried none at all is refused as well: a request the Host header cannot
    name is not one the allowlist can answer for. The same name twice,
    modulo a port, a case or a bracket, is one refusal, so a request cannot
    be named twice for one hostname.
    """
    refused = []
    seen = set()
    for name in (raw, effective):
        if not name or not host_is_allowed(name, configured_entries, environ):
            key = effective_host(name)
            if key not in seen:
                seen.add(key)
                refused.append(name)
    return refused


# One label of a hostname: letters, digits and inner hyphens, at most 63
# characters, neither starting nor ending in a hyphen.
_HOSTNAME_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
_APPROVABLE_HOSTNAME = re.compile(rf'^(?:{_HOSTNAME_LABEL})(?:\.{_HOSTNAME_LABEL})*$')


def is_approvable_host(host):
    """Whether the approval form may add this hostname to the allowlist.

    An IP literal or a plain name of labels. Anything else (a wildcard,
    whitespace, punctuation or text that is not a hostname) is refused, so the
    form can only ever add a name the allowlist can match.
    """
    host = str(host or '').strip().lower()
    if not host or len(host) > MAX_HOSTNAME_LENGTH:
        return False
    if _is_address(host):
        return True
    return bool(_APPROVABLE_HOSTNAME.match(host))


def peer_is_trusted_proxy(remote, trusted_proxy=None):
    """Whether a peer address may name the hostname the client arrived for.

    One canonical comparison, the one the backend's forwarded-header
    middleware applies: a lone ``*`` names every address, and any other
    value is compared with the peer exactly as written (waitress compares
    ``remote_peer == trusted_proxy``). A mapped IPv6 form of a configured
    IPv4 address is therefore a different peer here exactly as it is there,
    so one spelling of the setting works at both layers or neither trusts
    it. The setting's default applies when it is absent; an explicitly empty
    setting trusts nobody. Anything that is not one address (a
    comma-separated list, a network range) matches no one here, exactly as
    it does not at the backend.
    """
    if not remote:
        return False
    spec = '127.0.0.1' if trusted_proxy is None else str(trusted_proxy).strip()
    if not spec or spec.count(','):
        return False
    if spec == '*':
        return True
    return str(remote) == spec


class BoundedRefusalRecorder:
    """Bookkeeping for refusals, bounded on every axis.

    The record holds the ``REFUSED_HOSTS_LIMIT`` most recently refused hosts
    (a repeated one moves to the end, so the record is the most recent and not
    the first-sighted). The once-per-host log pace is kept apart from that
    record, so evicting a hostname does not reset its pace. Refusal lines are
    capped at ``REFUSAL_LOG_LINES_PER_HOUR`` per hour in total, and past the
    cap one summary line counts the suppressed ones once the hour it opened in
    has run. The caller owns the wording and the channel: ``note`` returns
    whether a per-host line may be written now, and ``take_summary`` returns
    the suppressed window when a summary line is due (and clears it).

    The two processes that apply this policy share no memory, so the hour's
    line budget is shared through the front listener's report: ``note`` takes
    ``line_at``, the time a console elsewhere already spent this hostname's
    line within the hour, and records the refusal without writing or
    suppressing a line for it, so the hostname is not named twice in the
    same window. A hostname refused for the first time after the recovery
    has no such time to carry, so the report also carries how many lines
    that console spent within the hour, and ``merge_hour_lines`` folds that
    many into this hour's cap: the refusals that follow the recovery draw
    on the one merged budget rather than a fresh cap.
    """

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self._hosts = OrderedDict()  # host -> time of its last refusal
        self._last_lines = OrderedDict()  # host -> time of its last log line
        self._line_times = []  # times of per-host refusal lines this hour
        self._suppressed = 0
        self._window_opens = None  # when the current suppressed count started
        self._merged_lines = 0  # the hour count already folded in from a console
        self._merged_at = None  # when that count was last folded in

    def note(self, host, line_at=None):
        """Record a refusal; True when a per-host line may be written now.

        ``line_at`` carries the time a console elsewhere spent this
        hostname's line within the hour, reported by the front listener
        that wrote it while the backend it fronts could not be told: the
        refusal is recorded all the same, but no second line is written
        for it and this hour's cap here is not spent on the one already
        spent there. A line held back this way is not counted as
        suppressed either, because the console that spent it keeps its own
        count of what it held back.
        """
        now = self._now()
        with self._lock:
            if host in self._hosts:
                self._hosts.move_to_end(host)
            self._hosts[host] = now
            while len(self._hosts) > REFUSED_HOSTS_LIMIT:
                self._hosts.popitem(last=False)
            self._prune(now)
            if line_at is not None and now - line_at < HOUR_SECONDS:
                if host in self._last_lines:
                    self._last_lines[host] = max(self._last_lines[host], line_at)
                else:
                    self._last_lines[host] = line_at
                return False
            if host in self._last_lines or len(self._line_times) >= REFUSAL_LOG_LINES_PER_HOUR:
                self._suppressed += 1
                return False
            self._last_lines[host] = now
            self._line_times.append(now)
            return True

    def hour_line_count(self):
        """How many refusal lines this hour has already spent here."""
        now = self._now()
        with self._lock:
            self._prune(now)
            return len(self._line_times)

    def merge_hour_lines(self, count):
        """Fold a console elsewhere's spent lines into this hour's cap.

        The reports a front listener sends once it can be told carry how
        many refusal lines its console spent within the hour, and a report
        goes out per hostname, so the same count arrives many times: the
        count of a window is folded in once, and a report that repeats it
        spends nothing again. Only the lines the hour still holds are
        folded in, so the answer stays bounded by the same cap the count
        came from.
        """
        now = self._now()
        if count <= 0:
            return
        with self._lock:
            self._prune(now)
            if self._merged_at is None or now - self._merged_at >= HOUR_SECONDS:
                self._merged_lines = 0
            spent = min(count, REFUSAL_LOG_LINES_PER_HOUR)
            if spent > self._merged_lines:
                self._line_times.extend([now] * (spent - self._merged_lines))
                self._merged_lines = spent
            self._merged_at = now

    def note_retry(self, host):
        """Record a retry's line; True when a per-host line may be written now.

        The pace and the cap are consulted as ``note`` consults them and an
        allowed answer spends the same allowance, but a line they hold back
        is not counted as suppressed: the delivery retries of a failed
        report ask here, and an hour of them must not add up to a summary
        that says a stream of refusals was held back when one hostname's
        line was. The refusal record itself is left alone, because a
        report's retry is not a new refusal.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            if host in self._last_lines or len(self._line_times) >= REFUSAL_LOG_LINES_PER_HOUR:
                return False
            self._last_lines[host] = now
            self._line_times.append(now)
            return True

    def _prune(self, now):
        self._line_times = [stamp for stamp in self._line_times if now - stamp < HOUR_SECONDS]
        self._last_lines = OrderedDict(
            (name, stamp) for name, stamp in self._last_lines.items() if now - stamp < HOUR_SECONDS)

    def take_summary(self):
        """The suppressed window when a summary line is due, else ``None``.

        A count that starts being suppressed is held for a full hour before
        it is reported, so the line counts every refusal suppressed in that
        window. The answer is that count with the window's two ends, and
        taking it clears the window.
        """
        now = self._now()
        with self._lock:
            if not self._suppressed:
                return None
            if self._window_opens is None:
                self._window_opens = now
                return None
            if now - self._window_opens < HOUR_SECONDS:
                return None
            suppressed = self._suppressed
            opened = self._window_opens
            self._suppressed = 0
            self._window_opens = None
            return suppressed, opened, now

    def recent(self, within_seconds):
        """The refused hosts of the window, newest first."""
        now = self._now()
        with self._lock:
            return [host for host, stamp in reversed(self._hosts.items())
                    if now - stamp < within_seconds]
