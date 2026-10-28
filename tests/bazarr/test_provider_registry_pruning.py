# coding=utf-8
"""Startup prunes provider choices only after Provider Hub registration succeeds."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]

CHILD_SCRIPT = '''\
import json
import os
import sys

root = os.environ["BAZARR_REPO_ROOT"]
sys.path.insert(0, os.path.join(root, "bazarr"))
sys.path.insert(0, os.path.join(root, "custom_libs"))

from app.get_args import args

assert os.path.realpath(args.config_dir) == os.path.realpath(os.environ["BAZARR_CONFIG_DIR"])

from provider_hub import registry as hub_registry
from subliminal_patch.extensions import provider_registry


def register_provider():
    if os.environ["REGISTRATION_OK"] == "false":
        raise RuntimeError("registration failed for the startup pruning test")

    class DisabledPlugin:
        pass

    provider_registry.register("prunetesthub", DisabledPlugin)
    return ["prunetesthub"]


hub_registry.register_active_provider_classes = register_provider

import init
from app.config import settings

print("__RESULT__" + json.dumps({
    "enabled": list(settings.general.enabled_providers),
    "disabled": list(settings.general.disabled_providers),
    "registration_ok": init.provider_hub_registration_ok,
}))
'''


@pytest.fixture(scope="module", params=[True, False], ids=["registered", "registration_failed"])
def startup_install(request, tmp_path_factory):
    root = tmp_path_factory.mktemp("provider_pruning")
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "config.ini").write_text(
        "[general]\n"
        "enabled_providers = ['opensubtitlescom', 'unknown_enabled']\n"
        "disabled_providers = ['embeddedsubtitles', 'unknown_disabled', 'prunetesthub']\n",
        encoding="utf-8",
    )
    for document in ("releases.txt", "announcements.txt"):
        (config_dir / document).write_text("[]", encoding="utf-8")

    stub_bin = root / "stub_bin"
    stub_bin.mkdir()
    for name in ("ffmpeg", "ffprobe", "7z"):
        executable = stub_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

    script = root / "boot_provider_pruning.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "BAZARR_REPO_ROOT": str(REPO_ROOT),
        "BAZARR_CONFIG_DIR": str(root),
        "BAZARR_DISABLE_PROVIDER_AUTOINSTALL": "true",
        "BAZARR_VERSION": "test",
        "NO_CLI": "false",
        "POSTGRES_ENABLED": "false",
        "REGISTRATION_OK": str(request.param).lower(),
        "SZ_USER_AGENT": "test",
        "PATH": os.pathsep.join([str(stub_bin), env.get("PATH", "")]),
    })
    proc = subprocess.run(
        [sys.executable, str(script), "-c", str(root), "--no-update"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    marker = "__RESULT__"
    result_line = next((line for line in proc.stdout.splitlines() if line.startswith(marker)), None)
    assert result_line is not None, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    result = json.loads(result_line[len(marker):])
    assert result["registration_ok"] is request.param
    saved = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
    return result, saved["general"], request.param


def test_startup_prunes_enabled_only_after_registration_succeeds(startup_install):
    result, saved, registration_ok = startup_install
    expected = ["opensubtitlescom"] if registration_ok else ["opensubtitlescom", "unknown_enabled"]

    assert result["enabled"] == expected
    assert saved["enabled_providers"] == expected


def test_startup_prunes_disabled_only_after_registration_succeeds(startup_install):
    result, saved, registration_ok = startup_install
    expected = (
        ["embeddedsubtitles", "prunetesthub"]
        if registration_ok
        else ["embeddedsubtitles", "unknown_disabled", "prunetesthub"]
    )

    assert result["disabled"] == expected
    assert saved["disabled_providers"] == expected
