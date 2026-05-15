from datetime import datetime
from datetime import timedelta

from utilities.pretty_date import pretty_date


def test_pretty_date_keeps_existing_long_output():
    now = datetime(2026, 5, 15, 12, 0, 0)

    assert pretty_date(now - timedelta(seconds=8), now=now) == "now"
    assert pretty_date(now - timedelta(minutes=1), now=now) == "a minute ago"
    assert pretty_date(now - timedelta(hours=2), now=now) == "2 hours ago"
    assert pretty_date(now - timedelta(days=1), now=now) == "yesterday"
    assert pretty_date(now + timedelta(days=1), now=now) == "tomorrow"


def test_pretty_date_keeps_existing_short_output():
    now = datetime(2026, 5, 15, 12, 0, 0)

    assert pretty_date(now - timedelta(seconds=8), now=now, short=True) == "now"
    assert pretty_date(now - timedelta(minutes=2), now=now, short=True) == "2m ago"
    assert pretty_date(now + timedelta(days=3), now=now, short=True) == "in 3d"


def test_pretty_date_accepts_unix_timestamp():
    now = datetime.fromtimestamp(1_768_563_600)

    assert pretty_date(1_768_560_000, now=now) == "an hour ago"
