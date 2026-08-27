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
    """Parse `package_info` into a dict, lowercased keys. {} when absent."""
    path = path or package_info_path()
    if not os.path.isfile(path):
        return {}

    info = {}
    with open(path) as handle:
        lines = []
        for line in handle.readlines():
            lines += line.split(r'\n')
        for line in lines:
            key, sep, value = line.partition('=')
            if sep:
                info[key.lower()] = value.replace('\n', '')
    return info
