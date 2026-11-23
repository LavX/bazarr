# coding=utf-8
"""Locale-independent case keys for Windows path comparisons."""

from functools import cache
from sys import maxunicode


@cache
def windows_path_lowercase():
    """Use Unicode lowercase per code point, independently of surrounding text."""
    mapping = {}
    for value in range(maxunicode + 1):
        original = chr(value)
        lowered = original.lower()
        if lowered != original:
            mapping[value] = lowered
    return mapping


def windows_path_lower(path):
    return path.translate(windows_path_lowercase()) if path is not None else None
