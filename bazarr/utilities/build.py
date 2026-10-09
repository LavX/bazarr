# coding=utf-8
"""Where the image's `BUILD` stamp lives, and how to read it.

A build must identify itself: the source commit and the build date are
stamped into the image at bake time and shown in System Status, so the owner
can tell which tree is running. The Dockerfile writes the stamp beside the
`bazarr` package and refuses to bake without it.

The repository does not track a `BUILD` file. A source checkout reads {} and
shows no build identity, which is correct: a checkout is not a build.
"""

import os


def build_stamp_path():
    """The `BUILD` stamp beside the `bazarr` package, i.e. the repository root.

    `__file__` here is `<root>/bazarr/utilities/build.py`, so the root is three
    directories up. In the shipped image that resolves to `/app/bazarr/BUILD`,
    which is where the Dockerfile puts it.
    """
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'BUILD')


def read_build_stamp(path=None):
    """Parse `BUILD` into a dict, lowercased keys (commit, date). {} when absent.

    Unreadable or undecodable is the same answer as absent: the status page
    reads this on every load, and a damaged stamp must not break the page it
    is only decorating.
    """
    path = path or build_stamp_path()
    if not os.path.isfile(path):
        return {}

    stamp = {}
    try:
        with open(path) as handle:
            lines = handle.readlines()
    except (OSError, UnicodeDecodeError):
        return {}
    for line in lines:
        key, sep, value = line.partition('=')
        if sep:
            stamp[key.lower()] = value.replace('\n', '')
    return stamp
