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

import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from ipaddress import ip_address
from pathlib import Path

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

# A suppressed window's identity, minted once when the window opens: the
# two layers that share a window have no memory in common, so this is how
# each tells a window it has already counted from one it has not, however
# often the window changes hands between them. The bits are random, so two
# windows minted in the two layers at the same moment are still distinct.
WINDOW_ID_BYTES = 8

# How long the console may keep re-carrying a sent copy no answer resolved,
# and the same span the backend must remember the window's ID: a copy the
# console can still re-carry always meets a backend that still remembers
# it, and a copy no backend remembers can no longer recur. One constant
# carries both sides, so the backend's retention and the console's hold
# cannot drift apart.
CARRY_RETENTION_SECONDS = 2 * HOUR_SECONDS


def _mint_window_id():
    """A fresh window ID: 64 random bits, unique across both layers."""
    return secrets.token_hex(WINDOW_ID_BYTES)


def _mint_line_id():
    """A fresh line identity: 64 random bits, unique across both layers.

    Two lines can share one timestamp and are still two lines, so the
    identity beside the time is what makes each spent line its own slot in
    the budget the reports merge, wherever the line was spent.
    """
    return secrets.token_hex(WINDOW_ID_BYTES)


def mint_notice_id():
    """A fresh notice identity: 64 random bits, unique across restarts.

    A pending notice of a hostname carries the identity from the moment it
    is queued to the moment the acknowledgement of its delivery resolves
    it, so a delivery that outlives its own notice is told apart from
    whatever fresh notice of the same hostname the queue holds by then, and
    the receipt the backend writes names the notice the console sent. The
    bits are random, so a console that restarts mints identities no
    previous incarnation minted and no stale receipt can match.
    """
    return secrets.token_hex(WINDOW_ID_BYTES)


# The record of the windows whose summaries have been paid, in the
# configuration directory the two processes of one install share. A payment
# that reaches a layer is written here before its answer is built, and the
# other layer reads it back before it hands a paid window to a backend that
# restarted, so a restart that loses every in-memory record loses no proof
# of payment and the pair never folds paid lines in twice.
COMPLETED_WINDOWS_RECORD = 'host-refusal-completed-windows.json'
COMPLETED_WINDOWS_RECORD_FILE_ENV = 'BAZARR_REFUSAL_COMPLETIONS_FILE'
# The lock that serializes updates of the record: a file beside the record,
# in whatever directory resolved the record itself.
COMPLETED_WINDOWS_RECORD_LOCK_SUFFIX = '.lock'


def completed_windows_record_path(environ=None):
    """Where the pair's paid-window record lives, resolved the way both
    processes resolve their configuration: the explicit file the
    environment names, then the configuration directory BAZARR_CONFIG_DIR
    names, then the data directory beside the code. The supervisor exports
    its own resolved configuration directory under BAZARR_CONFIG_DIR for
    the backend it spawns, so the two processes of one install always meet
    the same file, restarts and all.
    """
    environ = os.environ if environ is None else environ
    override = environ.get(COMPLETED_WINDOWS_RECORD_FILE_ENV, '').strip()
    if override:
        return override
    config_dir = environ.get('BAZARR_CONFIG_DIR', '').strip()
    if not config_dir:
        config_dir = str(Path(__file__).resolve().parents[2] / 'data')
    return str(Path(config_dir) / 'config' / COMPLETED_WINDOWS_RECORD)


def _read_completed_windows_record(path):
    """The record's contents, or nothing: the record is best effort, and a
    missing or unreadable one proves no payment."""
    try:
        with open(path) as record_file:
            record = json.load(record_file)
        return record if isinstance(record, dict) else {}
    except Exception:
        return {}


def record_completed_windows(window_ids, paid_at, path=None):
    """Write the payment of one or more windows into the shared record.

    The entry outlives the in-memory completion record of the layer that
    paid, held on the same retention span both sides share, so the console
    that still holds a copy of a paid window can verify the payment from
    here however long the answer that would have told it stayed lost, and
    past the whole span no copy is still held to be verified. The write is
    best effort: a record nothing can write proves no payment, and the
    pair keeps the lose-not-double trade rather than fail a payment.

    Both processes pay into the one record, the backend at the arrival of
    a report and the console on its own timer, so two payments can be in
    flight at once: each update reads the record, merges its payment in
    and replaces the file, and one complete update is held against the
    next on the lock file beside the record, so neither payment can erase
    the other's proof.
    """
    if not window_ids:
        return
    path = completed_windows_record_path() if path is None else path
    record_path = Path(path)
    lock_handle = None
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(str(record_path) + COMPLETED_WINDOWS_RECORD_LOCK_SUFFIX, 'a+')
        # flock is the serialization: the operating system holds the lock
        # to the open file and releases it when its holder closes it or
        # dies, so a process that crashes mid-update leaves no lock behind.
        # The lock is best effort with the write it guards, exactly as the
        # write is: a platform or a directory it cannot be taken in
        # proceeds as one unlocked update rather than proving no payment.
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
            lock_handle = None
    try:
        record = _read_completed_windows_record(path)
        record = {window_id: stamp for window_id, stamp in record.items()
                  if isinstance(stamp, (int, float)) and paid_at - stamp < CARRY_RETENTION_SECONDS}
        for window_id in window_ids:
            record[window_id] = paid_at
        fd, tmp = tempfile.mkstemp(dir=str(record_path.parent),
                                   prefix=f".{record_path.name}.")
        try:
            with os.fdopen(fd, 'w') as record_file:
                json.dump(record, record_file)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except Exception:
        pass
    finally:
        if lock_handle is not None:
            lock_handle.close()


def completed_windows_record_has(window_id, path=None):
    """Whether the shared record shows this window's summary was paid."""
    if not window_id:
        return False
    path = completed_windows_record_path() if path is None else path
    return window_id in _read_completed_windows_record(path)


# The record of the notices the backend has taken in, in the configuration
# directory the two processes of one install share. Each entry holds two
# facts the pair must not confuse: the receipt, written before the notice
# is processed, proves the notice reached a backend that took it in, and
# the outcome, retained once the accounting completes, proves the refusal
# the notice carried was named or merged there. The console whose delivery
# failed reads the outcome back before it names the refusal: a notice whose
# accounting completed is named at the backend and the console names it
# nowhere else, while a backend that took a notice in and died before its
# accounting completed leaves the refusal named nowhere, and the console
# accounts it itself, the only other layer that saw it. A delivery no
# backend ever took in leaves no entry at all, and the console accounts it
# the same way. The backend is the record's one writer and the console only
# reads, and the write replaces the file in one step, so a read always
# meets one whole record.
NOTICE_RECEIPTS_RECORD = 'host-refusal-notice-receipts.json'
NOTICE_RECEIPTS_RECORD_FILE_ENV = 'BAZARR_REFUSAL_NOTICE_RECEIPTS_FILE'


def notice_receipts_record_path(environ=None):
    """Where the pair's notice-receipt record lives, resolved the way both
    processes resolve their configuration: the explicit file the
    environment names, then the configuration directory BAZARR_CONFIG_DIR
    names, then the data directory beside the code, exactly as the record
    of completed windows resolves, so the two processes of one install
    always meet the same file, restarts and all.
    """
    environ = os.environ if environ is None else environ
    override = environ.get(NOTICE_RECEIPTS_RECORD_FILE_ENV, '').strip()
    if override:
        return override
    config_dir = environ.get('BAZARR_CONFIG_DIR', '').strip()
    if not config_dir:
        config_dir = str(Path(__file__).resolve().parents[2] / 'data')
    return str(Path(config_dir) / 'config' / NOTICE_RECEIPTS_RECORD)


def _read_notice_receipts_record(path):
    """The record's contents, or nothing: the record is best effort, and a
    missing or unreadable one proves no notice was taken in."""
    try:
        with open(path) as record_file:
            record = json.load(record_file)
        return record if isinstance(record, dict) else {}
    except Exception:
        return {}


def _notice_record_entry(record, notice_id):
    """The notice's entry in the record, or a fresh one: an entry an older
    write left as a bare stamp carries no outcome, so it starts over."""
    entry = record.get(notice_id)
    return entry if isinstance(entry, dict) else {}


def _prune_notice_record(record, now):
    """Keep the entries whose newest fact is still within the retention
    span, so the record stays bounded by the same span both sides keep for
    a delivery no answer resolved."""
    pruned = {}
    for notice_id, entry in record.items():
        if not isinstance(entry, dict):
            continue
        stamps = [stamp for stamp in (entry.get('received'), entry.get('accounted'))
                  if isinstance(stamp, (int, float))]
        if any(now - stamp < CARRY_RETENTION_SECONDS for stamp in stamps):
            pruned[notice_id] = entry
    return pruned


def _store_notice_record(path, record):
    """Replace the record in one step, best effort: a record nothing can
    write proves nothing, and the console accounts the refusal rather than
    trust a proof that never reached the file."""
    record_path = Path(path)
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(record_path.parent),
                                   prefix=f".{record_path.name}.")
        try:
            with os.fdopen(fd, 'w') as record_file:
                json.dump(record, record_file)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except Exception:
        pass


def record_notice_receipt(notice_id, received_at, path=None):
    """Write the receipt of one notice the backend has taken in.

    The entry lives as long as the delivery's outcome can still be
    unresolved, held on the same retention span both sides share, so the
    console that holds the notice can read the facts back however long
    the answer that would have told it stayed lost, and past the whole span
    no notice of the delivery is still held to be reconciled, so the record
    stays bounded. The receipt is a fact about the taking in alone: it
    proves the notice reached a backend that accepted it, not that the
    refusal it carried was ever recorded, and the console whose delivery
    failed reads the outcome the accounting retained, never the receipt
    alone. The write is best effort, exactly as the record of completed
    windows is.
    """
    if not notice_id:
        return
    path = notice_receipts_record_path() if path is None else path
    record = _read_notice_receipts_record(path)
    entry = _notice_record_entry(record, notice_id)
    entry['received'] = received_at
    record[notice_id] = entry
    _store_notice_record(path, _prune_notice_record(record, received_at))


def record_notice_accounted(notice_id, accounted_at, path=None):
    """Retain the outcome of one notice's accounting: its refusal was named.

    The backend writes this once the notice's accounting completed, in the
    line the hour's budget spent on the refusal or in the fold of a line a
    console elsewhere already spent, and a backend that restarts reads it
    back before it accounts a retried notice again, because the refusal was
    named here before the restart and the pair owes it no second
    accounting. The entry is kept even when the receipt never reached the
    file, so the outcome outlives whatever the taking in did: a write that
    fails proves no outcome, and the console accounts the refusal rather
    than trust a proof that never reached the file.
    """
    if not notice_id:
        return
    path = notice_receipts_record_path() if path is None else path
    record = _read_notice_receipts_record(path)
    entry = _notice_record_entry(record, notice_id)
    entry['accounted'] = accounted_at
    record[notice_id] = entry
    _store_notice_record(path, _prune_notice_record(record, accounted_at))


def notice_receipt_has(notice_id, path=None):
    """Whether the shared record shows the backend took this notice in."""
    if not notice_id:
        return False
    path = notice_receipts_record_path() if path is None else path
    return notice_id in _read_notice_receipts_record(path)


def notice_accounted_has(notice_id, path=None):
    """Whether the shared record shows the backend completed this notice's
    accounting: the refusal it carried was named or merged there, and the
    console whose delivery failed names it nowhere else."""
    if not notice_id:
        return False
    path = notice_receipts_record_path() if path is None else path
    entry = _read_notice_receipts_record(path).get(notice_id)
    return (isinstance(entry, dict)
            and isinstance(entry.get('accounted'), (int, float)))


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
    line budget and its one summary are shared through the front listener's
    report: ``note`` takes ``line_at``, the time a console elsewhere already
    spent this hostname's line within the hour, and records the refusal
    without writing or suppressing a line for it, so the hostname is not
    named twice in the same window. A hostname refused for the first time
    after the recovery has no such time to carry, so the report also carries
    the times of the lines that console spent within the hour, and
    ``merge_hour_lines`` folds the ones this hour still holds into its cap:
    the refusals that follow the recovery draw on the one merged budget
    rather than a fresh cap, and it rolls over exactly when the console's
    lines do. The suppressed window travels the same way: the first report a
    backend acknowledges takes the console's window out with
    ``take_carried_summary``, and ``merge_summary`` folds it into this
    layer's, so the two layers together owe the one summary the combined
    window counts.

    The window carries a stable ID from the moment it opens, wherever it
    travels next: the report that carries it and the acknowledgement that
    carries one back both hold it, and ``merge_summary`` folds nowhere a
    window whose ID this layer has already counted, whether it minted the
    ID at the window's first line or folded the window in from elsewhere.
    So a delivery that has to be retried, in either direction, counts the
    same window exactly once: the backend keeps the window it holds even
    when its acknowledgement is lost, because ``hour_suppressed_window``
    hands out a copy and not a take, and the console keeps its own through
    the take and fold back of a failed report, so neither direction can
    lose accounting to a delivery that did not land.

    Two more pieces travel the same road. The summaries this layer logs name
    their windows on the completed list, every ID the paid window counts and
    not its own minted one alone, and the acknowledgements carry that list,
    so the layer that holds a copy of a window whose summary has been paid
    discards it. And every report carries the newest bounded refused
    set with the refusals' own stamps, which ``merge_recent`` folds into the
    record by taking the newer stamp per host: the record holds the most
    recently refused names whichever layer saw them, whichever of them the
    reports reached first.
    """

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self._hosts = OrderedDict()  # host -> time of its last refusal
        self._last_lines = OrderedDict()  # host -> time of its last log line
        self._line_times = []  # times of per-host refusal lines this hour
        self._line_ids = []  # the identity of each spent line, parallel to its time
        self._suppressed = 0
        self._window_opens = None  # when the current suppressed count started
        self._window_id = None  # the current window's ID, minted at its first line
        self._window_ids = []  # every ID the current window counts, its own and each folded-in one
        self._counted_windows = OrderedDict()  # window ID -> when it was counted here
        self._completed_windows = OrderedDict()  # window ID -> when it was paid here
        self._first_failures = OrderedDict()  # host -> when its first failed notice counted
        self._notice_accounted = OrderedDict()  # notice identity -> when its refusal was named here

    def _count_suppressed(self, now):
        """Count one held-back line into the window, minting its stable ID.

        The ID is minted when the window opens, at its first held-back line,
        and never regenerated: every later handoff of the window, in a
        report, an acknowledgement or the console's fold back, carries the
        same one, so whichever layer receives the window can tell whether
        it has already counted it. The minted ID also opens the window's
        list of the IDs it counts, the ones the summary that pays the
        window completes on the list the acknowledgement carries.
        """
        if not self._suppressed:
            self._window_id = _mint_window_id()
            self._window_ids = [self._window_id]
            self._counted_windows[self._window_id] = now
        self._suppressed += 1

    def _keep_newest_hosts(self):
        """Order the record by the refusal stamps and keep the newest bound.

        The record's places are the stamps' places, not the arrival order
        of the notices that carried them: a report delivered late carries
        an older refusal, so ordering by stamp keeps it from the newest
        place, and the bound then drops the oldest refusal by its stamp
        and not a genuinely newer one an earlier report happened to name
        first.
        """
        self._hosts = OrderedDict(
            sorted(self._hosts.items(), key=lambda item: item[1]))
        while len(self._hosts) > REFUSED_HOSTS_LIMIT:
            self._hosts.popitem(last=False)

    def note(self, host, line_at=None, refused_at=None):
        """Record a refusal; True when a per-host line may be written now.

        ``line_at`` carries the time a console elsewhere spent this
        hostname's line within the hour, reported by the front listener
        that wrote it while the backend it fronts could not be told: the
        refusal is recorded all the same, but no second line is written
        for it and this hour's cap here is not spent on the one already
        spent there. A line held back this way is not counted as
        suppressed either, because the console that spent it keeps its own
        count of what it held back.

        ``refused_at`` carries the refusal's own time for a report that
        lands late, so the record holds when the refusal happened and not
        when its notice was delivered: the record is ordered by these
        stamps and its bound drops the oldest refusal by its stamp, so a
        delayed notice displaces no genuinely newer refusal, and a newer
        stamp already recorded for the host is never moved back.
        """
        now = self._now()
        with self._lock:
            stamp = refused_at if refused_at is not None else now
            known = self._hosts.get(host)
            if known is not None and known > stamp:
                stamp = known
            self._hosts[host] = stamp
            self._keep_newest_hosts()
            self._prune(now)
            if line_at is not None and now - line_at < HOUR_SECONDS:
                if host in self._last_lines:
                    self._last_lines[host] = max(self._last_lines[host], line_at)
                else:
                    self._last_lines[host] = line_at
                return False
            if host in self._last_lines or len(self._line_times) >= REFUSAL_LOG_LINES_PER_HOUR:
                self._count_suppressed(now)
                return False
            self._last_lines[host] = now
            self._line_times.append(now)
            self._line_ids.append(_mint_line_id())
            return True

    def hour_line_times(self):
        """The times of the refusal lines this hour has already spent here."""
        now = self._now()
        with self._lock:
            self._prune(now)
            return list(self._line_times)

    def hour_line_ids(self):
        """The identities of the hour's spent lines, parallel to their times.

        The report carries them beside the times, so the layer that folds
        them in spends one budget slot per line and not one per distinct
        timestamp: two refusals can share a timestamp and the lines written
        for them are still two lines.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            return list(self._line_ids)

    def hour_line_hosts(self):
        """The hostnames this hour has spent lines for, with each line's time.

        The acknowledgement of a report carries them back beside the
        aggregate line times, as the per-host pace of the hour's line
        budget: each hostname with the time its line was logged for, so
        the console that folds the answer holds a hostname this layer
        named from being named again in the same hour, whichever layer
        ends up the only one that can write. The answer is a copy and not
        a take, exactly as the aggregate line times are.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            return list(self._last_lines.items())

    def merge_hour_lines(self, times, ids=None):
        """Fold a console elsewhere's spent line times into this hour's cap.

        The reports a front listener sends once it can be told carry the
        times of the refusal lines its console spent within the hour, and a
        report goes out per hostname, so the same times arrive many times:
        a time already folded in is not folded in again, so a report that
        repeats them spends nothing again. Only the lines the hour still
        holds are folded in, so the answer stays bounded by the same cap the
        times came from, and the merged budget rolls over exactly when the
        console's lines do.

        The lines also carry their stable identities, parallel to their
        times, and an identity is what makes one spent line its own slot:
        two lines can share one timestamp and are still two lines, so the
        fold spends one slot per identity it has not folded in before and
        not one per distinct timestamp. An answer that carries no
        identities, from a console this pair has not caught up with, folds
        its unpaired times by the timestamp alone, exactly as it always
        did, and an identity already folded in spends nothing again however
        often the report that carries it is retried.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            known_ids = set(self._line_ids)
            for index, stamp in enumerate(times):
                line_id = ids[index] if ids is not None and index < len(ids) else None
                if line_id is None:
                    if now - stamp < HOUR_SECONDS and stamp not in self._line_times:
                        self._line_times.append(stamp)
                        line_id = _mint_line_id()
                        self._line_ids.append(line_id)
                        known_ids.add(line_id)
                    continue
                if line_id not in known_ids and now - stamp < HOUR_SECONDS:
                    self._line_times.append(stamp)
                    self._line_ids.append(line_id)
                    known_ids.add(line_id)

    def note_unreported(self):
        """Count one refusal no notice will ever report, as one suppressed
        line.

        A refusal whose notice the pending queue evicted, and a repeat that
        coalesced into a notice still waiting, reach no recorder through
        their delivery: the console that saw them is the pair's only
        recorder for them, so it counts each one into its own window here,
        one suppressed line each, exactly once. Each refusal event calls
        this once, and the window the counts open is the idempotent
        aggregate the pair already shares.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            self._count_suppressed(now)

    def merge_recent(self, pairs):
        """Fold the newest refused names another layer carried into the record.

        Every report carries the newest bounded refused set with the
        refusals' own stamps, and the record takes the newer stamp per host,
        so it holds the most recently refused names whichever layer saw them:
        a hostname whose report is skipped or held back would otherwise age
        out of the record while refusals of it stay the newest news about it.
        The record is ordered by the stamps before the bound drops anything,
        exactly as ``note`` orders it, so a set that arrives late folds the
        newest names in by their own recency and the bound drops the oldest
        refusal by its stamp, not a genuinely newer one an earlier report
        happened to name first. The log pace is not consulted and not
        spent: these are names a refusal already named, not lines to write.
        """
        now = self._now()
        with self._lock:
            for host, stamp in pairs:
                if not host or now - stamp >= DAY_SECONDS:
                    continue
                known = self._hosts.get(host)
                if known is not None and known >= stamp:
                    continue
                self._hosts[host] = stamp
            self._keep_newest_hosts()

    def note_retry(self, host):
        """Record a retry's line; True when a per-host line may be written now.

        The pace and the cap are consulted as ``note`` consults them and an
        allowed answer spends the same allowance, but a line they hold back
        is not counted as suppressed: the delivery retries of a failed
        report ask here, and an hour of them must not add up to a summary
        that says a stream of refusals was held back when one hostname's
        line was. The refusal record itself is left alone, because a
        report's retry is not a new refusal.

        The first failure is different: a report whose delivery fails leaves
        its hostname named nowhere else, so the first failed notice of a
        host whose line the hour's cap holds back counts toward the window
        the way its own held-back line would, and the host's later retries
        of that notice do not. The host is marked when it is counted, and
        the mark lives exactly as long as the notice: the log pace cannot
        tell the retries of one notice from a fresh notice of the same
        host, because no line was ever written for the host and
        ``_last_lines`` holds nothing for it, so the notice's own
        lifetime is the only boundary. The console clears the mark when
        the notice resolves, at the acknowledgement of its report and at
        the pending queue's eviction alike, and no hour horizon clears
        it in between, so a retry of an unchanged notice counts its
        first failure once, in whichever hour it fails.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            if host in self._last_lines:
                return False
            if len(self._line_times) >= REFUSAL_LOG_LINES_PER_HOUR:
                if host not in self._first_failures:
                    self._first_failures[host] = now
                    self._count_suppressed(now)
                return False
            self._last_lines[host] = now
            self._line_times.append(now)
            self._line_ids.append(_mint_line_id())
            return True

    def first_failure_resolved(self, host):
        """Clear the first-failure mark of a notice that resolved.

        The mark lives exactly as long as the pending notice it was
        counted for: the console clears it when the backend acknowledges
        the notice's report and when the pending queue evicts the notice,
        the two ways a notice leaves, so a fresh notice of the same host
        counts its own first failure anew however soon it follows one
        that resolved.
        """
        with self._lock:
            self._first_failures.pop(host, None)

    def first_failure_is_counted(self, host):
        """Whether a failed notice of this host already counted its line.

        The failure path of a report asks its line pace, and a line the
        hour's cap holds back counts once as the notice's first failure:
        the mark lives exactly as long as the notice, and the layer that
        resolves the notice clears it. The eviction of a pending notice
        asks this before it counts the refusal the notice carries, because
        a notice whose first failure already counted its line into the
        window counts nowhere a second time.
        """
        with self._lock:
            return host in self._first_failures

    def account_notice(self, notice_id):
        """Whether this arrival is the first accounting of its notice.

        The reports of one notice are one refusal: a delivery whose answer
        was lost is retried under the notice's own identity, and every
        retry would otherwise account the refusal again, a second line
        within the hour or a fresh one past it, or a count beside the line
        that already named it. The first accounting marks the identity, and
        the mark lives past the hour, on the same span both sides keep for
        a delivery no answer resolved, because a notice whose answer was
        lost can be retried across one. A notice with no identity, the
        refusal this layer saw itself, accounts every time it is refused:
        the pace of its hostname is what holds its repeats, exactly as it
        always did. The caller that skips the accounting on a retry still
        reports the hour's current budget, so a retry's answer carries
        everything the hour holds now and not the arrival's own news.
        """
        if not notice_id:
            return True
        now = self._now()
        with self._lock:
            self._prune(now)
            stamp = self._notice_accounted.get(notice_id)
            if stamp is not None and now - stamp < CARRY_RETENTION_SECONDS:
                return False
            self._notice_accounted[notice_id] = now
            while len(self._notice_accounted) > REFUSED_HOSTS_LIMIT:
                self._notice_accounted.popitem(last=False)
            return True

    def _prune(self, now):
        spent = [(stamp, line_id) for stamp, line_id in zip(self._line_times, self._line_ids)
                 if now - stamp < HOUR_SECONDS]
        self._line_times = [stamp for stamp, _line_id in spent]
        self._line_ids = [line_id for _stamp, line_id in spent]
        self._last_lines = OrderedDict(
            (name, stamp) for name, stamp in self._last_lines.items() if now - stamp < HOUR_SECONDS)
        # The first failures are not on the hourly roll: a mark lives
        # exactly as long as the pending notice it was counted for, and
        # the console clears it when the notice resolves, at the
        # acknowledgement of its report or at the pending queue's
        # eviction, so an hour rolling over under an unchanged notice
        # adds nothing to the window and a retry of the notice counts
        # its first failure once, in whichever hour it fails.
        # The window IDs are the carry protocol, not the hourly line budget,
        # so the two lists holding them outlast the hour: they are kept for
        # the whole span the console may keep re-carrying a sent copy no
        # answer resolved, on the one constant both sides share so the
        # retention and the console's hold cannot drift apart. The backend
        # stamps an ID when it first folds the window in, at or after the
        # carry began, so its retention of that first stamp always covers
        # the console's hold of the same copy, and once no backend remembers
        # the ID, the console can no longer re-carry the copy either.
        self._counted_windows = OrderedDict(
            (window_id, stamp) for window_id, stamp in self._counted_windows.items()
            if now - stamp < CARRY_RETENTION_SECONDS)
        self._completed_windows = OrderedDict(
            (window_id, stamp) for window_id, stamp in self._completed_windows.items()
            if now - stamp < CARRY_RETENTION_SECONDS)
        # The accounted notices are not on the hourly roll either: a
        # delivery whose answer was lost is retried under the notice's own
        # identity, and the retry can cross an hour, so the mark that holds
        # one notice to one accounting lives on the same span both sides
        # keep for a delivery no answer resolved, and the bound matches the
        # pending queue's, because a notice is what the queue holds.
        self._notice_accounted = OrderedDict(
            (notice_id, stamp) for notice_id, stamp in self._notice_accounted.items()
            if now - stamp < CARRY_RETENTION_SECONDS)

    def take_summary(self):
        """The suppressed window when a summary line is due, else ``None``.

        A count that starts being suppressed is held for a full hour before
        it is reported, so the line counts every refusal suppressed in that
        window. The answer is that count with the window's two ends and its
        stable ID, and taking it clears the window. Taking it does not
        forget the ID: the window has been counted here, so a copy of it
        that arrives again, from the other layer, folds nowhere. Every ID
        the window counts joins the completed list as well, its own minted
        one and each one folded in from another layer's window, held for
        the whole span the other layer may keep re-carrying the copies it
        took out, because whichever layer holds a copy of any of those
        windows needs to be told its summary has been paid, so it drops
        the copy it holds instead of carrying a window that is owed
        nothing anymore.

        A window the record of completed windows the two processes share
        names paid is dropped here rather than paid again: the other layer
        took its overlapping counts into the one summary line it wrote, and
        the record is the proof that outlives the answer which layer paid,
        so the pair keeps the one summary per window the hour's budget
        rules name, whichever layer's own hour ran first.
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
            if (self._window_id is not None
                    and completed_windows_record_has(self._window_id)):
                # The record of completed windows the two processes share
                # proves the one summary this window owes was paid by the
                # other layer: it took the window's overlapping counts into
                # the one summary line it wrote and named this window among
                # the IDs it completed, so this layer holds counts that are
                # already paid. The window is dropped rather than paid a
                # second time, whatever this layer's own hour says: the pair
                # keeps the one summary per window the hour's budget rules
                # name, paid once by whichever layer reached it first.
                self._suppressed = 0
                self._window_opens = None
                self._window_id = None
                self._window_ids = []
                return None
            suppressed = self._suppressed
            opened = self._window_opens
            window_id = self._window_id
            window_ids = self._window_ids
            self._suppressed = 0
            self._window_opens = None
            self._window_id = None
            self._window_ids = []
            for paid_id in window_ids:
                self._completed_windows[paid_id] = now
            return suppressed, opened, now, window_id

    def hour_suppressed_window(self):
        """The suppressed window this hour holds, taken by no one.

        The acknowledgement of a report carries it back the way the report
        carries the hour's spent line times: a copy, not a take, because the
        layer that holds the window keeps holding it whether or not its
        answer arrives, so an acknowledgement that is lost or mis-delivered
        never takes accounting with it. The ID travels with the copy, so
        the layer that holds the original recognizes the window when the
        copy is handed back and folds it nowhere.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            if not self._suppressed:
                return None
            if self._window_opens is None:
                self._window_opens = now
            return self._suppressed, self._window_opens, self._window_id

    def completed_windows(self):
        """The IDs of the windows this layer has logged and cleared.

        An acknowledgement carries them so the other layer can drop every
        copy it holds of a window whose summary has been paid: without them
        it could not tell a window it must keep carrying from one that is
        owed nothing anymore. The IDs are held for the whole span the other
        layer may keep re-carrying a copy no answer resolved, on the one
        constant both sides share, so the list stays bounded and an answer
        can still name a window paid however long its copy stayed
        unresolved.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            return list(self._completed_windows)

    def complete_window(self, window_id):
        """Stamp a window this layer's summary paid beside the ones it took.

        The summary that pays overlapping counts can carry the window the
        other layer's last acknowledgement mirrored back, the two windows'
        counts paid as one line by the one owner that holds both, so the
        payment completes the mirrored window as well: the record of
        completed windows the two processes share then proves the copy the
        other layer still holds is paid, and its own payment drops the
        window rather than log a second line for counts the one line
        already paid. The stamp lives on the same retention span the pair's
        completed records share, so the proof outlives the answer that
        would have carried it and expires once no layer can still be
        holding a copy to be told.
        """
        if not window_id:
            return
        now = self._now()
        with self._lock:
            self._prune(now)
            self._completed_windows[window_id] = now

    def take_carried_summary(self):
        """Take the suppressed window out, for the report that carries it.

        The console that holds lines back while its backend cannot be told
        keeps their count in a window of its own, and one summary line is
        all a window owes wherever it is paid from: the first report a
        backend acknowledges carries the window along, so the backend folds
        it into its own and the two layers together owe the one summary, not
        one each. A window taken for a delivery the backend did not
        acknowledge is folded back in, so a recovery that fails loses
        nothing. The window's ID is taken out with it as well: for as long
        as the window is in flight this layer has not counted it, so the
        fold back below lands and is not refused as a window this layer has
        already counted. The IDs folded into the window from elsewhere stay
        counted here: only the window's own travelling ID leaves with the
        take, and a re-carry of a folded-in ID still folds nowhere while
        this layer remembers it.
        """
        now = self._now()
        with self._lock:
            if not self._suppressed:
                return None
            count = self._suppressed
            opened = self._window_opens if self._window_opens is not None else now
            window_id = self._window_id
            self._suppressed = 0
            self._window_opens = None
            self._window_id = None
            self._window_ids = []
            if window_id is not None:
                self._counted_windows.pop(window_id, None)
            return count, opened, window_id

    def merge_summary(self, count, opened, window_id=None):
        """Fold a suppressed window another layer carried into this one.

        The window that opened first keeps its place, so the lines one layer
        held back before the recovery and the lines the other held back
        after it are counted by the one summary the combined window owes,
        once its hour has run.

        The window's ID is what makes the fold safe to repeat: a window
        this layer has already counted, whether it minted the ID at the
        window's first line or folded the window in from elsewhere, folds
        nowhere, so a report whose acknowledgement was lost and whose
        retry carries the window again does not count it twice, and the
        window the other layer hands back while this one still holds it
        costs nothing either. That last refusal belongs to the window the
        layer is still holding, ahead of the retention lists: a live
        window outlives its counted record's lease, and a replay of its
        own mirror arriving once that lease has aged out must not fold
        in the lines the window already counts, so a live window can
        never count its own mirror twice. A window whose summary this
        layer has already paid folds nowhere as well: its ID on the completed
        list refuses the fold, and the arrival renews the record's lease, so
        the completion is kept exactly until the sender stops re-carrying
        its copy and the pair never risks a second summary of a window
        the first one paid. The IDs are held for the whole span the
        other layer may keep re-carrying the window, on the one constant
        both sides share, so the set of them stays bounded and a copy the
        other layer can still re-carry always meets a layer that still
        remembers counting it.

        The folded-in ID joins the window's list of the IDs it counts as
        well, so the summary that pays the combined window completes it
        too and the acknowledgement names it: the layer holding a copy of
        the folded window drops it on the answer, instead of carrying it
        to a backend that no longer remembers counting it. The wire keeps
        the one shape it always had, with the window's own travelling ID
        alone on the report and the hour copy; only the completed list
        grows to cover the aggregate.
        """
        if not count or opened is None:
            return
        with self._lock:
            if window_id is not None and (window_id == self._window_id
                                          or window_id in self._window_ids):
                # The ID belongs to the window this layer is still
                # holding: the window already counts the lines the
                # carried copy stands for, and a live window outlives
                # the counted record's lease, so a replay of its own
                # mirror arriving once that lease has aged out must not
                # fold the lines in a second time.
                return
            if window_id is not None and window_id in self._counted_windows:
                return
            if window_id is not None and window_id in self._completed_windows:
                # A window whose summary this layer has already paid,
                # carried again by a sender whose reconciliation no answer
                # has reached: the fold refuses, and the arrival renews
                # the completion record's lease, so the record is retained
                # exactly until the sender stops re-carrying the window,
                # on the one retention span both sides share. Once the
                # sender has reconciled, the record ages out with the same
                # span, so the list stays bounded and a copy the sender
                # can still re-carry always meets a layer that still
                # remembers paying it.
                self._completed_windows[window_id] = self._now()
                return
            if window_id is not None:
                self._counted_windows[window_id] = self._now()
            self._suppressed += count
            if window_id is not None and window_id not in self._window_ids:
                self._window_ids.append(window_id)
            if self._window_opens is None or opened < self._window_opens:
                self._window_opens = opened
                self._window_id = window_id

    def recent(self, within_seconds):
        """The refused hosts of the window, newest first."""
        now = self._now()
        with self._lock:
            return [host for host, stamp in reversed(self._hosts.items())
                    if now - stamp < within_seconds]
