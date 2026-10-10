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
                          normalize_entries, allows_all_hosts,
                          record_completed_windows, record_notice_accounted, record_notice_receipt,
                          refused_hostnames)

# The refusal status: the frontend never renders it as the app.
REFUSED_STATUS = 421

# 64 bits of randomness in the approval code: cheap to mint, hopeless to guess.
CODE_BYTES = 8

# The logger that writes the approval code to the console and nowhere else.
CODE_LOGGER_NAME = 'bazarr.host_approval'

# The acknowledgement of a front listener's report carries this backend's own
# accounting on these headers: the times of the refusal lines its hour has
# spent, the hostnames those lines were logged for with the time of each, the
# suppressed window it holds with its stable ID, and the IDs of the windows
# it has logged and cleared, so the console can drop every copy it holds of
# a window whose summary has been paid. The supervisor defines the same
# names on its side, because the two processes share no imports. The
# per-host pace travels as two parallel lists, the same shape the report's
# newest names use, because a hostname itself is not a safe separator.
HOUR_LINES_HEADER = 'X-Bazarr-Hour-Lines'
HOUR_LINE_IDS_HEADER = 'X-Bazarr-Hour-Line-Ids'
HOUR_HOSTS_HEADER = 'X-Bazarr-Hour-Hosts'
HOUR_HOST_LINES_HEADER = 'X-Bazarr-Hour-Host-Lines'
SUPPRESSED_HEADER = 'X-Bazarr-Suppressed'
SUPPRESSED_SINCE_HEADER = 'X-Bazarr-Suppressed-Since'
SUPPRESSED_ID_HEADER = 'X-Bazarr-Suppressed-Id'
COMPLETED_WINDOWS_HEADER = 'X-Bazarr-Completed-Windows'

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
    # The API answers JSON under the install's base_url and at the root,
    # which a prefix-stripping proxy leaves it at: the same root path the
    # approval form is served at, so a refused root API request gets the
    # API's own refusal and not the page, exactly as under the prefix.
    base = (getattr(getattr(config, 'general', None), 'base_url', '') or '').rstrip('/')
    for prefix in (f'{base}/api', '/api'):
        if path == prefix or path.startswith(prefix + '/'):
            return True
    return False


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
    count, opened, closed, _window_id = summary
    opened_at = time.strftime('%H:%M', time.localtime(opened))
    closed_at = time.strftime('%H:%M', time.localtime(closed))
    logging.info('BAZARR suppressed %d host-refusal log lines between %s and %s.',
                 count, opened_at, closed_at)


def _flush_suppressed_summary(recorder=None):
    """Log the due summary, so it is not lost when refusals stop arriving.

    The count is held for a full hour before it is reported; once it is due,
    the next refusal and every visit to System > Status both deliver it.
    The payment is also written to the record the two processes share,
    before any answer is built, so a console that holds a copy of the
    window can verify the payment however its own answer turned out and
    however many backends have restarted since. The recorder defaults to
    the module's own; the summary timer below passes the one it armed with,
    so a timer left running flushes the record it holds and nothing a
    later swap put in its place.
    """
    recorder = _refusals if recorder is None else recorder
    summary = recorder.take_summary()
    if summary:
        _log_suppressed_summary(summary)
        record_completed_windows(recorder.completed_windows(), summary[2])


# The pace of the backend's own summary timer. The flush it runs is a take
# that logs only when a window has come due, so the pace is what bounds the
# timer's work: once a minute against windows an hour holds.
SUMMARY_FLUSH_PACE_SECONDS = 60.0

# The one summary timer this process runs, with the stop event that ends it
# and the recorder it armed with.
_summary_flushes = None
_summary_flushes_lock = threading.Lock()


def _summary_flush_loop(recorder, stop):
    """The backend's own timer, on the paced wait the console's worker uses.

    Each turn pays the summaries the recorder holds that have come due, so a
    stream of refusals that stops does not take its summary with it even
    though no request arrives and no report is delivered: the refusal path
    and the health list still pay what they reach, and this timer is what
    reaches everything else, once per pace and never in a spin. The recorder
    is the one the timer armed with, the module's own for the process's
    lifetime, and the loop is a daemon that dies with the process.
    """
    while True:
        _flush_suppressed_summary(recorder)
        if stop.wait(SUMMARY_FLUSH_PACE_SECONDS):
            return


def start_summary_flushes():
    """Arm the backend's summary timer, once, beside the check it serves.

    Arming is idempotent, so the boot surface that announces the approval
    code arms the timer with it exactly once, and so does the first refusal
    the check takes once it is active, because the check can be switched on
    with the process live and the refusals that follow are owed the same
    timer the boot would have armed. A process that never runs the check
    active never runs the timer. The timer holds the recorder it armed
    with, so whatever later replaces the module's record, the timer
    flushes the one the process keeps for its own lifetime.
    """
    global _summary_flushes
    with _summary_flushes_lock:
        if _summary_flushes is not None:
            return False
        recorder, stop = _refusals, threading.Event()
        thread = threading.Thread(target=_summary_flush_loop, args=(recorder, stop),
                                   daemon=True)
        _summary_flushes = (thread, stop, recorder)
        thread.start()
    return True


def stop_summary_flushes():
    """End the backend's summary timer, and wait for its turn to finish."""
    global _summary_flushes
    with _summary_flushes_lock:
        flushes = _summary_flushes
        _summary_flushes = None
    if flushes is None:
        return
    thread, stop, _recorder = flushes
    stop.set()
    thread.join(timeout=SUMMARY_FLUSH_PACE_SECONDS + 5)


def announce_code(config=settings):
    """Announce the approval code at boot, when the check is armed.

    The same boot arms the backend's summary timer beside the announcement:
    the check that refuses hostnames holds their summary owed for an hour
    after the refusals stop, and the timer is what pays it when no request
    arrives to.
    """
    if not check_active(config):
        return False
    start_summary_flushes()
    return _approval_code.announce()


def verify_approval_code(supplied):
    """Whether the supplied approval code is the current one."""
    return _approval_code.verify(supplied)


def record_refusal(host, config=settings, line_at=None, hour_lines=None, hour_line_ids=None,
                   suppressed=0, suppressed_since=None, suppressed_id=None, mirrored=0,
                   mirrored_since=None, mirrored_id=None, recent=None, refused_at=None,
                   notice=None):
    """Record a refused hostname and keep a code on the console.

    The backend's own refusals call this, and so does the report the front
    listener in front of this backend sends: the hostnames that layer refuses
    never reach this process, so without the report they would never appear on
    the health page and the console code would never be repeated while they
    continue. The report also carries the time that console spent the
    hostname's refusal line while this backend could not be told, so no
    second line is written for it in the same hour, and the times of the
    refusal lines that console spent within the hour, so a hostname refused
    for the first time after the recovery draws on the one merged hour
    budget rather than a fresh cap, which rolls over exactly at those
    lines' times. A report from a console whose backend was down also
    carries the suppressed window it holds, so the two layers owe the one
    summary the combined window counts.

    The window travels under its stable ID, so a report whose answer was
    lost and whose retry carries it again folds it in once however many
    times it arrives, and the window the console mirrors back from the last
    acknowledgement folds nowhere while this backend still holds it: the
    mirrored window is this backend's own, carried back to it so that a
    restart that lost it can be told it again, not a second window owed a
    second summary.

    The report also carries the newest bounded set of refused names with the
    times that console saw the refusals, and the record takes the newer stamp
    per host: a hostname whose report the console skips, because it was told
    within the hour, or holds back past the reporting bound would otherwise
    age out of the record while refusals of it stay the newest news about it,
    so the record keeps reflecting the most recently refused names. The
    notice of a hostname no backend has acknowledged carries the refusal's
    own time as well, kept beside its retry bookkeeping: the newest names
    are bounded and a roll of newer ones rolls a waiting hostname out of
    the set, and the line the console spent for it is kept for its hour
    alone, so that retained time is what the record holds when neither of
    the others survives to the delivery.

    The report of a pending notice carries the notice's own identity as
    well, and the reports of one notice are one refusal: the first delivery
    that reaches here accounts it, in the line the hour spends on it or in
    the fold of a line a console elsewhere already spent, and the retries
    a lost answer leaves behind account it nowhere again, within the hour
    or across one, while every retry's answer still reports the budget
    the hour holds now. The mark that holds them lives in this process's
    own memory and nowhere else, so a backend that restarts or is replaced
    holds no mark: the retry tells it the notice afresh and it records the
    refusal in its own right, the way every backend that receives the
    notice does. The outcome the record of notices keeps is the console's
    fact and not this backend's brake: a notice this backend accounted
    before it died keeps the console's fallback silent, and one whose
    accounting never completed, because this backend died between its
    receipt and its record, leaves the refusal to the console that saw it.
    """
    shown = effective_host(host)
    if not shown or not check_active(config):
        return
    # The check is active, so refusals are being named and their suppressed
    # window is owed its summary: the flush is armed here and not at the
    # boot alone, because the check can be switched on with the process
    # live, and the refusals the switch lets through are owed the same
    # timer the boot would have armed.
    start_summary_flushes()
    if hour_lines:
        _refusals.merge_hour_lines(hour_lines, hour_line_ids)
    _refusals.merge_summary(suppressed, suppressed_since, suppressed_id)
    _refusals.merge_summary(mirrored, mirrored_since, mirrored_id)
    if recent:
        named = []
        for name, stamp in recent:
            shown_name = effective_host(name)
            if shown_name:
                named.append((shown_name, stamp))
                if shown_name == shown:
                    refused_at = stamp
        if named:
            _refusals.merge_recent(named)
    # A report that lands late still carries the refusal's own time, in the
    # set of the newest names, in the pending notice's own stamp, or in the
    # line the console spent for it: the record keeps that time and not the
    # delivery's, so a delayed notice displaces no genuinely newer refusal.
    # The pending stamp is the one that survives the others: the newest
    # names roll a waiting hostname out of their bound and the line the
    # console spent expires with its hour, while the notice holds the
    # refusal's time until its report is acknowledged.
    if refused_at is None:
        refused_at = line_at
    accounted = _refusals.account_notice(notice)
    if accounted:
        if _refusals.note(shown, line_at, refused_at):
            logging.warning('BAZARR refused a request for an unrecognized host: %s. Allow it under '
                            'Settings > General > Allowed Hosts, or set %s.', shown, ALLOWED_HOSTS_ENV)
        if notice:
            # The outcome is retained once the accounting completed, so the
            # console whose delivery failed reads back a fact about the
            # refusal and not only about the taking in, and a backend that
            # restarts holds its own retries to the one accounting.
            record_notice_accounted(notice, time.time())
    _flush_suppressed_summary()
    # Announce the code so a refused hostname has one in the console to
    # use; the announcement itself repeats at most once an hour.
    _approval_code.announce()


def front_refusal_notice(config=settings):
    """The report the front listener in front of this backend sends.

    It carries the token the supervisor started this process with, and the
    sender bounds it to at most one report per hostname per hour, so the work
    here is only the bookkeeping and the announcement. It also carries the
    time that console spent the hostname's refusal line, the times of the
    refusal lines it spent within the hour, the suppressed window it holds
    under that window's stable ID, and the newest bounded set of refused
    names with the times it saw those refusals, so the hour's line budget,
    its one summary and the record of the newest names are the two
    processes' shared allowance. The notice of a hostname no backend has
    acknowledged carries the refusal's own time as well, beside the retry
    bookkeeping and independently of the newest names and the line history,
    so the record holds the refusal's own time however late the notice is
    delivered.

    The answer carries this backend's own accounting back on the response
    headers: the times of the lines its hour has spent, the hostnames those
    lines were logged for with the time of each, the suppressed window it
    holds, again with the ID, and the IDs of the windows it has logged and
    cleared, so the console discards every copy it holds of a window whose
    summary has been paid. The hostnames travel as the per-host pace of the
    hour's line budget, so the console that folds them holds a hostname
    this backend named from being named again in the same hour, whichever
    layer ends up the only one that can write. The window goes as a copy
    and not a take, so an answer that is lost or mis-delivered leaves this
    backend holding what it held, and the console keeps the copy beside
    its own window rather than inside it, so the one summary a window owes
    is logged once, by whichever layer holds the window when its hour runs.

    The notice also carries its own stable identity, and two facts about
    it are written to the record the two processes share: the receipt, on
    the token that matched and before this backend processes anything, and
    the outcome, once the notice's accounting completed. A delivery whose
    answer was lost is reconciled from the outcome, so the console that
    holds the notice does not name the refusal a second time from its
    failure path when this backend accounted it, and the pair writes one
    line for the refusal, whichever layer's channel it reached first. A
    backend that dies between the receipt and the record leaves the
    refusal named nowhere, and the console accounts it itself, the only
    other layer that saw it.
    """
    from flask import request

    from .app import SUPERVISOR_TOKEN_HEADER, supervisor_token
    expected = supervisor_token()
    supplied = request.headers.get(SUPERVISOR_TOKEN_HEADER, '')
    if not expected or not secrets.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
        return '', 403
    notice_id = request.form.get('notice', '').strip()
    if notice_id:
        # The receipt is written before the notice is processed, on the
        # token that matched: it proves the notice reached a backend that
        # took it in, and the outcome that follows the accounting is what
        # the console whose delivery failed reads back before it names the
        # refusal the notice carried, so a backend that accounted the
        # notice holds the console from naming the hostname a second time,
        # while a backend that took it in and died before the record leaves
        # the refusal to the console, and a delivery no backend ever took
        # in leaves no entry at all.
        record_notice_receipt(notice_id, time.time())
    logged = request.form.get('logged', '')
    try:
        line_at = float(logged) if logged else None
    except ValueError:
        line_at = None
    spent = []
    for stamp in request.form.get('hour_lines', '').split(','):
        try:
            spent.append(float(stamp))
        except ValueError:
            pass
    spent_ids = [line_id for line_id in request.form.get('hour_lines_ids', '').split(',')
                 if line_id]
    suppressed = request.form.get('suppressed', '')
    try:
        held_back = int(suppressed) if suppressed else 0
    except ValueError:
        held_back = 0
    suppressed_since = request.form.get('suppressed_since', '')
    try:
        window_opens = float(suppressed_since) if suppressed_since else None
    except ValueError:
        window_opens = None
    window_id = request.form.get('suppressed_id', '') or None
    mirrored = request.form.get('mirrored', '')
    try:
        mirrored_back = int(mirrored) if mirrored else 0
    except ValueError:
        mirrored_back = 0
    mirrored_since = request.form.get('mirrored_since', '')
    try:
        mirrored_opens = float(mirrored_since) if mirrored_since else None
    except ValueError:
        mirrored_opens = None
    mirrored_id = request.form.get('mirrored_id', '') or None
    recent_names = [name for name in request.form.get('recent', '').split(',') if name]
    recent_times = []
    for stamp in request.form.get('recent_at', '').split(','):
        try:
            recent_times.append(float(stamp))
        except ValueError:
            recent_times.append(None)
    recent = [(name, stamp) for name, stamp in zip(recent_names, recent_times)
              if stamp is not None]
    pending = request.form.get('refused_at', '')
    try:
        pending_at = float(pending) if pending else None
    except ValueError:
        pending_at = None
    record_refusal(request.form.get('host', ''), config=config, line_at=line_at,
                   hour_lines=spent, hour_line_ids=spent_ids, suppressed=held_back,
                   suppressed_since=window_opens,
                   suppressed_id=window_id, mirrored=mirrored_back, mirrored_since=mirrored_opens,
                   mirrored_id=mirrored_id, recent=recent, refused_at=pending_at,
                   notice=notice_id or None)
    headers = {}
    times = _refusals.hour_line_times()
    if times:
        headers[HOUR_LINES_HEADER] = ','.join(str(stamp) for stamp in times)
    line_ids = _refusals.hour_line_ids()
    if line_ids:
        headers[HOUR_LINE_IDS_HEADER] = ','.join(line_ids)
    hour_hosts = _refusals.hour_line_hosts()
    if hour_hosts:
        headers[HOUR_HOSTS_HEADER] = ','.join(host for host, _stamp in hour_hosts)
        headers[HOUR_HOST_LINES_HEADER] = ','.join(str(stamp) for _host, stamp in hour_hosts)
    held = _refusals.hour_suppressed_window()
    if held is not None:
        headers[SUPPRESSED_HEADER] = str(held[0])
        headers[SUPPRESSED_SINCE_HEADER] = str(held[1])
        if held[2] is not None:
            headers[SUPPRESSED_ID_HEADER] = held[2]
    completed = _refusals.completed_windows()
    if completed:
        headers[COMPLETED_WINDOWS_HEADER] = ','.join(completed)
    return '', 204, headers


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
