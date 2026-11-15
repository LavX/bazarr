#!/usr/bin/env python3
# coding=utf-8
"""Reset the termination signals a launcher ignored, then exec the command given.

A background launch (nohup, a detached terminal, or `( cmd & )` from a
non-interactive shell) starts the local CI mirror with SIGHUP, SIGINT or
SIGQUIT ignored, and every job it runs inherits those dispositions. A signal
ignored on entry to a shell cannot be trapped again, so the installer's
HUP and INT traps never engage and its tests fail however the suite itself was
launched. A shell cannot reset the disposition itself, so run-local.sh
re-execs itself through here, where python can, before it does anything else.
"""

import os
import signal
import sys

RESET_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT)


def reset_signals():
    for number in RESET_SIGNALS:
        signal.signal(number, signal.SIG_DFL)


if __name__ == '__main__':
    reset_signals()
    os.execve(sys.argv[1], sys.argv[1:], os.environ)
