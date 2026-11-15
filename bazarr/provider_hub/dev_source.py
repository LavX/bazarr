# coding=utf-8
"""The development install path: one added catalog source that may carry plugins of a new kind.

With ``BAZARR_DEV_PLUGIN_SOURCE`` set to the name of one added catalog source, that source may list
plugins of kinds other than the subtitle provider, so a plugin can be installed from a catalog branch
before the official catalog publishes it. Nothing else about the source changes. It is still a community
source: its entries and the plugins installed from it are untrusted, it is never the official source, it
is read as the legacy catalog (never through the signature check), and it never latches a channel or
touches a counter. With the variable unset, the rule that keeps new kinds to the official catalog
refuses it like any other community source.

This is for a side container used to develop plugins. Nothing public documents it.
"""
from __future__ import annotations

import os

from typing import Any

from .state import OFFICIAL_CATALOG_SOURCE_ID

DEV_SOURCE_ENV = "BAZARR_DEV_PLUGIN_SOURCE"


def dev_source_name() -> str | None:
    """The name of the development source, or None when the variable is unset or blank."""
    value = (os.environ.get(DEV_SOURCE_ENV) or "").strip()
    return value or None


def is_dev_source(source_id: Any, source: Any = None) -> bool:
    """Whether the source ``source_id`` (with its stored record, when the caller has it) is the development source.

    The official source never is, whatever it is called.
    """
    name = dev_source_name()
    if name is None or source_id == OFFICIAL_CATALOG_SOURCE_ID or not isinstance(source_id, str):
        return False
    if isinstance(source, dict) and source.get("official") is True:
        return False
    return source_id == name or (isinstance(source, dict) and source.get("name") == name)
