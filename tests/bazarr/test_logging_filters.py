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
