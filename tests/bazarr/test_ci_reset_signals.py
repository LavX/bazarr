# coding=utf-8
"""scripts/ci/reset_sighup.py hands its command signals that are not ignored.

A background launch leaves SIGHUP, SIGINT and SIGQUIT ignored, and bash cannot
trap a signal that was ignored on entry. The helper undoes that before it execs.
"""

import pathlib
import signal
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "ci" / "reset_sighup.py"
SIGNALS = ("SIGHUP", "SIGINT", "SIGQUIT")


def _ignore_all():
    for name in SIGNALS:
        signal.signal(getattr(signal, name), signal.SIG_IGN)


def test_the_helper_stops_ignoring_signals():
    probe = (
        "import signal; "
        "print(','.join(str(signal.getsignal(getattr(signal, n)) != signal.SIG_IGN) "
        "for n in ('SIGHUP', 'SIGINT', 'SIGQUIT')))"
    )
    result = subprocess.run(
        [sys.executable, str(HELPER), sys.executable, "-c", probe],
        preexec_fn=_ignore_all,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True,True,True"


def test_the_probe_sees_ignored_signals_without_the_helper():
    probe = "import signal; print(signal.getsignal(signal.SIGINT) == signal.SIG_IGN)"
    result = subprocess.run(
        [sys.executable, "-c", probe], preexec_fn=_ignore_all, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "True"
