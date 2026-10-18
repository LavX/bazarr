#!/usr/bin/env python3
# coding=utf-8
"""Reset a SIGHUP the launcher ignored, then exec the command given.

A background launch (nohup, a detached terminal) starts the local CI mirror
with SIGHUP ignored, and every job it runs inherits that disposition. A
signal ignored on entry to a shell cannot be trapped again, so the
installer's HUP trap never engages and its tests fail however the suite
itself was launched. A shell cannot reset the disposition itself, so run-local.sh
re-execs itself through here, where python can, before it does anything else.
"""

import os
import signal
import sys

signal.signal(signal.SIGHUP, signal.SIG_DFL)
os.execve(sys.argv[1], sys.argv[1:], os.environ)
