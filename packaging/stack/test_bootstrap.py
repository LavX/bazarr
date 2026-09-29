"""Exercise the shipped Compose bootstrap against disposable directories."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "bazarr"
        self.data = self.root / "translator"
        self.config.mkdir()
        self.data.mkdir()
        compose = yaml.safe_load(Path(__file__).with_name("compose.yaml").read_text())
        self.script = compose["services"]["initialize"]["command"][0]

    def run_bootstrap(self):
        return subprocess.run(
            [sys.executable, "-c", self.script, str(self.config), str(self.data),
             str(os.getuid()), str(os.getgid()), str(os.getuid()), str(os.getgid())],
            capture_output=True, text=True, check=False,
        )

    def test_fresh_install_shares_key_and_leaves_onboarding_available(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = yaml.safe_load((self.config / "config/config.yaml").read_text())
        key = (self.data / "encryption.key").read_text().strip()
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(config["translator"]["openrouter_encryption_key"], key)
        self.assertEqual(config["translator"]["openrouter_url"], "http://subtitle-translator:8765")
        self.assertFalse(config.get("general", {}).get("setup_complete", False))
        self.assertNotIn(key, result.stdout + result.stderr)
        self.assertEqual((self.data / "encryption.key").stat().st_mode & 0o777, 0o600)

    def test_restart_keeps_user_changes_and_original_key(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        config_path = self.config / "config/config.yaml"
        config_path.write_text("translator:\n  openrouter_url: http://custom-translator:8765\n")
        before_config = config_path.read_bytes()
        before_key = (self.data / "encryption.key").read_bytes()
        self.assertEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(config_path.read_bytes(), before_config)
        self.assertEqual((self.data / "encryption.key").read_bytes(), before_key)

    def test_existing_unmanaged_config_is_not_overwritten(self):
        (self.config / "config").mkdir()
        path = self.config / "config/config.yaml"
        path.write_text("general:\n  port: 7000\n")
        before = path.read_bytes()
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_missing_key_on_restart_fails_instead_of_rotating(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        (self.data / "encryption.key").unlink()
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertFalse((self.data / "encryption.key").exists())

    def test_existing_translator_data_is_not_rekeyed(self):
        path = self.data / "encryption.key"
        path.write_text("a" * 64)
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(path.read_text(), "a" * 64)
        self.assertEqual(list(self.config.iterdir()), [])

    def test_mismatched_volume_key_fails_without_rewriting_either_volume(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        path = self.data / "encryption.key"
        path.write_text("b" * 64)
        before = (self.config / "config/config.yaml").read_bytes()
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(path.read_text(), "b" * 64)
        self.assertEqual((self.config / "config/config.yaml").read_bytes(), before)

    def test_symlink_key_on_restart_is_not_followed(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        path = self.data / "encryption.key"
        outside = self.root / "outside-key"
        outside.write_text("c" * 64)
        path.unlink()
        path.symlink_to(outside)
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(outside.read_text(), "c" * 64)

    def test_symlink_config_directory_is_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.config / "config").symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
