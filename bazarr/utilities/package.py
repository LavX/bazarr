# coding=utf-8
"""Where the packager's `package_info` lives, and how to read it.

Kept out of `init` and free of heavy imports so the location can be asserted
without booting the application. It is worth asserting: the block that consumes
this file is wrapped in a bare try/except, so looking in the wrong place is
indistinguishable from the file being absent, and everything the packager
configured is skipped without a word in the log.
"""

import os


def package_info_path():
    """The `package_info` beside the `bazarr` package, i.e. the repository root.

    `__file__` here is `<root>/bazarr/utilities/package.py`, so the root is three
    directories up. In the shipped image that resolves to
    `/app/bazarr/package_info`, which is where the Dockerfile puts it.
    """
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'package_info')


def read_package_info(path=None):
    """Parse `package_info` into a dict, lowercased keys. {} when absent.

    Unreadable or undecodable is the same answer as absent: this runs at
    `app.get_args` import time, before any broad guard, and a mispackaged
    file must not abort startup.
    """
    path = path or package_info_path()
    if not os.path.isfile(path):
        return {}

    info = {}
    try:
        with open(path) as handle:
            lines = []
            for line in handle.readlines():
                lines += line.split(r'\n')
    except (OSError, UnicodeDecodeError):
        return {}
    for line in lines:
        key, sep, value = line.partition('=')
        if sep:
            info[key.lower()] = value.replace('\n', '')
    return info


def updates_are_externally_managed(path=None, info=None):
    """True when the packager set `updatemethod=External`.

    That is the documented way a package says it owns updating: a container
    image, a distribution package, an unRAID template. Honouring it is the
    caller's job, and for a long time nobody did, so the setting only ever set
    an environment variable nothing reads.

    A git checkout is none of those. The repository tracks a package_info
    written for the shipped image, so beside a `.git` the marker describes the
    image the checkout could become, not the install it is; source installs
    keep the in-app updater.
    """
    if info is None:
        resolved = path or package_info_path()
        if os.path.isdir(os.path.join(os.path.dirname(os.path.abspath(resolved)), '.git')):
            return False
        info = read_package_info(resolved)
    return info.get('updatemethod', '') == 'External'
