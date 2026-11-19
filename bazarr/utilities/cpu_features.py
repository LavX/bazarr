# coding=utf-8
"""Detect x86-64 CPUs that cannot run the numpy build Bazarr ships.

numpy 2.4 raised its x86-64 baseline to x86-64-v2, so a CPU without SSE4.2 or
POPCNT dies with an illegal instruction the moment numpy is imported. That is a
signal, not an exception, so no try/except can recover from it. The check here
therefore reads /proc/cpuinfo instead of importing anything, and it never
raises: when the answer is unknown the CPU is assumed to be fine.

ffsubsync, autosubsync and json_tricks all import numpy. ALASS is a separate
executable and does not.

The check is Linux only, on purpose. /proc/cpuinfo is the one source that can be
read without running a CPU instruction or importing anything that might fault.
Windows and macOS would need a cpuid probe through ctypes or a helper process,
which is more machinery than this guard is worth, so neither is covered. On those platforms the check reports nothing missing and
the engines are left alone.
"""

import logging
import platform
import threading

CPUINFO_PATH = '/proc/cpuinfo'

# The flags numpy's x86-64-v2 baseline needs that older x86-64 CPUs lack.
REQUIRED_CPU_FLAGS = ('sse4_2', 'popcnt')

# Sync engines that import numpy in process.
NUMPY_SYNC_ENGINES = ('ffsubsync', 'autosubsync')

_FLAG_LABELS = {'sse4_2': 'SSE4.2', 'popcnt': 'POPCNT'}

_X86_64_MACHINES = ('x86_64', 'amd64')

_lock = threading.Lock()
_cached_missing_flags = None
_engines_logged = False


def read_cpu_flags(cpuinfo_path=CPUINFO_PATH):
    """Return the flag set of the first CPU in cpuinfo, or None when unreadable.

    Only the first block is read. Every logical CPU of one x86-64 machine reports
    the same SSE4.2 and POPCNT flags, hybrid performance and efficiency cores
    included, and stopping there keeps this cheap on machines with many cores.
    """
    try:
        with open(cpuinfo_path, 'r', encoding='utf-8', errors='replace') as cpuinfo:
            for line in cpuinfo:
                key, separator, value = line.partition(':')
                if separator and key.strip().lower() == 'flags':
                    return set(value.split())
    except (OSError, ValueError):
        return None
    return None


def missing_numpy_cpu_flags(cpuinfo_path=CPUINFO_PATH, system=None, machine=None):
    """List the required CPU flags this machine lacks.

    Empty means numpy can run, or that the answer is unknown. Only Linux on
    x86-64 is judged: other platforms ship numpy builds with their own baseline.
    """
    try:
        system = system if system is not None else platform.system()
        machine = machine if machine is not None else platform.machine()
        if system != 'Linux' or str(machine).lower() not in _X86_64_MACHINES:
            return []
        flags = read_cpu_flags(cpuinfo_path)
        if flags is None:
            return []
        return [flag for flag in REQUIRED_CPU_FLAGS if flag not in flags]
    except Exception:  # pragma: no cover - the check must never stop startup
        return []


def describe_flags(flags):
    return ' and '.join(_FLAG_LABELS.get(flag, flag.upper()) for flag in flags)


def numpy_cpu_missing_flags():
    """The missing flags for this machine, read once per process."""
    global _cached_missing_flags
    with _lock:
        if _cached_missing_flags is None:
            _cached_missing_flags = missing_numpy_cpu_flags()
        return list(_cached_missing_flags)


def numpy_engines_usable():
    return not numpy_cpu_missing_flags()


def log_cpu_check():
    """Log one warning at startup when numpy based sync engines cannot run."""
    missing = numpy_cpu_missing_flags()
    if missing:
        logging.warning(
            'BAZARR this CPU lacks %s, which numpy needs. The numpy based sync engines (ffsubsync and '
            'autosubsync) cannot run on this CPU and are left out of subtitle synchronization. ALASS does '
            'not use numpy and still works.', describe_flags(missing))


def usable_sync_engines(engines):
    """Drop the engines this CPU cannot run, keeping the order.

    The saved settings are left alone: the engines come back as soon as the app
    runs on a CPU that can host them. The drop is logged once per process.
    """
    global _engines_logged
    missing = numpy_cpu_missing_flags()
    if not missing:
        return list(engines)
    kept = [engine for engine in engines if engine not in NUMPY_SYNC_ENGINES]
    dropped = [engine for engine in engines if engine in NUMPY_SYNC_ENGINES]
    if dropped:
        with _lock:
            first = not _engines_logged
            _engines_logged = True
        if first:
            logging.warning('BAZARR leaving %s out of subtitle synchronization: this CPU lacks %s, which numpy '
                            'needs.', ' and '.join(dropped), describe_flags(missing))
    return kept
