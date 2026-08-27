# coding=utf-8
"""RAR extraction has to work the way the shipped image is configured.

`bazarr/init.py` calls `init_binaries()` at module scope, which globally
repoints rarfile at whichever extractor it finds. Any test that imports the app
therefore runs against the production configuration, and the archive tests that
pass in a pristine process say nothing about what users get.

That difference is not academic. The image once shipped an archiver that
recognises RAR as a format but carries no RAR decompression codec, with the two
real extractors absent, so every RAR-delivered subtitle failed and the provider
throttled for ten minutes. CI hid it by running the two test trees in separate
processes, where the archive test never met init_binaries().

This test deliberately runs init_binaries() first, so it fails on a machine
configured the way that image was.
"""

import os
import pathlib
import shutil

import pytest

_FIXTURE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "subliminal_patch", "data", "archive_2.rar")


@pytest.mark.skipif(not os.path.isfile(_FIXTURE), reason="RAR fixture is not present")
def test_a_rar_archive_extracts_after_init_binaries_has_configured_the_tools():
    from init import init_binaries

    import rarfile
    from subliminal_patch.providers import utils

    tool = init_binaries()
    assert tool, "no RAR extraction tool was configured at all"

    with rarfile.RarFile(_FIXTURE) as archive:
        assert utils.get_subtitle_from_archive(archive, episode=4) is not None


def test_this_machine_has_a_tool_that_can_actually_decompress_rar():
    """p7zip recognises RAR as a format but cannot decompress it, so finding it
    is not the same as being able to read an archive.

    This covers whatever machine the suite runs on, which is what makes the test
    above meaningful. The image is a separate question, covered below.
    """
    assert shutil.which("unrar") or shutil.which("unar"), (
        "neither unrar nor unar is installed; 7z alone cannot decompress RAR, "
        "which is exactly the state that made every RAR subtitle fail"
    )


def test_the_dockerfile_installs_a_tool_that_can_decompress_rar():
    """The image is what users run, and the test above only sees this machine.

    The Dockerfile is the artefact that decides, so it is read directly: an edit
    dropping unar would otherwise sail through a CI run whose runner installs it
    separately, and ship the exact bug this file exists for.
    """
    installed = _packages_in_stage("production")

    assert "unar" in installed or "unrar" in installed, (
        "the production stage installs neither unar nor unrar; p7zip alone "
        "cannot decompress RAR, so every RAR-delivered subtitle would fail in "
        f"the image. It installs: {sorted(installed)}"
    )


def _dockerfile_lines():
    return (pathlib.Path(__file__).resolve().parents[2] / "Dockerfile").read_text().splitlines()


def _packages_in_stage(stage, lines=None):
    """Package-looking tokens inside one build stage only.

    The image has three stages. Reading the whole file would accept a package
    installed in a builder and dropped from the final image, which is precisely
    the state that ships a broken RAR extractor while the test stays green.
    """
    wanted = []
    in_stage = False
    for line in (lines if lines is not None else _dockerfile_lines()):
        stripped = line.strip()
        if stripped.upper().startswith("FROM "):
            in_stage = stripped.rstrip().lower().endswith(f" as {stage}")
            continue
        if in_stage:
            wanted.append(stripped.rstrip(" \\"))
    return set(wanted)


def test_a_package_only_in_a_builder_stage_does_not_count():
    """The guard above is only worth anything if it reads the stage users run."""
    lines = [
        "FROM python:3.14-slim-trixie AS python-builder",
        "RUN apt-get install -y \\",
        "    unar \\",
        "    build-essential",
        "FROM python:3.14-slim-trixie AS production",
        "RUN apt-get install -y \\",
        "    p7zip-full",
    ]

    assert "unar" in _packages_in_stage("python-builder", lines)
    assert "unar" not in _packages_in_stage("production", lines), (
        "a package installed only in a builder stage was credited to the image "
        "users actually run"
    )
