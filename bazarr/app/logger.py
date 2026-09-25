# coding=utf-8

import os
import logging
import re
import platform
import sys
import time
import warnings

from logging.handlers import TimedRotatingFileHandler
from literals import (LOG_BACKUP_COUNT_DEFAULT, LOG_BACKUP_COUNT_MAX, LOG_BACKUP_COUNT_MIN,
                      LOG_MAX_FILE_SIZE_MB_DEFAULT, LOG_MAX_FILE_SIZE_MB_MAX, LOG_MAX_FILE_SIZE_MB_MIN)
from utilities.central import get_log_file_path

from .config import settings


logger = logging.getLogger()

# The file handler configure_logging installs, kept so the log can be emptied and
# its rotation limits changed without rebuilding the handlers.
fh = None


class FileHandlerFormatter(logging.Formatter):
    """Formatter that removes apikey from logs."""
    # Bare "key=" too: 2Captcha-compatible captcha vendors carry the account
    # key as ?key=... on res.php polling, and urllib3 logs request targets.
    APIKEY_RE = re.compile(r'((?:api)?key)(?:=|%3D)([a-zA-Z0-9]+)')
    IPv4_RE = re.compile(r'\b(?<!Failed\sauthentication\sfrom\s)(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.)'
                         r'{3}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\b')
    PLEX_URL_RE = re.compile(r'(?:https?://)?[0-9\-]+\.[a-f0-9]+\.plex\.direct(?::\d+)?')

    def formatException(self, exc_info):
        """
        Format an exception so that it prints on a single line.
        """
        result = super(FileHandlerFormatter, self).formatException(exc_info)
        return repr(result)  # or format into one line however you want to

    def formatApikey(self, s):
        return re.sub(self.APIKEY_RE, r'\1=(removed)', s)

    def formatIPv4(self, s):
        return re.sub(self.IPv4_RE, '***.***.***.***', s)

    def formatPlexUrl(self, s):
        def sanitize_plex_url(match):
            url = match.group(0)
            # Extract protocol and port for reconstruction
            if '://' in url:
                protocol = url.split('://')[0] + '://'
                domain_part = url.split('://')[1]
            else:
                protocol = ''
                domain_part = url
            
            # Extract port if present
            if ':' in domain_part and domain_part.split(':')[-1].isdigit():
                port = ':' + domain_part.split(':')[-1]
                domain_part = domain_part.rsplit(':', 1)[0]
            else:
                port = ''
            
            # Extract the part before .plex.direct
            if '.plex.direct' in domain_part:
                plex_prefix = domain_part.replace('.plex.direct', '')
                # Show first 4 and last 4 characters with asterisks in between
                if len(plex_prefix) > 8:
                    sanitized_domain = f"{plex_prefix[:4]}{'*' * 6}{plex_prefix[-4:]}.plex.direct"
                else:
                    sanitized_domain = f"***{plex_prefix[-4:]}.plex.direct" if len(plex_prefix) >= 4 else "***.plex.direct"
            else:
                sanitized_domain = domain_part
            
            return f"{protocol}{sanitized_domain}{port}"
        
        return re.sub(self.PLEX_URL_RE, sanitize_plex_url, s)

    def format(self, record):
        s = super(FileHandlerFormatter, self).format(record)
        if record.exc_text:
            s = s.replace('\n', '') + '|'

        s = self.formatApikey(s)
        s = self.formatIPv4(s)
        s = self.formatPlexUrl(s)

        return s


class NoExceptionFormatter(FileHandlerFormatter):
    def format(self, record):
        record.exc_text = ''  # ensure formatException gets called
        return super(NoExceptionFormatter, self).format(record)

    def formatException(self, record):
        return ''


class UnwantedWaitressMessageFilter(logging.Filter):
    def filter(self, record):
        if settings.general.debug or "BAZARR" in record.msg:
            # no filtering in debug mode or if originating from us
            return True

        if record.levelno < logging.ERROR:
            return False

        unwantedMessages = [
            "Exception while serving /api/socket.io/",
            ['Session is disconnected', 'Session not found'],

            "Exception while serving /api/socket.io/",
            ["'Session is disconnected'", "'Session not found'"],

            "Exception while serving /api/socket.io/",
            ['"Session is disconnected"', '"Session not found"'],

            "Exception when servicing %r",
            [],
        ]

        wanted = True
        listLength = len(unwantedMessages)
        for i in range(0, listLength, 2):
            if record.msg == unwantedMessages[i]:
                exceptionTuple = record.exc_info
                if exceptionTuple is not None:
                    if len(unwantedMessages[i+1]) == 0 or str(exceptionTuple[1]) in unwantedMessages[i+1]:
                        wanted = False
                        break

        return wanted


# Per-logger levels as (level in a normal install, level with general.debug on).
#
# Every logger this module grades appears here once, in both columns, so a logger
# cannot end up debug-only or normal-only. The two columns are deliberately not
# symmetric: what a bug report needs is a line that is already there before anyone
# asks the reporter to enable debug and reproduce, so the normal column is the one
# that has to carry the provider lifecycle.
#
# `subliminal_patch` and `subzero` are Bazarr+'s own vendored fork under custom_libs/,
# not third-party dependencies, so they do not get the ERROR or CRITICAL a foreign
# library gets. A diagnosis needs `Terminating provider whisperai` as much as it needs
# the discard warning beside it, and that line is INFO: a WARNING floor would drop it
# from exactly the install that has to be read. Genuinely foreign libraries keep the
# floor they already had, and no floor here is lower than the one it replaces.
LOGGER_LEVELS = {
    'alembic.runtime.migration': (logging.CRITICAL, logging.DEBUG),
    'apprise': (logging.WARNING, logging.DEBUG),
    'apscheduler': (logging.WARNING, logging.DEBUG),
    'engineio.server': (logging.ERROR, logging.DEBUG),
    'ffsubsync.aligners': (logging.ERROR, logging.DEBUG),
    'ffsubsync.ffsubsync': (logging.ERROR, logging.DEBUG),
    'ffsubsync.speech_transformers': (logging.ERROR, logging.DEBUG),
    'ffsubsync.subtitle_parser': (logging.ERROR, logging.DEBUG),
    'ga4mp.ga4mp': (logging.ERROR, logging.WARNING),
    # Debug-only upstream, so in a normal install it inherits the root level. Spelled
    # out at INFO to keep that, rather than let a future root change move it.
    'git': (logging.INFO, logging.DEBUG),
    'SignalRCoreClient': (logging.CRITICAL, logging.WARNING),
    'socketio.server': (logging.ERROR, logging.DEBUG),
    'srt': (logging.ERROR, logging.DEBUG),
    'subliminal': (logging.CRITICAL, logging.DEBUG),
    'subliminal_patch': (logging.INFO, logging.DEBUG),
    'subzero': (logging.WARNING, logging.DEBUG),
    'websocket': (logging.CRITICAL, logging.WARNING),
}

# Loggers Bazarr+ does not own that emit a line per event rather than a line per
# decision. general.debug may not take these below their ceiling: one DEBUG emit per
# socket broadcast is most of a debug-mode log, and nothing diagnosable is knowable
# only from `emitting event "data" to all [/]`, because the payload is already
# reconstructable from the emitting caller's own logging. WARNING and ERROR are
# untouched, so a transport or scheduler fault still surfaces. log.verbose_loggers
# names the exceptions for someone debugging that transport on purpose.
LOGGER_LEVEL_CEILINGS = {
    'apscheduler': logging.WARNING,
    'engineio.server': logging.WARNING,
    'socketio.server': logging.WARNING,
}


def _verbose_loggers():
    """Logger names whose ceiling general.debug is allowed to lift.

    Ships empty. A single name read back from the config file as a string is split
    on commas and whitespace so a one-name list still lifts exactly that name.
    """
    configured = settings.get('log.verbose_loggers') or []
    if isinstance(configured, str):
        configured = re.split(r'[,\s]+', configured)
    return {str(name).strip() for name in configured if str(name).strip()}


def resolve_logger_levels(debug=False):
    """The level each graded logger gets, as {name: level}.

    Kept apart from configure_logging so the decision can be asserted without
    installing handlers and a file on the root logger.
    """
    verbose = _verbose_loggers()
    levels = {}
    for name, (normal_level, debug_level) in LOGGER_LEVELS.items():
        level = debug_level if debug else normal_level
        ceiling = LOGGER_LEVEL_CEILINGS.get(name)
        if debug and ceiling is not None and name not in verbose:
            # max() is the more restrictive of the two, which is the ceiling's job.
            # A normal install's own level is already at or above the ceiling, so
            # verbose_loggers is only ever consulted in debug.
            level = max(level, ceiling)
        levels[name] = level
    return levels


def configure_logging(debug=False):
    warnings.simplefilter('ignore', category=ResourceWarning)

    if debug:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logger.handlers = []

    logger.setLevel(log_level)

    # Console logging
    ch = logging.StreamHandler()
    cf = (debug and FileHandlerFormatter or NoExceptionFormatter)(
        '%(asctime)-15s - %(name)-32s (%(thread)x) :  %(levelname)s (%(module)s:%(lineno)d) - %(message)s')
    ch.setFormatter(cf)

    ch.setLevel(logging.DEBUG)
    logger.addHandler(ch)

    # File Logging
    global fh
    max_bytes, backup_count = log_rotation_limits()
    fh = SizeAndTimeRotatingFileHandler(get_log_file_path(), maxBytes=max_bytes, backupCount=backup_count,
                                        delay=True, encoding='utf-8')
    f = FileHandlerFormatter('%(asctime)s|%(levelname)-8s|%(name)-32s|%(message)s|',
                             '%Y-%m-%d %H:%M:%S')
    fh.setFormatter(f)
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    for name, level in resolve_logger_levels(debug).items():
        logging.getLogger(name).setLevel(level)

    if debug:
        logging.debug('Bazarr version: %s', os.environ["BAZARR_VERSION"])
        logging.debug('Bazarr branch: %s', settings.general.branch)
        logging.debug('Operating system: %s', platform.platform())
        logging.debug('Python version: %s', platform.python_version())

    logging.getLogger("waitress").setLevel(logging.INFO)
    logging.getLogger("waitress").addFilter(UnwantedWaitressMessageFilter())
    logging.getLogger("knowit").setLevel(logging.CRITICAL)
    logging.getLogger("enzyme").setLevel(logging.CRITICAL)
    logging.getLogger("guessit").setLevel(logging.WARNING)
    logging.getLogger("rebulk").setLevel(logging.WARNING)
    logging.getLogger("stevedore.extension").setLevel(logging.CRITICAL)
    logging.getLogger("plexapi").setLevel(logging.ERROR)

def _bounded_setting(key, default, lowest, highest):
    """An integer setting held inside its bounds, or its default when unreadable.

    The validators in config.py already refuse a value outside the bounds, so this
    only matters for a value that reached the settings some other way. The log
    folder's ceiling is the point of these two settings, and a bad value must not
    be the thing that removes it.
    """
    value = settings.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(value, lowest), highest)


def log_rotation_limits():
    """(max bytes of the live file, rolled files to keep), from log.* settings."""
    max_file_size_mb = _bounded_setting('log.max_file_size_mb', LOG_MAX_FILE_SIZE_MB_DEFAULT,
                                        LOG_MAX_FILE_SIZE_MB_MIN, LOG_MAX_FILE_SIZE_MB_MAX)
    backup_count = _bounded_setting('log.backup_count', LOG_BACKUP_COUNT_DEFAULT,
                                    LOG_BACKUP_COUNT_MIN, LOG_BACKUP_COUNT_MAX)
    return max_file_size_mb * 1024 * 1024, backup_count


def apply_log_rotation_settings():
    """Give the live file handler the current log.max_file_size_mb and log.backup_count.

    A smaller size takes effect at the next record, a smaller count at the next roll.
    """
    handler = fh
    if handler is None:
        return
    max_bytes, backup_count = log_rotation_limits()
    handler.acquire()
    try:
        handler.maxBytes = max_bytes
        handler.backupCount = backup_count
    finally:
        handler.release()


def empty_file(filename):
    # Open the log file in write mode to clear its contents
    with open(filename, 'w'):
        pass  # Just opening and closing the file will clear it

def empty_log():
    # Under the handler's lock throughout, including the note, so no other thread's
    # record lands between the roll, the truncation and the note, and the file the
    # note leaves behind can be recognised by the next empty.
    handler = fh
    handler.acquire()
    try:
        handler.roll_before_emptying()
        empty_file(get_log_file_path())
        logging.info('BAZARR Log file emptied')
        handler.note_emptied()
    finally:
        handler.release()


class SizeAndTimeRotatingFileHandler(TimedRotatingFileHandler):
    """Roll the live log at midnight, or earlier once it reaches maxBytes.

    The standard library has no handler that does both, and TimedRotatingFileHandler
    cannot simply be given a size check. It names a rolled file after the day the file
    covers, so a second roll on the same day computes the same name, and depending on
    the Python version it then either deletes the earlier file or stops rolling until
    the next restart.

    Naming: the first roll of a day is `bazarr.log.YYYY-MM-DD`, the name every midnight
    roll has always had, and each further roll that day is `bazarr.log.YYYY-MM-DD.N`,
    with N one higher than any already present. No roll overwrites or removes another,
    and a day that never reaches the size limit keeps exactly the one file it had
    before. The day is the one the file's records began in, as it always was.

    Pruning: the date and N together order every file this handler writes, oldest
    first, so the newest backupCount are kept by that order. A name the handler could
    not have produced, such as a compressed copy or another program's file, is neither
    counted nor deleted.

    The size check is made before a record is written, so the live file can pass
    maxBytes by at most one record. That keeps the check to a tell() rather than
    formatting every record twice. A size roll leaves the midnight deadline where it
    is, and every name is taken from that deadline, so neither a DST change nor a
    clock that steps back mid-day can give a file an earlier day than the one it
    covers, which pruning would then delete first.

    A roll that fails, such as a rename refused by a read-only folder or a file held
    open elsewhere, keeps logging into the live file, says so once on stderr, and is
    tried again at the next midnight or once the file has grown by another maxBytes.
    """

    clock = staticmethod(time.time)

    def __init__(self, filename, maxBytes=0, backupCount=0, encoding=None, delay=False, utc=False,
                 errors=None):
        super().__init__(filename, when='midnight', interval=1, backupCount=backupCount,
                         encoding=encoding, delay=delay, utc=utc, errors=errors)
        self.maxBytes = maxBytes
        # Size the live file must reach before a size roll is tried again after a
        # failed one; 0 when the last roll did not fail.
        self._retry_size = 0
        self._failure_reported = False
        # Size of the live file right after empty_log() wrote its note, while
        # nothing else has been written; None once anything has rolled.
        self._emptied_size = None
        base_name = os.path.basename(self.baseFilename)
        self._rolled_name = re.compile(re.escape(base_name) + r'\.(\d{4}-\d{2}-\d{2})(?:\.([1-9]\d*))?',
                                       re.ASCII)

    def _now(self):
        return int(self.clock())

    def _over_size(self):
        if self.maxBytes <= 0:
            return False
        if self.stream is None:
            # delay=True leaves the file unopened until the first record.
            self.stream = self._open()
        try:
            return self.stream.tell() >= max(self.maxBytes, self._retry_size)
        except (OSError, ValueError):
            # Not seekable, so not a regular file: nothing to measure or roll.
            return False

    def shouldRollover(self, record):
        now = self._now()
        if now < self.rolloverAt and not self._over_size():
            return False
        # Never roll anything but a regular file, such as a log path pointed at a
        # device. A file that is missing is still rolled, which reopens it.
        if os.path.exists(self.baseFilename) and not os.path.isfile(self.baseFilename):
            self.rolloverAt = self.computeRollover(now)
            return False
        return True

    def _day_of_current_file(self):
        """The day the live file's records began in.

        rolloverAt is the next local midnight as computeRollover works it out, DST
        included, and only a midnight roll moves it, so the second before it falls
        on the live file's day whenever and however often the file rolls on size.
        """
        last_second = self.rolloverAt - 1
        return time.strftime(self.suffix, time.gmtime(last_second) if self.utc else time.localtime(last_second))

    def _matching_names(self):
        directory = os.path.dirname(self.baseFilename)
        for file_name in os.listdir(directory):
            match = self._rolled_name.fullmatch(file_name)
            if match:
                yield match.group(1), int(match.group(2) or 0), os.path.join(directory, file_name)

    def rolled_files(self):
        """Every file this handler has rolled, as (day, number, path), oldest first."""
        return sorted(entry for entry in self._matching_names() if os.path.isfile(entry[2]))

    def _next_rolled_name(self):
        day = self._day_of_current_file()
        # Any entry holding a name counts, not only regular files: a directory in
        # the way would otherwise refuse the same rename on every roll.
        numbers = [number for rolled_day, number, _ in self._matching_names() if rolled_day == day]
        name = f'{self.baseFilename}.{day}'
        if numbers:
            name = f'{name}.{max(numbers) + 1}'
        return name

    def getFilesToDelete(self):
        rolled = self.rolled_files()
        excess = len(rolled) - self.backupCount
        if self.backupCount <= 0 or excess <= 0:
            return []
        return [path for _, _, path in rolled[:excess]]

    def _prune(self):
        try:
            stale = self.getFilesToDelete()
        except OSError:
            return
        for path in stale:
            try:
                os.remove(path)
            except OSError:
                pass

    def _report_failure(self, error):
        # stderr rather than a log record: logging from here would re-enter the roll.
        if not self._failure_reported:
            self._failure_reported = True
            sys.stderr.write(f'Bazarr could not roll {self.baseFilename} ({error}). Logging continues in that '
                             f'file, and the roll is tried again at midnight or after another '
                             f'{self.maxBytes} bytes.\n')

    def roll_before_emptying(self):
        """Roll the live file so emptying it keeps its records, unless it holds nothing new.

        A file holding only the note the previous empty_log() wrote has nothing worth
        a backup slot, and rolling it on every click would push a real day out each
        time.
        """
        if self._emptied_size is not None and self._live_size() == self._emptied_size:
            if self.stream:
                self.stream.close()
                self.stream = None
            return
        self.doRollover()

    def note_emptied(self):
        self._emptied_size = self._live_size()

    def _live_size(self):
        try:
            return os.path.getsize(self.baseFilename)
        except OSError:
            return 0

    def doRollover(self):
        now = self._now()
        if self.stream:
            self.stream.close()
            self.stream = None
        # An empty file has nothing worth keeping, and rolling it would spend a
        # backup slot, pushing the oldest real day out, on nothing.
        if os.path.isfile(self.baseFilename) and os.path.getsize(self.baseFilename) > 0:
            # A failure to roll must not cost the record that asked for the roll.
            try:
                self.rotate(self.baseFilename, self._next_rolled_name())
            except OSError as error:
                self._retry_size = self._live_size() + self.maxBytes
                self._report_failure(error)
            else:
                self._retry_size = 0
                self._failure_reported = False
                self._emptied_size = None
                self._prune()
        if not self.delay:
            self.stream = self._open()
        # Only midnight moves the deadline; see the class docstring.
        if now >= self.rolloverAt:
            self.rolloverAt = self.computeRollover(now)
