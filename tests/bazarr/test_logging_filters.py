import importlib
import logging
import os
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml
from dynaconf.validator import ValidationError

from app.config import settings
from app.logger import (LOGGER_LEVELS, LOGGER_LEVEL_CEILINGS, CoverStreamAborted, ExpiredSocketSessionFilter,
                        SizeAndTimeRotatingFileHandler, UnwantedWaitressMessageFilter, configure_logging,
                        empty_log, log_rotation_limits, resolve_logger_levels)

# The modules the names above came from. `from app import logger` is not always the
# same module in a full run: a test file that imports under a mocked sys.modules and
# then restores it can drop app.logger from sys.modules while the package attribute
# keeps pointing at the dropped copy, whose fh configure_logging never sets.
config = sys.modules["app.config"]
logger_module = sys.modules["app.logger"]
assert logger_module.configure_logging is configure_logging
assert logger_module.settings is settings

def test_true_for_bazarr():
  record = logging.LogRecord("", logging.INFO, "", 0, "a message from BAZARR for logging", (), None)
  assert UnwantedWaitressMessageFilter().filter(record)

def test_false_below_error():
  record = logging.LogRecord("", logging.INFO, "", 0, "", (), None)
  assert not UnwantedWaitressMessageFilter().filter(record)

def test_true_above_error():
  record = logging.LogRecord("", logging.CRITICAL, "", 0, "", (), None)
  assert UnwantedWaitressMessageFilter().filter(record)


def _serving_error(error):
    """The record waitress logs when a response body raises partway through."""
    try:
        raise error
    except Exception:
        exc_info = sys.exc_info()
    return logging.LogRecord("waitress", logging.ERROR, "", 0,
                             "Exception while serving /images/series/MediaCover/1/poster-500.jpg", (), exc_info)


def test_an_aborted_cover_is_not_logged_as_a_traceback_outside_debug():
    assert not UnwantedWaitressMessageFilter(debug=False).filter(_serving_error(CoverStreamAborted()))
    # Any other failure while serving a cover is still a traceback worth reading.
    assert UnwantedWaitressMessageFilter(debug=False).filter(_serving_error(ValueError("broken")))
    assert UnwantedWaitressMessageFilter(debug=True).filter(_serving_error(CoverStreamAborted()))


def test_the_waitress_filter_follows_the_debug_it_was_configured_with(monkeypatch):
    """--debug turns debug logging on without general.debug, and the filter has to
    agree with the levels configure_logging set rather than read the setting."""
    monkeypatch.setattr(settings.general, "debug", False)
    below_error = logging.LogRecord("waitress", logging.INFO, "", 0, "", (), None)

    assert UnwantedWaitressMessageFilter(debug=True).filter(below_error)
    assert not UnwantedWaitressMessageFilter(debug=False).filter(below_error)


def test_apikey_redaction_covers_apikey_and_bare_key():
    from app.logger import FileHandlerFormatter

    fmt = FileHandlerFormatter()
    assert fmt.formatApikey("GET /api?apikey=SECRET123 done") == "GET /api?apikey=(removed) done"
    # 2Captcha-compatible vendors poll res.php with ?key=<account key>;
    # urllib3 logs that URL at DEBUG, so the bare form must redact too.
    assert fmt.formatApikey(
        'GET /res.php?key=SECRETKEY9&action=get HTTP/1.1'
    ) == 'GET /res.php?key=(removed)&action=get HTTP/1.1'
    assert fmt.formatApikey("googlekey=PUBLICSITEKEY rest") == "googlekey=(removed) rest"


def test_tmdb_v3_key_is_redacted_in_a_urllib3_request_line():
    """Discover authenticates on the query string, so the log line carries the key.

    A TMDB v3 key travels as ?api_key=<32 hex> rather than in a header, and
    urllib3 logs the request target at DEBUG. The underscore is the part worth
    pinning: the pattern matches the trailing "key", so the redaction has to
    survive the "api_" prefix rather than skip the parameter entirely.
    """
    from app.logger import FileHandlerFormatter

    fmt = FileHandlerFormatter()
    line = ('https://api.themoviedb.org:443 "GET '
            '/3/search/movie?api_key=431a8708161bcd1f1fbe7536137e61ed&language=en-US&query=dune '
            'HTTP/1.1" 200 None')
    redacted = fmt.formatApikey(line)
    assert "431a8708161bcd1f1fbe7536137e61ed" not in redacted
    assert "api_key=(removed)" in redacted
    # The rest of the target survives, which is the reason to log it at all.
    assert "language=en-US&query=dune" in redacted
    # The same key percent-encoded, which is how it appears once a URL is
    # re-quoted before logging.
    assert fmt.formatApikey(
        "GET /3/movie/42?api_key%3D431a8708161bcd1f1fbe7536137e61ed"
    ) == "GET /3/movie/42?api_key=(removed)"


GEMINI_SHAPED_KEY = (
    "AIzaSyA-"
    "fake_key"
    "-0123456"
    "789abcde"
    "fghijkl"
)
OPENROUTER_SHAPED_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
DOTTED_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXZhbHVl"


def _eight_character_windows(value):
    return [value[start:start + 8] for start in range(len(value) - 7)]


@pytest.mark.parametrize("name", ["key", "apikey", "api_key", "apiKey", "token", "api_token", "access_token",
                                  "X-Plex-Token"])
@pytest.mark.parametrize("value", [GEMINI_SHAPED_KEY, OPENROUTER_SHAPED_KEY, DOTTED_TOKEN, "abc+def/ghij==",
                                   "123456:AAH-secret_tail", "user@secret!tail$x*y,z;w(1)#2[3]", "sécret-tail"])
def test_redaction_removes_the_whole_value_up_to_the_next_delimiter(name, value):
    from app.logger import FileHandlerFormatter

    fmt = FileHandlerFormatter()
    line = f"GET https://host.example/v1/models?{name}={value}&alt=json HTTP/1.1 '{name}%3D{value}'|done"
    redacted = fmt.formatApikey(line)
    assert redacted == (f"GET https://host.example/v1/models?{name}=(removed)&alt=json HTTP/1.1 "
                        f"'{name}=(removed)'|done")
    assert not [window for window in _eight_character_windows(value) if window in redacted]


def test_redaction_reaches_a_key_inside_a_logged_traceback():
    from app.logger import FileHandlerFormatter

    try:
        raise RuntimeError("400 Client Error: Bad Request for url: "
                           f"https://host.example/v1beta/models/m:generateContent?key={GEMINI_SHAPED_KEY}")
    except RuntimeError:
        record = logging.LogRecord("root", logging.ERROR, "", 0, "Exception raised while running function",
                                   (), sys.exc_info())
    formatted = FileHandlerFormatter('%(levelname)s|%(message)s|').format(record)
    assert "generateContent?key=(removed)" in formatted
    assert not [window for window in _eight_character_windows(GEMINI_SHAPED_KEY) if window in formatted]


# ---------------------------------------------------------------------------
# Graded per-logger levels
# ---------------------------------------------------------------------------

# The lines a filed defect was diagnosed from, with the logger and the level each
# is emitted at in the tree today. The rule is that a line is load-bearing if it
# was cited in the diagnosis of a filed defect, so this list is evidence rather
# than taste: every entry has to be emitted at a level a normal install accepts.
# One that stops being accepted is a level that is wrong, not a line to accept
# losing. Three of these four needed general.debug to appear at all before the
# levels below were graded, which is the state a bug report is read in.
LOAD_BEARING_LINES = [
    ("", logging.ERROR,
     "bazarr/subtitles/manual.py: downloaded subtitle is not valid for this file"),
    ("subliminal_patch", logging.WARNING,
     "subliminal_patch/core.py: provider is discarded"),
    ("subliminal_patch", logging.INFO,
     "subliminal_patch/core.py: terminating provider"),
    ("", logging.INFO,
     "bazarr/app/get_providers.py: provider not used until a later time"),
]


# The loggers configure_logging hangs a filter on, which a test must leave as it
# found them.
FILTERED_LOGGERS = ("waitress", "engineio.server")


@contextmanager
def _configured(monkeypatch, tmp_path, debug):
    """Run the real configure_logging, then put the process back as it was.

    configure_logging replaces the root handlers and the root level, and this file
    shares one pytest process with the rest of the suite, so a test that leaves it
    applied would change the level every later test logs at.
    """
    # On the module configure_logging reads, which a dotted string need not reach.
    monkeypatch.setattr(logger_module, "get_log_file_path", lambda: str(tmp_path / "bazarr.log"))
    # configure_logging replaces the module's file handler too; put it back after.
    monkeypatch.setattr(logger_module, "fh", logger_module.fh)
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_graded = {name: logging.getLogger(name).level for name in LOGGER_LEVELS}
    saved_filters = {name: list(logging.getLogger(name).filters) for name in FILTERED_LOGGERS}
    configure_logging(debug)
    installed = logger_module.fh
    try:
        yield
    finally:
        installed.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_graded.items():
            logging.getLogger(name).setLevel(level)
        for name, filters in saved_filters.items():
            logging.getLogger(name).filters = filters


def _records_from(logger_name):
    """Collect what one logger actually emits, level gate included.

    A pytest caplog handler sits on the root logger and sees a record that the
    emitting logger's own level already let through, so it cannot tell a line that
    was muted from one that was never logged. A handler on the logger under test
    sits behind that gate.
    """
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger(logger_name).addHandler(handler)
    return records, handler


def test_the_level_table_grades_every_configured_logger():
    """One table, two columns, every logger in both.

    The failure this guards is a logger that exists in only one mode. That is how
    SignalRCoreClient, websocket and ga4mp.ga4mp came to inherit DEBUG whenever
    debug was on, and how the whole of a debug-mode log got its volume.
    """
    assert set(LOGGER_LEVELS) == {
        "alembic.runtime.migration", "apprise", "apscheduler", "engineio.server",
        "ffsubsync.aligners", "ffsubsync.ffsubsync", "ffsubsync.speech_transformers",
        "ffsubsync.subtitle_parser", "ga4mp.ga4mp", "git", "SignalRCoreClient",
        "socketio.server", "srt", "subliminal", "subliminal_patch", "subzero", "websocket",
    }
    for name, levels in LOGGER_LEVELS.items():
        assert len(levels) == 2, f"{name} is graded in one mode only"
        assert all(isinstance(level, int) for level in levels), name


@pytest.mark.parametrize("debug", [False, True])
def test_configure_logging_applies_every_level_in_the_table(monkeypatch, tmp_path, debug):
    expected = resolve_logger_levels(debug)

    with _configured(monkeypatch, tmp_path, debug):
        for name, level in expected.items():
            assert logging.getLogger(name).level == level, name


@pytest.mark.parametrize("name,level,emitter", LOAD_BEARING_LINES,
                         ids=[line[2] for line in LOAD_BEARING_LINES])
def test_load_bearing_lines_survive_with_debug_off(monkeypatch, tmp_path, name, level, emitter):
    with _configured(monkeypatch, tmp_path, debug=False):
        assert logging.getLogger(name).isEnabledFor(level), (
            f"{emitter} would be missing from a normal install's log")


def test_third_party_ceiling_holds_with_debug_on(monkeypatch, tmp_path):
    """Debug does not reach the loggers that emit a line per event.

    One socketio line per broadcast and two apscheduler lines per job run are most
    of a debug-mode log, and neither is Bazarr+ output. Nothing diagnosable is
    knowable only from `emitting event "data" to all [/]`, because the payload is
    already reconstructable from the emitting caller's own logging.
    """
    monkeypatch.setattr(settings.log, "verbose_loggers", [])

    with _configured(monkeypatch, tmp_path, debug=True):
        for name, ceiling in LOGGER_LEVEL_CEILINGS.items():
            assert logging.getLogger(name).level >= ceiling, name


def test_verbose_loggers_lifts_the_ceiling_for_the_named_logger_only(monkeypatch):
    monkeypatch.setattr(settings.log, "verbose_loggers", ["socketio.server"])

    levels = resolve_logger_levels(debug=True)

    assert levels["socketio.server"] < logging.WARNING
    for name, ceiling in LOGGER_LEVEL_CEILINGS.items():
        if name != "socketio.server":
            assert levels[name] >= ceiling, name


def test_a_normal_install_ignores_verbose_loggers(monkeypatch):
    """The escape hatch is for debug mode. Naming a logger while debug is off must
    not be a second way to turn one on."""
    monkeypatch.setattr(settings.log, "verbose_loggers", list(LOGGER_LEVEL_CEILINGS))

    levels = resolve_logger_levels(debug=False)

    for name in LOGGER_LEVEL_CEILINGS:
        assert levels[name] == LOGGER_LEVELS[name][0], name


def test_a_discarded_provider_is_reported_at_its_call_site_with_debug_off(monkeypatch, tmp_path):
    """`Provider 'x' is discarded` is the most useful of the lines a filed defect
    was diagnosed from, and it sits in the vendored fork, which used to be silenced
    to CRITICAL in a normal install. Pin the call site rather than only the table,
    so a rebase that re-levels the line fails here.
    """
    from subliminal_patch import core

    with _configured(monkeypatch, tmp_path, debug=False):
        records, handler = _records_from("subliminal_patch")
        try:
            pool = core.SZProviderPool({"stub"}, {})
            pool.discarded_providers = {"stub"}
            assert pool.download_subtitle(SimpleNamespace(provider_name="stub")) is False
        finally:
            logging.getLogger("subliminal_patch").removeHandler(handler)

    assert "Provider 'stub' is discarded" in [record.getMessage() for record in records]


def test_terminating_a_provider_is_reported_at_its_call_site_with_debug_off(monkeypatch, tmp_path):
    """The other half of that pair, and the line a WARNING floor would have cost:
    `Terminating provider whisperai` is emitted at INFO, so the fork's floor has to
    admit INFO rather than WARNING for provider lifecycle to be readable.
    """
    from subliminal_patch import core

    with _configured(monkeypatch, tmp_path, debug=False):
        records, handler = _records_from("subliminal_patch")
        try:
            pool = core.SZProviderPool({"stub"}, {})
            pool.initialized_providers["stub"] = SimpleNamespace(terminate=lambda: None)
            pool.retire_provider("stub")
        finally:
            logging.getLogger("subliminal_patch").removeHandler(handler)

    assert "Terminating provider stub" in [record.getMessage() for record in records]


# ---------------------------------------------------------------------------
# Expected transport noise
# ---------------------------------------------------------------------------

@pytest.fixture
def real_app(monkeypatch):
    """app.app.create_app, as the server calls it, with the Socket.IO state put back.

    create_app initialises the module's one SocketIO object, which the rest of
    the suite shares, so the server it builds must not outlive the test.
    """
    from app import app as app_module

    socketio = app_module.socketio
    for name in ("server", "sockio_mw", "async_mode"):
        monkeypatch.setattr(socketio, name, getattr(socketio, name, None), raising=False)
    monkeypatch.setattr(socketio, "server_options", dict(socketio.server_options))
    return app_module.create_app


def _log_text(tmp_path):
    path = tmp_path / "bazarr.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.parametrize("debug", [False, True], ids=["normal", "debug"])
def test_a_poll_for_a_socket_session_the_server_dropped_is_logged_only_in_debug(monkeypatch, tmp_path, real_app,
                                                                               debug):
    """engineio logs the first poll for an unknown session at ERROR. A browser tab
    that missed its pings on an overloaded host sends exactly that, so a normal
    install's log must not read it as a fault.

    The request goes through the real application rather than a made-up record,
    so a change to engineio's wording fails here. general.debug stays off in the
    debug case: --debug turns debug logging on without it, and the filter has to
    follow what configure_logging was told.
    """
    monkeypatch.setattr(settings.general, "debug", False)
    with _configured(monkeypatch, tmp_path, debug):
        response = real_app().test_client().get("/api/socket.io/?EIO=4&transport=polling&sid=bogus")
        # A real transport fault on the same logger still gets through.
        logging.getLogger("engineio.server").error("post request handler error")
        written = _log_text(tmp_path)

    assert response.status_code == 400
    assert "post request handler error" in written
    if debug:
        assert "Invalid session bogus" in written
    else:
        assert "Invalid session" not in written


def _closed_but_not_yet_removed(eio):
    """A session engineio closed for missing its pings, which stays among its
    sockets until the next pass of its service task."""
    from engineio.socket import Socket

    session = Socket(eio, "closed")
    session.closed = True
    eio.sockets["closed"] = session
    return "closed", "'Session is disconnected' closed"


class _SessionsLosingOne(dict):
    """engineio's sessions as a poll finds them when another thread drops the one
    it asks for between the membership check and the lookup."""

    def __init__(self, sessions, sid):
        super().__init__(sessions)
        self.sid = sid

    def __contains__(self, sid):
        return sid == self.sid or super().__contains__(sid)


def _removed_between_check_and_lookup(eio):
    eio.sockets = _SessionsLosingOne(eio.sockets, "gone")
    return "gone", "'Session not found' gone"


@pytest.mark.parametrize("session", [_closed_but_not_yet_removed, _removed_between_check_and_lookup],
                         ids=["disconnected", "not-found"])
@pytest.mark.parametrize("debug", [False, True], ids=["normal", "debug"])
def test_a_poll_for_a_socket_session_the_server_has_closed_is_logged_only_in_debug(monkeypatch, tmp_path, real_app,
                                                                                  session, debug):
    """The same late poll reaches engineio's other wording when its session is
    closed but still listed, which is how an overloaded host leaves it, or gone
    by the time it is looked up. engineio logs either once at ERROR, under the
    same key as the unknown session, so whichever comes first would take it."""
    from app import app as app_module

    monkeypatch.setattr(settings.general, "debug", False)
    with _configured(monkeypatch, tmp_path, debug):
        client = real_app().test_client()
        sid, message = session(app_module.socketio.server.eio)
        response = client.get(f"/api/socket.io/?EIO=4&transport=polling&sid={sid}")
        written = _log_text(tmp_path)

    assert response.status_code == 400
    if debug:
        assert message in written
    else:
        assert message not in written
        assert "Session" not in written


def test_reconfiguring_logging_does_not_stack_filters(monkeypatch, tmp_path):
    """configure_logging runs again on every debug toggle, and each run used to add
    another waitress filter. The last run's debug has to be the one that holds."""
    with _configured(monkeypatch, tmp_path, debug=True):
        for debug in (False, False, True):
            logger_module.fh.close()
            configure_logging(debug)
        logger_module.fh.close()
        waitress_filters = [f for f in logging.getLogger("waitress").filters
                            if isinstance(f, UnwantedWaitressMessageFilter)]
        session_filters = [f for f in logging.getLogger("engineio.server").filters
                           if isinstance(f, ExpiredSocketSessionFilter)]

    assert len(waitress_filters) == 1
    assert len(session_filters) == 1
    assert waitress_filters[0].debug is True
    assert session_filters[0].debug is True


def test_a_filter_from_an_earlier_import_of_the_logger_module_is_replaced(monkeypatch, tmp_path):
    """The loggers outlive the module that filtered them. A test that imports the app
    under a mocked sys.modules runs configure_logging with its own copy of this
    module, then drops it, and that copy's filter stays on engineio.server. It is
    an instance of a class that is no longer ExpiredSocketSessionFilter, so an
    isinstance check keeps it, and set up without debug it drops the very record
    the debug run is meant to keep."""
    # The same name in the same module, and nothing else in common, which is what
    # a dropped copy of the module leaves behind.
    stale_class = type(ExpiredSocketSessionFilter.__name__, (logging.Filter,), {
        "__module__": ExpiredSocketSessionFilter.__module__,
        "__qualname__": ExpiredSocketSessionFilter.__qualname__,
        "filter": lambda self, record: False,
    })
    session_logger = logging.getLogger("engineio.server")
    monkeypatch.setattr(session_logger, "filters", [*session_logger.filters, stale_class()])

    with _configured(monkeypatch, tmp_path, debug=True):
        installed = [type(f) for f in session_logger.filters
                     if type(f).__qualname__ == ExpiredSocketSessionFilter.__qualname__]

    assert installed == [ExpiredSocketSessionFilter]


# ---------------------------------------------------------------------------
# Rotation by size as well as by day
# ---------------------------------------------------------------------------

# 2026-09-25 00:00:00 UTC. The handlers below run on UTC and a clock the test
# sets, so each test decides the day and when midnight falls.
DAY = 1790294400
HOUR = 3600


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _rotating_handler(tmp_path, clock, max_bytes=200, backup_count=7):
    handler = SizeAndTimeRotatingFileHandler(str(tmp_path / "bazarr.log"), maxBytes=max_bytes,
                                             backupCount=backup_count, delay=True, encoding="utf-8",
                                             utc=True)
    handler.clock = clock
    handler.rolloverAt = handler.computeRollover(clock())
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _write(handler, number):
    """One record of exactly 100 bytes with its newline, so two fill a 200-byte limit."""
    message = f"record {number}".ljust(99, ".")
    handler.handle(logging.LogRecord("rotation-test", logging.INFO, __file__, 0, message, (), None))


def _records(path):
    with open(path, encoding="utf-8") as file:
        return [line.split(".")[0] for line in file.read().splitlines()]


def _rolled(handler):
    return [os.path.basename(path) for _, _, path in handler.rolled_files()]


def test_a_file_past_the_size_limit_rolls_before_midnight(tmp_path):
    handler = _rotating_handler(tmp_path, _Clock(DAY + 10 * HOUR))
    try:
        for number in range(3):
            _write(handler, number)
    finally:
        handler.close()

    assert sorted(os.listdir(tmp_path)) == ["bazarr.log", "bazarr.log.2026-09-25"]
    assert _records(tmp_path / "bazarr.log.2026-09-25") == ["record 0", "record 1"]
    # The live file keeps its name and starts again from the record that rolled it.
    assert _records(tmp_path / "bazarr.log") == ["record 2"]


def test_a_second_size_roll_on_the_same_day_keeps_the_first(tmp_path):
    """The defect a size check alone would have shipped.

    TimedRotatingFileHandler names a rolled file after its day, so a second roll
    that day computes the name of the first and either removes it or stops rolling.
    Both rolls have to survive, and between them and the live file every record has
    to be present exactly once, in order.
    """
    handler = _rotating_handler(tmp_path, _Clock(DAY + 10 * HOUR))
    try:
        for number in range(5):
            _write(handler, number)
    finally:
        handler.close()

    assert _rolled(handler) == ["bazarr.log.2026-09-25", "bazarr.log.2026-09-25.1"]
    assert _records(tmp_path / "bazarr.log.2026-09-25") == ["record 0", "record 1"]
    assert _records(tmp_path / "bazarr.log.2026-09-25.1") == ["record 2", "record 3"]
    assert _records(tmp_path / "bazarr.log") == ["record 4"]


def test_midnight_after_size_rolls_takes_the_next_number_for_that_day(tmp_path):
    clock = _Clock(DAY + 10 * HOUR)
    handler = _rotating_handler(tmp_path, clock)
    try:
        for number in range(5):
            _write(handler, number)
        # The day's last stretch rolls at midnight and must not land on, or push
        # out, the two size rolls that came before it.
        clock.now = DAY + 24 * HOUR + 5
        for number in range(5, 8):
            _write(handler, number)
    finally:
        handler.close()

    assert _rolled(handler) == [
        "bazarr.log.2026-09-25", "bazarr.log.2026-09-25.1", "bazarr.log.2026-09-25.2",
        "bazarr.log.2026-09-26",
    ]
    assert _records(tmp_path / "bazarr.log.2026-09-25.2") == ["record 4"]
    assert _records(tmp_path / "bazarr.log.2026-09-26") == ["record 5", "record 6"]
    # Reading the rolled files in the order pruning uses, then the live file, is
    # reading the log in the order it was written.
    written = [record for _, _, path in handler.rolled_files() for record in _records(path)]
    assert written + _records(tmp_path / "bazarr.log") == [f"record {n}" for n in range(8)]


def test_a_day_that_never_reaches_the_limit_keeps_the_name_it_always_had(tmp_path):
    """Upgrading must not rename anything: a quiet day still rolls to bazarr.log.DAY."""
    clock = _Clock(DAY + 10 * HOUR)
    handler = _rotating_handler(tmp_path, clock, max_bytes=10_000)
    try:
        _write(handler, 0)
        clock.now = DAY + 24 * HOUR + 5
        _write(handler, 1)
    finally:
        handler.close()

    assert sorted(os.listdir(tmp_path)) == ["bazarr.log", "bazarr.log.2026-09-25"]
    assert _records(tmp_path / "bazarr.log.2026-09-25") == ["record 0"]


def test_pruning_keeps_the_newest_rolled_files_by_day_and_number(tmp_path):
    """Newest by the order the names encode, not by modification time or by string.

    `.10` sorts before `.2` as a string, and an older day's file can carry a newer
    mtime after a copy or a restore, so both would keep the wrong files. Names the
    handler could not have written are neither counted nor deleted.
    """
    ours = ["bazarr.log.2026-09-20", "bazarr.log.2026-09-21", "bazarr.log.2026-09-25",
            "bazarr.log.2026-09-25.2", "bazarr.log.2026-09-25.9", "bazarr.log.2026-09-25.10"]
    not_ours = ["bazarr.log.2026-09-01.gz", "bazarr.log.old", "other.log.2026-09-01",
                "bazarr.log.2026-09-02.0"]
    for age, name in enumerate(ours + not_ours):
        path = tmp_path / name
        path.write_text(name + "\n", encoding="utf-8")
        # The oldest name gets the newest mtime.
        os.utime(path, (DAY + age * -HOUR, DAY + age * -HOUR))

    handler = _rotating_handler(tmp_path, _Clock(DAY + 12 * HOUR), backup_count=3)
    try:
        for number in range(3):
            _write(handler, number)
    finally:
        handler.close()

    assert _rolled(handler) == [
        "bazarr.log.2026-09-25.9", "bazarr.log.2026-09-25.10", "bazarr.log.2026-09-25.11",
    ]
    assert _records(tmp_path / "bazarr.log.2026-09-25.11") == ["record 0", "record 1"]
    for name in not_ours:
        assert (tmp_path / name).exists(), name


def test_rolling_an_empty_or_missing_file_spends_no_backup_slot(tmp_path):
    """A roll with nothing to keep must not push the oldest real day out."""
    (tmp_path / "bazarr.log.2026-09-24").write_text("yesterday\n", encoding="utf-8")
    handler = _rotating_handler(tmp_path, _Clock(DAY + 10 * HOUR), backup_count=1)
    try:
        handler.doRollover()
        (tmp_path / "bazarr.log").write_text("", encoding="utf-8")
        handler.doRollover()
    finally:
        handler.close()

    assert _rolled(handler) == ["bazarr.log.2026-09-24"]


def test_empty_log_after_a_size_roll_keeps_both_rolled_files(monkeypatch, tmp_path):
    """The DELETE route calls empty_log, which rolls the live file and truncates it.

    Rolling there is the second roll of the day, so it takes the next number
    rather than replacing the size roll that came first.
    """
    record_logger = logging.getLogger("rotation-test")
    with _configured(monkeypatch, tmp_path, debug=False):
        logger_module.fh.maxBytes = 300
        for number in range(6):
            record_logger.info("record %d", number)
        rolled_before = logger_module.fh.rolled_files()
        empty_log()
        rolled_after = logger_module.fh.rolled_files()
        live = (tmp_path / "bazarr.log").read_text(encoding="utf-8")

    assert len(rolled_before) == 1
    assert len(rolled_after) == 2
    assert rolled_after[0] == rolled_before[0]
    assert rolled_after[1][0] == rolled_before[0][0] and rolled_after[1][1] == 1
    assert "record 5" in open(rolled_after[1][2], encoding="utf-8").read()
    assert "record" not in live
    assert "BAZARR Log file emptied" in live


def test_emptying_the_log_again_with_nothing_new_keeps_the_real_days(monkeypatch, tmp_path):
    """empty_log() writes a note after it truncates, so the file is never empty
    when the next click comes. Rolling that note each time would push one real
    day out per click; a click after real records still keeps them."""
    for day in ("2026-09-20", "2026-09-21", "2026-09-22"):
        (tmp_path / f"bazarr.log.{day}").write_text(f"{day}\n", encoding="utf-8")
    monkeypatch.setattr(logger_module.settings.log, "backup_count", 3)
    record_logger = logging.getLogger("rotation-test")

    with _configured(monkeypatch, tmp_path, debug=False):
        record_logger.info("record before the first empty")
        for _ in range(3):
            empty_log()
        after_clicks = [os.path.basename(path) for _, _, path in logger_module.fh.rolled_files()]
        record_logger.info("record after the clicks")
        empty_log()
        after_records = logger_module.fh.rolled_files()

    # The first click rolled the real record; the next two had nothing to keep.
    assert len(after_clicks) == 3
    assert "bazarr.log.2026-09-21" in after_clicks and "bazarr.log.2026-09-22" in after_clicks
    newest = open(after_records[-1][2], encoding="utf-8").read()
    assert "record after the clicks" in newest


def test_emptying_the_log_keeps_it_when_the_roll_aside_fails(monkeypatch, tmp_path):
    """Emptying rolls the file aside first so its records survive. When that roll
    fails, truncating would delete the only copy, so the file is kept and the
    request fails instead."""
    record_logger = logging.getLogger("rotation-test")
    with _configured(monkeypatch, tmp_path, debug=False):
        record_logger.info("record that must survive")

        def refuse(source, dest):
            raise PermissionError("directory refuses renames")

        monkeypatch.setattr(logger_module.fh, "rotate", refuse)
        with pytest.raises(OSError, match="not emptied"):
            empty_log()
        live = (tmp_path / "bazarr.log").read_text(encoding="utf-8")

    assert "record that must survive" in live
    assert "BAZARR Log file emptied" not in live


@pytest.fixture
def budapest_time():
    """Local time with DST, for the path bazarr runs (the other tests use UTC)."""
    if not os.path.exists("/usr/share/zoneinfo/Europe/Budapest"):
        pytest.skip("no tz database")
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Budapest"
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def test_a_size_roll_before_the_spring_dst_switch_is_named_for_its_own_day(tmp_path, budapest_time):
    # 2026-03-29 01:30 CET, half an hour before clocks go forward.
    clock = _Clock(1774744200)
    handler = SizeAndTimeRotatingFileHandler(str(tmp_path / "bazarr.log"), maxBytes=200, backupCount=7,
                                             delay=True, encoding="utf-8")
    handler.clock = clock
    handler.rolloverAt = handler.computeRollover(clock())
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        for number in range(3):
            _write(handler, number)
    finally:
        handler.close()

    assert _rolled(handler) == ["bazarr.log.2026-03-29"]


def test_a_restart_after_the_autumn_dst_switch_names_the_old_file_for_its_day(tmp_path, budapest_time):
    live = tmp_path / "bazarr.log"
    live.write_text("from a summer day\n", encoding="utf-8")
    # Last written 2026-10-24 12:00 CEST; the restart comes on 2026-11-10 in CET.
    os.utime(live, (1792836000, 1792836000))
    handler = SizeAndTimeRotatingFileHandler(str(live), maxBytes=10_000, backupCount=7, delay=True,
                                             encoding="utf-8")
    handler.clock = _Clock(1794297600)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        _write(handler, 0)
    finally:
        handler.close()

    assert _rolled(handler) == ["bazarr.log.2026-10-24"]


def test_a_restart_rolls_an_old_live_file_under_its_own_day(tmp_path):
    live = tmp_path / "bazarr.log"
    live.write_text("from yesterday\n", encoding="utf-8")
    os.utime(live, (DAY - 12 * HOUR, DAY - 12 * HOUR))
    handler = SizeAndTimeRotatingFileHandler(str(live), maxBytes=10_000, backupCount=7, delay=True,
                                             encoding="utf-8", utc=True)
    handler.clock = _Clock(DAY + 10 * HOUR)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        _write(handler, 0)
    finally:
        handler.close()

    assert _rolled(handler) == ["bazarr.log.2026-09-24"]
    assert _records(live) == ["record 0"]


@pytest.mark.parametrize("stepped_to", [DAY - 24 * HOUR + 10 * HOUR, 0], ids=["a day back", "to 1970"])
def test_a_clock_stepping_back_does_not_give_a_size_roll_an_older_day(tmp_path, stepped_to):
    """A misnamed file sorts oldest, and pruning deletes it first."""
    clock = _Clock(DAY + 10 * HOUR)
    handler = _rotating_handler(tmp_path, clock, backup_count=2)
    try:
        for number in range(3):
            _write(handler, number)
        clock.now = stepped_to
        # Two more size rolls: the second is named from whatever the first left
        # as the midnight deadline.
        for number in range(3, 7):
            _write(handler, number)
    finally:
        handler.close()

    assert _rolled(handler) == ["bazarr.log.2026-09-25.1", "bazarr.log.2026-09-25.2"]
    assert _records(tmp_path / "bazarr.log.2026-09-25.2") == ["record 4", "record 5"]


def test_a_failed_roll_keeps_every_record_says_so_once_and_backs_off(tmp_path, capsys):
    handler = _rotating_handler(tmp_path, _Clock(DAY + 10 * HOUR))
    attempts = []

    def refuse(source, dest):
        attempts.append(dest)
        raise PermissionError("read-only folder")

    handler.rotate = refuse
    try:
        for number in range(10):
            _write(handler, number)
        # Tried once per further 200 bytes, not on every record past the limit.
        assert len(attempts) == 4
        del handler.rotate
        for number in range(10, 12):
            _write(handler, number)
    finally:
        handler.close()

    notices = capsys.readouterr().err.count("could not roll")
    assert notices == 1
    written = [record for _, _, path in handler.rolled_files() for record in _records(path)]
    assert written + _records(tmp_path / "bazarr.log") == [f"record {n}" for n in range(12)]


def test_a_rolled_file_that_cannot_be_deleted_is_reported_once_and_pruning_goes_on(monkeypatch, tmp_path,
                                                                                    capsys):
    """An old file pruning cannot delete used to be skipped in silence, so the
    folder kept one file more than backup_count for good and nobody knew why."""
    for day in ("2026-09-20", "2026-09-21", "2026-09-22"):
        (tmp_path / f"bazarr.log.{day}").write_text(f"{day}\n", encoding="utf-8")
    stuck = str(tmp_path / "bazarr.log.2026-09-20")
    remove = os.remove

    def refuse_the_oldest(path):
        if path == stuck:
            raise PermissionError(13, "Permission denied", path)
        remove(path)

    monkeypatch.setattr(logger_module.os, "remove", refuse_the_oldest)
    handler = _rotating_handler(tmp_path, _Clock(DAY + 10 * HOUR), max_bytes=1000, backup_count=3)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    live = tmp_path / "bazarr.log"
    try:
        _write(handler, 0)
        handler.doRollover()
        _write(handler, 1)
        after_first_prune = live.read_text(encoding="utf-8")
        handler.doRollover()
        _write(handler, 2)
        after_second_prune = live.read_text(encoding="utf-8")
    finally:
        handler.close()

    # Both rolls happened, the stuck file is kept, and the second roll still
    # pruned the next oldest.
    assert _rolled(handler) == [
        "bazarr.log.2026-09-20", "bazarr.log.2026-09-22", "bazarr.log.2026-09-25", "bazarr.log.2026-09-25.1",
    ]
    # Once on stderr, and once at the top of the live file that roll started.
    assert capsys.readouterr().err.count("could not delete") == 1
    first_lines = after_first_prune.splitlines()
    assert first_lines[0].startswith("WARNING ")
    assert "could not delete" in first_lines[0] and stuck in first_lines[0]
    assert first_lines[1].startswith("INFO record 1")
    assert "could not delete" not in after_second_prune
    assert after_second_prune.startswith("INFO record 2")


def test_an_unreadable_log_folder_is_reported_once_and_the_roll_still_happens(tmp_path, capsys):
    handler = _rotating_handler(tmp_path, _Clock(DAY + 10 * HOUR), max_bytes=1000, backup_count=3)

    def unreadable():
        raise PermissionError(13, "Permission denied", str(tmp_path))

    handler.getFilesToDelete = unreadable
    try:
        for number in range(3):
            _write(handler, number)
            handler.doRollover()
    finally:
        handler.close()

    assert sorted(os.listdir(tmp_path)) == [
        "bazarr.log.2026-09-25", "bazarr.log.2026-09-25.1", "bazarr.log.2026-09-25.2",
    ]
    assert capsys.readouterr().err.count("could not list") == 1
    # The notice went into the file the first failed prune started.
    assert "could not list" in (tmp_path / "bazarr.log.2026-09-25.1").read_text(encoding="utf-8")
    assert "could not list" not in (tmp_path / "bazarr.log.2026-09-25.2").read_text(encoding="utf-8")


def test_a_prune_failure_that_clears_and_comes_back_is_reported_again(tmp_path, capsys):
    """Once a prune works, the failure it had is over. If the same trouble returns
    later it is news again, and staying quiet about it would hide it for good."""
    handler = _rotating_handler(tmp_path, _Clock(DAY + 10 * HOUR), max_bytes=1000, backup_count=3)
    listing = handler.getFilesToDelete
    # The first roll cannot list the folder, the second can, the third cannot.
    unreadable_on_roll = iter([True, False, True])

    def sometimes_unreadable():
        if next(unreadable_on_roll):
            raise PermissionError(13, "Permission denied", str(tmp_path))
        return listing()

    handler.getFilesToDelete = sometimes_unreadable
    try:
        for number in range(3):
            _write(handler, number)
            handler.doRollover()
        # The notice waits for the next record to reach the live file.
        _write(handler, 3)
    finally:
        handler.close()

    assert capsys.readouterr().err.count("could not list") == 2
    assert "could not list" in (tmp_path / "bazarr.log.2026-09-25.1").read_text(encoding="utf-8")
    assert "could not list" not in (tmp_path / "bazarr.log.2026-09-25.2").read_text(encoding="utf-8")
    assert "could not list" in (tmp_path / "bazarr.log").read_text(encoding="utf-8")


def test_something_in_the_way_of_a_name_is_stepped_over(tmp_path):
    (tmp_path / "bazarr.log.2026-09-25").mkdir()
    handler = _rotating_handler(tmp_path, _Clock(DAY + 10 * HOUR))
    try:
        for number in range(3):
            _write(handler, number)
    finally:
        handler.close()

    assert _rolled(handler) == ["bazarr.log.2026-09-25.1"]
    assert (tmp_path / "bazarr.log.2026-09-25").is_dir()


def test_configure_logging_sizes_the_file_handler_from_the_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(logger_module.settings.log, "max_file_size_mb", 5)
    monkeypatch.setattr(logger_module.settings.log, "backup_count", 3)

    with _configured(monkeypatch, tmp_path, debug=False):
        handler = logger_module.fh
        assert isinstance(handler, SizeAndTimeRotatingFileHandler)
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 3
        assert handler.baseFilename == str(tmp_path / "bazarr.log")


@pytest.mark.parametrize("key,stored,expected_index,expected", [
    ("max_file_size_mb", 0, 0, 1 * 1024 * 1024),
    ("max_file_size_mb", 5000, 0, 1024 * 1024 * 1024),
    ("max_file_size_mb", "not a number", 0, 32 * 1024 * 1024),
    ("max_file_size_mb", None, 0, 32 * 1024 * 1024),
    ("max_file_size_mb", True, 0, 32 * 1024 * 1024),
    ("backup_count", 0, 1, 1),
    ("backup_count", 1000, 1, 100),
    ("backup_count", "7", 1, 7),
])
def test_an_unusable_rotation_setting_stays_inside_the_bounds(monkeypatch, key, stored, expected_index,
                                                              expected):
    """A value only the validator should have seen can still not remove the ceiling."""
    monkeypatch.setattr(logger_module.settings.log, key, stored)

    assert log_rotation_limits()[expected_index] == expected


def _rotation_validator(name):
    return next(v for v in config.validators if v.names == (name,))


def _validated(tmp_path, name, value):
    """Run one rotation validator over a config holding `value`, in a Dynaconf of its own.

    Not over the live settings: a failed validate() leaves the live object holding
    the refused value in a section monkeypatch can no longer reach to put back.
    """
    from dynaconf import Dynaconf

    section, key = name.split(".")
    stored = tmp_path / "config.yaml"
    stored.write_text(yaml.safe_dump({section: {key: value}}), encoding="utf-8")
    loaded = Dynaconf(settings_files=[str(stored)], core_loaders=["YAML"])
    _rotation_validator(name).validate(loaded)
    return loaded


@pytest.mark.parametrize("name,default,lowest,highest", [
    ("log.max_file_size_mb", 32, 1, 1024),
    ("log.backup_count", 7, 1, 100),
])
def test_the_rotation_validators_hold_their_default_and_bounds(tmp_path, name, default, lowest, highest):
    validator = _rotation_validator(name)
    assert validator.default == default
    assert validator.must_exist is True
    assert validator.operations.get("gte") == lowest
    assert validator.operations.get("lte") == highest
    assert isinstance(settings.get(name), int)

    for accepted in (lowest, default, highest):
        assert _validated(tmp_path, name, accepted).get(name) == accepted
    # True passes is_type_of=int, and a form value of "true" is saved as True.
    for refused in (lowest - 1, highest + 1, "12", 12.5, True):
        with pytest.raises(ValidationError):
            _validated(tmp_path, name, refused)


def test_a_config_written_before_the_rotation_settings_gets_their_defaults(tmp_path):
    from dynaconf import Dynaconf

    old_config = tmp_path / "config.yaml"
    old_config.write_text("log:\n  include_filter: ''\n", encoding="utf-8")
    loaded = Dynaconf(settings_files=[str(old_config)], core_loaders=["YAML"])
    for name in ("log.max_file_size_mb", "log.backup_count"):
        _rotation_validator(name).validate(loaded)

    assert loaded.log.max_file_size_mb == 32
    assert loaded.log.backup_count == 7


def test_applying_the_rotation_settings_updates_the_running_handler(monkeypatch, tmp_path):
    """Without this a smaller limit would wait for the next restart to hold."""
    handler = _rotating_handler(tmp_path, _Clock(DAY), max_bytes=32 * 1024 * 1024, backup_count=7)
    monkeypatch.setattr(logger_module, "fh", handler)
    monkeypatch.setattr(logger_module.settings.log, "max_file_size_mb", 4)
    monkeypatch.setattr(logger_module.settings.log, "backup_count", 2)

    try:
        logger_module.apply_log_rotation_settings()
    finally:
        handler.close()

    assert handler.maxBytes == 4 * 1024 * 1024
    assert handler.backupCount == 2


@pytest.mark.parametrize("key,submitted,applies", [
    ("settings-log-max_file_size_mb", "4", True),
    ("settings-log-backup_count", "2", True),
    ("settings-log-ignore_case", "true", False),
])
def test_saving_a_rotation_setting_reapplies_the_limits(monkeypatch, key, submitted, applies):
    """save_settings imports the logger when it runs, so patch the module it will get."""

    class _Update:
        def values(self, **_kwargs):
            return self

    live_config = importlib.import_module("app.config")
    live_logger = importlib.import_module("app.logger")
    applied = []
    monkeypatch.setattr(live_logger, "apply_log_rotation_settings", lambda: applied.append(key))
    section_key = key.split("-")[-1]
    monkeypatch.setattr(live_config.settings.log, section_key, live_config.settings.log[section_key])
    monkeypatch.setattr(live_config, "write_config", lambda: True)
    monkeypatch.setattr(live_config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(live_config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda _statement: None),
        update=lambda _model: _Update(),
        System=object,
    ))

    live_config.save_settings([(key, [submitted])])

    assert applied == ([key] if applies else [])
