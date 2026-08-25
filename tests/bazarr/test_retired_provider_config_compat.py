# coding=utf-8
"""Upgrade compatibility for configs that still name a retired provider.

Providers get retired when their upstream site dies (hosszupuska, and before it
tusubtitulo, argenteamdump...). An install that had the provider enabled keeps
naming it in ``general.enabled_providers`` (plus the ``provider_priorities`` /
``provider_languages`` / ``subsync.checker.blacklisted_providers`` side tables,
and possibly a stale ``[<provider>]`` config section) long after the module is
gone.

That upgrade must be a non-event: Bazarr boots, silently drops the unknown id,
and keeps every other provider and every unrelated setting.

Two independent production filters make that true, and this test exercises both
of them rather than a copy of either:

* the startup strip in ``bazarr/init.py``, which runs after Provider Hub
  registration and persists the cleaned list. The child imports ``init`` itself,
  so the assertions ride on the real module.
* the runtime filter in ``app.get_providers.get_providers()``, which covers the
  window before the strip persists. The child re-dirties ``enabled_providers``
  in memory after startup so this filter has something left to drop.

Neutering either one has to turn this file red.

Provider-auth hygiene (no leftover ``get_providers_auth()`` entry for an id with
no provider class) is not asserted here: it is already covered by
``test_app_get_providers.py::test_get_providers_auth_with_provider_registry``.
"""

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

# The id this test guards. It must NOT resolve to a registered provider class.
RETIRED_PROVIDER = "hosszupuska"

SURVIVING_PROVIDERS = ["opensubtitlescom", "embeddedsubtitles"]

# Priorities are deliberately NOT in enabled_providers order, so a sorted result
# that merely echoes insertion order fails the get_providers_sorted() assertion.
# No credential-shaped value goes in here: the child ships this config back out
# through stdout, and stdout lands in CI logs on failure.
LEGACY_CONFIG_INI = """\
[general]
enabled_providers = ['{retired}', 'opensubtitlescom', 'embeddedsubtitles']
provider_priorities = {{'{retired}': 10, 'opensubtitlescom': 50, 'embeddedsubtitles': 20}}
provider_languages = {{'{retired}': ['hun'], 'opensubtitlescom': ['eng']}}
port = 6768
minimum_score = 77

[{retired}]
username = someuser
password = somepass

[subsync]
checker = {{'blacklisted_providers': ['{retired}', 'opensubtitlescom'], 'blacklisted_languages': []}}

[sonarr]
ip = 192.0.2.9
port = 8990
""".format(retired=RETIRED_PROVIDER)

CHILD_SCRIPT = '''\
import json
import os
import sys

ROOT = os.environ["BAZARR_REPO_ROOT"]
EXPECTED_CONFIG_DIR = os.environ["EXPECTED_CONFIG_DIR"]
RETIRED = os.environ["RETIRED_PROVIDER"]

sys.path.insert(0, os.path.join(ROOT, "bazarr"))
sys.path.insert(0, os.path.join(ROOT, "custom_libs"))

# Config-dir isolation is load bearing: startup rewrites config.ini in place and
# creates a database, so a child that resolved config_dir to the repo would be
# writing over a real install. get_args only honours argv when NO_CLI is unset or
# false, and the test package sets NO_CLI=true for everyone else. Prove argv won
# before anything writes.
from app.get_args import args  # noqa: E402

assert os.path.realpath(args.config_dir) == os.path.realpath(EXPECTED_CONFIG_DIR), (
    "child resolved config_dir to %r instead of the temporary install %r"
    % (args.config_dir, EXPECTED_CONFIG_DIR)
)

# The real startup path, not a copy of it: registers Provider Hub plugins, strips
# ids with no provider class out of enabled_providers, and persists the result.
import init  # noqa: E402,F401

from app.config import settings, sync_checker  # noqa: E402
from subliminal_patch.extensions import provider_registry  # noqa: E402


class _Subtitle:
    """Minimal duck type. sync_checker only reads provider_name."""

    def __init__(self, provider_name):
        self.provider_name = provider_name


after_startup = list(settings.general.enabled_providers)
side_tables = {
    "provider_priorities": dict(settings.general.provider_priorities or {}),
    "provider_languages": dict(settings.general.provider_languages or {}),
    "blacklisted_providers": list(settings.subsync.checker.blacklisted_providers),
    "sync_checker": dict(
        (name, sync_checker(_Subtitle(name)))
        for name in [RETIRED, "opensubtitlescom", "embeddedsubtitles"]
    ),
}

from app import get_providers  # noqa: E402

# Hand the runtime filter a dirty list, in memory only, so it is actually asked to
# drop something. Nothing may write the config after this point: the parent reads
# config.yaml back off disk to check what startup persisted.
runtime_input = [RETIRED] + after_startup
settings.general.enabled_providers = list(runtime_input)

result = {
    "registry_knows_retired": RETIRED in provider_registry.names(),
    "enabled_providers_after_startup": after_startup,
    "runtime_input": runtime_input,
    "active_providers": get_providers.get_providers() or [],
    "sorted_providers": get_providers.get_providers_sorted() or [],
    "sonarr_ip": settings.sonarr.ip,
    "sonarr_port": settings.sonarr.port,
    "general_port": settings.general.port,
    "minimum_score": settings.general.minimum_score,
}
result.update(side_tables)
sys.stdout.write("__RESULT__" + json.dumps(result) + "\\n")
'''


@pytest.fixture(scope="module")
def legacy_install(tmp_path_factory):
    # Module scoped: booting the whole config stack costs seconds, and every test
    # in this file asks the same question of the same boot.
    tmp_path = tmp_path_factory.mktemp("legacy_install")
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.ini").write_text(LEGACY_CONFIG_INI, encoding="utf-8")
    # Startup fetches releases and announcements when these are missing. Seed them
    # so the child stays offline.
    (config_dir / "releases.txt").write_text("[]", encoding="utf-8")
    (config_dir / "announcements.txt").write_text("[]", encoding="utf-8")

    # get_binary() falls back to an untimed download into <repo>/bin when a binary
    # is not on PATH. Put no-op stubs in front of PATH (POSIX; CI is Linux) so the
    # child can never reach that path.
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    for name in ("ffmpeg", "ffprobe", "7z"):
        stub = stub_bin / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)

    script = tmp_path / "boot_legacy_config.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "BAZARR_REPO_ROOT": REPO_ROOT,
        "EXPECTED_CONFIG_DIR": str(tmp_path),
        "RETIRED_PROVIDER": RETIRED_PROVIDER,
        "SZ_USER_AGENT": "test",
        "BAZARR_VERSION": "test",
        "NO_CLI": "false",
        "PATH": os.pathsep.join([str(stub_bin), env.get("PATH", "")]),
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


def test_startup_strips_the_retired_provider(legacy_install):
    # Guards the strip in bazarr/init.py.
    result, _ = legacy_install
    assert RETIRED_PROVIDER not in result["enabled_providers_after_startup"]
    assert result["enabled_providers_after_startup"] == SURVIVING_PROVIDERS


def test_runtime_filter_drops_the_retired_provider(legacy_install):
    # Guards the filter in app.get_providers.get_providers(). The input really did
    # name the retired provider, so this cannot pass by having nothing to drop.
    result, _ = legacy_install
    assert RETIRED_PROVIDER in result["runtime_input"]
    assert RETIRED_PROVIDER not in result["active_providers"]
    assert result["active_providers"] == SURVIVING_PROVIDERS


def test_surviving_providers_keep_their_priority_order(legacy_install):
    # opensubtitlescom is 50 and embeddedsubtitles is 20, so a get_providers_sorted()
    # that returned insertion order (or lost the stale retired priority) fails here.
    result, _ = legacy_install
    assert result["sorted_providers"] == ["embeddedsubtitles", "opensubtitlescom"]
    assert result["sorted_providers"] != result["active_providers"]


def test_unrelated_settings_survive_the_upgrade(legacy_install):
    result, _ = legacy_install
    assert result["sonarr_ip"] == "192.0.2.9"
    assert result["sonarr_port"] == 8990
    assert result["general_port"] == 6768
    assert result["minimum_score"] == 77


def test_stale_side_tables_are_tolerated(legacy_install):
    # provider_priorities / provider_languages / subsync.checker.blacklisted_providers
    # are keyed by provider id and never validated against the registry. Leftover
    # entries for the retired provider must survive startup as inert data, and the
    # one table with a live consumer must keep working for the providers that remain.
    result, _ = legacy_install

    assert result["provider_priorities"].get(RETIRED_PROVIDER) == 10
    assert result["provider_priorities"].get("opensubtitlescom") == 50
    assert result["provider_languages"].get(RETIRED_PROVIDER) == ["hun"]
    assert RETIRED_PROVIDER in result["blacklisted_providers"]

    # sync_checker() is that live consumer: it refuses to sync subtitles from a
    # blacklisted provider. The stale retired entry changes nothing for the
    # providers that are still around.
    assert result["sync_checker"]["opensubtitlescom"] is False
    assert result["sync_checker"]["embeddedsubtitles"] is True
    assert result["sync_checker"][RETIRED_PROVIDER] is False


def test_config_is_rewritten_without_the_retired_provider(legacy_install):
    import yaml

    _, tmp_path = legacy_install
    on_disk = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert RETIRED_PROVIDER not in on_disk["general"]["enabled_providers"]
    assert on_disk["general"]["enabled_providers"] == SURVIVING_PROVIDERS
    # Unrelated sections must not be collateral damage of the rewrite.
    assert on_disk["sonarr"]["ip"] == "192.0.2.9"
