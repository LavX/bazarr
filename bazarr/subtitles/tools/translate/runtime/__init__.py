# coding=utf-8
"""The v2 translation runtime: engine instances, connections and everything built on them.

One switch, ``translator.runtime_v2``, decides whether any of it runs. It is off
by default, it is not offered by the settings API, and ``runtime_v2_enabled`` is
the only place that reads it. Callers branch on that function at the dispatch
boundary (enqueue, the availability gate, route registration) and nowhere else.
Modules in this package may be imported with the switch off; they must not run.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SWITCH_PATH = "translator.runtime_v2"

_REQUIRED_TABLES = frozenset({"translator_engine_instances", "translator_connections", "translator_secrets"})
# A positive answer is final for the process: tables are never dropped while it runs.
_tables_present = False


def runtime_v2_flag() -> bool:
    """The switch alone, read straight from the settings. Touches no database."""
    try:
        from app.config import settings

        return getattr(settings.translator, "runtime_v2", False) is True
    except Exception:
        return False


def _runtime_tables_exist() -> bool:
    global _tables_present
    if _tables_present:
        return True
    try:
        from sqlalchemy import inspect

        from app.database import engine

        present = _REQUIRED_TABLES <= set(inspect(engine).get_table_names())
    except Exception:
        logger.exception("Could not check for the translation runtime tables")
        return False
    _tables_present = present
    return present


def runtime_v2_enabled() -> bool:
    """True when the switch is on and the runtime tables exist.

    With the switch off this returns False before it looks at the database.
    """
    if not runtime_v2_flag():
        return False
    return _runtime_tables_exist()
