"""Behavioral checks for the release package command."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
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

    def test_prepare_still_requires_its_lock_and_output_arguments(self):
        cases = {
            "no lock and no output": (["--tag", "v2.7.0"],
                                      "error: the following arguments are required: --output"),
            "no output": (["--tag", "v2.7.0", "--lock", "packaging/releases/2.7.0.json"],
                           "error: the following arguments are required: --output"),
            "no lock with output": (["--tag", "v2.7.0", "--output", "/tmp/unused"],
                                    "error: --tag and --lock are required unless --all-locks is used"),
        }
        for case, (arguments, message) in cases.items():
            with self.subTest(case):
                result = subprocess.run([sys.executable, str(COMMAND), *arguments],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)

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


class LockResolutionTests(unittest.TestCase):
    """Check the automatic release lock resolution for a tag whose image build completed."""

    EXPECTED_271 = {
        "schema": 1,
        "version": "2.7.1",
        "app": {"digest": NEXT_DIGEST, "image": "ghcr.io/lavx/bazarr"},
        "companions": {
            "flaresolverr": {"digest": "sha256:c80ae007ce2ccdcd217a12426e4f039ef763ff90738c808d38810c3e59323767",
                             "image": "ghcr.io/flaresolverr/flaresolverr", "tag": "v3.5.2"},
            "translator": {"digest": "sha256:e22abba9625b96df6727354f10933da312a084ffeae68f0eb1f9a82492f5e48d",
                           "image": "ghcr.io/lavx/ai-subtitle-translator", "tag": "v2.1.1"},
        },
        "platform_revisions": {"casaos": "1.0.0", "runtipi": 2, "stack": "1.0.0",
                               "truenas": "1.0.1", "unraid": "1.0.0"},
        "runtime_profile": "atlas-sidecar",
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.releases = self.root / "releases"
        self.releases.mkdir()
        # The checked-in 2.7.0 lock is the resolution base for the 2.7 line.
        shutil.copyfile(PACKAGE_ROOT / "releases" / "2.7.0.json", self.releases / "2.7.0.json")

    def registry_responses(self, digest=NEXT_DIGEST, architectures=("amd64", "arm64")):
        """Two GHCR round trips: the resolver fetches the index digest, then re-verifies it."""
        token = ({"token": "local-test-token"}, {})
        index = ({"mediaType": "application/vnd.oci.image.index.v1+json",
                  "manifests": [{"digest": "sha256:" + "a" * 64,
                                 "platform": {"os": "linux", "architecture": architecture}}
                                for architecture in architectures]},
                 {"Docker-Content-Digest": digest})
        return [token, index, token, index]

    def stub_resolution(self, responses, attestation=subprocess.CompletedProcess([], 0)):
        """Isolate the release directory, the registry and gh from the test run."""
        gh = (patch.object(release.subprocess, "run", side_effect=attestation)
              if isinstance(attestation, BaseException)
              else patch.object(release.subprocess, "run", return_value=attestation))
        for managed in (patch.object(release, "PACKAGING", self.root),
                        patch.object(release, "request_json", side_effect=responses),
                        gh):
            managed.start()
            self.addCleanup(managed.stop)

    def test_an_existing_lock_passes_through_unchanged(self):
        existing = self.releases / "2.7.1.json"
        existing.write_text(json.dumps(lock_data("2.7.1", NEXT_DIGEST)))
        before = existing.read_bytes()
        with patch.object(release, "PACKAGING", self.root), \
             patch.object(release, "request_json", side_effect=AssertionError("the registry must not be read")), \
             patch.object(release.subprocess, "run", side_effect=AssertionError("gh must not run")):
            resolved = release.resolve_lock("v2.7.1")
        self.assertEqual(resolved, existing)
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.releases.iterdir()), ["2.7.0.json", "2.7.1.json"])

    def test_an_existing_lock_passes_through_the_cli_unchanged(self):
        existing = self.releases / "2.7.1.json"
        existing.write_text(json.dumps(lock_data("2.7.1", NEXT_DIGEST)))
        before = existing.read_bytes()
        with patch.object(release, "PACKAGING", self.root), \
             patch.object(release, "request_json", side_effect=AssertionError("the registry must not be read")), \
             patch.object(release.subprocess, "run", side_effect=AssertionError("gh must not run")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(release.main(["--resolve-lock", "--tag", "v2.7.1"]), 0)
        self.assertEqual(out.getvalue(), f"{existing}\n")
        self.assertEqual(existing.read_bytes(), before)

    def test_an_existing_lock_in_the_passed_directory_passes_through_without_resolution(self):
        # A tag whose lock is already reviewed on development passes through; the local checkout never sees a write.
        development = self.root / "development-releases"
        development.mkdir()
        existing = development / "2.7.1.json"
        existing.write_text(json.dumps(lock_data("2.7.1", NEXT_DIGEST)))
        before = existing.read_bytes()
        with patch.object(release, "PACKAGING", self.root), \
             patch.object(release, "request_json", side_effect=AssertionError("the registry must not be read")), \
             patch.object(release.subprocess, "run", side_effect=AssertionError("gh must not run")):
            resolved = release.resolve_lock("v2.7.1", development)
        self.assertEqual(resolved, existing)
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.releases.iterdir()), ["2.7.0.json"])

    def test_a_local_lock_refuses_resolution_before_any_registry_work(self):
        # The workflow checks the local file first, but a CLI run with --releases-dir must
        # also refuse up front instead of resolving and only then reporting a collision.
        development = self.root / "development-releases"
        development.mkdir()
        shutil.copyfile(PACKAGE_ROOT / "releases" / "2.7.0.json", development / "2.7.0.json")
        existing = self.releases / "2.7.1.json"
        existing.write_text(json.dumps(lock_data("2.7.1", NEXT_DIGEST)))
        before = existing.read_bytes()
        with patch.object(release, "PACKAGING", self.root), \
             patch.object(release, "request_json", side_effect=AssertionError("an existing local lock reads no registry")), \
             patch.object(release.subprocess, "run", side_effect=AssertionError("an existing local lock runs no attestation")), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as refused:
                release.main(["--resolve-lock", "--tag", "v2.7.1", "--releases-dir", str(development)])
        self.assertEqual(refused.exception.code, 1)
        self.assertIn("release preparation refused", errors.getvalue())
        self.assertIn(f"Release lock already exists in the local checkout: {existing}", errors.getvalue())
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in development.iterdir()), ["2.7.0.json"])
        self.assertEqual(sorted(path.name for path in self.releases.iterdir()), ["2.7.0.json", "2.7.1.json"])

    def test_a_first_release_of_a_line_needs_a_hand_reviewed_lock(self):
        cases = {
            "no base in the line": ([], "v2.7.1", "2.7"),
            "new major.minor line": (["2.7.0.json"], "v2.8.0", "2.8"),
        }
        for case, (kept, tag, line) in cases.items():
            with self.subTest(case):
                for path in list(self.releases.iterdir()):
                    path.unlink()
                for name in kept:
                    shutil.copyfile(PACKAGE_ROOT / "releases" / "2.7.0.json", self.releases / name)
                with patch.object(release, "PACKAGING", self.root), \
                     patch.object(release, "request_json", side_effect=AssertionError("a refusal needs no registry read")):
                    with self.assertRaisesRegex(release.ReleaseError, f"in the {line} line.*hand-reviewed lock"):
                        release.resolve_lock(tag)
                self.assertEqual(sorted(path.name for path in self.releases.iterdir()), kept)

    def test_resolution_uses_the_closest_previous_lock_in_the_line(self):
        closer = lock_data("2.7.1", NEXT_DIGEST)
        closer["companions"]["translator"]["digest"] = "sha256:" + "d" * 64
        (self.releases / "2.7.1.json").write_text(json.dumps(closer))
        self.stub_resolution(self.registry_responses())
        release.resolve_lock("v2.7.2")
        resolved = json.loads((self.releases / "2.7.2.json").read_text())
        self.assertEqual(resolved["version"], "2.7.2")
        self.assertEqual(resolved["companions"], closer["companions"])

    def test_resolution_reads_the_base_from_the_passed_directory(self):
        # Lock pull requests merge into development, so v2.7.1's reviewed lock can be absent from the master checkout.
        development = self.root / "development-releases"
        development.mkdir()
        reviewed = lock_data("2.7.1", NEXT_DIGEST)
        reviewed["companions"]["translator"]["digest"] = "sha256:" + "d" * 64
        reviewed["platform_revisions"]["runtipi"] = 3
        reviewed["platform_revisions"]["truenas"] = "1.0.4"
        (development / "2.7.1.json").write_text(json.dumps(reviewed))
        self.stub_resolution(self.registry_responses())
        resolved = release.resolve_lock("v2.7.2", development)
        self.assertEqual(resolved, self.releases / "2.7.2.json")
        lock = json.loads((self.releases / "2.7.2.json").read_text())
        self.assertEqual(lock["version"], "2.7.2")
        self.assertEqual(lock["companions"], reviewed["companions"])
        self.assertEqual(lock["platform_revisions"]["runtipi"], 4)
        self.assertEqual(lock["platform_revisions"]["truenas"], "1.0.5")
        self.assertEqual(sorted(path.name for path in self.releases.iterdir()), ["2.7.0.json", "2.7.2.json"])
        self.assertEqual(sorted(path.name for path in development.iterdir()), ["2.7.1.json"])

    def test_only_runtipi_and_truenas_bump_their_revisions(self):
        cases = {
            "first update": ({"stack": "1.0.0", "casaos": "1.0.0", "runtipi": 1, "truenas": "1.0.0", "unraid": "1.0.0"},
                             {"stack": "1.0.0", "casaos": "1.0.0", "runtipi": 2, "truenas": "1.0.1", "unraid": "1.0.0"}),
            "later updates": ({"stack": "2.3.4", "casaos": "1.2.0", "runtipi": 7, "truenas": "4.5.9", "unraid": "1.0.0"},
                             {"stack": "2.3.4", "casaos": "1.2.0", "runtipi": 8, "truenas": "4.5.10", "unraid": "1.0.0"}),
        }
        for case, (revisions, expected) in cases.items():
            with self.subTest(case):
                for path in list(self.releases.iterdir()):
                    path.unlink()
                base = lock_data("2.7.0")
                base["platform_revisions"] = dict(revisions)
                (self.releases / "2.7.0.json").write_text(json.dumps(base))
                self.stub_resolution(self.registry_responses())
                release.resolve_lock("v2.7.1")
                written = (self.releases / "2.7.1.json").read_bytes()
                resolved = json.loads(written)
                self.assertEqual(resolved["platform_revisions"], expected)
                self.assertEqual(written, release.canonical_json(resolved))

    def test_registry_and_attestation_failures_refuse_without_writing(self):
        mismatch = list(self.registry_responses())
        mismatch[3] = (mismatch[3][0], {"Docker-Content-Digest": "sha256:" + "b" * 64})
        cases = {
            "digest mismatch": (mismatch, "GHCR image not ready", subprocess.CompletedProcess([], 0)),
            "missing amd64": (self.registry_responses(architectures=("arm64",)), "GHCR image not ready",
                              subprocess.CompletedProcess([], 0)),
            "missing arm64": (self.registry_responses(architectures=("amd64",)), "GHCR image not ready",
                              subprocess.CompletedProcess([], 0)),
            "attestation failure": (self.registry_responses(), "attestation",
                                    subprocess.CalledProcessError(1, "gh attestation verify")),
        }
        for case, (responses, message, attestation) in cases.items():
            with self.subTest(case):
                gh = (patch.object(release.subprocess, "run", side_effect=attestation)
                      if isinstance(attestation, BaseException)
                      else patch.object(release.subprocess, "run", return_value=attestation))
                for managed in (patch.object(release, "PACKAGING", self.root),
                                patch.object(release, "READINESS_SECONDS", 0),
                                patch.object(release.time, "monotonic", return_value=0),
                                patch.object(release.time, "sleep"),
                                patch.object(release, "request_json", side_effect=responses),
                                gh):
                    managed.start()
                    self.addCleanup(managed.stop)
                with self.assertRaisesRegex(release.ReleaseError, message):
                    release.resolve_lock("v2.7.1")
                self.assertEqual([path.name for path in self.releases.iterdir()], ["2.7.0.json"])

    def test_a_lock_appearing_during_resolution_is_never_overwritten(self):
        # A lock written between the existence check and the write must be refused, never clobbered.
        canonical = release.canonical_json
        sentinel = b"hand written lock"

        def appearing_file(lock):
            (self.releases / "2.7.1.json").write_bytes(sentinel)
            return canonical(lock)

        def appearing_symlink(lock):
            (self.root / "hand-written").write_bytes(sentinel)
            (self.releases / "2.7.1.json").symlink_to(self.root / "hand-written")
            return canonical(lock)

        cases = {"appearing file": appearing_file, "appearing symlink": appearing_symlink}
        for case, appearing in cases.items():
            with self.subTest(case):
                for leftover in (self.releases / "2.7.1.json", self.root / "hand-written"):
                    leftover.unlink(missing_ok=True)
                self.stub_resolution(self.registry_responses())
                with patch.object(release, "canonical_json", side_effect=appearing):
                    with self.assertRaisesRegex(release.ReleaseError, "Release lock appeared during resolution"):
                        release.resolve_lock("v2.7.1")
                self.assertEqual((self.releases / "2.7.1.json").read_bytes(), sentinel)

    def test_resolution_reproduces_the_expected_lock(self):
        with patch.object(release, "PACKAGING", self.root), \
             patch.object(release, "request_json", side_effect=self.registry_responses()), \
             patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as attested:
            resolved = release.resolve_lock("v2.7.1")
        self.assertEqual(resolved, self.releases / "2.7.1.json")
        attested.assert_called_once()
        command = attested.call_args[0][0]
        self.assertEqual(command[:3], ["gh", "attestation", "verify"])
        self.assertIn(f"oci://ghcr.io/lavx/bazarr@{NEXT_DIGEST}", command)
        self.assertIn("refs/tags/v2.7.1", command)
        self.assertIn("LavX/bazarr/.github/workflows/build-docker.yml", command)
        written = (self.releases / "2.7.1.json").read_bytes()
        self.assertEqual(json.loads(written), self.EXPECTED_271)
        self.assertEqual(written, release.canonical_json(self.EXPECTED_271))


class LockBaseTests(unittest.TestCase):
    """Check the lock-base modes that record and recheck the base a resolution used."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.releases = self.root / "releases"
        self.releases.mkdir()
        # The checked-in 2.7.0 lock is the resolution base for the 2.7 line.
        shutil.copyfile(PACKAGE_ROOT / "releases" / "2.7.0.json", self.releases / "2.7.0.json")
        self.base_digest = hashlib.sha256((self.releases / "2.7.0.json").read_bytes()).hexdigest()

    def run_mode(self, *arguments):
        """Run one of the lock-base modes with the registry and gh fenced off."""
        with patch.object(release, "PACKAGING", self.root), \
             patch.object(release, "request_json", side_effect=AssertionError("the lock-base modes read no registry")), \
             patch.object(release.subprocess, "run", side_effect=AssertionError("the lock-base modes run no gh")), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            try:
                code = release.main(list(arguments))
            except SystemExit as exited:
                code = exited.code
        return code, out.getvalue(), errors.getvalue()

    def test_lock_base_reports_the_closest_previous_lock(self):
        code, out, _ = self.run_mode("--lock-base", "--tag", "v2.7.1", "--releases-dir", str(self.releases))
        self.assertEqual(code, 0)
        self.assertEqual(out, f"2.7.0.json {self.base_digest}\n")

    def test_lock_base_reports_none_below_the_first_tag_of_a_line(self):
        for tag in ("v2.7.0", "v2.8.0"):
            with self.subTest(tag):
                code, out, _ = self.run_mode("--lock-base", "--tag", tag, "--releases-dir", str(self.releases))
                self.assertEqual(code, 0)
                self.assertEqual(out, "none\n")

    def test_check_lock_base_accepts_a_matching_base(self):
        code, _, _ = self.run_mode("--check-lock-base", "--tag", "v2.7.1", "--releases-dir", str(self.releases),
                                   "--expected-base", "2.7.0.json", "--expected-base-sha256", self.base_digest)
        self.assertEqual(code, 0)

    def test_check_lock_base_accepts_a_recorded_absent_base(self):
        for name in ("none", ""):
            with self.subTest(name=name):
                code, _, _ = self.run_mode("--check-lock-base", "--tag", "v2.7.0", "--releases-dir", str(self.releases),
                                           "--expected-base", name, "--expected-base-sha256", "")
                self.assertEqual(code, 0)

    def test_check_lock_base_refuses_each_drift_form(self):
        (self.releases / "2.7.1.json").write_text(json.dumps(lock_data("2.7.1", NEXT_DIGEST)))
        cases = {
            "moved name": ("v2.7.2", "2.7.0.json", self.base_digest,
                           ["2.7.1.json", "2.7.0.json", "Re-run all jobs"]),
            "changed bytes": ("v2.7.1", "2.7.0.json", "0" * 64, ["2.7.0.json", "changed bytes"]),
            "appeared base": ("v2.7.1", "none", "", ["2.7.0.json", "recorded no lock base"]),
            "vanished base": ("v2.7.0", "2.7.0.json", self.base_digest,
                              ["2.7.0.json", "no longer carries"]),
        }
        for case, (tag, name, digest, fragments) in cases.items():
            with self.subTest(case):
                code, _, errors = self.run_mode("--check-lock-base", "--tag", tag,
                                                "--releases-dir", str(self.releases),
                                                "--expected-base", name, "--expected-base-sha256", digest)
                self.assertEqual(code, 1)
                for fragment in fragments:
                    self.assertIn(fragment, errors)


class ResolveCommandTests(unittest.TestCase):
    """Check the resolve-lock flag wiring without touching the network."""

    def test_unsupported_flag_combinations_refuse_before_any_work(self):
        cases = {
            "missing tag": ([], "--resolve-lock requires --tag"),
            "with a lock": (["--tag", "v2.7.1", "--lock", "releases/2.7.1.json"],
                            "--resolve-lock cannot be combined with --lock, --output or --verify-live"),
            # v2.7.0 has a checked-in lock, so a command that ignores --output would pass through and succeed.
            "with an output": (["--tag", "v2.7.0", "--output", "unused"],
                               "--resolve-lock cannot be combined with --lock, --output or --verify-live"),
            "with every lock": (["--tag", "v2.7.1", "--all-locks", "releases"],
                                "--all-locks cannot be combined with --tag, --lock, --resolve-lock or --verify-live"),
            "with live verification": (["--tag", "v2.7.1", "--verify-live"],
                                       "--resolve-lock cannot be combined with --lock, --output or --verify-live"),
        }
        for case, (arguments, message) in cases.items():
            with self.subTest(case):
                result = subprocess.run([sys.executable, str(COMMAND), "--resolve-lock", *arguments],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)


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


class WorkflowRunStructureTests(unittest.TestCase):
    """Check the workflow_run machinery that GitHub Actions cannot validate locally."""

    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW.read_text())
        self.triggers = self.workflow[True]
        self.jobs = self.workflow["jobs"]
        self.prepare_steps = {step["name"]: step for step in self.jobs["prepare"]["steps"] if "name" in step}
        self.order = [step.get("name") for step in self.jobs["prepare"]["steps"]]

    def test_a_completed_image_build_triggers_the_lane(self):
        self.assertEqual(self.triggers["workflow_run"], {"workflows": ["Build Docker Image"], "types": ["completed"]})
        self.assertEqual(self.triggers["pull_request"]["paths"],
                         ["packaging/**", "LICENSE", "NOTICES.md", ".github/workflows/platform-packages.yml"])
        self.assertEqual(self.triggers["workflow_dispatch"]["inputs"]["tag"]["required"], True)
        self.assertNotIn("release", self.triggers)
        self.assertIn("only uses the copy of this workflow on the default branch", WORKFLOW.read_text())

    def test_the_lane_runs_per_tag_with_least_privilege(self):
        self.assertIn("github.event.workflow_run.head_branch", self.workflow["concurrency"]["group"])
        self.assertEqual(self.workflow["permissions"], {"contents": "read", "packages": "read"})

    def test_the_lane_requires_a_successful_build_without_job_level_regex(self):
        condition = self.jobs["prepare"]["if"]
        self.assertIn("github.event.workflow_run.conclusion == 'success'", condition)
        self.assertNotIn("=~", condition)

    def test_the_final_tag_shape_is_validated_in_a_step(self):
        validation = self.prepare_steps["Validate final stable tag"]
        self.assertEqual(validation["if"], "github.event_name == 'workflow_run'")
        self.assertIn("^v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$", validation["run"])
        preparation = self.prepare_steps["Verify published release and prepare packages"]
        self.assertEqual(preparation["env"]["RELEASE_TAG"],
                         "${{ github.event.workflow_run.head_branch || inputs.tag }}")
        self.assertEqual(preparation["if"], "github.event_name != 'pull_request'")

    def test_the_lock_is_resolved_before_the_lane_prepares(self):
        resolution = self.prepare_steps["Resolve release lock"]
        self.assertEqual(resolution["if"], "github.event_name == 'workflow_run'")
        self.assertIn("release.py --resolve-lock", resolution["run"])
        self.assertIn("release.py --lock-base --tag \"$RELEASE_TAG\"", resolution["run"])
        self.assertIn("git fetch --depth=1 origin development", resolution["run"])
        self.assertIn("git archive origin/development packaging/releases", resolution["run"])
        self.assertIn("--releases-dir", resolution["run"])
        self.assertLess(self.order.index("Validate final stable tag"), self.order.index("Resolve release lock"))
        self.assertLess(self.order.index("Resolve release lock"),
                        self.order.index("Verify published release and prepare packages"))

    def test_the_proposal_guard_pages_every_open_proposal(self):
        proposing = [step for step in self.jobs["propose-lock"]["steps"] if "run" in step][0]
        self.assertIn("gh pr list --state open --base development --limit 200", proposing["run"])
        self.assertIn("too many to check the lock line safely", proposing["run"])

    def test_the_proposal_rechecks_the_resolution_base(self):
        proposing = [step for step in self.jobs["propose-lock"]["steps"] if "run" in step][0]
        self.assertIn("git fetch --depth=1 origin development", proposing["run"])
        self.assertIn("release.py --check-lock-base --tag \"v${VERSION}\"", proposing["run"])
        self.assertEqual(proposing["env"]["RESOLVED"], "${{ needs.prepare.outputs.lock_resolved }}")
        self.assertEqual(proposing["env"]["LOCK_BASE"], "${{ needs.prepare.outputs.lock_base }}")
        self.assertEqual(proposing["env"]["LOCK_BASE_SHA256"], "${{ needs.prepare.outputs.lock_base_sha256 }}")
        outputs = self.jobs["prepare"]["outputs"]
        self.assertEqual(outputs["lock_base"], "${{ steps.lock.outputs.base }}")
        self.assertEqual(outputs["lock_base_sha256"], "${{ steps.lock.outputs.base_sha256 }}")

    def test_a_newly_resolved_lock_reaches_the_proposal_job(self):
        upload = self.prepare_steps["Upload resolved lock"]
        self.assertEqual(upload["if"], "github.event_name == 'workflow_run' && steps.lock.outputs.resolved == 'true'")
        self.assertEqual(upload["with"]["name"], self.jobs["prepare"]["outputs"]["lock_artifact"])
        self.assertEqual(upload["with"]["overwrite"], True)
        proposal = self.jobs["propose-lock"]
        self.assertEqual(proposal["needs"], ["prepare"])
        self.assertIn("github.event_name == 'workflow_run'", proposal["if"])
        self.assertIn("needs.prepare.outputs.lock_resolved == 'true'", proposal["if"])
        self.assertEqual(proposal["permissions"], {"contents": "write", "pull-requests": "write"})
        checkouts = [step for step in proposal["steps"] if step.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual([step["with"]["ref"] for step in checkouts], ["development"])
        downloads = [step for step in proposal["steps"] if step.get("uses", "").startswith("actions/download-artifact@")]
        self.assertEqual([step["with"]["name"] for step in downloads],
                         ["${{ needs.prepare.outputs.lock_artifact }}"])
        proposing = [step for step in proposal["steps"] if "run" in step][0]
        self.assertEqual(proposing["env"]["PRODUCING_RUN"],
                         "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}")
        self.assertEqual(proposing["env"]["IMAGE_BUILD_RUN"], "${{ github.event.workflow_run.html_url }}")
        runs = [step["run"] for step in proposal["steps"] if "run" in step]
        self.assertTrue(any("--base development" in run for run in runs))
        self.assertTrue(all("master" not in run and "pr merge" not in run for run in runs))


class WorkflowRunLaneTests(unittest.TestCase):
    """Run the workflow_run lane steps against a disposable repository."""

    RECORDING_GH = '#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_LOG"\nexit 0\n'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = package_repository(self.root / "repository")
        self.outputs = self.root / "github-output"
        workflow = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        self.steps = {step["name"]: step for step in workflow["prepare"]["steps"] if "name" in step}
        self.proposal = [step["run"] for step in workflow["propose-lock"]["steps"] if "run" in step][0]

    def run_step(self, script, **environment):
        env = {**os.environ, **environment}
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                               cwd=self.repository, env=env, capture_output=True, text=True, check=False)

    def development_origin(self, name=None, data=None):
        """Point the fixture at a bare file:// origin whose development branch adds a reviewed lock."""
        remote = self.root / "remote.git"
        git(self.repository, "init", "-q", "--bare", str(remote))
        git(self.repository, "remote", "add", "origin", f"file://{remote}")
        upstream = self.root / "upstream"
        git(self.root, "clone", "-q", str(self.repository), str(upstream))
        git(upstream, "checkout", "-q", "-b", "development")
        if name is not None:
            (upstream / "packaging/releases" / name).write_bytes(data)
            git(upstream, "add", "-A")
            git(upstream, "commit", "-q", "-m", "reviewed lock")
        git(upstream, "push", "-q", str(remote), "development")

    def open_proposals_stub(self, heads, replay_number=""):
        """A gh stub that pages the open proposal heads the way gh itself does."""
        listed = " ".join(f'"{head}"' for head in heads)
        return ('#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$GH_LOG"\n'
                'case " $* " in\n'
                '  *" --head "*)\n'
                f'    printf "%s\\n" "{replay_number}"\n'
                '    ;;\n'
                '  *" headRefName "*)\n'
                '    limit=30\n'
                '    while [ "$#" -gt 0 ]; do\n'
                '      if [ "$1" = "--limit" ]; then limit="$2"; fi\n'
                '      shift\n'
                '    done\n'
                f'    printf "%s\\n" {listed} | head -n "$limit"\n'
                '    ;;\n'
                'esac\n'
                'exit 0\n')

    def proposal_environment(self, version, lock_bytes, gh_script=None, open_heads=(), replay_number="",
                             resolved="", lock_base="", lock_base_sha256=""):
        """Set up a bare origin, the downloaded lock and a gh stub for the proposal step."""
        remote = self.root / "remote.git"
        if not remote.exists():
            git(self.repository, "init", "-q", "--bare", str(remote))
        if "origin" not in git(self.repository, "remote").split():
            git(self.repository, "remote", "add", "origin", str(remote))
        downloaded = self.repository / "resolved-lock" / f"{version}.json"
        downloaded.parent.mkdir()
        downloaded.write_bytes(lock_bytes)
        tools = self.root / "bin"
        tools.mkdir()
        gh_log = self.root / "gh.log"
        gh = tools / "gh"
        gh.write_text(self.open_proposals_stub(open_heads, replay_number) if gh_script is None else gh_script)
        gh.chmod(0o755)
        return gh_log, {
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "GH_LOG": str(gh_log),
            "GH_TOKEN": "fake-token",
            "GH_REPO": "LavX/bazarr",
            "VERSION": version,
            "EXPECTED_SHA256": hashlib.sha256(lock_bytes).hexdigest(),
            "RESOLVED": resolved,
            "LOCK_BASE": lock_base,
            "LOCK_BASE_SHA256": lock_base_sha256,
            "PRODUCING_RUN": "https://github.com/LavX/bazarr/actions/runs/2",
            "IMAGE_BUILD_RUN": "https://github.com/LavX/bazarr/actions/runs/1",
            "RUNNER_TEMP": str(self.root / "runner"),
        }

    def test_only_final_tags_pass_the_lane_validation(self):
        cases = {"final tag": ("v2.7.1", 0), "release candidate": ("v2.7.1-rc1", 1), "branch build": ("master", 1)}
        for case, (tag, code) in cases.items():
            with self.subTest(case):
                result = self.run_step(self.steps["Validate final stable tag"]["run"], RELEASE_TAG=tag)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_an_existing_lock_bypasses_the_resolver(self):
        lock = self.repository / "packaging/releases/2.7.1.json"
        lock.write_text(json.dumps(lock_data("2.7.1", NEXT_DIGEST)))
        tools = self.root / "bin"
        tools.mkdir()
        python3 = tools / "python3"
        python3.write_text('#!/bin/sh\necho "the resolver must not run for an existing lock" >&2\nexit 1\n')
        python3.chmod(0o755)
        result = self.run_step(self.steps["Resolve release lock"]["run"],
                               PATH=f"{tools}{os.pathsep}{os.environ['PATH']}",
                               RELEASE_TAG="v2.7.1", GITHUB_OUTPUT=str(self.outputs))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs.read_text(),
                         f"resolved=false\nversion=2.7.1\nsha256={hashlib.sha256(lock.read_bytes()).hexdigest()}\n")

    def test_a_reviewed_development_lock_is_copied_without_resolving(self):
        reviewed = (json.dumps(lock_data("2.7.1", NEXT_DIGEST), indent=2) + "\n").encode()
        self.development_origin("2.7.1.json", reviewed)
        tools = self.root / "bin"
        tools.mkdir()
        python3_log = self.root / "python3.log"
        python3 = tools / "python3"
        python3.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PYTHON3_LOG"\nexit 1\n')
        python3.chmod(0o755)
        runner = self.root / "runner"
        runner.mkdir()
        result = self.run_step(self.steps["Resolve release lock"]["run"],
                               PATH=f"{tools}{os.pathsep}{os.environ['PATH']}",
                               PYTHON3_LOG=str(python3_log), RUNNER_TEMP=str(runner),
                               RELEASE_TAG="v2.7.1", GITHUB_OUTPUT=str(self.outputs))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(python3_log.exists())
        self.assertEqual((self.repository / "packaging/releases/2.7.1.json").read_bytes(), reviewed)
        self.assertEqual(self.outputs.read_text(),
                         f"resolved=false\nversion=2.7.1\nsha256={hashlib.sha256(reviewed).hexdigest()}\n")

    def test_resolution_reads_the_exported_development_locks(self):
        reviewed = (json.dumps(lock_data("2.7.1", NEXT_DIGEST), indent=2) + "\n").encode()
        self.development_origin("2.7.1.json", reviewed)
        tools = self.root / "bin"
        tools.mkdir()
        python3_log = self.root / "python3.log"
        python3 = tools / "python3"
        python3.write_text('#!/bin/sh\n'
                           'printf "%s\\n" "$*" >> "$PYTHON3_LOG"\n'
                           'case " $* " in\n'
                           '  *" --lock-base "*) printf "2.7.0.json base-digest\\n" ;;\n'
                           '  *) printf "stub resolved lock\\n" > "packaging/releases/2.7.2.json" ;;\n'
                           'esac\n')
        python3.chmod(0o755)
        runner = self.root / "runner"
        runner.mkdir()
        result = self.run_step(self.steps["Resolve release lock"]["run"],
                               PATH=f"{tools}{os.pathsep}{os.environ['PATH']}",
                               PYTHON3_LOG=str(python3_log), RUNNER_TEMP=str(runner),
                               RELEASE_TAG="v2.7.2", GITHUB_OUTPUT=str(self.outputs))
        self.assertEqual(result.returncode, 0, result.stderr)
        exported = f"{runner}/dev-releases/packaging/releases"
        self.assertEqual(python3_log.read_text().splitlines(),
                         [f"packaging/release.py --resolve-lock --tag v2.7.2 --releases-dir {exported}",
                          f"packaging/release.py --lock-base --tag v2.7.2 --releases-dir {exported}"])
        stub = b"stub resolved lock\n"
        self.assertEqual(self.outputs.read_text(),
                         f"resolved=true\nbase=2.7.0.json\nbase_sha256=base-digest\n"
                         f"version=2.7.2\nsha256={hashlib.sha256(stub).hexdigest()}\n")

    def test_the_proposal_commits_only_the_resolved_lock(self):
        remote = self.root / "remote.git"
        git(self.repository, "init", "-q", "--bare", str(remote))
        git(self.repository, "remote", "add", "origin", str(remote))
        lock = self.repository / "resolved-lock/2.7.1.json"
        lock.parent.mkdir()
        lock.write_text(json.dumps(lock_data("2.7.1", NEXT_DIGEST)))
        tools = self.root / "bin"
        tools.mkdir()
        gh_log = self.root / "gh.log"
        gh = tools / "gh"
        gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_LOG"\nexit 0\n')
        gh.chmod(0o755)
        result = self.run_step(self.proposal,
                               PATH=f"{tools}{os.pathsep}{os.environ['PATH']}", GH_LOG=str(gh_log),
                               GH_TOKEN="fake-token", GH_REPO="LavX/bazarr", VERSION="2.7.1",
                               EXPECTED_SHA256=hashlib.sha256(lock.read_bytes()).hexdigest(),
                               RESOLVED="", LOCK_BASE="", LOCK_BASE_SHA256="",
                               PRODUCING_RUN="https://github.com/LavX/bazarr/actions/runs/2",
                               IMAGE_BUILD_RUN="https://github.com/LavX/bazarr/actions/runs/1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.repository, "diff", "--name-only", "HEAD~1", "HEAD").split(),
                         ["packaging/releases/2.7.1.json"])
        self.assertEqual(git(self.repository, "branch", "--show-current").strip(), "packaging/lock-v2.7.1")
        self.assertIn("refs/heads/packaging/lock-v2.7.1", git(self.repository, "ls-remote", "--heads", "origin"))
        calls = gh_log.read_text().splitlines()
        self.assertTrue(any("pr create --base development" in call for call in calls))
        body = (self.repository / "proposal-body.md").read_text()
        self.assertIn(NEXT_DIGEST, body)
        self.assertIn("ghcr.io/lavx/ai-subtitle-translator:v2.1.1@", body)
        self.assertIn("produced by https://github.com/LavX/bazarr/actions/runs/2", body)
        self.assertIn("https://github.com/LavX/bazarr/actions/runs/1", body)
        self.assertIn("runtipi: 1", body)
        self.assertIn("truenas: 1.0.0", body)

    def test_an_open_lower_patch_proposal_refuses_before_the_push(self):
        downloaded = json.dumps(lock_data("2.7.2", NEXT_DIGEST)).encode()
        gh_log, environment = self.proposal_environment("2.7.2", downloaded,
                                                        open_heads=["packaging/lock-v2.7.1"])
        head = git(self.repository, "rev-parse", "HEAD")
        result = self.run_step(self.proposal, **environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Merge the v2.7.1 release lock first, then Re-run all jobs",
                      result.stdout + result.stderr)
        self.assertEqual(git(self.repository, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repository, "branch", "--list", "packaging/lock-v*"), "")
        self.assertEqual(git(self.repository, "ls-remote", "--heads", "origin"), "")
        self.assertFalse((self.repository / "packaging/releases/2.7.2.json").exists())
        self.assertEqual(gh_log.read_text().splitlines(),
                         ["pr list --state open --base development --limit 200 --json headRefName --jq .[].headRefName"])

    def test_an_open_lower_patch_proposal_beyond_the_first_page_still_refuses(self):
        heads = [f"unrelated/pr-{index}" for index in range(30)] + ["packaging/lock-v2.7.1"]
        downloaded = json.dumps(lock_data("2.7.2", NEXT_DIGEST)).encode()
        gh_log, environment = self.proposal_environment("2.7.2", downloaded, open_heads=heads)
        head = git(self.repository, "rev-parse", "HEAD")
        result = self.run_step(self.proposal, **environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Merge the v2.7.1 release lock first, then Re-run all jobs",
                      result.stdout + result.stderr)
        self.assertEqual(git(self.repository, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repository, "branch", "--list", "packaging/lock-v*"), "")
        self.assertEqual(git(self.repository, "ls-remote", "--heads", "origin"), "")
        self.assertFalse((self.repository / "packaging/releases/2.7.2.json").exists())
        self.assertEqual(gh_log.read_text().splitlines(),
                         ["pr list --state open --base development --limit 200 --json headRefName --jq .[].headRefName"])

    def test_a_page_limit_reached_fails_closed(self):
        heads = [f"unrelated/pr-{index}" for index in range(200)]
        downloaded = json.dumps(lock_data("2.7.2", NEXT_DIGEST)).encode()
        gh_log, environment = self.proposal_environment("2.7.2", downloaded, open_heads=heads)
        head = git(self.repository, "rev-parse", "HEAD")
        result = self.run_step(self.proposal, **environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("too many to check the lock line safely", result.stdout + result.stderr)
        self.assertEqual(git(self.repository, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repository, "branch", "--list", "packaging/lock-v*"), "")
        self.assertEqual(git(self.repository, "ls-remote", "--heads", "origin"), "")
        self.assertEqual(gh_log.read_text().splitlines(),
                         ["pr list --state open --base development --limit 200 --json headRefName --jq .[].headRefName"])

    def test_a_malformed_head_with_a_wildcard_separator_does_not_refuse(self):
        downloaded = json.dumps(lock_data("2.7.2", NEXT_DIGEST)).encode()
        gh_log, environment = self.proposal_environment("2.7.2", downloaded,
                                                        open_heads=["packaging/lock-v2x7.1"])
        result = self.run_step(self.proposal, **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refs/heads/packaging/lock-v2.7.2", git(self.repository, "ls-remote", "--heads", "origin"))
        self.assertTrue(any("pr create --base development" in call for call in gh_log.read_text().splitlines()))
        self.assertNotIn("v2.7.1", result.stdout + result.stderr)

    def test_a_merged_lower_lock_after_resolution_refuses_before_the_push(self):
        reviewed = (json.dumps(lock_data("2.7.1", NEXT_DIGEST), indent=2) + "\n").encode()
        self.development_origin("2.7.1.json", reviewed)
        base_bytes = (self.repository / "packaging/releases/2.7.0.json").read_bytes()
        downloaded = json.dumps(lock_data("2.7.2", NEXT_DIGEST)).encode()
        gh_log, environment = self.proposal_environment(
            "2.7.2", downloaded, resolved="true", lock_base="2.7.0.json",
            lock_base_sha256=hashlib.sha256(base_bytes).hexdigest())
        head = git(self.repository, "rev-parse", "HEAD")
        result = self.run_step(self.proposal, **environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The lock base moved to 2.7.1.json on development, not the recorded 2.7.0.json",
                      result.stdout + result.stderr)
        self.assertIn("Re-run all jobs so the lock resolves from the current base", result.stdout + result.stderr)
        self.assertEqual(git(self.repository, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repository, "branch", "--list", "packaging/lock-v*"), "")
        self.assertNotIn("packaging/lock-v2.7.2", git(self.repository, "ls-remote", "--heads", "origin"))
        self.assertFalse((self.repository / "packaging/releases/2.7.2.json").exists())
        self.assertEqual(gh_log.read_text().splitlines(),
                         ["pr list --state open --base development --limit 200 --json headRefName --jq .[].headRefName"])

    def test_the_same_base_after_resolution_still_pushes(self):
        self.development_origin()
        base_bytes = (self.repository / "packaging/releases/2.7.0.json").read_bytes()
        downloaded = json.dumps(lock_data("2.7.2", NEXT_DIGEST)).encode()
        gh_log, environment = self.proposal_environment(
            "2.7.2", downloaded, resolved="true", lock_base="2.7.0.json",
            lock_base_sha256=hashlib.sha256(base_bytes).hexdigest())
        result = self.run_step(self.proposal, **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refs/heads/packaging/lock-v2.7.2", git(self.repository, "ls-remote", "--heads", "origin"))
        self.assertTrue(any("pr create --base development" in call for call in gh_log.read_text().splitlines()))

    def test_a_replay_after_a_lower_lock_merges_resolves_and_pushes_from_the_new_base(self):
        merged = lock_data("2.7.1", NEXT_DIGEST)
        merged["platform_revisions"]["runtipi"] = 2
        merged["platform_revisions"]["truenas"] = "1.0.1"
        reviewed = (json.dumps(merged, sort_keys=True, indent=2) + "\n").encode()
        self.development_origin("2.7.1.json", reviewed)
        tools = self.root / "resolver-bin"
        tools.mkdir()
        python3 = tools / "python3"
        python3.write_text(
            '#!/bin/sh\n'
            'tag=\n'
            'releases=\n'
            'previous=\n'
            'for argument in "$@"; do\n'
            '  case "$previous" in\n'
            '    --tag) tag="$argument" ;;\n'
            '    --releases-dir) releases="$argument" ;;\n'
            '  esac\n'
            '  previous="$argument"\n'
            'done\n'
            'case " $* " in\n'
            '  *" --resolve-lock "*)\n'
            '    base_name="$(command -p python3 packaging/release.py --lock-base --tag "$tag" --releases-dir "$releases")"\n'
            '    base_name="${base_name%% *}"\n'
            "    awk -v version=\"${tag#v}\" '\n"
            "      /\"version\":/ { sub(/\"[0-9][0-9.]*\"/, \"\\\"\" version \"\\\"\") }\n"
            "      /\"runtipi\":/ { match($0, /[0-9]+/); $0 = substr($0, 1, RSTART - 1) (substr($0, RSTART, RLENGTH) + 1) substr($0, RSTART + RLENGTH) }\n"
            "      /\"truenas\":/ { match($0, /\"[0-9]+\\.[0-9]+\\.[0-9]+\"/); split(substr($0, RSTART + 1, RLENGTH - 2), parts, \".\"); $0 = substr($0, 1, RSTART - 1) \"\\\"\" parts[1] \".\" parts[2] \".\" (parts[3] + 1) \"\\\"\" substr($0, RSTART + RLENGTH) }\n"
            "      { print }\n"
            "    ' \"$releases/$base_name\" > \"packaging/releases/${tag#v}.json\"\n"
            '    ;;\n'
            '  *)\n'
            f'    exec {shlex.quote(sys.executable)} "$@"\n'
            '    ;;\n'
            'esac\n')
        python3.chmod(0o755)
        runner = self.root / "runner"
        runner.mkdir()
        result = self.run_step(self.steps["Resolve release lock"]["run"],
                               PATH=f"{tools}{os.pathsep}{os.environ['PATH']}",
                               RUNNER_TEMP=str(runner), RELEASE_TAG="v2.7.2", GITHUB_OUTPUT=str(self.outputs))
        self.assertEqual(result.returncode, 0, result.stderr)
        # The propose job checks out development, whose tree does not carry the resolved lock yet.
        resolved_path = self.repository / "packaging/releases/2.7.2.json"
        resolved = resolved_path.read_bytes()
        resolved_path.unlink()
        recorded = dict(line.split("=", 1) for line in self.outputs.read_text().splitlines())
        self.assertEqual(recorded, {
            "resolved": "true",
            "base": "2.7.1.json",
            "base_sha256": hashlib.sha256(reviewed).hexdigest(),
            "version": "2.7.2",
            "sha256": hashlib.sha256(resolved).hexdigest(),
        })
        gh_log, environment = self.proposal_environment("2.7.2", resolved, resolved="true",
                                                        lock_base=recorded["base"],
                                                        lock_base_sha256=recorded["base_sha256"])
        result = self.run_step(self.proposal, **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refs/heads/packaging/lock-v2.7.2", git(self.repository, "ls-remote", "--heads", "origin"))
        self.assertTrue(any("pr create --base development" in call for call in gh_log.read_text().splitlines()))
        pushed = json.loads(git(self.repository, "show", "origin/packaging/lock-v2.7.2:packaging/releases/2.7.2.json"))
        self.assertEqual(pushed["platform_revisions"]["runtipi"], 3)
        self.assertEqual(pushed["platform_revisions"]["truenas"], "1.0.2")

    def test_open_proposals_outside_the_lower_patches_still_push(self):
        stub = ('#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$GH_LOG"\n'
                'case " $* " in\n'
                '  *" headRefName "*) printf "%s\\n" "packaging/lock-v2.8.0" "packaging/lock-v2.7.2" ;;\n'
                'esac\n'
                'exit 0\n')
        downloaded = json.dumps(lock_data("2.7.1", NEXT_DIGEST)).encode()
        gh_log, environment = self.proposal_environment("2.7.1", downloaded, stub)
        result = self.run_step(self.proposal, **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refs/heads/packaging/lock-v2.7.1", git(self.repository, "ls-remote", "--heads", "origin"))
        self.assertTrue(any("pr create --base development" in call for call in gh_log.read_text().splitlines()))

    def test_matching_development_lock_bytes_leave_nothing_to_propose(self):
        reviewed = (json.dumps(lock_data("2.7.1", NEXT_DIGEST), indent=2) + "\n").encode()
        (self.repository / "packaging/releases/2.7.1.json").write_bytes(reviewed)
        gh_log, environment = self.proposal_environment("2.7.1", reviewed, self.RECORDING_GH)
        result = self.run_step(self.proposal, **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to propose", result.stdout)
        self.assertFalse(gh_log.exists())
        self.assertEqual(git(self.repository, "ls-remote", "--heads", "origin"), "")

    def test_conflicting_development_lock_bytes_refuse_and_push_nothing(self):
        reviewed = (json.dumps(lock_data("2.7.1", BASE_DIGEST), indent=2) + "\n").encode()
        (self.repository / "packaging/releases/2.7.1.json").write_bytes(reviewed)
        downloaded = (json.dumps(lock_data("2.7.1", NEXT_DIGEST), indent=2) + "\n").encode()
        gh_log, environment = self.proposal_environment("2.7.1", downloaded, self.RECORDING_GH)
        result = self.run_step(self.proposal, **environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Decide by hand which lock fits the tag", result.stderr)
        self.assertFalse(gh_log.exists())
        self.assertEqual(git(self.repository, "ls-remote", "--heads", "origin"), "")

    def test_a_replayed_proposal_refreshes_its_open_pull_request(self):
        stub = ('#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "$GH_LOG"\n'
                'case " $* " in\n'
                '  *" headRefName "*) printf "%s\\n" "packaging/lock-v2.7.1" ;;\n'
                '  *" --head "*) printf "%s\\n" 17 ;;\n'
                'esac\n'
                'exit 0\n')
        downloaded = json.dumps(lock_data("2.7.1", NEXT_DIGEST)).encode()
        gh_log, environment = self.proposal_environment("2.7.1", downloaded, stub)
        result = self.run_step(self.proposal, **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = gh_log.read_text().splitlines()
        self.assertIn("pr edit 17 --body-file proposal-body.md", calls)
        self.assertFalse(any("pr create" in call for call in calls))
        self.assertIn("refs/heads/packaging/lock-v2.7.1", git(self.repository, "ls-remote", "--heads", "origin"))
        self.assertIn(NEXT_DIGEST, (self.repository / "proposal-body.md").read_text())


if __name__ == "__main__":
    unittest.main()
