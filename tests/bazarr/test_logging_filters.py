import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.config import settings
from app.logger import (LOGGER_LEVELS, LOGGER_LEVEL_CEILINGS, UnwantedWaitressMessageFilter,
                        configure_logging, resolve_logger_levels)

def test_true_for_bazarr():
  record = logging.LogRecord("", logging.INFO, "", 0, "a message from BAZARR for logging", (), None)
  assert UnwantedWaitressMessageFilter().filter(record)

def test_false_below_error():
  record = logging.LogRecord("", logging.INFO, "", 0, "", (), None)
  assert not UnwantedWaitressMessageFilter().filter(record)

def test_true_above_error():
  record = logging.LogRecord("", logging.CRITICAL, "", 0, "", (), None)
  assert UnwantedWaitressMessageFilter().filter(record)


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


@contextmanager
def _configured(monkeypatch, tmp_path, debug):
    """Run the real configure_logging, then put the process back as it was.

    configure_logging replaces the root handlers and the root level, and this file
    shares one pytest process with the rest of the suite, so a test that leaves it
    applied would change the level every later test logs at.
    """
    monkeypatch.setattr("app.logger.get_log_file_path", lambda: str(tmp_path / "bazarr.log"))
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_graded = {name: logging.getLogger(name).level for name in LOGGER_LEVELS}
    configure_logging(debug)
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_graded.items():
            logging.getLogger(name).setLevel(level)


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
