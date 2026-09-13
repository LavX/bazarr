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
