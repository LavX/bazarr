"""Behavioral checks for the release package command."""

import contextlib
import hashlib
import io
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


class ResolveCommandTests(unittest.TestCase):
    """Check the resolve-lock flag wiring without touching the network."""

    def test_unsupported_flag_combinations_refuse_before_any_work(self):
        cases = {
            "missing tag": ([], "--resolve-lock requires --tag"),
            "with a lock": (["--tag", "v2.7.1", "--lock", "releases/2.7.1.json"],
                            "--resolve-lock cannot be combined with --lock or --verify-live"),
            "with every lock": (["--tag", "v2.7.1", "--all-locks", "releases"],
                                "--all-locks cannot be combined with --tag, --lock, --resolve-lock or --verify-live"),
            "with live verification": (["--tag", "v2.7.1", "--verify-live"],
                                       "--resolve-lock cannot be combined with --lock or --verify-live"),
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
        self.assertLess(self.order.index("Validate final stable tag"), self.order.index("Resolve release lock"))
        self.assertLess(self.order.index("Resolve release lock"),
                        self.order.index("Verify published release and prepare packages"))

    def test_a_newly_resolved_lock_reaches_the_proposal_job(self):
        upload = self.prepare_steps["Upload resolved lock"]
        self.assertEqual(upload["if"], "github.event_name == 'workflow_run' && steps.lock.outputs.resolved == 'true'")
        proposal = self.jobs["propose-lock"]
        self.assertEqual(proposal["needs"], ["prepare"])
        self.assertIn("github.event_name == 'workflow_run'", proposal["if"])
        self.assertIn("needs.prepare.outputs.lock_resolved == 'true'", proposal["if"])
        self.assertEqual(proposal["permissions"], {"contents": "write", "pull-requests": "write"})
        checkouts = [step for step in proposal["steps"] if step.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual([step["with"]["ref"] for step in checkouts], ["development"])
        downloads = [step for step in proposal["steps"] if step.get("uses", "").startswith("actions/download-artifact@")]
        self.assertEqual([step["with"]["name"] for step in downloads], [upload["with"]["name"]])
        runs = [step["run"] for step in proposal["steps"] if "run" in step]
        self.assertTrue(any("--base development" in run for run in runs))
        self.assertTrue(all("master" not in run and "pr merge" not in run for run in runs))


class WorkflowRunLaneTests(unittest.TestCase):
    """Run the workflow_run lane steps against a disposable repository."""

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
                               PRODUCING_RUN="https://github.com/LavX/bazarr/actions/runs/1")
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
        self.assertIn("https://github.com/LavX/bazarr/actions/runs/1", body)
        self.assertIn("runtipi: 1", body)
        self.assertIn("truenas: 1.0.0", body)


if __name__ == "__main__":
    unittest.main()
