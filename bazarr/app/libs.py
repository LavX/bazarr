# coding=utf-8

import os
import sys


def set_libs():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), '../custom_libs/'))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), '../'))
    # The second entry puts the repository root on the path, which is what makes
    # ``import bazarr.<x>`` resolvable alongside the plain ``<x>`` this process
    # already uses. It was added so queued jobs could be imported, back when
    # jobs_queue derived their module names relative to the root. It no longer
    # derives them that way, so nothing here needs it.
    #
    # Left in place because removing a sys.path entry is not obviously safe for
    # out-of-tree callers, but be aware this is what allows one file to be
    # imported under two names, each with its own module-level state. See the
    # comment in subtitles/cache.py for what that cost last time.


set_libs()
