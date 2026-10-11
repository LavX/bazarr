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


def _prune_completed_windows_record(record, now):
    """Keep the pair's record of windows bounded, each entry on the rule
    its own life keeps.

    A completed payment is a proof, held on the retention span both sides
    share because no layer re-carries a copy past it and the answer that
    would carry the proof cannot lag behind by more. An unpaid ownership
    is a promise rather than a proof, and a promise no passage of time
    pays: the console that claimed it owes the one summary its counts
    stand for until the payment completes the identities or a release
    returns them, and the moment the claim was taken says nothing about
    when a payer comes back to it, so the entry is held past every span
    and no update of the record ages it out. The ownerships are bounded
    in number instead, on the pair's own bound of refused hosts, and the
    bound retires whole claims oldest first, because one claim writes
    its facts under every identity it covers and a bound that dropped
    half of one would hand another layer a constituent the promise still
    owns: the newest claims are the ones a payer can still come back to,
    and a claim that fell out of that many newer ones has outlasted
    every bounded memory the pair keeps of anything else.
    """
    pruned = {}
    claims = {}
    for window_id, entry in record.items():
        if isinstance(entry, (int, float)):
            if now - entry < CARRY_RETENTION_SECONDS:
                pruned[window_id] = entry
        elif isinstance(entry, dict):
            ids = entry.get('ids')
            key = tuple(ids) if isinstance(ids, list) else (window_id,)
            claims.setdefault(key, []).append(window_id)
    ordered = sorted(claims.items(), key=lambda claim: min(
        record[window_id].get('claimed', 0.0) for window_id in claim[1]))
    for _ids, window_ids in ordered[-REFUSED_HOSTS_LIMIT:]:
        for window_id in window_ids:
            pruned[window_id] = record[window_id]
    return pruned


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
    the other's proof. The record also holds the ownerships a console
    claimed before its payment was due, written beside the decision as
    their own shape, and a payment that lands on one completes it, the
    entry taking the payment's stamp the way every completed window's
    does, while the ownerships no payment touched keep their places
    through the merge, held past every span until the payment completes
    them or a release returns them, and bounded on the pair's own count
    of claims rather than the span.
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
        record = _prune_completed_windows_record(
            _read_completed_windows_record(path), paid_at)
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


def pending_window_ownerships(path=None):
    """The window ownerships the record still holds as unpaid, with their
    facts.

    A claim a console takes on a mirrored window's identities before
    either hour runs is a promise to pay, and the promise travels with
    the decision: the window's own facts are written beside it on the
    record the two processes share, so the console that replaces a lost
    one reads the unpaid ownerships back from here and the debt a
    restart would have orphaned finds its payer in whichever console
    follows. The identities a standing ownership covers read as paid to
    the other layer, so no timer of the pair pays the counts while the
    ownership holds them, and a payment completes it, the entry taking
    the payment's stamp the way every completed window's does, so
    nothing here reads as unpaid after the line it owed was logged. The
    read is best effort exactly as the record's other reads are: a
    record nothing can read proves no ownership, and the pair owes no
    debt it cannot see.
    """
    path = completed_windows_record_path() if path is None else path
    record = _read_completed_windows_record(path)
    return [entry for entry in record.values() if isinstance(entry, dict)]


def claim_summary_payment(window_ids, paid_at, path=None, ownership=None):
    """Take the one payment decision for the windows a summary pays,
    across both processes.

    The backend's paced flush and the console's aggregate can both be one
    step from paying the same window, each having read the record before
    the other wrote it, and a payment each of them decides alone would log
    the window's lines twice. The claim is the decision, taken under the
    same lock the record's own updates hold and before any line is logged,
    so two payments cannot both pass it however closely they overlap: the
    side that claims first owns the one summary the window owes, and the
    side that finds the identity already on the record pays nothing, its
    copy of the window dropped rather than logged a second time. The
    claim's stamp is the payment's own time, so the proof it writes
    outlives the answer the payer would have built, the way a payment
    written after the log does.

    The IDs a summary pays are every window it counts, the one the paying
    layer minted and each one folded in from another layer's window, and
    the claim takes them as a whole in the one locked step: an ID another
    payment already claimed means that payment took some of these counts
    into its own line, so the whole set is refused and the layer that
    finds it pays none of them this way, however many of its IDs were
    still free. A window refused whole is settled by its caller identity
    by identity, one claim at a time on this same lock, so the counts of
    the IDs another payment never claimed still find their payer: only a
    window whose every ID is claimed drops whole, and the payment that
    wins claims the whole set, so every layer holding a copy of any
    window in it reads the proof the same moment the line is owed. One
    identity is the shape the console's own claim arrives in, the shape
    a settlement takes as well, and a summary with no identity to claim
    costs nothing to grant, because no copy of it anywhere can be proven
    paid.

    A claim taken before its payment is due is a promise rather than a
    proof, and the caller that makes one names the ownership it holds: the
    window's own facts travel with the decision, the counts the promise
    stands for and the moment its window opened, written beside the claim
    as their own shape no payment wrote, so the claim reads as a promise
    to pay and not as a payment already made. The identities a standing
    ownership covers read as paid to the other layer all the same, so no
    timer of the pair pays the counts while the promise holds them, and
    the payment that keeps it completes it, the entry taking the
    payment's stamp the way every completed window's does: a console
    that dies between the two leaves the promise on the record, and the
    console that replaces it reads the debt back and pays it, so the
    pair loses no summary a claim already owns. A claim with no
    ownership named beside it is the payment's own proof exactly as it
    always was, the stamp of a line the claim decided to log.

    The write is best effort, exactly as the record's own updates are: a
    record nothing can read or write proves no payment, so the payer
    proceeds rather than fail a payment, and the pair keeps the
    lose-not-double trade on whatever the record could not decide.
    """
    if isinstance(window_ids, str):
        window_ids = [window_ids]
    window_ids = [window_id for window_id in window_ids if window_id]
    if not window_ids:
        return True
    path = completed_windows_record_path() if path is None else path
    record_path = Path(path)
    lock_handle = None
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(str(record_path) + COMPLETED_WINDOWS_RECORD_LOCK_SUFFIX, 'a+')
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
            lock_handle = None
    try:
        record = _read_completed_windows_record(path)
        claimed = all(window_id not in record for window_id in window_ids)
        if claimed:
            record = _prune_completed_windows_record(record, paid_at)
            for window_id in window_ids:
                if ownership is None:
                    record[window_id] = paid_at
                else:
                    record[window_id] = {'count': ownership[0], 'opened': ownership[1],
                                         'ids': list(window_ids), 'claimed': paid_at}
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
        claimed = True
    finally:
        if lock_handle is not None:
            lock_handle.close()
    return claimed


def release_summary_claim(window_ids, path=None):
    """Return the one payment decision on a window to whichever layer
    holds it.

    The console takes the claim on a mirrored window's identities when it
    holds both that window and one of its own, because the one summary
    they owe is its to pay, and the claim covers every constituent the
    mirror counts. Its own window can leave in a report instead, folded
    into the window the mirror stands for, and the merged window is the
    backend's from then on: whichever layer holds a window when its hour
    runs is the one that reports it, so the console returns the decision
    before the claim can block the merged window's payment, and every
    identity the claim took comes back with it. The removal is held on
    the lock the record's own updates hold, best effort with them: a
    record nothing can write keeps the claim, and the payment that cannot
    take the merged window whole settles by identity instead, the folded
    remainder paying and the claimed counts still owed by the layer that
    claimed them.
    """
    if isinstance(window_ids, str):
        window_ids = [window_ids]
    window_ids = [window_id for window_id in window_ids if window_id]
    if not window_ids:
        return
    path = completed_windows_record_path() if path is None else path
    record_path = Path(path)
    lock_handle = None
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(str(record_path) + COMPLETED_WINDOWS_RECORD_LOCK_SUFFIX, 'a+')
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
            lock_handle = None
    try:
        record = _read_completed_windows_record(path)
        held = [window_id for window_id in window_ids if window_id in record]
        if held:
            for window_id in held:
                record.pop(window_id, None)
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


# The record of the moments the pair logged a summary line, in the
# configuration directory the two processes of one install share. The
# windows the pair pays are identities, and the claim on them decides which
# layer pays a window the two hold copies of, but two windows no
# acknowledgement ever carried between the layers share no identity: an
# outage that lets no report out and no answer back leaves each layer
# holding a window of its own, and the timers of the two processes would
# pay them side by side, one line for each, within the same hour. The
# emission times are what those two coordinate on instead: whichever
# layer pays first writes the moment its line landed here, and the other
# layer reads the newest moment back before it pays, holding its own
# line until the hour since the last one has run, so the pair keeps its
# ceiling of one summary an hour however the traffic split the counts.
SUMMARY_EMISSIONS_RECORD = 'host-refusal-summary-emissions.json'
SUMMARY_EMISSIONS_RECORD_FILE_ENV = 'BAZARR_REFUSAL_SUMMARY_EMISSIONS_FILE'
SUMMARY_EMISSIONS_RECORD_LOCK_SUFFIX = '.lock'


def summary_emissions_record_path(environ=None):
    """Where the pair's summary-emission record lives, resolved the way
    both processes resolve their configuration: the explicit file the
    environment names, then the configuration directory BAZARR_CONFIG_DIR
    names, then the data directory beside the code, exactly as the record
    of completed windows resolves, so the two processes of one install
    always meet the same file, restarts and all.
    """
    environ = os.environ if environ is None else environ
    override = environ.get(SUMMARY_EMISSIONS_RECORD_FILE_ENV, '').strip()
    if override:
        return override
    config_dir = environ.get('BAZARR_CONFIG_DIR', '').strip()
    if not config_dir:
        config_dir = str(Path(__file__).resolve().parents[2] / 'data')
    return str(Path(config_dir) / 'config' / SUMMARY_EMISSIONS_RECORD)


def _read_summary_emissions_record(path):
    """The record's emission moments, or nothing: the record is best
    effort, and a missing or unreadable one proves no line the pair can
    wait for."""
    try:
        with open(path) as record_file:
            record = json.load(record_file)
        if not isinstance(record, list):
            return []
        return [stamp for stamp in record if isinstance(stamp, (int, float))]
    except Exception:
        return []


def record_summary_emission(emitted_at, path=None):
    """Write the moment one summary line landed, for the other process.

    Both processes write here, the backend at the flush that logged its
    window and the console at the emission that logged its own, and the
    moments are kept on the retention span both sides share, so the pair
    keeps the proof of when it last named a window's counts for as long
    as any layer could still be holding a copy of anything the hour
    counted. The write is held against the next on the lock file beside
    the record, exactly as the record of completed windows is, and it is
    best effort with the same trade: a record nothing can write proves no
    emission, and the pair pays the summary it owes rather than hold a
    line on a proof that never reached the file.
    """
    if not isinstance(emitted_at, (int, float)):
        return
    path = summary_emissions_record_path() if path is None else path
    record_path = Path(path)
    lock_handle = None
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(str(record_path) + SUMMARY_EMISSIONS_RECORD_LOCK_SUFFIX, 'a+')
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
        lock_handle = None
    try:
        record = _read_summary_emissions_record(path)
        record.append(emitted_at)
        record = [stamp for stamp in record if emitted_at - stamp < CARRY_RETENTION_SECONDS]
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


def summary_ceiling_blocks(now, path=None):
    """Whether the pair's ceiling of one summary an hour holds a new one.

    The newest moment the pair logged a summary line decides: a line
    within the hour means the hour's one summary is spent, whichever
    layer paid it and whichever windows the line counted, so the payment
    that asks here waits for the hour to run and the worker's next paced
    look pays it once the ceiling opens. A moment that reads as newer
    than the asking clock holds nothing, because a clock that has not
    reached the recorded moment does not share it: the pair's two
    processes keep one wall clock, and a stamp from ahead of the asker
    is a clock the asker is not on, so the pair treats it as no proof
    rather than a ceiling that never opens. The read is best effort
    exactly as the record's other proofs are: a record nothing can read
    proves no emission, and the pair pays the summary it owes rather
    than hold a line on a proof that never reached the file. The moment
    the reading layer paid its own line is on the record with the rest,
    so the ceiling holds a layer to the pair's cadence and not only the
    other one's. The payment paths take the hour's one slot on the
    reservation beside this read, one locked step with the decision
    itself, so the cadence this read names and the ceiling the pair
    keeps are the same rule.
    """
    path = summary_emissions_record_path() if path is None else path
    emissions = _read_summary_emissions_record(path)
    newest = max(emissions) if emissions else None
    return (newest is not None
            and 0 <= now - newest < HOUR_SECONDS)


def reserve_summary_emission(now, path=None):
    """Take the pair's one emission slot for the hour, or answer that a
    line within it holds this payment back.

    The ceiling of one summary an hour is a decision the pair's two
    processes take in one step, because their timers can reach the same
    moment together, each holding a window no acknowledgement ever
    carried between the layers, so no identity the pair shares could
    decide between them: the read that found the hour open and the write
    that spent it were two steps once, and a payment that read before
    the other layer's line landed paid beside it anyway, so the pair
    named two lines the one summary an hour does not allow. The decision
    and the spend are one locked step here instead: whichever payment
    asks first takes the slot with it, writing the moment under the same
    lock the record's own updates hold, and the payment that comes
    second meets the moment the first wrote and holds its own line back
    with every count it keeps, until the hour since the pair's last line
    has run. Every payment path asks here, the whole claim and the
    settlement by identity alike, because a settlement pays counts into a
    line the same way a whole window does, and the write the caller's own
    line follows re-stamps the moment the reservation already took. The
    moment the reservation decides on is captured under the lock itself,
    from the clock the caller passes, because a timer's own capture
    precedes the lock's wait: another process can pay a summary while a
    stalled timer waits for the lock, and the capture the timer took
    before the wait would read the pair's newer reservation from an hour
    it has long left, finding no proof where the pair's newest line
    stands and paying a second line beside it. The locked capture asks
    inside whichever hour the pair's newest line opened, however long the
    wait that led to it ran, and the same capture is the moment the
    reservation records and the stamp the payment carries, so the line
    the caller logs and the record the pair shares name one moment and
    the ceiling reads the hour the line opened. A recorded moment still
    ahead of the capture can only be the pair's own clocks disagreeing,
    and the hour holds it back within that hour, while a moment further
    ahead than the hour is a clock the pair does not share, and the pair
    pays the summary it owes rather than hold it on a ceiling no shared
    clock ever opens. The reservation answers with the captured stamp
    when it takes the slot and with nothing when the ceiling holds the
    payment back, so a caller asks whether the answer is nothing and
    carries the stamp otherwise, and a stamp of zero still proves a slot
    the caller took. The write is best
    effort with the same trade the record's other updates keep: a record
    nothing can read or write proves no line the pair can wait for, and
    the payment proceeds rather than hold a summary on a proof that never
    reached the file.
    """
    if not (isinstance(now, (int, float)) or callable(now)):
        return True
    path = summary_emissions_record_path() if path is None else path
    record_path = Path(path)
    lock_handle = None
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(str(record_path) + SUMMARY_EMISSIONS_RECORD_LOCK_SUFFIX, 'a+')
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
        lock_handle = None
    stamp = now() if callable(now) else now
    try:
        record = _read_summary_emissions_record(path)
        newest = max(record) if record else None
        if (newest is not None
                and -HOUR_SECONDS <= stamp - newest < HOUR_SECONDS):
            return None
        record.append(stamp)
        record = [moment for moment in record if stamp - moment < CARRY_RETENTION_SECONDS]
        fd, tmp = tempfile.mkstemp(dir=str(record_path.parent),
                                   prefix=f".{record_path.name}.")
        try:
            with os.fdopen(fd, 'w') as record_file:
                json.dump(record, record_file)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
        return stamp
    except Exception:
        return stamp
    finally:
        if lock_handle is not None:
            lock_handle.close()


# The record of the hour's spent refusal lines, in the configuration
# directory the two processes of one install share. The pair's line cap
# is one budget whichever layer writes the line, and each layer's own
# in-memory allowance only knows the lines it spent and the ones its
# answers folded in: an acknowledgement whose answer was lost and a
# report no backend ever processed leave the two budgets strangers to
# each other, so the pair could draw twice the cap from one hour. Every
# layer reserves the per-host and the total allowance here, under the
# lock, before it logs, and a refused reservation holds the line back
# the way the hour's own cap does.
SPENT_REFUSAL_LINES_RECORD = 'host-refusal-spent-lines.json'
SPENT_REFUSAL_LINES_RECORD_FILE_ENV = 'BAZARR_REFUSAL_SPENT_LINES_FILE'
SPENT_REFUSAL_LINES_RECORD_LOCK_SUFFIX = '.lock'


def spent_refusal_lines_record_path(environ=None):
    """Where the pair's spent-line record lives, resolved the way both
    processes resolve their configuration: the explicit file the
    environment names, then the configuration directory BAZARR_CONFIG_DIR
    names, then the data directory beside the code, exactly as the record
    of completed windows resolves, so the two processes of one install
    always meet the same file, restarts and all.
    """
    environ = os.environ if environ is None else environ
    override = environ.get(SPENT_REFUSAL_LINES_RECORD_FILE_ENV, '').strip()
    if override:
        return override
    config_dir = environ.get('BAZARR_CONFIG_DIR', '').strip()
    if not config_dir:
        config_dir = str(Path(__file__).resolve().parents[2] / 'data')
    return str(Path(config_dir) / 'config' / SPENT_REFUSAL_LINES_RECORD)


def _read_spent_refusal_lines_record(path):
    """The record's spent lines, or nothing: the record is best effort,
    and a missing or unreadable one proves no line the pair has spent."""
    try:
        with open(path) as record_file:
            record = json.load(record_file)
        if not isinstance(record, dict):
            return {}, []
        hosts = record.get('hosts')
        times = record.get('times')
        hosts = ({name: stamp for name, stamp in hosts.items()
                  if isinstance(name, str) and isinstance(stamp, (int, float))}
                 if isinstance(hosts, dict) else {})
        times = ([stamp for stamp in times if isinstance(stamp, (int, float))]
                 if isinstance(times, list) else [])
        return hosts, times
    except Exception:
        return {}, []


def reserve_refusal_line(host, now, path=None):
    """Take the pair's one line slot for the hostname in the hour, or
    answer that the shared budget has spent it.

    The hour's cap of refusal lines and the hostname's one line an hour
    are the pair's budget and not one layer's: the backend writes lines
    for the notices it accounts and the console writes them for the
    failures no backend answered, and the two processes share the one
    budget the cap names. Each layer's own in-memory allowance knows
    only the lines it spent itself and the ones the answers folded in,
    so a burst of notices whose acknowledgements were lost and a burst
    of failures no backend processed can each draw a whole cap beside
    the other's: the reservation is taken here instead, in the shared
    state under the record's lock, before either process logs, so the
    pair's one budget bounds both layers' lines together. The per-host
    allowance is the hostname's own fact and the total is the hour's,
    and both are held on the hour the reservation's own captured
    moment names: an entry outside the hour on either side of it is a
    clock the pair does not share, and it binds nothing here. The
    moment the reservation decides on is captured under the lock
    itself, from the clock the caller passes, because the caller's own
    capture precedes the lock's wait: another process can spend the
    pair's whole budget while a stalled caller waits for the lock, and
    the capture the caller took before the wait would prune the newer
    stamps away as clocks the pair does not share, finding an empty
    budget where the pair's lines stand and granting a line over them.
    The locked capture decides on whichever hour the pair's newest
    lines spent, however long the wait that led to it ran, and the
    same capture is the moment the reservation records and the stamp
    the caller's line accounting carries. The reservation answers with
    the captured stamp when it takes the slot and with nothing when
    the budget refuses, so a caller asks whether the answer is nothing
    and carries the stamp otherwise, and a stamp of zero still proves
    a slot the caller took. A refused reservation holds the line back
    the way the hour's own cap does, and the caller counts the refusal
    into its window as a held-back line. The write is best effort
    exactly as the record's other updates are: a record nothing can
    read or write reserves nothing, and the pair writes the line
    rather than hold it on a proof that never reached the file.
    """
    if not host or not (isinstance(now, (int, float)) or callable(now)):
        return True
    path = spent_refusal_lines_record_path() if path is None else path
    record_path = Path(path)
    lock_handle = None
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(str(record_path) + SPENT_REFUSAL_LINES_RECORD_LOCK_SUFFIX, 'a+')
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
        lock_handle = None
    stamp = now() if callable(now) else now
    try:
        hosts, times = _read_spent_refusal_lines_record(path)
        hosts = {name: moment for name, moment in hosts.items()
                 if -HOUR_SECONDS <= stamp - moment < HOUR_SECONDS}
        times = [moment for moment in times
                if -HOUR_SECONDS <= stamp - moment < HOUR_SECONDS]
        if (hosts.get(host) is not None
                or len(times) >= REFUSAL_LOG_LINES_PER_HOUR):
            return None
        hosts[host] = stamp
        times.append(stamp)
        fd, tmp = tempfile.mkstemp(dir=str(record_path.parent),
                                   prefix=f".{record_path.name}.")
        try:
            with os.fdopen(fd, 'w') as record_file:
                json.dump({'hosts': hosts, 'times': times}, record_file)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
        return stamp
    except Exception:
        return stamp
    finally:
        if lock_handle is not None:
            lock_handle.close()


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
# the same way. The backend writes the facts of an entry and the console
# trims the whole entry once an answer resolves the notice it names, so
# the resolved identities leave the record first and the backend's
# bounded accounting of notices drops their marks before the mark of any
# notice still waiting. Each update reads the record, merges its change in
# and replaces the file in one step, and one complete update is held
# against the next on the lock file beside the record, so neither write
# can erase the other's fact and a read always meets one whole record.
NOTICE_RECEIPTS_RECORD = 'host-refusal-notice-receipts.json'
NOTICE_RECEIPTS_RECORD_FILE_ENV = 'BAZARR_REFUSAL_NOTICE_RECEIPTS_FILE'
NOTICE_RECEIPTS_RECORD_LOCK_SUFFIX = '.lock'

# The two layers that can hold the naming of one refusal: the backend at
# its own accounting, and the console at the line a failed delivery wrote
# or the count a held-back first failure joined a window. The outcome each
# claim writes carries its claimant beside it, so the pair can tell whose
# naming stands on an entry: a console's naming bars every layer, while a
# backend's bars the console alone, because a backend that restarts holds
# none of the one before it and names the refusal in its own record's
# right.
NOTICE_ACCOUNTED_BY_BACKEND = 'backend'
NOTICE_ACCOUNTED_BY_CONSOLE = 'console'


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


def _notice_record_resolved(record, notice_id):
    """Whether the record shows this notice resolved, an answer or the
    queue's bound having ended its life.

    An entry the record no longer holds is a resolved notice whose
    resolution aged out of the retention span, and an entry holding its
    resolution is one the console wrote on the acknowledgement or the
    eviction: both read the same here, because the resolution is the
    last word on the notice's life whichever shape it meets the reader
    in, and the facts of its delivery mean nothing after it.
    """
    entry = record.get(notice_id)
    if not isinstance(entry, dict):
        return True
    return isinstance(entry.get('resolved'), (int, float))


def _prune_notice_record(record, now):
    """Keep the entries whose facts are still within their retention
    spans, so the record stays bounded: a receipt is a fact about one
    delivery, held on the span both sides keep for a delivery no answer
    resolved, while an outcome is the pair's proof a notice is accounted,
    held for as long as the notice it names can still be waiting, because
    the console holds a notice until an acknowledgement resolves it or
    the pending queue evicts it and neither boundary is a span of time.
    The outcome is therefore kept past every span: the retry of a notice
    that failed a day or more ago still reads it back and names the
    refusal nowhere again, and the entry leaves the record only at the
    acknowledgement that resolves the notice, in the trim the console
    makes, so a record that keeps an unresolved notice's outcome cannot
    outgrow the notices the pair still holds. A resolution is held on the
    retention span both sides share, because it exists to refuse the
    writes a lost answer can let lag behind it, and no write lags behind
    its notice by more than the span a delivery no answer resolved is
    worth, so the resolutions age out on their own and the record stays
    bounded whatever the pair's deliveries do. The resolutions are
    bounded in number beside the span, on the pair's own bound of
    refused hosts: the newest of them are the ones a lagging write can
    still be racing, and one whose notice fell out of that many newer
    resolutions has outlasted every bounded memory the pair keeps of it,
    the pending queue, the newest-refused set and the hour's line
    bookkeeping all holding the same bound, so the record keeps no more
    of the retired past than the pair keeps of anything else, and a
    burst of evictions retires its own history as it goes."""
    pruned = {}
    resolutions = []
    for notice_id, entry in record.items():
        if not isinstance(entry, dict):
            continue
        resolved = entry.get('resolved')
        if isinstance(resolved, (int, float)):
            if now - resolved < CARRY_RETENTION_SECONDS:
                resolutions.append((resolved, notice_id, entry))
            continue
        received = entry.get('received')
        accounted = entry.get('accounted')
        if ((isinstance(received, (int, float)) and now - received < CARRY_RETENTION_SECONDS)
                or isinstance(accounted, (int, float))):
            pruned[notice_id] = entry
    resolutions.sort(key=lambda resolution: resolution[0])
    pruned.update({notice_id: entry
                   for _resolved, notice_id, entry in resolutions[-REFUSED_HOSTS_LIMIT:]})
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


def _record_notice_fact(notice_id, key, stamp, path):
    """Write one fact of one notice into the shared record, held on the
    lock beside it.

    The two facts of one entry are written by turns, the receipt of the
    taking in and the outcome of the accounting beside it, and each write
    reads the record, merges its fact in and replaces the file: one
    complete update is held against the next on the lock file beside the
    record, so neither write can erase the other's fact, exactly as the
    record of completed windows is held. The lock is best effort with the
    write it guards, exactly as the write is: a platform or a directory
    it cannot be taken in proceeds as one unlocked update rather than
    proving the fact never happened.

    The lifecycle state of the notice decides before either fact lands:
    a notice the record shows resolved, by the acknowledgement that
    answered it or the eviction that dropped it from the pending queue,
    holds its resolution on the record under this same lock, and a fact
    that arrives after the resolution writes nothing. The client of a
    delivery can give up on its answer before the backend ever took the
    report in, and the backend's writes follow that timeout by however
    long the handler waited, so the receipt and the outcome can both
    land for a notice nothing will ever deliver again: an outcome like
    that stays on the record with nothing to remove it, because the
    notice's own lifetime ended at the eviction and no answer will trim
    it, and the accounting bound that reconciles its marks against the
    record reads the orphan as a notice still waiting, however often
    the pair resolves notices the backend never answered. The resolution
    on the record is the last word on the notice's life, and the writes
    that follow it find it here, under the lock, and write nothing.
    """
    record_path = Path(path)
    lock_handle = None
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(str(record_path) + NOTICE_RECEIPTS_RECORD_LOCK_SUFFIX, 'a+')
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
            lock_handle = None
    try:
        record = _read_notice_receipts_record(path)
        entry = _notice_record_entry(record, notice_id)
        if isinstance(entry.get('resolved'), (int, float)):
            # The notice is resolved and its resolution is on the record:
            # no fact of its delivery can land for it any more, because
            # nothing will ever carry the notice again and nothing will
            # ever trim what a write would leave.
            return
        entry[key] = stamp
        record[notice_id] = entry
        _store_notice_record(path, _prune_notice_record(record, stamp))
    except Exception:
        pass
    finally:
        if lock_handle is not None:
            lock_handle.close()


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
    alone. The write is held on the lock the record's own updates hold
    and is best effort, exactly as the record of completed windows is.
    """
    if not notice_id:
        return
    path = notice_receipts_record_path() if path is None else path
    _record_notice_fact(notice_id, 'received', received_at, path)


def record_notice_accounted(notice_id, accounted_at, path=None,
                             claimant=NOTICE_ACCOUNTED_BY_BACKEND):
    """Retain the outcome of one notice's accounting, and answer whether
    the naming of its refusal is this claimant's to deliver.

    A layer writes this once its accounting of the notice completed, the
    backend in the line the hour's budget spent on the refusal or in the
    fold of a line a console elsewhere already spent, and the console in
    the line a failed delivery wrote or in the count a held-back first
    failure joined a window, and the write is the naming's assignment,
    taken under the lock the record's own updates hold before any line is
    logged: the assignment is the pair's one decision on who names the
    refusal, and the answer tells the caller whether the line is its to
    write. Both layers claim under the same lock, and the outcome each
    writes carries its claimant beside it: a console's naming bars every
    claim that follows, because the refusal is named at the console that
    saw it and the late accounting of a delivery whose answer a client's
    timeout ended meets the claim and names the refusal nowhere a second
    time, while a backend's naming bars the console alone, because a
    backend that restarts holds none of the one before it and names the
    refusal in its own record's right the way its own bounded accounting
    does, so the pair keeps one naming per live recorder and the fresh
    backend's own line is no second one. A notice the record shows
    resolved answers false and
    its refusal is named nowhere here: the resolution, the
    acknowledgement that answered the notice or the eviction that
    dropped it from the pending queue, is the last word on the notice's
    life under this same lock, and the backend's late work honors it,
    because the answer that resolved the notice already told the pair
    everything it owed and the console named the refusal at the failure
    that lost the answer, in a line of its own or in the window its
    held-back lines count into, so the pair keeps the one naming the
    refusal owes whichever channel reached it first and no line of a
    notice nothing will ever deliver again joins the one that stands. A
    backend that restarts renews a standing outcome of its own kind with
    its claim, because the recorder that named the refusal first is gone
    and the fresh one names it in its own record's right, the way its own
    bounded accounting does. The entry is kept
    even when the receipt never reached the file, so the outcome outlives
    whatever the taking in did. The refusal is held on the resolution and
    on the outcome that already stands, and the write is best effort
    exactly as the record's other updates are: a record nothing can write
    keeps the facts it held, and the claimant names the refusal rather
    than hold it on a proof that never reached the file.
    """
    if not notice_id:
        return True
    path = notice_receipts_record_path() if path is None else path
    record_path = Path(path)
    lock_handle = None
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(str(record_path) + NOTICE_RECEIPTS_RECORD_LOCK_SUFFIX, 'a+')
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
            lock_handle = None
    try:
        record = _read_notice_receipts_record(path)
        entry = _notice_record_entry(record, notice_id)
        if isinstance(entry.get('resolved'), (int, float)):
            # The notice's life ended at the answer that resolved it or at
            # the eviction that dropped it, and the naming of its refusal
            # belongs to whoever named it first: the late accounting of a
            # delivery the queue already gave up on names it nowhere again.
            return False
        if (isinstance(entry.get('accounted'), (int, float))
                and (claimant == NOTICE_ACCOUNTED_BY_CONSOLE
                     or entry.get('accounted_by', NOTICE_ACCOUNTED_BY_BACKEND)
                     == NOTICE_ACCOUNTED_BY_CONSOLE)):
            # The naming of this notice's refusal is assigned already, at
            # the console whose failed delivery wrote the line or whose
            # held-back count joined a window, or at another recorder of
            # this claimant's own kind for a console's claim, which asks
            # only after its guard read the record: a console's claim
            # bars every layer, because the refusal was named at the
            # console that saw it and the late accounting of a delivery
            # whose answer a client's timeout ended meets the claim and
            # names the refusal nowhere a second time. A backend's claim
            # renews a standing outcome of its own kind instead of
            # failing on it, because the recorder that named the refusal
            # first is gone with its restart and the fresh one names the
            # refusal in its own record's right.
            return False
        entry['accounted'] = accounted_at
        entry['accounted_by'] = claimant
        record[notice_id] = entry
        _store_notice_record(path, _prune_notice_record(record, accounted_at))
    except Exception:
        pass
    finally:
        if lock_handle is not None:
            lock_handle.close()
    return True


def notice_named_elsewhere(notice_id, path=None):
    """Whether the shared record shows the naming of this notice's refusal
    is assigned to a layer away from the asker, the notice resolved or
    not.

    The outcome of a notice's accounting is the decision that assigned
    the naming of its refusal to the layer that claimed it, the backend
    at its own accounting or the console at the line a failed delivery
    wrote, and the assignment is what the pair coordinates its one
    naming on: the console whose delivery failed asks here before it
    names the refusal the notice carried, and a notice whose outcome
    stands on the record, resolved or not, is named there and the
    console writes no line and counts nothing beside it. The assignment
    survives the notice's own resolution, because an eviction that ends
    a notice's life while a naming already stands keeps the assignment
    on the record, and the refusal keeps the one naming the pair owes
    it whichever side of the eviction the backend's processing landed
    on; an outcome a console still waiting on its answer reads means
    the same thing, so the one reader answers both. The stricter
    question, whether the record holds a live outcome for a notice
    still waiting, has its own reader, and the accounting bound and the
    record's own bookkeeping keep using that one.
    """
    if not notice_id:
        return False
    path = notice_receipts_record_path() if path is None else path
    record = _read_notice_receipts_record(path)
    entry = _notice_record_entry(record, notice_id)
    return isinstance(entry.get('accounted'), (int, float))


def trim_notice_record(notice_id, path=None):
    """Resolve the notice on the record, and drop the facts of its
    delivery with the resolution.

    The console resolves a notice here, at the acknowledgement that
    answered it and at the eviction that dropped it from the pending
    queue, and the resolution is written as the notice's lifecycle state
    under the lock the record's own updates hold: the facts of the
    delivery leave with it, and a write that follows the resolution finds
    the state here and lands nothing, because the client of a delivery
    can give up on its answer before the backend ever took the report
    in, and the backend's writes follow that timeout by however long its
    handler waited. An outcome that landed for a resolved notice would
    stay on the record with nothing to remove it, because no delivery
    will ever carry the notice again, and the accounting bound that
    reconciles its marks against the record reads the stale outcome as a
    notice still waiting, so the resolution is the last word on the
    notice's life and the writes coordinate on it, under the one lock
    they share. The backend's bounded accounting of notices reads the
    state the same way and drops the notice's mark before the mark of
    any notice still waiting, however many acknowledged notices arrive
    around one that is. The trim is best effort with the record's
    other updates: a record nothing can write keeps the facts it held,
    and the retention ages them out.

    The outcome is the one exception the resolution keeps, because it
    is the naming ownership the pair owes the refusal: the outcome is
    the decision that assigned the refusal's one naming to the layer
    that claimed it, the backend at its own accounting or the console at
    the line a failed delivery wrote, taken under this same lock before
    the line that delivered it, and an eviction that resolves the notice
    while a naming already stands keeps the assignment beside the
    resolution, because the pair owes the refusal one naming whichever
    channel reached it first and the console whose delivery then fails
    reads the assignment back and names the refusal nowhere a second
    time, the same way it does for a notice still waiting. The receipt
    is a fact about the taking in alone and nothing reads it past the
    notice's life, so it leaves with the resolution. The state and the
    assignment it kept are held on the retention span both sides share,
    so they outlive any write the timeout let lag and leave the record
    on their own.
    """
    if not notice_id:
        return
    path = notice_receipts_record_path() if path is None else path
    record_path = Path(path)
    lock_handle = None
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(str(record_path) + NOTICE_RECEIPTS_RECORD_LOCK_SUFFIX, 'a+')
        import fcntl
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
            lock_handle = None
    try:
        stamp = time.time()
        record = _read_notice_receipts_record(path)
        entry = _notice_record_entry(record, notice_id)
        resolution = {'resolved': stamp}
        if isinstance(entry.get('accounted'), (int, float)):
            # The naming of the refusal already stands on the record, the
            # backend's accounting or the console's own failure naming,
            # under the lock this trim holds: the assignment survives the
            # notice's resolution, so the console that reconciles the lost
            # answer at its failure names the refusal nowhere a second
            # time, and the pair keeps the one naming whichever side of
            # the eviction the backend's processing landed on.
            resolution['accounted'] = entry['accounted']
        record[notice_id] = resolution
        _store_notice_record(path, _prune_notice_record(record, stamp))
    except Exception:
        pass
    finally:
        if lock_handle is not None:
            lock_handle.close()


def notice_receipt_has(notice_id, path=None):
    """Whether the shared record shows the backend took this notice in.

    A resolved notice reads false: its resolution is the last word on
    its life, and a receipt that lags behind one means nothing, because
    nothing will ever carry the notice again.
    """
    if not notice_id:
        return False
    path = notice_receipts_record_path() if path is None else path
    record = _read_notice_receipts_record(path)
    if _notice_record_resolved(record, notice_id):
        return False
    entry = _notice_record_entry(record, notice_id)
    return isinstance(entry.get('received'), (int, float))


def notice_accounted_has(notice_id, path=None):
    """Whether the shared record shows the backend completed this notice's
    accounting: the refusal it carried was named or merged there, and the
    console whose delivery failed names it nowhere else.

    A resolved notice reads false: the resolution dropped the facts of
    its delivery, because the acknowledgement or the eviction that
    resolved it already told the pair everything the notice owed, and
    the writes that lag behind the resolution land nothing.
    """
    if not notice_id:
        return False
    path = notice_receipts_record_path() if path is None else path
    record = _read_notice_receipts_record(path)
    if _notice_record_resolved(record, notice_id):
        return False
    entry = _notice_record_entry(record, notice_id)
    return isinstance(entry.get('accounted'), (int, float))


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
        self._window_id = None  # the current window's travelling ID, the earliest window it counts
        self._local_window_id = None  # the stable identity of the counts this layer's own refusals add
        self._window_ids = []  # every ID the current window counts, its own and each folded-in one
        self._window_counts = OrderedDict()  # window ID -> the counts it contributed to the current window
        self._counted_windows = OrderedDict()  # window ID -> when it was counted here
        self._completed_windows = OrderedDict()  # window ID -> when it was paid here
        self._first_failures = OrderedDict()  # host -> when its first failed notice counted
        self._named_notices = OrderedDict()  # host -> the notice whose line was written here
        self._folded_paces = OrderedDict()  # host -> the backend line whose fold holds its pace
        self._notice_accounted = OrderedDict()  # notice identity -> when its refusal was named here

    def _count_suppressed(self, now):
        """Count one held-back line into the window, minting its stable ID.

        The ID is minted when the window's own first held-back line counts,
        and never regenerated: every later handoff of the window, in a
        report, an acknowledgement or the console's fold back, carries the
        same one, so whichever layer receives the window can tell whether
        it has already counted it. The minted ID also opens the window's
        list of the IDs it counts, the ones the summary that pays the
        window completes on the list the acknowledgement carries, and the
        count joins its own place in the map the window keeps per ID, so a
        payment that finds some of the IDs claimed elsewhere can settle
        the rest by identity. The mint is the identity of this layer's own
        contribution and nothing else: a fold that carries an older
        window's ID in moves the ID the aggregate travels under, the
        earliest window's, and every count this layer's own refusals add
        keeps joining the mint instead, however many windows fold in
        around it, so a payment that finds the folded identity claimed
        elsewhere still pays these counts under their own.
        """
        if self._local_window_id is None:
            self._local_window_id = _mint_window_id()
            self._counted_windows[self._local_window_id] = now
            if self._local_window_id not in self._window_ids:
                self._window_ids.append(self._local_window_id)
            if self._window_id is None:
                self._window_id = self._local_window_id
        self._suppressed += 1
        self._window_counts[self._local_window_id] = self._window_counts.get(self._local_window_id, 0) + 1

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

    def note(self, host, line_at=None, refused_at=None, counted_elsewhere=False):
        """Record a refusal; True when a per-host line may be written now.

        ``line_at`` carries the time a console elsewhere spent this
        hostname's line within the hour, reported by the front listener
        that wrote it while the backend it fronts could not be told: the
        refusal is recorded all the same, but no second line is written
        for it and this hour's cap here is not spent on the one already
        spent there. A line held back this way is not counted as
        suppressed either, because the console that spent it keeps its own
        count of what it held back. The fold is remembered as the
        hostname's pace apart from the lines this layer spent on its own,
        because the two spend their one line on different refusals: the
        fold named the hour's earlier refusal at the backend that logged
        it, while a fresh refusal of the hostname, once a backend has
        restarted or a console's hour has rolled, is named nowhere yet,
        and the failure of its first notice asks the pace and counts
        that one refusal here, the only layer left that can.

        ``refused_at`` carries the refusal's own time for a report that
        lands late, so the record holds when the refusal happened and not
        when its notice was delivered: the record is ordered by these
        stamps and its bound drops the oldest refusal by its stamp, so a
        delayed notice displaces no genuinely newer refusal, and a newer
        stamp already recorded for the host is never moved back.

        ``counted_elsewhere`` says the refusal this notice carries was
        counted by the console that saw it, in a line it wrote or in the
        window its held-back lines count into: the hostname is recorded
        here all the same, its refusal time folded in, but no line is
        written and no count joins a window, because the pair names the
        refusal once and the count the console holds is already it. The
        report says so beside the notice's own identity, independently of
        the line times the hour can roll away while the notice still
        waits for its answer, so a recovery that lands past them records
        the hostname without counting the refusal again.

        The line this layer would write is reserved in the shared budget
        before it is logged: the pair's cap of lines an hour and a
        hostname's one line an hour are the pair's facts and not one
        layer's, held in the shared state under the record's lock, so a
        backend whose answers were lost and a console whose reports
        reached nothing cannot each draw a whole cap beside the other's
        lines. The reservation captures the moment it decides on under
        that lock, after the wait, from this recorder's own clock, so a
        caller stalled behind the lock past an hour still meets the
        stamps the pair's newer lines spent, and the granted stamp is
        the moment the line's accounting here carries, the hostname's
        pace and the hour's line times alike. A reservation the shared
        budget refuses holds the line back here exactly as the hour's
        own cap does, and the refusal counts into the window as a
        held-back line, at the moment the wait has run to.
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
                # The fold is remembered as the hostname's pace, because
                # it is a line a backend wrote and not one this layer
                # spent: the refusal the fold arrived on was named there,
                # while a fresh refusal of the hostname within the hour is
                # named nowhere yet, and the failure of its first notice
                # is the first layer that can be told so.
                self._folded_paces[host] = self._last_lines[host]
                return False
            if counted_elsewhere:
                # The console that saw the refusal counted it into the
                # window the report carries, or named it in a line of its
                # own: the hostname joins the record with its refusal
                # time, and nothing increments here, because the pair
                # owes the refusal one naming and this count is already
                # made.
                return False
            if host in self._last_lines or len(self._line_times) >= REFUSAL_LOG_LINES_PER_HOUR:
                self._count_suppressed(now)
                return False
            granted = reserve_refusal_line(host, self._now)
            if granted is None:
                # The pair's shared budget spent this hour's line already,
                # in the other process: the console drew the cap with
                # lines whose answers never folded back, or the hostname's
                # own line stands there for the hour, and no line of this
                # layer's joins it. The reservation captured the moment
                # it decided on under the record's lock, after the wait,
                # so the held-back line belongs to the hour the pair's
                # newest lines opened and not to the stale capture the
                # wait began with: the refusal counts into the window
                # here the way every other held-back line does, at the
                # moment the wait has run to, so the pair's one budget
                # loses no refusal it holds back.
                self._count_suppressed(self._now())
                return False
            self._last_lines[host] = granted
            self._line_times.append(granted)
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

    def note_retry(self, host, notice_id=None):
        """Record a retry's line; True when a per-host line may be written now.

        The pace and the cap are consulted as ``note`` consults them and an
        allowed answer spends the same allowance, but a line they hold back
        is not counted as suppressed: the delivery retries of a failed
        report ask here, and an hour of them must not add up to a summary
        that says a stream of refusals was held back when one hostname's
        line was. The refusal record itself is left alone, because a
        report's retry is not a new refusal.

        The retries are held to one naming by the notice they carry: the
        line a first failure wrote is remembered under the notice's own
        identity, and the hour's pace that held the retries back within
        the hour is not the boundary the pair keeps across one, because
        the notice is unchanged however far the hour has rolled. The
        notice's own lifetime is: a retry of a notice whose line was
        written here writes no second line past the hour either, until
        the acknowledgement resolves the notice or the pending queue
        evicts it, and no count joins a window for a refusal a line here
        named already.

        The first failure is different: a report whose delivery fails leaves
        its hostname named nowhere else, so the first failed notice of a
        host whose line the hour's cap holds back counts toward the window
        the way its own held-back line would, and the host's later retries
        of that notice do not. The hour's pace holds a fresh notice the
        same way the cap does when the pace was a backend's to spend: a
        line a backend logged and the acknowledgement folded into this
        recorder named the hour's earlier refusal, and a fresh refusal of
        the hostname once a backend has restarted is still owed its
        naming, whichever notice carries it, so the failure counts that
        notice once as suppressed and no line is stamped for it, because
        no layer ever wrote one. A pace this console spent itself names
        the refusal the report carries, at the refusal that queued the
        notice or at a failed delivery of this very notice, and the
        failure counts nothing beside it. The host is marked when it is
        counted, and the mark lives exactly as long as the notice: the log
        pace cannot tell the retries of one notice from a fresh notice of
        the same host, because no line was ever written for the host and
        ``_last_lines`` holds nothing for it, so the notice's own
        lifetime is the only boundary. The console clears the mark when
        the notice resolves, at the acknowledgement of its report and at
        the pending queue's eviction alike, and no hour horizon clears
        it in between, so a retry of an unchanged notice counts its
        first failure once, in whichever hour it fails.

        The line the failure would write is reserved in the shared
        budget before it is logged, exactly as the backend's own line
        is: the pair's cap of lines an hour and a hostname's one line an
        hour are the pair's facts, held in the shared state under the
        record's lock, so a console whose failures reached no backend
        cannot draw a second cap beside the lines a backend already
        spent, however lost the answers that would have folded them in
        were. The reservation captures the moment it decides on under
        that lock, after the wait, from this recorder's own clock, so a
        caller stalled behind the lock past an hour still meets the
        stamps the pair's newer lines spent, and the granted stamp is
        the moment the line's accounting here carries, the hostname's
        pace and the hour's line times alike. A refused reservation
        holds the line back the way the hour's own cap does, and the
        first failure it holds back counts once into the window, at the
        moment the wait has run to.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            if (notice_id is not None
                    and self._named_notices.get(host) == notice_id):
                # The line this hour's pace has forgotten was written for
                # this very notice, and the notice is unchanged: its
                # refusal is named here once already, and a rolled hour
                # grants its retry neither a second line nor a count.
                return False
            if host in self._last_lines:
                # The hostname's one line for the hour is spent, and the
                # spender decides what the failure owes. A line this
                # console wrote named the refusal the report carries, or
                # a line a failed delivery of this very notice named,
                # and the failure counts nothing beside it. A backend's
                # line the acknowledgement folded in spent the pace on a
                # refusal the hour before this one, and a fresh notice
                # of the hostname, the first after a restart or the
                # first behind a lost hour, is still owed its naming:
                # no layer writes the fresh refusal a line within the
                # hour, so the failure counts it once as suppressed, the
                # way a line the hour's cap holds back is counted, and
                # the mark holds the notice's retries to the one count.
                # A retry of a notice that already counted reads the
                # mark and counts nothing.
                if (host not in self._first_failures
                        and host in self._folded_paces):
                    self._first_failures[host] = now
                    self._count_suppressed(now)
                return False
            if len(self._line_times) >= REFUSAL_LOG_LINES_PER_HOUR:
                if host not in self._first_failures:
                    self._first_failures[host] = now
                    self._count_suppressed(now)
                return False
            granted = reserve_refusal_line(host, self._now)
            if granted is None:
                # The pair's shared budget spent this hour's line already,
                # in the other process: the backend drew the cap with lines
                # whose answers never folded back, or the hostname's own
                # line stands there for the hour, and this console's
                # fallback writes no second line beside them. The
                # reservation captured the moment it decided on under the
                # record's lock, after the wait, so the failure and its
                # count belong to the hour the pair's newest lines opened
                # and not to the stale capture the wait began with. The
                # first failure counts once into the window here, at the
                # moment the wait has run to, the way a line the hour's
                # own cap holds back is counted, and no layer writes the
                # refusal a line within the hour.
                moment = self._now()
                if host not in self._first_failures:
                    self._first_failures[host] = moment
                    self._count_suppressed(moment)
                return False
            self._last_lines[host] = granted
            if notice_id is not None:
                self._named_notices[host] = notice_id
            self._line_times.append(granted)
            self._line_ids.append(_mint_line_id())
            return True

    def note_held_back(self, host):
        """Count one refusal whose line the hour's pace holds back, and mark
        the host the way a first failure's count is marked.

        A fresh notice of a hostname whose line the hour's budget has spent
        carries that spent line's time in its report, so the backend that
        answers folds the fresh refusal's line and no layer will ever write
        it: this layer is the pair's only count for the refusal, and it
        counts the refusal into its own window here, exactly once, the way
        the lines the hour's cap holds back are counted. The mark joins the
        first failures', because this count is the notice's first failure:
        a retry of the notice that later fails counts nothing beside it,
        and an eviction after it counts nothing either, while a fresh
        notice that arrives once the pace has run counts its own refusal
        anew.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            if host not in self._first_failures:
                self._first_failures[host] = now
                self._count_suppressed(now)

    def first_failure_resolved(self, host):
        """Clear the first-failure marks of a notice that resolved.

        The marks live exactly as long as the pending notice they were
        counted or named for: the console clears them when the backend
        acknowledges the notice's report and when the pending queue evicts
        the notice, the two ways a notice leaves, so a fresh notice of the
        same host counts its own first failure anew however soon it
        follows one that resolved, and names its refusal in a line of its
        own once the pace has stopped holding the hostname back.
        """
        with self._lock:
            self._first_failures.pop(host, None)
            self._named_notices.pop(host, None)

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
        that already named it. The first accounting marks the identity,
        and an arrival of a marked identity renews the mark, its stamp and
        its place both: an arrival is the notice still on its way, and a
        mark that kept the stamp and the place of its first accounting
        would let the bound below evict it under a notice still arriving,
        so the retry itself keeps the mark current and newest. The mark
        still lives past the hour and past the carry span both sides keep
        for a delivery no answer resolved, however long the notices wait:
        the console holds its notices until an acknowledgement resolves
        them or the pending queue evicts them, and neither boundary is a
        span of time, so the mark is kept the whole time the notice can
        still be on its way, and only the record's reconciliation and the
        bound's own eviction ever end it, never a day that ran out with no
        arrival to renew it. A notice with no identity, the refusal this
        layer saw itself, accounts every time it is refused: the pace of
        its hostname is what holds its repeats, exactly as it always did.
        The caller that skips the accounting on a retry still reports the
        hour's current budget, so a retry's answer carries everything the
        hour holds now and not the arrival's own news.

        The bound the marks live in is reconciled with the record of
        notices the pair shares: the console resolves an identity's entry
        as the answer that resolves it lands and at the eviction that
        drops it from the pending queue, on the acknowledgement and the
        eviction alike, so an identity the record shows resolved, its
        entry holding the resolution or aged out past it, belongs to a
        notice that is over, and its mark leaves the bound before the
        mark of any notice still waiting, however many acknowledged
        notices arrive around one that is. Only when the record holds
        every identity the bound holds unresolved, and the bound must
        still give a place, does the oldest mark leave.
        """
        if not notice_id:
            return True
        now = self._now()
        with self._lock:
            self._prune(now)
            stamp = self._notice_accounted.get(notice_id)
            if stamp is not None:
                # The identity is marked, so its refusal was accounted
                # here already and the notice is still on its way: the
                # mark lives until the notice resolves, however many
                # hours or days have passed without an arrival to renew
                # it, so the retry accounts the refusal nowhere again.
                self._notice_accounted[notice_id] = now
                self._notice_accounted.move_to_end(notice_id)
                return False
            self._notice_accounted[notice_id] = now
            while len(self._notice_accounted) > REFUSED_HOSTS_LIMIT:
                recorded = _read_notice_receipts_record(notice_receipts_record_path())
                resolved = [known_id for known_id in self._notice_accounted
                            if _notice_record_resolved(recorded, known_id)]
                if not resolved:
                    self._notice_accounted.popitem(last=False)
                    break
                del self._notice_accounted[resolved[0]]
            return True

    def _prune(self, now):
        spent = [(stamp, line_id) for stamp, line_id in zip(self._line_times, self._line_ids)
                 if now - stamp < HOUR_SECONDS]
        self._line_times = [stamp for stamp, _line_id in spent]
        self._line_ids = [line_id for _stamp, line_id in spent]
        self._last_lines = OrderedDict(
            (name, stamp) for name, stamp in self._last_lines.items() if now - stamp < HOUR_SECONDS)
        self._folded_paces = OrderedDict(
            (name, stamp) for name, stamp in self._folded_paces.items()
            if now - stamp < HOUR_SECONDS)
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
        # The accounted notices are not on the hourly roll at all: a
        # delivery whose answer was lost is retried under the notice's own
        # identity, and the retry can cross an hour, the carry span both
        # sides keep for a delivery no answer resolved and any span of
        # outage besides, because the notice stays pending until an
        # acknowledgement resolves it or the pending queue evicts it and
        # neither is a span of time. The mark that holds one notice to one
        # accounting therefore lives exactly as long as the notice does:
        # the record's reconciliation and the bound's own eviction end it,
        # at the acknowledgement and at the place the bound gives a newer
        # identity, and no passage of time without an arrival expires it,
        # so a notice recovered past a sustained outage still finds its
        # one accounting held.

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
        rules name, whichever layer's own hour ran first. The drop is
        decided by one claim on the record, taken under the lock the
        record's own updates hold, so two layers one step from paying the
        same window cannot both pass it: the claim that wins pays the one
        summary, and the take that loses drops the window it took without
        logging anything, however closely the two payments overlap.

        The claim is taken before any line is logged and it covers every
        ID the window counts, its own minted one and each one folded in,
        all in the one locked step: the layer that pays the aggregate
        claims each window it folded in the moment it decides to pay, so
        a payer that dies between its line and its own record write has
        still claimed every ID the line counted, and whichever layer
        holds a copy of any of them reads the proof from the record,
        whatever the write the death ate was for. The write the line's
        caller makes afterwards is a re-stamp of the same payment, and it
        is kept for the answer that would carry the proof, not for the
        decision itself. A claim that meets some of the IDs already
        claimed settles the window by identity instead: the claimed
        constituents' counts drop with the payment the other layer
        logged, and each unpaid one claims its own identity here and pays
        its counts as the one summary the window still owes, so no count
        dies beside a claim it never joined and none is paid a second
        time.

        The pair's ceiling of one summary an hour holds a payment that
        finds another line within the hour, on the record of emission
        moments the two processes share: two windows no acknowledgement
        ever carried between the layers share no identity, so the claim
        cannot decide between them, and the moments decide instead. The
        decision and the spend of the hour's one slot are a single
        reserved step, taken under the lock the record's own updates
        hold, and the moment it decides on is captured under that lock
        itself, so a timer that waited behind another process's update
        asks inside the hour the pair's newest line opened however long
        the wait ran, and two payments deciding inside the same moment
        cannot both pass it, and every payment path asks it, the whole
        claim and the settlement by identity alike, because a settlement
        pays counts into a line the same way a whole window does. A
        payment
        the ceiling holds returns its claims and waits with every count
        it keeps, and the next paced look pays it once the hour since
        the pair's last line has run, so the summaries the pair logs
        stay an hour apart however the traffic split the counts. The
        ceiling holds best effort exactly as the record's other proofs
        are, and a record nothing can read holds nothing.
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
            if (self._window_ids
                    and not claim_summary_payment(self._window_ids, now)):
                # The one payment decision on the record the two processes
                # share went partly to the other layer: it took some of
                # these counts into the one summary line it wrote and
                # named one of this window's IDs among the ones it
                # completed, whether before this take ran or inside the
                # same moment of it. The payment settles the window by
                # identity rather than refusing it whole: each ID the
                # other layer has not claimed is claimed here, one by one
                # under the same lock, and the counts those IDs
                # contributed are the ones this summary pays, while the
                # claimed ones' counts drop with the payment the other
                # layer logged, so no unpaid counts die beside a claim
                # they never joined and no claimed ones are paid twice.
                unpaid_ids = []
                paid_counts = 0
                for known_id in self._window_ids:
                    if claim_summary_payment(known_id, now):
                        unpaid_ids.append(known_id)
                    else:
                        paid_counts += self._window_counts.get(known_id, 0)
                unpaid_counts = self._suppressed - paid_counts
                if not unpaid_ids and not unpaid_counts:
                    # Every constituent's counts are paid by the other
                    # layer's line already: the window is dropped rather
                    # than paid a second time, whatever this layer's own
                    # hour says, and the pair keeps the one summary per
                    # window the hour's budget rules name, paid once by
                    # whichever layer claimed it first.
                    self._suppressed = 0
                    self._window_opens = None
                    self._window_id = None
                    self._local_window_id = None
                    self._window_ids = []
                    self._window_counts = OrderedDict()
                    return None
                # The counts that survive are owed their one line here,
                # under the identity of the first constituent this
                # payment claimed, so the answer that follows names it
                # and the layer holding a copy of it drops the copy.
                suppressed = unpaid_counts
                window_id = unpaid_ids[0] if unpaid_ids else None
                window_ids = unpaid_ids
            else:
                suppressed = self._suppressed
                window_id = self._window_id
                window_ids = self._window_ids
            stamp = reserve_summary_emission(self._now)
            if stamp is None:
                # The pair logged a summary line within the hour, whichever
                # layer paid it and whichever windows the line counted, so
                # the ceiling the two processes share holds this payment
                # until the hour has run and the next paced look pays it
                # then, the whole claim and the settlement alike: the
                # settlement pays counts into a line the same way a whole
                # window does, and the pair's one summary an hour bounds
                # whichever path a payment takes. The reservation is one
                # locked step with the decision, and the moment it decides
                # on is captured under that lock itself, so a payment that
                # waited behind another process's update asks inside the
                # hour the pair's newest line opened, however long the
                # wait ran, and two payments deciding inside the same
                # moment cannot both pass it however
                # closely their turns overlap. The claims the held payment
                # made are returned with it, because the decision the
                # record keeps is the payment and not the hold: a layer
                # whose payment waits must not leave the other holding a
                # copy of the same window it reads as paid, and the claim
                # the payment retakes when the ceiling opens is the one
                # the pair's decision stands on. The window keeps every
                # count it holds while it waits.
                release_summary_claim(window_ids)
                return None
            opened = self._window_opens
            self._suppressed = 0
            self._window_opens = None
            self._window_id = None
            self._local_window_id = None
            self._window_ids = []
            self._window_counts = OrderedDict()
            for paid_id in window_ids:
                self._completed_windows[paid_id] = stamp
            return suppressed, opened, stamp, window_id

    def hour_suppressed_window(self):
        """The suppressed window this hour holds, taken by no one.

        The acknowledgement of a report carries it back the way the report
        carries the hour's spent line times: a copy, not a take, because the
        layer that holds the window keeps holding it whether or not its
        answer arrives, so an acknowledgement that is lost or mis-delivered
        never takes accounting with it. The ID travels with the copy, so
        the layer that holds the original recognizes the window when the
        copy is handed back and folds it nowhere. The copy carries every
        identity the window counts as well, the earliest window's first
        and each folded-in one beside it, with the counts each
        contributed, so the layer that holds the copy can claim the whole
        aggregate under the one decision its payment takes and tell a
        backend that lost the window what every constituent is owed,
        without the aggregate's total standing in for the parts it was
        built from.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            if not self._suppressed:
                return None
            if self._window_opens is None:
                self._window_opens = now
            window_ids = list(self._window_ids)
            if self._window_id is not None:
                window_ids = ([self._window_id]
                              + [known_id for known_id in window_ids
                                 if known_id != self._window_id])
            counts = [self._window_counts.get(window_id, 0) for window_id in window_ids]
            return self._suppressed, self._window_opens, self._window_id, window_ids, counts

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
        counted here: only the window's own identities leave with the take,
        the travelling one and the local contribution mint beside it when a
        fold moved the travelling one, and a re-carry of a folded-in ID
        still folds nowhere while this layer remembers it.
        """
        now = self._now()
        with self._lock:
            if not self._suppressed:
                return None
            count = self._suppressed
            opened = self._window_opens if self._window_opens is not None else now
            window_id = self._window_id
            local_window_id = self._local_window_id
            self._suppressed = 0
            self._window_opens = None
            self._window_id = None
            self._local_window_id = None
            self._window_ids = []
            self._window_counts = OrderedDict()
            if window_id is not None:
                self._counted_windows.pop(window_id, None)
            if local_window_id is not None and local_window_id != window_id:
                # The travelling ID the carry hands out is the earliest
                # window's, and the local contribution identity is this
                # layer's own beside it: both counted here leave with the
                # window, so a re-carry of either folds nowhere while this
                # layer remembers counting it.
                self._counted_windows.pop(local_window_id, None)
            return count, opened, window_id

    def merge_summary(self, count, opened, window_id=None, folded_ids=None, folded_counts=None):
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

        The carried window can itself be an aggregate of several windows,
        and its constituents travel with their identities and their counts
        beside the total, the window's own travelling ID first among
        them: the fold judges every constituent by its own identity, and
        each one this layer has not counted joins the window's list of
        the IDs it counts with the counts it contributed, so the summary
        that pays the combined window completes it too, a payment that
        finds some of the identities claimed elsewhere settles the rest
        by identity with the counts each one brought, and no folded
        counts die beside a claim they never joined. A constituent that
        has already counted here folds nowhere on its own, leaving the
        rest of the aggregate to fold, and one already paid here renews
        its completion the same way an identity alone always has, so the
        aggregate never counts a constituent a second time either. The
        earliest window's ID is the one the merged aggregate travels
        under, and the travelling ID moves to a folded constituent only
        when the fold took the identity it arrived with.
        """
        if not count or opened is None:
            return
        if folded_ids and folded_counts and len(folded_ids) == len(folded_counts):
            constituents = [(known_id, known_count)
                            for known_id, known_count in zip(folded_ids, folded_counts)
                            if known_id]
        elif window_id is not None:
            constituents = [(window_id, count)]
        else:
            constituents = [(None, count)]
        with self._lock:
            fresh_counts = 0
            primary_fresh = False
            for known_id, known_count in constituents:
                if known_id is None:
                    # The counts arrive with no identity to their name,
                    # the one shape a window with no stable ID keeps: the
                    # fold takes them in under no identity at all, so no
                    # handoff of theirs can ever tell whether it has
                    # counted them, and the wire carries nothing that
                    # could.
                    fresh_counts += known_count
                    continue
                if known_id == self._window_id or known_id in self._window_ids:
                    # The ID belongs to the window this layer is still
                    # holding: the window already counts the lines the
                    # carried copy stands for, and a live window outlives
                    # the counted record's lease, so a replay of its own
                    # mirror arriving once that lease has aged out must not
                    # fold the lines in a second time.
                    continue
                if known_id in self._counted_windows:
                    continue
                if known_id in self._completed_windows:
                    # A window whose summary this layer has already paid,
                    # carried again by a sender whose reconciliation no
                    # answer has reached: the fold refuses, and the
                    # arrival renews the completion record's lease, so the
                    # record is retained exactly until the sender stops
                    # re-carrying the window, on the one retention span
                    # both sides share. Once the sender has reconciled,
                    # the record ages out with the same span, so the list
                    # stays bounded and a copy the sender can still
                    # re-carry always meets a layer that still remembers
                    # paying it.
                    self._completed_windows[known_id] = self._now()
                    continue
                self._counted_windows[known_id] = self._now()
                self._window_counts[known_id] = self._window_counts.get(known_id, 0) + known_count
                if known_id not in self._window_ids:
                    self._window_ids.append(known_id)
                fresh_counts += known_count
                if known_id == window_id:
                    primary_fresh = True
            if not fresh_counts:
                return
            self._suppressed += fresh_counts
            if self._window_opens is None or opened < self._window_opens:
                self._window_opens = opened
                if window_id is None or primary_fresh:
                    self._window_id = window_id

    def recent(self, within_seconds):
        """The refused hosts of the window, newest first."""
        now = self._now()
        with self._lock:
            return [host for host, stamp in reversed(self._hosts.items())
                    if now - stamp < within_seconds]
