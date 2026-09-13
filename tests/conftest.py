# -*- coding: utf-8 -*-
import os
import sys
from tempfile import TemporaryDirectory

# Set the default before any child conftest imports application configuration.
# Explicit lane configuration remains authoritative. Keep this directory alive
# until process shutdown so late worker cleanup cannot touch a reused path.
_test_config = None
if not os.environ.get("BAZARR_CONFIG_DIR", "").strip():
    _test_config = TemporaryDirectory(prefix="bazarr-test-")
    os.environ["BAZARR_CONFIG_DIR"] = _test_config.name

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../bazarr/"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../custom_libs/"))

# Prepare the data tree for every suite, including compat, before application
# imports can open the database. Tests do not run the full application boot
# that normally creates these directories. Match get_args path normalization.
_TEST_CONFIG_DIR = os.path.realpath(os.environ["BAZARR_CONFIG_DIR"].strip())
for _subdirectory in ("", "backup", "cache", "config", "db", "log", "restore"):
    os.makedirs(os.path.join(_TEST_CONFIG_DIR, _subdirectory), exist_ok=True)

# Never clear an explicit lane directory: callers may deliberately share its
# database between pytest invocations. Unconfigured processes are isolated.
