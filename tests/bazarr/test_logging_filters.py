import logging

from app.logger import UnwantedWaitressMessageFilter

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
