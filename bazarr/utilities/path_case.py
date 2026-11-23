# coding=utf-8
"""Locale-independent case keys for Windows path comparisons."""

from functools import cache
from sys import maxunicode


@cache
def windows_path_case_mapping():
    """Build the default Unicode case-fold mapping for SQL's sparse path comparison."""
    mapping = {}
    for value in range(maxunicode + 1):
        original = chr(value)
        folded = original.casefold()
        if folded != original:
            mapping[value] = folded
    return mapping


def windows_path_casefold(path):
    """Apply default Unicode case folding to the original spelling of the path."""
    return path.casefold() if path is not None else None
