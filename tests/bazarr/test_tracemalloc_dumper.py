import signal
import sys
import tracemalloc

import pytest

from utilities import tracemalloc_dumper


def test_noop_when_env_unset(monkeypatch):
    monkeypatch.delenv("BAZARR_TRACEMALLOC", raising=False)
    previous = signal.getsignal(signal.SIGUSR1) if hasattr(signal, "SIGUSR1") else None

    installed = tracemalloc_dumper.install()
    assert installed is False

    if hasattr(signal, "SIGUSR1"):
        # handler must remain whatever it was before (we did not register).
        assert signal.getsignal(signal.SIGUSR1) == previous


@pytest.mark.skipif(sys.platform.startswith("win"), reason="no SIGUSR1 on Windows")
def test_installs_when_env_set(monkeypatch):
    monkeypatch.setenv("BAZARR_TRACEMALLOC", "1")
    started_here = not tracemalloc.is_tracing()
    previous_handler = signal.getsignal(signal.SIGUSR1)

    try:
        installed = tracemalloc_dumper.install()
        assert installed is True
        assert tracemalloc.is_tracing()
        handler = signal.getsignal(signal.SIGUSR1)
        assert handler is tracemalloc_dumper._handle_sigusr1
    finally:
        signal.signal(signal.SIGUSR1, previous_handler or signal.SIG_DFL)
        if started_here and tracemalloc.is_tracing():
            tracemalloc.stop()
        tracemalloc_dumper._previous_snapshot = None
