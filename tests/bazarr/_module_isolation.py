# coding=utf-8
"""Undo a module's import side effects without breaking C extensions.

Several test modules install mocks in sys.modules, import the module under
test so it binds against them, and then put sys.modules back. Restoring by
deleting everything that appeared is too broad: a third-party package first
imported during that window gets deleted too, and an extension module cannot
be imported a second time in one process. CPython raises

    ImportError: cannot load module more than once per process

which surfaces much later, in whichever unrelated test imports numpy next, and
only when the suite runs in an order that reaches this module first.

Only the mocks and this repo's own modules need dropping: nothing else was
replaced, so nothing else has a stale binding to undo.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _is_replaceable(module):
    """True for a mock or one of our own modules, false for anything installed."""
    origin = getattr(module, "__file__", None)
    if not origin:
        # A mock, or a namespace package we introduced. Either way it is ours.
        return True

    return os.path.abspath(origin).startswith(_REPO_ROOT + os.sep)


def restore(before):
    """Return sys.modules to ``before``, leaving installed packages alone."""
    for name in list(sys.modules):
        if name in before:
            continue
        if _is_replaceable(sys.modules[name]):
            del sys.modules[name]

    for name, module in before.items():
        sys.modules[name] = module
