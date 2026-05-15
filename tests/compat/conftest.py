import os
import sys

# Prevent argparse from parsing pytest's own argv, which would cause
# bazarr/app/get_args.py to exit with "unrecognized arguments".
os.environ["NO_CLI"] = "true"
os.environ.setdefault("SZ_USER_AGENT", "test")
os.environ.setdefault("BAZARR_VERSION", "test")

# Add bazarr source trees to sys.path so that `from app.config import ...`
# resolves correctly, matching what tests/bazarr/conftest.py does.
_repo = os.path.join(os.path.dirname(__file__), "../..")
sys.path.insert(0, os.path.join(_repo, "bazarr"))
sys.path.insert(0, os.path.join(_repo, "custom_libs"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _guarantee_compat_secrets():
    """Reset compat_endpoint secrets to known-good values BEFORE each test.

    Several tests in the suite monkeypatch dotted-path secrets to empty
    strings or short values to exercise edge cases (test_bootstrap,
    test_feature_gate_at_boot). pytest's monkeypatch reverts via getattr
    + setattr through the DynaBox wrapper, which does NOT reliably
    restore Dynaconf's layered storage - the shadow attribute persists
    and poisons later tests that assume valid secrets.

    Belt and braces: write through both the DynaBox's __setattr__ AND
    the underlying dict (settings["compat_endpoint"][...]). The dict
    path is what compat_admin._set_compat_secret uses and what
    settings.as_dict() reads from; the DynaBox path is what the test
    monkeypatches touch. Hitting both guarantees the values are in
    sync regardless of which layer any individual caller reads.
    """
    from app.config import settings
    _defaults = {
        "enabled": True,
        "token": "t" * 32,
        "jwt_secret": "j" * 32,
        "file_id_secret": "f" * 32,
        "jwt_ttl_seconds": 86400,
        "file_id_ttl_seconds": 3600,
        "stream_token_ttl_seconds": 300,
    }
    for name, value in _defaults.items():
        settings["compat_endpoint"][name] = value
        try:
            setattr(settings.compat_endpoint, name, value)
        except Exception:
            pass
    yield
