# coding=utf-8
"""Upgrade compatibility for configs that still name a retired provider.

Providers get retired when their upstream site dies (podnapisi, subscenter and
xsubs, and before them hosszupuska, tusubtitulo, argenteamdump...). An install
that had one enabled keeps naming it in ``general.enabled_providers`` (plus the
``provider_priorities`` / ``provider_languages`` /
``subsync.checker.blacklisted_providers`` side tables, and possibly a stale
``[<provider>]`` config section carrying the options and credentials the
provider used to validate) long after the module is gone.

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

Neutering either one has to turn this file red, for every id in
``RETIRED_PROVIDERS``.

The boot is shared (module-scoped fixture) because it costs seconds and every
test here asks the same question of the same boot, but every assertion that can
be per-provider is parametrised, so coverage is real for each id rather than
inferred from one of them passing.

Provider-auth hygiene (no leftover ``get_providers_auth()`` entry for an id with
no provider class) is also covered by
``test_app_get_providers.py::test_get_providers_auth_with_provider_registry``;
it is asserted here too because that is the check that catches a retirement
which deleted the module but left the credential plumbing behind.
"""

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

# The ids this test guards. None of them may resolve to a registered provider
# class. Keep in step with provider_hub.migration.RETIRED_BUILT_IN_PROVIDER_IDS
# for the ids that were still shipping when they were retired: the ones removed
# long before that set existed have no config left to be compatible with.
RETIRED_PROVIDERS = ["hosszupuska", "podnapisi", "subscenter", "xsubs"]

SURVIVING_PROVIDERS = ["opensubtitlescom", "embeddedsubtitles"]

# A leftover at-rest credential from the secret store. It carries the marker
# prefix but is NOT decryptable, which is the point: once the retired provider's
# paths leave USER_VISIBLE_SECRETS nothing tries to decrypt it, so it survives
# verbatim. While the paths were still registered this same value made the boot
# log a decrypt failure and the rewrite replace it.
INERT_AT_REST_VALUE = "enc:v1:not-a-real-cipher-payload"


def _legacy_config_ini():
    enabled = RETIRED_PROVIDERS + SURVIVING_PROVIDERS
    # Priorities are deliberately NOT in enabled_providers order, so a sorted
    # result that merely echoes insertion order fails the get_providers_sorted()
    # assertion.
    priorities = {name: 10 for name in RETIRED_PROVIDERS}
    priorities["opensubtitlescom"] = 50
    priorities["embeddedsubtitles"] = 20
    languages = {name: ["hun"] for name in RETIRED_PROVIDERS}
    languages["opensubtitlescom"] = ["eng"]
    blacklisted = RETIRED_PROVIDERS + ["opensubtitlescom"]

    # Stale sections, shaped like the ones the retired providers really had.
    # podnapisi.verify_ssl is a non-default value so a validator that came back
    # from the dead would show up as a VALIDATOR RESET. xsubs carried a login
    # pair, and its password was a secret_store path, so it gets the at-rest
    # form. No real credential goes in here: the child ships this config back
    # out through stdout, and stdout lands in CI logs on failure.
    sections = [
        "[hosszupuska]\nusername = someuser\npassword = somepass\n",
        "[podnapisi]\nverify_ssl = False\n",
        "[subscenter]\nusername = someuser\npassword = somepass\n",
        "[xsubs]\nusername = someuser\npassword = %s\n" % INERT_AT_REST_VALUE,
    ]

    return (
        "[general]\n"
        "enabled_providers = %r\n"
        "provider_priorities = %r\n"
        "provider_languages = %r\n"
        "port = 6768\n"
        "minimum_score = 77\n"
        "\n"
        "%s"
        "\n"
        "[subsync]\n"
        "checker = %r\n"
        "\n"
        "[sonarr]\n"
        "ip = 192.0.2.9\n"
        "port = 8990\n"
        % (
            enabled,
            priorities,
            languages,
            "\n".join(sections),
            {"blacklisted_providers": blacklisted, "blacklisted_languages": []},
        )
    )


LEGACY_CONFIG_INI = _legacy_config_ini()

CHILD_SCRIPT = '''\
import json
import os
import sys

ROOT = os.environ["BAZARR_REPO_ROOT"]
EXPECTED_CONFIG_DIR = os.environ["EXPECTED_CONFIG_DIR"]
RETIRED = json.loads(os.environ["RETIRED_PROVIDERS"])
SURVIVING = json.loads(os.environ["SURVIVING_PROVIDERS"])

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

from app.config import settings, sync_checker, validators  # noqa: E402
from secret_store.registry import USER_VISIBLE_SECRET_LISTS, USER_VISIBLE_SECRETS  # noqa: E402
from subliminal_patch.extensions import provider_registry  # noqa: E402


class _Subtitle:
    """Minimal duck type. sync_checker only reads provider_name."""

    def __init__(self, provider_name):
        self.provider_name = provider_name


def _retired_section(path):
    return str(path).split(".")[0] in RETIRED


after_startup = list(settings.general.enabled_providers)
side_tables = {
    "provider_priorities": dict(settings.general.provider_priorities or {}),
    "provider_languages": dict(settings.general.provider_languages or {}),
    "blacklisted_providers": list(settings.subsync.checker.blacklisted_providers),
    "sync_checker": dict(
        (name, sync_checker(_Subtitle(name)))
        for name in RETIRED + SURVIVING
    ),
}

from app import get_providers  # noqa: E402

# Hand the runtime filter a dirty list, in memory only, so it is actually asked to
# drop something. Nothing may write the config after this point: the parent reads
# config.yaml back off disk to check what startup persisted.
runtime_input = list(RETIRED) + after_startup
settings.general.enabled_providers = list(runtime_input)

result = {
    "registry_knows_retired": dict(
        (name, name in provider_registry.names()) for name in RETIRED
    ),
    "retired_provider_auth_keys": sorted(
        key for key in (get_providers.get_providers_auth() or {}) if key in RETIRED
    ),
    "retired_config_validator_names": sorted(
        set(
            name
            for validator in validators
            for name in getattr(validator, "names", ())
            if _retired_section(name)
        )
    ),
    "retired_secret_registry_paths": sorted(
        path
        for path in (USER_VISIBLE_SECRETS | USER_VISIBLE_SECRET_LISTS)
        if _retired_section(path)
    ),
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
    # in this file asks the same question of the same boot. One config names every
    # retired provider at once, which is also the shape of the install this change
    # has to survive.
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
        "RETIRED_PROVIDERS": json.dumps(RETIRED_PROVIDERS),
        "SURVIVING_PROVIDERS": json.dumps(SURVIVING_PROVIDERS),
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


@pytest.mark.parametrize("retired", RETIRED_PROVIDERS)
def test_retired_provider_is_not_registered(legacy_install, retired):
    result, _ = legacy_install
    assert result["registry_knows_retired"][retired] is False


@pytest.mark.parametrize("retired", RETIRED_PROVIDERS)
def test_startup_strips_the_retired_provider(legacy_install, retired):
    # Guards the strip in bazarr/init.py.
    result, _ = legacy_install
    assert retired not in result["enabled_providers_after_startup"]


def test_startup_keeps_exactly_the_surviving_providers(legacy_install):
    result, _ = legacy_install
    assert result["enabled_providers_after_startup"] == SURVIVING_PROVIDERS


@pytest.mark.parametrize("retired", RETIRED_PROVIDERS)
def test_runtime_filter_drops_the_retired_provider(legacy_install, retired):
    # Guards the filter in app.get_providers.get_providers(). The input really did
    # name the retired provider, so this cannot pass by having nothing to drop.
    result, _ = legacy_install
    assert retired in result["runtime_input"]
    assert retired not in result["active_providers"]


def test_runtime_filter_keeps_exactly_the_surviving_providers(legacy_install):
    result, _ = legacy_install
    assert result["active_providers"] == SURVIVING_PROVIDERS


@pytest.mark.parametrize("retired", RETIRED_PROVIDERS)
def test_retired_provider_keeps_no_credential_plumbing(legacy_install, retired):
    # A retirement that deleted the module but left the provider's entry in
    # get_providers_auth(), its config validators, or its secret_store paths
    # would boot fine and still be wrong: the auth entry crashes pool
    # construction, and the other two keep a dead provider's settings alive.
    result, _ = legacy_install
    assert retired not in result["retired_provider_auth_keys"]
    assert not [
        name for name in result["retired_config_validator_names"]
        if name.split(".")[0] == retired
    ]
    assert not [
        path for path in result["retired_secret_registry_paths"]
        if path.split(".")[0] == retired
    ]


def test_surviving_providers_keep_their_priority_order(legacy_install):
    # opensubtitlescom is 50 and embeddedsubtitles is 20, so a get_providers_sorted()
    # that returned insertion order (or lost the stale retired priorities) fails here.
    result, _ = legacy_install
    assert result["sorted_providers"] == ["embeddedsubtitles", "opensubtitlescom"]
    assert result["sorted_providers"] != result["active_providers"]


def test_unrelated_settings_survive_the_upgrade(legacy_install):
    result, _ = legacy_install
    assert result["sonarr_ip"] == "192.0.2.9"
    assert result["sonarr_port"] == 8990
    assert result["general_port"] == 6768
    assert result["minimum_score"] == 77


@pytest.mark.parametrize("retired", RETIRED_PROVIDERS)
def test_stale_side_tables_are_tolerated(legacy_install, retired):
    # provider_priorities / provider_languages / subsync.checker.blacklisted_providers
    # are keyed by provider id and never validated against the registry. Leftover
    # entries for a retired provider must survive startup as inert data.
    result, _ = legacy_install

    assert result["provider_priorities"].get(retired) == 10
    assert result["provider_languages"].get(retired) == ["hun"]
    assert retired in result["blacklisted_providers"]
    assert result["sync_checker"][retired] is False


def test_side_tables_keep_working_for_surviving_providers(legacy_install):
    # sync_checker() is the one side table with a live consumer: it refuses to
    # sync subtitles from a blacklisted provider. The stale retired entries
    # change nothing for the providers that are still around.
    result, _ = legacy_install
    assert result["provider_priorities"].get("opensubtitlescom") == 50
    assert result["provider_languages"].get("opensubtitlescom") == ["eng"]
    assert result["sync_checker"]["opensubtitlescom"] is False
    assert result["sync_checker"]["embeddedsubtitles"] is True


@pytest.mark.parametrize("retired", RETIRED_PROVIDERS)
def test_config_is_rewritten_without_the_retired_provider(legacy_install, retired):
    import yaml

    _, tmp_path = legacy_install
    on_disk = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert retired not in on_disk["general"]["enabled_providers"]


def test_config_rewrite_keeps_the_surviving_providers_and_other_sections(legacy_install):
    import yaml

    _, tmp_path = legacy_install
    on_disk = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["general"]["enabled_providers"] == SURVIVING_PROVIDERS
    # Unrelated sections must not be collateral damage of the rewrite.
    assert on_disk["sonarr"]["ip"] == "192.0.2.9"


def test_stale_retired_sections_survive_untouched_on_disk(legacy_install):
    # The rewrite persists settings.as_dict(), so a section for a provider that
    # no longer has validators rides along as inert data. It must not be reset to
    # a default, and the leftover at-rest credential must not be decrypted,
    # re-encrypted or dropped now that its secret_store paths are gone.
    import yaml

    _, tmp_path = legacy_install
    on_disk = yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert on_disk["podnapisi"]["verify_ssl"] is False
    assert on_disk["xsubs"]["username"] == "someuser"
    assert on_disk["xsubs"]["password"] == INERT_AT_REST_VALUE
