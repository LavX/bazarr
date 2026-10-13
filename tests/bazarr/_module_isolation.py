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


def _resync_parent_attribute(name):
    """Re-point a repo parent package's attribute at what sys.modules holds.

    Importing a submodule binds it as an attribute on its parent package, and
    that binding survives a sys.modules restore: the parent keeps pointing at
    a deleted module while sys.modules holds another one, or nothing. A later
    ``from parent import child`` then binds the parent's stale copy while a
    ``from parent.child import X`` imports a fresh module, so the two disagree
    and a class patched through one is not the class the other constructs
    from.
    """
    parent_name, _, child = name.rpartition(".")
    if not parent_name:
        return
    parent = sys.modules.get(parent_name)
    if parent is None or not _is_replaceable(parent):
        return
    module = sys.modules.get(name)
    if module is not None:
        setattr(parent, child, module)
        return
    stale = getattr(parent, child, None)
    if stale is not None and _is_replaceable(stale):
        try:
            delattr(parent, child)
        except AttributeError:
            pass


def restore(before):
    """Return sys.modules to ``before``, leaving installed packages alone."""
    touched = set(sys.modules) | set(before)
    for name in list(sys.modules):
        if name in before:
            continue
        if _is_replaceable(sys.modules[name]):
            del sys.modules[name]

    for name, module in before.items():
        sys.modules[name] = module

    # Package attributes must agree with sys.modules again, or a later
    # from-import binds a copy the restore just deleted.
    for name in touched:
        _resync_parent_attribute(name)
