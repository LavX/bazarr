# coding=utf-8
"""Upgrade compatibility for configs that still name a retired provider.

Providers get retired when their upstream site dies (hosszupuska, and before it
subdivx, tusubtitulo, argenteamdump...). An install that had the provider
enabled keeps naming it in ``general.enabled_providers`` (plus the
``provider_priorities`` / ``provider_languages`` / ``blacklisted_providers``
side tables, and possibly a stale ``[<provider>]`` config section) long after
the module is gone.

That upgrade must be a non-event: Bazarr boots, silently drops the unknown id,
and keeps every other provider and every unrelated setting. This test boots the
real config stack in a child process against a legacy ``config.ini`` to prove
it, rather than asserting it from reading the code.
"""

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

# The id this test guards. It must NOT resolve to a registered provider class.
RETIRED_PROVIDER = "hosszupuska"

LEGACY_CONFIG_INI = """\
[general]
enabled_providers = ['{retired}', 'opensubtitlescom', 'embeddedsubtitles']
provider_priorities = {{'{retired}': 10, 'opensubtitlescom': 20}}
provider_languages = {{'{retired}': ['hun']}}
blacklisted_providers = ['{retired}']
port = 6768
minimum_score = 77

[{retired}]
username = someuser
password = somepass

[sonarr]
ip = 10.0.0.9
apikey = deadbeefdeadbeef
port = 8990
""".format(retired=RETIRED_PROVIDER)

# Runs inside the child: boots app.config against the legacy config dir, applies
# the same enabled_providers strip init.py runs on every startup, persists it,
# then exercises the real runtime consumers in app.get_providers.
CHILD_SCRIPT = '''\
import json
import os
import sys

ROOT = os.environ["BAZARR_REPO_ROOT"]
sys.path.insert(0, os.path.join(ROOT, "bazarr"))
sys.path.insert(0, os.path.join(ROOT, "custom_libs"))

from app.config import settings, write_config  # noqa: E402
from subliminal_patch.extensions import provider_registry  # noqa: E402

existing_providers = provider_registry.names()
# Verbatim mirror of the startup strip in bazarr/init.py.
settings.general.enabled_providers = [
    x for x in settings.general.enabled_providers if x in existing_providers
]
write_config()

from app import get_providers  # noqa: E402

result = {
    "registry_knows_retired": os.environ["RETIRED_PROVIDER"] in existing_providers,
    "enabled_providers": list(settings.general.enabled_providers),
    "active_providers": get_providers.get_providers() or [],
    "sorted_providers": get_providers.get_providers_sorted() or [],
    "auth_providers": sorted(get_providers.get_providers_auth()),
    "sonarr_ip": settings.sonarr.ip,
    "sonarr_apikey": settings.sonarr.apikey,
    "sonarr_port": settings.sonarr.port,
    "general_port": settings.general.port,
    "minimum_score": settings.general.minimum_score,
    "provider_priorities": dict(settings.general.provider_priorities or {}),
}
sys.stdout.write("__RESULT__" + json.dumps(result) + "\\n")
'''


@pytest.fixture
def legacy_install(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.ini").write_text(LEGACY_CONFIG_INI, encoding="utf-8")

    script = tmp_path / "boot_legacy_config.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "BAZARR_REPO_ROOT": REPO_ROOT,
        "RETIRED_PROVIDER": RETIRED_PROVIDER,
        "SZ_USER_AGENT": "test",
        "BAZARR_VERSION": "test",
        "NO_CLI": "false",
    })

    proc = subprocess.run(
        [sys.executable, str(script), "-c", str(tmp_path)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        "booting the config stack on a legacy install crashed\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    marker = "__RESULT__"
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith(marker)), None
    )
    assert line is not None, f"child produced no result\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return json.loads(line[len(marker):]), tmp_path


def test_retired_provider_is_not_registered(legacy_install):
    result, _ = legacy_install
    assert result["registry_knows_retired"] is False


def test_retired_provider_is_dropped_from_enabled_providers(legacy_install):
    result, _ = legacy_install
    assert RETIRED_PROVIDER not in result["enabled_providers"]
    assert RETIRED_PROVIDER not in result["active_providers"]
    assert RETIRED_PROVIDER not in result["sorted_providers"]
    assert RETIRED_PROVIDER not in result["auth_providers"]


def test_other_providers_survive_the_upgrade(legacy_install):
    result, _ = legacy_install
    assert result["enabled_providers"] == ["opensubtitlescom", "embeddedsubtitles"]
    assert result["active_providers"] == ["opensubtitlescom", "embeddedsubtitles"]
    assert result["sorted_providers"] == ["opensubtitlescom", "embeddedsubtitles"]


def test_unrelated_settings_survive_the_upgrade(legacy_install):
    result, _ = legacy_install
    assert result["sonarr_ip"] == "10.0.0.9"
    assert result["sonarr_apikey"] == "deadbeefdeadbeef"
    assert result["sonarr_port"] == 8990
    assert result["general_port"] == 6768
    assert result["minimum_score"] == 77


def test_stale_side_tables_are_tolerated(legacy_install):
    # provider_priorities / provider_languages / blacklisted_providers are keyed
    # by provider id and are never validated against the registry. A leftover
    # entry for the retired provider must be inert, not fatal.
    result, _ = legacy_install
    assert result["provider_priorities"].get("opensubtitlescom") == 20


def test_config_is_rewritten_without_the_retired_provider(legacy_install):
    import yaml

    _, tmp_path = legacy_install
    on_disk = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert RETIRED_PROVIDER not in on_disk["general"]["enabled_providers"]
    assert on_disk["general"]["enabled_providers"] == [
        "opensubtitlescom",
        "embeddedsubtitles",
    ]
    # Unrelated sections must not be collateral damage of the rewrite.
    assert on_disk["sonarr"]["ip"] == "10.0.0.9"
