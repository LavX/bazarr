"""Behavioral checks for the release package command."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import release


PACKAGE_ROOT = Path(__file__).resolve().parent
COMMAND = PACKAGE_ROOT / "release.py"
BASE_DIGEST = "sha256:90a5c184b0af41602ff78ea7286e0c5f2c4c284c9b18f71427d9ddbeb0b6531c"


class ReleaseCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = self.root / "lock.json"
        self.output = self.root / "output"
        self.data = {
            "schema": 1,
            "version": "2.7.0",
            "app": {"image": "ghcr.io/lavx/bazarr", "digest": BASE_DIGEST},
            "companions": {
                "translator": {"image": "ghcr.io/lavx/ai-subtitle-translator", "tag": "v2.1.1", "digest": "sha256:" + "e" * 64},
                "flaresolverr": {"image": "ghcr.io/flaresolverr/flaresolverr", "tag": "v3.5.2", "digest": "sha256:" + "f" * 64},
            },
            "runtime_profile": "atlas-sidecar",
            "platform_revisions": {"stack": "1.0.0", "casaos": "1.0.0", "runtipi": 1, "truenas": "1.0.0", "unraid": "1.0.0"},
        }

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
        expected_dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", "packaging"], cwd=PACKAGE_ROOT.parent, capture_output=True, text=True, check=True).stdout.strip())
        self.assertEqual(provenance["packaging_git"]["dirty"], expected_dirty)
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


if __name__ == "__main__":
    unittest.main()
