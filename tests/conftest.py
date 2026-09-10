# -*- coding: utf-8 -*-
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../bazarr/"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../custom_libs/"))

# A dedicated data directory for every test tree under tests/, set here rather
# than in a per-tree conftest so no tree can be left out. Importing a module
# that pulls in app.database creates the engine and its tables eagerly, and
# with the default location that lands a real bazarr.db inside the checkout,
# built from the ORM metadata and carrying no alembic_version row. The boot
# test that runs after pytest in CI then starts against those tables, walks the
# whole migration chain over a schema no migration produced, and the sports
# migration refuses to adopt what it cannot verify. Bazarr+ fails to start.
#
# This lived in tests/bazarr/conftest.py alone, which left the compat tree
# unprotected: its step runs first in CI, imports app.database, and wrote the
# polluted database that broke the boot step.
#
# One directory for the whole run, not one per process: a pid-keyed path gives
# every pytest invocation an empty tree, and a test that opens the real engine
# then fails on a db/ directory that was never created. The tree is built here
# rather than left to bazarr.init, which only runs for a full application boot.
_TEST_CONFIG_DIR = os.environ.setdefault(
    "BAZARR_CONFIG_DIR", os.path.join(tempfile.gettempdir(), "bazarr-test-data")
)
for _subdirectory in ("", "backup", "cache", "config", "db", "log", "restore"):
    os.makedirs(os.path.join(_TEST_CONFIG_DIR, _subdirectory), exist_ok=True)

# The database is deliberately NOT cleared here. It is tempting to, because the
# directory is shared and a second run on the same machine inherits the rows
# the first one wrote, so the compat contract suite passes once and then fails
# on a duplicate movie id. Clearing it costs more than it saves: CI runs its
# third pytest step one file per process, and Base.metadata.create_all only
# builds the tables whose models that process imported, so an isolated session
# starting from an empty file gets a PARTIAL schema. Those runs depend on the
# fully populated database the earlier step leaves behind;
# test_threading_followup fails with "no such table: table_settings_languages"
# without it. Re-running the compat suite locally needs
# `rm -rf $TMPDIR/bazarr-test-data` first.
