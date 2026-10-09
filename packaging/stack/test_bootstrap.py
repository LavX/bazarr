"""Exercise the shipped Compose bootstraps against disposable directories."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


HERE = Path(__file__).resolve().parent
JOURNAL = ".bazarr-stack-init.json"
MARKER = ".bazarr-stack-v1.json"

# Test-only wrapper: interrupts the initializer at its Nth filesystem call.
FAULT_HARNESS = r"""
import os
import sys

script, mode, target, log = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
del sys.argv[1:5]
calls = []

def boundary(name, real):
    def wrapped(*args, **kwargs):
        calls.append(name)
        with open(log, "w") as handle:
            handle.write("\n".join(calls))
        if len(calls) == target:
            if mode == "before":
                os._exit(137)
            if mode == "deny":
                raise PermissionError(1, "Operation not permitted")
            if mode == "after" and name == "write":
                real(args[0], bytes(args[1])[:max(1, len(args[1]) // 2)])
                os._exit(137)
        result = real(*args, **kwargs)
        if len(calls) == target and mode == "after":
            os._exit(137)
        return result
    return wrapped

for name in ("open", "write", "fsync", "close", "replace", "rename", "mkdir", "chown", "fchown", "unlink", "rmdir"):
    setattr(os, name, boundary(name, getattr(os, name)))
exec(compile(script, "initialize", "exec"), {"__name__": "__main__"})
"""


class BootstrapBehavior:
    COMPOSE = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.use_volumes("first")
        compose = yaml.safe_load(self.COMPOSE.read_text())
        self.script = compose["services"]["initialize"]["command"][0]

    def use_volumes(self, name):
        self.config = self.root / name / "bazarr"
        self.data = self.root / name / "translator"
        self.config.mkdir(parents=True)
        self.data.mkdir()

    def arguments(self):
        return [str(self.config), str(self.data),
                str(os.getuid()), str(os.getgid()), str(os.getuid()), str(os.getgid())]

    def run_bootstrap(self, timeout=None):
        return subprocess.run([sys.executable, "-c", self.script, *self.arguments()],
                              capture_output=True, text=True, check=False, timeout=timeout)

    def run_with_fault(self, mode, target):
        log = self.root / "calls.log"
        result = subprocess.run([sys.executable, "-c", FAULT_HARNESS, self.script, mode, str(target), str(log),
                                 *self.arguments()], capture_output=True, text=True, check=False)
        calls = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return result, calls

    def fresh_install_calls(self):
        self.use_volumes("count")
        result, calls = self.run_with_fault("count", 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        return calls

    def state(self):
        return {path.relative_to(self.root).as_posix(): (path.readlink() if path.is_symlink() else path.read_bytes())
                for path in sorted((self.root).rglob("*")) if path.is_symlink() or path.is_file()}

    def assert_complete_install(self):
        key = (self.data / "encryption.key").read_text()
        self.assertRegex(key, r"\A[0-9a-f]{64}\n\Z")
        key = key.strip()
        config = yaml.safe_load((self.config / "config/config.yaml").read_text())
        self.assertEqual(config["translator"]["openrouter_encryption_key"], key)
        self.assertEqual(config["translator"]["openrouter_url"], "http://subtitle-translator:8765")
        self.assertEqual(json.loads((self.config / MARKER).read_text()),
                         {"version": 1, "key_sha256": hashlib.sha256(key.encode()).hexdigest()})
        self.assertEqual(sorted(path.name for path in self.config.iterdir()), [MARKER, "config"])
        self.assertEqual([path.name for path in (self.config / "config").iterdir()], ["config.yaml"])
        self.assertEqual([path.name for path in self.data.iterdir()], ["encryption.key"])
        for path in (self.data / "encryption.key", self.config / "config/config.yaml", self.config / MARKER):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)

    def interrupted_with_key(self):
        """Leave volumes as a crash would, right after the translator key reached disk."""
        for target in range(1, len(self.fresh_install_calls()) + 1):
            self.use_volumes(f"key-{target}")
            result, _ = self.run_with_fault("after", target)
            self.assertEqual(result.returncode, 137, result.stderr)
            key = self.data / "encryption.key"
            if key.is_file() and len(key.read_bytes()) == 65:
                return key.read_bytes()
        self.fail("No interrupted state contained a written key")

    def test_fresh_install_shares_key_and_leaves_onboarding_available(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_complete_install()
        config = yaml.safe_load((self.config / "config/config.yaml").read_text())
        key = (self.data / "encryption.key").read_text().strip()
        self.assertFalse(config.get("general", {}).get("setup_complete", False))
        self.assertNotIn(key, result.stdout + result.stderr)

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

    def test_interruption_at_any_filesystem_step_resumes_on_next_start(self):
        calls = self.fresh_install_calls()
        for target in range(1, len(calls) + 1):
            for mode in ("before", "after"):
                with self.subTest(step=target, call=calls[target - 1], mode=mode):
                    self.use_volumes(f"crash-{target}-{mode}")
                    interrupted, _ = self.run_with_fault(mode, target)
                    self.assertEqual(interrupted.returncode, 137, interrupted.stderr)
                    key = self.data / "encryption.key"
                    written = key.read_bytes() if key.is_file() and len(key.read_bytes()) == 65 else None
                    resumed = self.run_bootstrap()
                    self.assertEqual(resumed.returncode, 0, resumed.stderr)
                    self.assert_complete_install()
                    if written is not None:
                        self.assertEqual(key.read_bytes(), written, "a key already on disk was rotated")
                    self.assertEqual(self.run_bootstrap().returncode, 0)

    def test_ownership_failure_reports_cleanly_and_resumes_after_fix(self):
        calls = self.fresh_install_calls()
        owner_steps = [index for index, name in enumerate(calls, 1) if name in ("chown", "fchown")]
        self.assertGreaterEqual(len(owner_steps), 3)
        for target in owner_steps:
            with self.subTest(step=target):
                self.use_volumes(f"deny-{target}")
                denied, _ = self.run_with_fault("deny", target)
                self.assertNotEqual(denied.returncode, 0)
                self.assertNotIn("Traceback", denied.stderr)
                self.assertIn("Operation not permitted", denied.stderr)
                resumed = self.run_bootstrap()
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assert_complete_install()

    def test_interrupted_setup_with_unmanaged_files_is_left_untouched(self):
        self.interrupted_with_key()
        for volume in (self.config, self.data):
            with self.subTest(volume=volume.name):
                extra = volume / "user-notes.txt"
                extra.write_text("keep me\n")
                before = self.state()
                self.assertNotEqual(self.run_bootstrap().returncode, 0)
                self.assertEqual(self.state(), before)
                extra.unlink()

    def test_interrupted_setup_with_content_it_did_not_write_is_left_untouched(self):
        written = self.interrupted_with_key()
        for path, content in ((self.data / "encryption.key", "b" * 64 + "\n"),
                              (self.config / "config/config.yaml.tmp", "general:\n  port: 7000\n")):
            with self.subTest(path=path.name):
                path.write_text(content)
                before = self.state()
                self.assertNotEqual(self.run_bootstrap().returncode, 0)
                self.assertEqual(self.state(), before)
                (self.data / "encryption.key").write_bytes(written)
                (self.config / "config/config.yaml.tmp").unlink(missing_ok=True)

    def test_interrupted_setup_does_not_follow_symlinks(self):
        written = self.interrupted_with_key()
        outside = self.root / "outside-key"
        for name in ("encryption.key", "encryption.key.tmp"):
            with self.subTest(name=name):
                outside.write_bytes(written)
                link = self.data / name
                link.unlink(missing_ok=True)
                link.symlink_to(outside)
                before = self.state()
                self.assertNotEqual(self.run_bootstrap().returncode, 0)
                self.assertEqual(self.state(), before)
                link.unlink()

    def test_unrecognized_journal_is_not_adopted(self):
        journal = self.config / JOURNAL
        journal.write_text('{"owner": "someone else"}\n')
        before = self.state()
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(self.state(), before)

    def test_partial_journal_next_to_other_data_is_not_adopted(self):
        (self.config / JOURNAL).write_text('{"stack')
        (self.data / "jobs.db").write_text("translator data\n")
        before = self.state()
        self.assertNotEqual(self.run_bootstrap().returncode, 0)
        self.assertEqual(self.state(), before)

    def assert_fifo_rejected_promptly(self, fifo):
        self.assertNotEqual(self.run_bootstrap(timeout=5).returncode, 0)
        self.assertTrue(fifo.is_fifo())

    def test_fifo_journal_is_rejected_without_blocking(self):
        fifo = self.config / JOURNAL
        os.mkfifo(fifo)
        self.assert_fifo_rejected_promptly(fifo)
        self.assertEqual([path.name for path in self.config.iterdir()], [JOURNAL])
        self.assertEqual(list(self.data.iterdir()), [])

    def test_fifo_marker_on_restart_is_rejected_without_blocking(self):
        self.assertEqual(self.run_bootstrap().returncode, 0)
        before = self.state()
        fifo = self.config / MARKER
        fifo.unlink()
        os.mkfifo(fifo)
        self.assert_fifo_rejected_promptly(fifo)
        del before[f"first/bazarr/{MARKER}"]
        self.assertEqual(self.state(), before)


class StackBootstrapTests(BootstrapBehavior, unittest.TestCase):
    COMPOSE = HERE / "compose.yaml"


class CasaOSStackBootstrapTests(BootstrapBehavior, unittest.TestCase):
    COMPOSE = HERE.parent / "casaos/Apps/BazarrPlusStack/docker-compose.yml"


if __name__ == "__main__":
    unittest.main()
