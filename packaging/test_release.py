"""Behavioral checks for the release package command."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

import release


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
COMMAND = PACKAGE_ROOT / "release.py"
WORKFLOW = REPO_ROOT / ".github/workflows/platform-packages.yml"
BASE_DIGEST = "sha256:90a5c184b0af41602ff78ea7286e0c5f2c4c284c9b18f71427d9ddbeb0b6531c"
NEXT_DIGEST = "sha256:" + "a" * 64


def lock_data(version="2.7.0", digest=BASE_DIGEST):
    return {
        "schema": 1,
        "version": version,
        "app": {"image": "ghcr.io/lavx/bazarr", "digest": digest},
        "companions": {
            "translator": {"image": "ghcr.io/lavx/ai-subtitle-translator", "tag": "v2.1.1", "digest": "sha256:" + "e" * 64},
            "flaresolverr": {"image": "ghcr.io/flaresolverr/flaresolverr", "tag": "v3.5.2", "digest": "sha256:" + "f" * 64},
        },
        "runtime_profile": "atlas-sidecar",
        "platform_revisions": {"stack": "1.0.0", "casaos": "1.0.0", "runtipi": 1, "truenas": "1.0.0", "unraid": "1.0.0"},
    }


def git(repository, *args):
    return subprocess.run(["git", "-c", "user.name=Package Test", "-c", "user.email=package-test@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                          cwd=repository, capture_output=True, text=True, check=True).stdout


def package_repository(destination):
    """Commit the package sources, shared license files and an unrelated file into a disposable repository."""
    shutil.copytree(PACKAGE_ROOT, destination / "packaging", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("LICENSE", "NOTICES.md"):
        shutil.copyfile(REPO_ROOT / name, destination / name)
    (destination / "README.md").write_text("Unrelated project file\n")
    git(destination, "init", "-q")
    git(destination, "add", "-A")
    git(destination, "commit", "-q", "-m", "fixture")
    return destination


class ReleaseCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = self.root / "lock.json"
        self.output = self.root / "output"
        self.data = lock_data()

    def run_command(self, *extra, tag="v2.7.0"):
        self.lock.write_text(json.dumps(self.data))
        return subprocess.run(
            [sys.executable, str(COMMAND), "--tag", tag, "--lock", str(self.lock), "--output", str(self.output), *extra],
            capture_output=True, text=True, check=False,
        )

    def test_renders_all_adapters_with_locked_companions(self):
        result = self.run_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ghcr.io/lavx/bazarr:2.7.0@" + BASE_DIGEST,
                      (self.output / "rendered/stack/compose.yaml").read_text())
        self.assertIn("ghcr.io/lavx/ai-subtitle-translator:v2.1.1@", (self.output / "rendered/stack/compose.yaml").read_text())
        self.assertIn("ghcr.io/flaresolverr/flaresolverr:v3.5.2@", (self.output / "rendered/casaos/Apps/BazarrPlusStack/docker-compose.yml").read_text())
        self.assertIn("ghcr.io/lavx/bazarr:2.7.0@" + BASE_DIGEST, (self.output / "rendered/runtipi/bazarr-plus/docker-compose.yml").read_text())
        self.assertEqual(json.loads((self.output / "rendered/runtipi/bazarr-plus/config.json").read_text())["version"], "2.7.0")
        self.assertIn("tag: 2.7.0@" + BASE_DIGEST, (self.output / "rendered/truenas/ix-dev/community/bazarr-plus/ix_values.yaml").read_text())
        self.assertIn("<Repository>ghcr.io/lavx/bazarr:2.7.0@" + BASE_DIGEST + "</Repository>", (self.output / "rendered/unraid/templates/bazarr-plus.xml").read_text())

    def test_rejects_invalid_lock_and_existing_output_without_rewriting_it(self):
        self.data["runtime_profile"] = "cortex-plugin"
        failed = self.run_command()
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.output.exists())
        self.data["runtime_profile"] = "atlas-sidecar"
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("untouched")
        failed = self.run_command()
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(marker.read_text(), "untouched")

    def test_archives_and_provenance_are_deterministic(self):
        first = self.run_command()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_sums = (self.output / "SHA256SUMS").read_bytes()
        first_provenance = (self.output / "provenance.json").read_bytes()
        first_archive = (self.output / "bazarr-plus-v2.7.0-stack-r1.0.0.tar.gz").read_bytes()
        self.output = self.root / "again"
        second = self.run_command()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.output / "SHA256SUMS").read_bytes(), first_sums)
        self.assertEqual((self.output / "provenance.json").read_bytes(), first_provenance)
        self.assertEqual((self.output / "bazarr-plus-v2.7.0-stack-r1.0.0.tar.gz").read_bytes(), first_archive)
        provenance = json.loads(first_provenance)
        self.assertEqual(len(provenance["packaging_git"]["commit"]), 40)

    def test_registry_refuses_malformed_index_and_missing_architecture(self):
        good_headers = {"Docker-Content-Digest": BASE_DIGEST}
        token = ({"token": "local-test-token"}, {})
        malformed = ({"mediaType": "application/vnd.oci.image.manifest.v1+json", "manifests": []}, good_headers)
        with patch.object(release, "request_json", side_effect=[token, malformed]):
            with self.assertRaisesRegex(release.ReleaseError, "image index"):
                release.verify_index("ghcr.io/lavx/bazarr", "2.7.0", BASE_DIGEST)
        missing_arm = ({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
            {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
        ]}, good_headers)
        with patch.object(release, "request_json", side_effect=[token, missing_arm]):
            with self.assertRaisesRegex(release.ReleaseError, "amd64/arm64"):
                release.verify_index("ghcr.io/lavx/bazarr", "2.7.0", BASE_DIGEST)

    def test_new_patch_version_updates_all_runtime_references(self):
        self.data["version"] = "2.7.1"
        self.data["app"]["digest"] = "sha256:" + "a" * 64
        result = self.run_command(tag="v2.7.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        pinned = "ghcr.io/lavx/bazarr:2.7.1@sha256:" + "a" * 64
        for relative in ("stack/compose.yaml", "casaos/Apps/BazarrPlusStack/docker-compose.yml",
                         "casaos/Apps/BazarrPlus/docker-compose.yml", "runtipi/bazarr-plus/docker-compose.yml",
                         "unraid/templates/bazarr-plus.xml"):
            text = (self.output / "rendered" / relative).read_text()
            self.assertIn(pinned, text, relative)
            self.assertNotIn("ghcr.io/lavx/bazarr:2.7.0", text, relative)
        self.assertIn("tag: 2.7.1@sha256:" + "a" * 64,
                      (self.output / "rendered/truenas/ix-dev/community/bazarr-plus/ix_values.yaml").read_text())
        self.assertIn("app_version: 2.7.1", (self.output / "rendered/truenas/ix-dev/community/bazarr-plus/app.yaml").read_text())
        self.assertIn("v2.1.1@sha256:" + "e" * 64, (self.output / "rendered/stack/compose.yaml").read_text())
        self.assertTrue((self.output / "rendered/truenas/ix-dev/community/bazarr-plus/templates/library/base_v2_3_14/validations.py").is_file())

    def test_refuses_unsupported_tags_and_path_traversal(self):
        for tag in ("v2.8.0", "v2.7.1-rc1", "v2.7.00"):
            result = self.run_command(tag=tag)
            self.assertNotEqual(result.returncode, 0, tag)
            self.assertFalse(self.output.exists())
        self.output = self.root / "middle" / ".." / "output"
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "output").exists())

    def test_refuses_symlink_lock_and_output(self):
        self.lock.write_text(json.dumps(self.data))
        target = self.root / "real-lock.json"
        self.lock.rename(target)
        self.lock.symlink_to(target)
        failed = subprocess.run([sys.executable, str(COMMAND), "--tag", "v2.7.0",
                                 "--lock", str(self.lock), "--output", str(self.output)],
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.lock.unlink()
        self.output.symlink_to(self.root / "elsewhere")
        failed = self.run_command()
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((self.root / "elsewhere").exists())

    def test_live_verification_waits_for_digest_readiness(self):
        published = {"tag_name": "v2.7.0", "draft": False, "prerelease": False,
                     "published_at": "2026-09-28T00:00:00Z"}
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(published))
        with patch.object(release.subprocess, "run", return_value=result), \
             patch.object(release, "READINESS_SECONDS", 5), \
             patch.object(release, "RETRY_SECONDS", 0), \
             patch.object(release.time, "monotonic", return_value=0), \
             patch.object(release.time, "sleep"), \
             patch.object(release, "verify_index", side_effect=[
                 release.RegistryNotReady("tag not ready"), None, None, None]) as index:
            release.verify_live(self.data, "v2.7.0")
        self.assertEqual(index.call_count, 4)


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = package_repository(self.root / "repository")
        self.runs = 0

    def reported_dirty(self):
        self.runs += 1
        output = self.root / f"output-{self.runs}"
        result = subprocess.run([sys.executable, str(self.repository / "packaging/release.py"), "--tag", "v2.7.0",
                                 "--lock", str(self.repository / "packaging/releases/2.7.0.json"), "--output", str(output)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((output / "provenance.json").read_text())["packaging_git"]["dirty"]

    def test_clean_sources_report_clean(self):
        self.assertIs(self.reported_dirty(), False)

    def test_any_packaged_input_change_reports_dirty(self):
        for name in ("LICENSE", "NOTICES.md", "packaging/stack/compose.yaml"):
            with self.subTest(name=name):
                path = self.repository / name
                original = path.read_bytes()
                path.write_bytes(original + b"\nlocal edit\n")
                self.assertIs(self.reported_dirty(), True)
                path.write_bytes(original)
                self.assertIs(self.reported_dirty(), False)

    def test_files_outside_the_package_inputs_do_not_mark_it_dirty(self):
        (self.repository / "README.md").write_text("Edited project file\n")
        (self.repository / "scratch.txt").write_text("untracked\n")
        self.assertIs(self.reported_dirty(), False)


class LockSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.locks = self.root / "releases"
        self.locks.mkdir()
        self.output = self.root / "candidates"

    def write_lock(self, name, data):
        (self.locks / name).write_text(json.dumps(data))

    def run_selection(self):
        return subprocess.run([sys.executable, str(COMMAND), "--all-locks", str(self.locks), "--output", str(self.output)],
                              capture_output=True, text=True, check=False)

    def test_every_lock_becomes_its_own_candidate(self):
        self.write_lock("2.7.0.json", lock_data())
        self.write_lock("2.7.1.json", lock_data("2.7.1", NEXT_DIGEST))
        result = self.run_selection()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.output / "v2.7.0"), str(self.output / "v2.7.1")])
        for version, digest in (("2.7.0", BASE_DIGEST), ("2.7.1", NEXT_DIGEST)):
            candidate = self.output / f"v{version}"
            self.assertIn(f"ghcr.io/lavx/bazarr:{version}@{digest}", (candidate / "rendered/stack/compose.yaml").read_text())
            self.assertEqual(json.loads((candidate / "provenance.json").read_text())["tag"], f"v{version}")
            checked = subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=candidate, capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_rejected_locks_fail_the_whole_selection(self):
        broken = lock_data("2.7.1", "sha256:not-a-digest")
        cases = {
            "invalid digest": ("2.7.1.json", broken),
            "version differs from file name": ("2.7.2.json", lock_data("2.7.1", NEXT_DIGEST)),
            "unsupported release line": ("2.8.0.json", lock_data("2.8.0", NEXT_DIGEST)),
            "unrecognized lock name": ("next.json", lock_data("2.7.1", NEXT_DIGEST)),
            "stray file": ("2.7.1.yaml", lock_data("2.7.1", NEXT_DIGEST)),
        }
        for case, (name, data) in cases.items():
            with self.subTest(case):
                for path in self.locks.iterdir():
                    path.unlink()
                self.write_lock("2.7.0.json", lock_data())
                self.write_lock(name, data)
                result = self.run_selection()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("release preparation refused", result.stderr)
                self.assertFalse(self.output.exists())

    def test_empty_lock_directory_fails(self):
        result = self.run_selection()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release preparation refused", result.stderr)
        self.assertFalse(self.output.exists())


class PullRequestWorkflowTests(unittest.TestCase):
    """Run the workflow's pull request steps against a disposable repository and a recording Docker."""

    STEPS = ("Prepare offline PR candidate", "Validate generated package formats",
             "Render TrueNAS templates with pinned platform tooling")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = package_repository(self.root / "repository")
        (self.repository / "packaging/releases/2.7.1.json").write_text(json.dumps(lock_data("2.7.1", NEXT_DIGEST)))
        self.runner_temp = self.root / "runner"
        self.runner_temp.mkdir()
        tools = self.root / "bin"
        tools.mkdir()
        self.docker_log = self.root / "docker.log"
        docker = tools / "docker"
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\n')
        docker.chmod(0o755)
        self.path = f"{tools}{os.pathsep}{os.environ['PATH']}"
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["prepare"]["steps"]
        self.steps = {step["name"]: step for step in steps if "name" in step}

    def run_step(self, name):
        step = self.steps[name]
        self.assertIn(step.get("if", "github.event_name == 'pull_request'"), {"github.event_name == 'pull_request'"})
        env = {**os.environ, **{key: str(value) for key, value in step.get("env", {}).items()},
               "RUNNER_TEMP": str(self.runner_temp), "PATH": self.path, "DOCKER_LOG": str(self.docker_log)}
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                              cwd=self.repository, env=env, capture_output=True, text=True, check=False)

    def test_every_checked_in_lock_is_rendered_and_validated(self):
        for name in self.STEPS:
            result = self.run_step(name)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")
        docker = self.docker_log.read_text().splitlines()
        rendered_versions = []
        for version in ("2.7.0", "2.7.1"):
            candidate = self.runner_temp / f"platform-packages/v{version}"
            for compose in ("stack/compose.yaml", "casaos/Apps/BazarrPlus/docker-compose.yml",
                            "casaos/Apps/BazarrPlusStack/docker-compose.yml", "runtipi/bazarr-plus/docker-compose.yml"):
                self.assertIn(f"compose -f {candidate}/rendered/{compose} config --quiet", docker)
        for line in docker:
            if "apps_render_app" in line:
                workspace = line.split(" -v ", 1)[1].split(":/workspace", 1)[0]
                app = yaml.safe_load(Path(workspace, "ix-dev/community/bazarr-plus/app.yaml").read_text())
                rendered_versions.append(str(app["app_version"]))
        self.assertEqual(sorted(rendered_versions), ["2.7.0", "2.7.0", "2.7.1", "2.7.1"])

    def test_tampered_candidate_fails_checksum_validation(self):
        self.assertEqual(self.run_step(self.STEPS[0]).returncode, 0)
        archive = self.runner_temp / "platform-packages/v2.7.1/bazarr-plus-v2.7.1-stack-r1.0.0.tar.gz"
        archive.write_bytes(archive.read_bytes() + b"tampered")
        self.assertNotEqual(self.run_step(self.STEPS[1]).returncode, 0)

    def test_invalid_checked_in_lock_fails_the_pull_request(self):
        (self.repository / "packaging/releases/2.7.1.json").write_text(json.dumps(lock_data("2.7.1", "sha256:bad")))
        result = self.run_step(self.STEPS[0])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release preparation refused", result.stderr)


if __name__ == "__main__":
    unittest.main()
