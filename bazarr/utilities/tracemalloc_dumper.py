# coding=utf-8
"""Dev-only tracemalloc snapshot dumper. Enable by exporting

    BAZARR_TRACEMALLOC=1

before starting Bazarr, then send `kill -USR1 <pid>` to print a diff
of the top 30 allocators since the previous SIGUSR1 (or since startup
on first signal). On Windows or in a non-main thread, install() is a
no-op since signal.signal() is unsupported there. Zero overhead when
disabled.
"""

import logging
import os
import signal
import sys
import threading
import tracemalloc

logger = logging.getLogger(__name__)

_previous_snapshot = None
_lock = threading.Lock()


def _is_enabled() -> bool:
    return os.getenv("BAZARR_TRACEMALLOC", "").lower() in ("1", "true", "yes", "on")


def _handle_sigusr1(signum, frame):
    global _previous_snapshot
    with _lock:
        snapshot = tracemalloc.take_snapshot()
        if _previous_snapshot is None:
            stats = snapshot.statistics("lineno")[:30]
            logger.warning("TRACEMALLOC initial snapshot, top 30 by line:")
        else:
            stats = snapshot.compare_to(_previous_snapshot, "lineno")[:30]
            logger.warning("TRACEMALLOC diff since previous SIGUSR1, top 30 by line:")
        for stat in stats:
            logger.warning("  %s", stat)
        _previous_snapshot = snapshot


def install() -> bool:
    """Start tracemalloc and register the SIGUSR1 handler. Returns True
    iff installation actually happened. No-op when the env var is
    unset, when running on Windows, or when called from a non-main
    thread."""
    if not _is_enabled():
        return False
    if sys.platform.startswith("win"):
        logger.info("Tracemalloc dumper unsupported on Windows; skipping.")
        return False
    if threading.current_thread() is not threading.main_thread():
        logger.info("Tracemalloc dumper requires main thread; skipping.")
        return False
    if not tracemalloc.is_tracing():
        tracemalloc.start(25)  # 25-frame stack depth
    try:
        signal.signal(signal.SIGUSR1, _handle_sigusr1)
    except (ValueError, OSError) as exc:
        logger.warning("Tracemalloc dumper could not register SIGUSR1: %s", exc)
        return False
    logger.warning(
        "Tracemalloc dumper armed (pid=%d). Send `kill -USR1 %d` to dump a snapshot diff.",
        os.getpid(),
        os.getpid(),
    )
    return True
