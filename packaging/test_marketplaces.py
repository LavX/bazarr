"""Behavioral checks for the store repository export command."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import yaml

import marketplaces
import release
from test_release import BASE_DIGEST, lock_data, package_repository


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
COMMAND = PACKAGE_ROOT / "marketplaces.py"
WORKFLOW = REPO_ROOT / ".github/workflows/platform-packages.yml"
APP_IMAGE = "ghcr.io/lavx/bazarr:2.7.0@" + BASE_DIGEST
TRANSLATOR_IMAGE = "ghcr.io/lavx/ai-subtitle-translator:v2.1.1@sha256:" + "e" * 64
FLARESOLVERR_IMAGE = "ghcr.io/flaresolverr/flaresolverr:v3.5.2@sha256:" + "f" * 64
TRUENAS_APP = "submissions/truenas/ix-dev/community/bazarr-plus"
TRUENAS_SOURCE = PACKAGE_ROOT / "truenas/ix-dev/community/bazarr-plus"
LIBRARY_TESTS = "templates/library/base_v2_3_14/tests"
TEMPLATES = PACKAGE_ROOT / "marketplace-templates"
SCAFFOLD = {".github/workflows/store.yml": "store.yml", ".github/scripts/verify_export.py": "verify_export.py",
            "category-list.json": "category-list.json", "featured-apps.json": "featured-apps.json",
            "recommend-list.json": "recommend-list.json", "build/.gitkeep": "build/.gitkeep"}
ENGINE_SHA = "c9bb47fad5b32d7928a07978fad68801f52e910f"
LEGACY_SHA = "0909364b800950030e71ea82355a5969a1c08b39"
EM_DASH = chr(0x2014)
EXPECTED_ROOTS = {"apps", "Apps", "store-config.json", "supported-languages.json", "templates", "ca_profile.xml",
                  "stacks", "submissions", "LICENSE", "NOTICES.md", "README.md", "EXPORT.json", ".github", "build",
                  "category-list.json", "featured-apps.json", "recommend-list.json"}


def tree(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = self.root / "lock.json"
        self.data = lock_data()

    def export(self, output, repository="LavX/bazarr-packages", ref="main", command=COMMAND, data=None):
        self.lock.write_text(json.dumps(data or self.data))
        return subprocess.run(
            [sys.executable, str(command), "--tag", "v2.7.0", "--lock", str(self.lock), f"--repository={repository}",
             f"--ref={ref}", "--output", str(output)],
            capture_output=True, text=True, check=False,
        )

    def exported(self, name="store", **kwargs):
        output = self.root / name
        result = self.export(output, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(output))
        return output


class StoreLayoutTests(ExportCase):
    def test_store_roots_match_ingestion_paths_without_archive_wrappers(self):
        store = self.exported()
        self.assertEqual({path.name for path in store.iterdir()}, EXPECTED_ROOTS)
        files = set(tree(store))
        for expected in ("apps/bazarr-plus/config.json", "apps/bazarr-plus/docker-compose.yml",
                         "apps/bazarr-plus/metadata/description.md", "apps/bazarr-plus/metadata/logo.jpg",
                         "Apps/BazarrPlus/docker-compose.yml", "Apps/BazarrPlus/icon.png",
                         "Apps/BazarrPlus/thumbnail.png", "Apps/BazarrPlusStack/docker-compose.yml",
                         "templates/bazarr-plus.xml", "stacks/bazarr-plus/compose.yaml",
                         "stacks/bazarr-plus/compose.media.yaml.example", f"{TRUENAS_APP}/app.yaml",
                         f"{TRUENAS_APP}/questions.yaml", f"{TRUENAS_APP}/ix_values.yaml",
                         f"{TRUENAS_APP}/templates/docker-compose.yaml"):
            self.assertIn(expected, files)
        self.assertFalse(any(name.endswith("RELEASE.md") or name.endswith(".tar.gz") for name in files))
        self.assertNotIn(f"{TRUENAS_APP}/item.yaml", files)
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in files))

    def test_unraid_template_url_points_at_the_destination_file(self):
        store = self.exported(repository="Example-Org/store.repo", ref="release/2.7")
        template = ET.parse(store / "templates/bazarr-plus.xml").getroot()
        self.assertEqual(template.findtext("TemplateURL"),
                         "https://raw.githubusercontent.com/Example-Org/store.repo/release/2.7/templates/bazarr-plus.xml")
        self.assertEqual(template.findtext("Repository"), APP_IMAGE)
        ET.parse(store / "ca_profile.xml")

    def test_every_compose_file_pins_locked_images(self):
        store = self.exported()
        expected = {
            "apps/bazarr-plus/docker-compose.yml": {APP_IMAGE},
            "Apps/BazarrPlus/docker-compose.yml": {APP_IMAGE},
            "Apps/BazarrPlusStack/docker-compose.yml": {APP_IMAGE, TRANSLATOR_IMAGE, FLARESOLVERR_IMAGE},
            "stacks/bazarr-plus/compose.yaml": {APP_IMAGE, TRANSLATOR_IMAGE, FLARESOLVERR_IMAGE},
        }
        for relative, images in expected.items():
            services = yaml.safe_load((store / relative).read_text())["services"]
            self.assertEqual({service["image"] for service in services.values()}, images, relative)

    def test_companion_overlay_and_licenses_are_preserved(self):
        store = self.exported()
        self.assertEqual((store / "stacks/bazarr-plus/compose.media.yaml.example").read_bytes(),
                         (PACKAGE_ROOT / "stack/compose.media.yaml.example").read_bytes())
        for name in ("LICENSE", "NOTICES.md"):
            self.assertEqual((store / name).read_bytes(), (REPO_ROOT / name).read_bytes())

    def test_store_metadata_uses_official_formats(self):
        store = self.exported()
        config = json.loads((store / "store-config.json").read_text())
        self.assertEqual(config["version"], 2)
        self.assertRegex(config["store_id"], r"\A[a-z0-9._-]*[a-z0-9][a-z0-9._-]*\Z")
        self.assertNotEqual(config["store_id"], "zimaos-appstore")
        self.assertIsInstance(config["name"]["en_US"], str)
        self.assertEqual(config["maintainer"], "LavX")
        self.assertEqual(config["url"], "https://github.com/LavX/bazarr-packages")
        self.assertEqual(json.loads((store / "supported-languages.json").read_text()), ["en_US"])

    def test_runtipi_config_drops_only_the_unresolved_schema_hint(self):
        store = self.exported()
        exported = json.loads((store / "apps/bazarr-plus/config.json").read_text())
        source = json.loads((PACKAGE_ROOT / "runtipi/bazarr-plus/config.json").read_text())
        self.assertNotIn("$schema", exported)
        del source["$schema"]
        self.assertEqual(exported, source)
        for name, data in tree(store).items():
            self.assertNotIn(b"app-info-schema", data, name)

    def test_truenas_submission_restores_source_fixtures_and_library_license(self):
        store = self.exported()
        app = store / TRUENAS_APP
        for name in ("basic-values.yaml", "media-host-paths.yaml"):
            self.assertEqual((app / "templates/test_values" / name).read_bytes(),
                             (TRUENAS_SOURCE / "templates/test_values" / name).read_bytes())
        source_tests = sorted(path.name for path in (TRUENAS_SOURCE / LIBRARY_TESTS).glob("*.py"))
        self.assertIn("__init__.py", source_tests)
        self.assertEqual(sorted(path.name for path in (app / LIBRARY_TESTS).iterdir()), source_tests)
        for name in ("LICENSE.LGPL-3.0", "THIRD_PARTY.md"):
            self.assertEqual((store / "submissions/truenas" / name).read_bytes(),
                             (PACKAGE_ROOT / "truenas" / name).read_bytes())
        self.assertEqual(yaml.safe_load((app / "ix_values.yaml").read_text())["images"]["image"]["tag"],
                         "2.7.0@" + BASE_DIGEST)

    def test_export_record_hashes_every_exported_file(self):
        store = self.exported()
        record = json.loads((store / "EXPORT.json").read_text())
        files = tree(store)
        del files["EXPORT.json"]
        self.assertEqual(record["files"], {name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
        self.assertEqual((record["tag"], record["repository"], record["ref"]), ("v2.7.0", "LavX/bazarr-packages", "main"))
        self.assertIs(record["live_verified"], False)
        self.assertIsInstance(record["packaging_git"]["dirty"], bool)
        self.assertRegex(record["packaging_git"]["commit"], r"\A[0-9a-f]{40}\Z")
        self.assertEqual(record["lock_sha256"], hashlib.sha256(self.lock.read_bytes()).hexdigest())
        self.assertEqual(record["platform_revisions"], self.data["platform_revisions"])
        fixture = f"{TRUENAS_APP}/templates/test_values/basic-values.yaml"
        self.assertEqual(record["truenas_fixtures"][fixture], {
            "source": "packaging/truenas/ix-dev/community/bazarr-plus/templates/test_values/basic-values.yaml",
            "sha256": hashlib.sha256((TRUENAS_SOURCE / "templates/test_values/basic-values.yaml").read_bytes()).hexdigest(),
        })

    def test_readme_names_destination_urls_and_honest_status(self):
        store = self.exported(repository="Example-Org/store.repo", ref="release/2.7")
        readme = (store / "README.md").read_text()
        for url in ("https://github.com/Example-Org/store.repo",
                    "https://example-org.github.io/store.repo",
                    "https://raw.githubusercontent.com/Example-Org/store.repo/release/2.7/templates/bazarr-plus.xml",
                    "https://raw.githubusercontent.com/Example-Org/store.repo/release/2.7/stacks/bazarr-plus/compose.yaml"):
            self.assertIn(url, readme)
        self.assertIn("offline", readme.lower())
        self.assertIn("not publication evidence", readme)
        self.assertNotIn(EM_DASH, readme)
        for name, data in tree(store).items():
            self.assertNotIn(b"bazarr-packages", data, name)
        for link in re.findall(r"\]\(([^)]+)\)", readme):
            if not link.startswith("https://"):
                self.assertTrue((store / link.split("#")[0]).exists(), link)

    def test_identical_inputs_produce_identical_trees(self):
        self.assertEqual(tree(self.exported("first")), tree(self.exported("second")))

    def test_each_package_revision_changes_its_store_files_and_keeps_pins(self):
        base = tree(self.exported("base"))
        expected = {
            "casaos": {"Apps/BazarrPlus/docker-compose.yml", "Apps/BazarrPlusStack/docker-compose.yml"},
            "runtipi": {"apps/bazarr-plus/config.json"},
            "stack": {"stacks/bazarr-plus/compose.yaml"},
            "truenas": {f"{TRUENAS_APP}/app.yaml"},
            "unraid": {"templates/bazarr-plus.xml"},
        }
        for platform, changed_files in expected.items():
            data = json.loads(json.dumps(self.data))
            data["platform_revisions"][platform] = 2 if platform == "runtipi" else "1.0.1"
            bumped = tree(self.exported(f"bump-{platform}", data=data))
            self.assertEqual(set(bumped), set(base), platform)
            changed = {name for name in base if base[name] != bumped[name]}
            self.assertEqual(changed, changed_files | {"README.md", "EXPORT.json"}, platform)
            for name in changed_files:
                if name.endswith((".yml", ".xml")):
                    self.assertIn(APP_IMAGE.encode(), bumped[name], name)

    def test_verify_live_follows_release_semantics(self):
        self.lock.write_text(json.dumps(self.data))
        calls = []
        with patch.object(release, "verify_live", side_effect=lambda lock, tag: calls.append(tag)), \
                contextlib.redirect_stdout(io.StringIO()):
            code = marketplaces.main(["--tag", "v2.7.0", "--lock", str(self.lock), "--repository", "LavX/bazarr-packages",
                                      "--ref", "main", "--output", str(self.root / "live"), "--verify-live"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["v2.7.0"])
        self.assertIs(json.loads((self.root / "live/EXPORT.json").read_text())["live_verified"], True)
        self.assertNotIn("offline", (self.root / "live/README.md").read_text().lower())

    def test_destination_scaffold_is_exported_verbatim_and_hashed(self):
        store = self.exported()
        record = json.loads((store / "EXPORT.json").read_text())
        for target, source in SCAFFOLD.items():
            data = (TEMPLATES / source).read_bytes()
            self.assertEqual((store / target).read_bytes(), data, target)
            self.assertEqual(record["files"][target], hashlib.sha256(data).hexdigest(), target)
        self.assertEqual((store / "build/.gitkeep").read_bytes(), b"")
        self.assertEqual([entry["name"] for entry in json.loads((store / "category-list.json").read_text())], ["Media"])
        for name in ("featured-apps.json", "recommend-list.json"):
            self.assertEqual(json.loads((store / name).read_text()), [], name)


def job_steps(job):
    return [step.get("uses", "") + step.get("run", "") for step in job["steps"]]


class StoreWorkflowTests(ExportCase):
    def setUp(self):
        super().setUp()
        self.store = self.exported()
        self.workflow = yaml.safe_load((self.store / ".github/workflows/store.yml").read_text())
        self.jobs = self.workflow["jobs"]

    def test_builds_on_review_and_manual_runs_without_privileged_triggers(self):
        self.assertEqual(set(self.workflow.get("on", self.workflow.get(True))), {"pull_request", "workflow_dispatch"})
        self.assertEqual(self.workflow["permissions"], {})
        for name, job in self.jobs.items():
            if name != "deploy":
                self.assertEqual(job["permissions"], {"contents": "read"}, name)
            for step in job["steps"]:
                if step.get("uses", "").startswith("actions/checkout@"):
                    self.assertIs(step["with"]["persist-credentials"], False, name)

    def test_both_feeds_use_pinned_official_tooling_on_separate_runners(self):
        v2 = [step for step in self.jobs["casaos-v2"]["steps"] if "build-appstore-action" in step.get("uses", "")]
        self.assertEqual([step["uses"] for step in v2], [f"IceWhaleTech/build-appstore-action@{ENGINE_SHA}"])
        legacy = self.jobs["legacy-v1"]
        checkout = [step["with"] for step in legacy["steps"] if step.get("with", {}).get("repository")]
        self.assertEqual([(entry["repository"], entry["ref"]) for entry in checkout],
                         [("IceWhaleTech/CasaOS-AppStore", LEGACY_SHA)])
        self.assertTrue(any("build_store_v1.py" in step for step in job_steps(legacy)))
        self.assertFalse(any("build_store_v1.py" in step for step in job_steps(self.jobs["casaos-v2"])))
        for name in ("casaos-v2", "legacy-v1"):
            steps = job_steps(self.jobs[name])
            self.assertIn("python3 .github/scripts/verify_export.py", steps[1], name)
            self.assertTrue(any(step.startswith("actions/upload-artifact@") for step in steps), name)

    def test_pages_deploy_only_from_a_verified_main_dispatch_with_least_privilege(self):
        for name in ("pages", "deploy"):
            condition = self.jobs[name]["if"]
            self.assertIn("github.event_name == 'workflow_dispatch'", condition, name)
            self.assertIn("github.ref == 'refs/heads/main'", condition, name)
        self.assertEqual(set(self.jobs["pages"]["needs"]), {"casaos-v2", "legacy-v1"})
        steps = job_steps(self.jobs["pages"])
        gate = next(index for index, step in enumerate(steps) if "verify_export.py --publish" in step)
        upload = next(index for index, step in enumerate(steps) if step.startswith("actions/upload-pages-artifact@"))
        self.assertLess(gate, upload)
        deploy = self.jobs["deploy"]
        self.assertEqual(deploy["needs"], "pages")
        self.assertEqual(deploy["permissions"], {"pages": "write", "id-token": "write"})
        self.assertEqual(deploy["environment"]["name"], "github-pages")
        # One queue per ref covers build and deploy, so an older main build can never publish last.
        self.assertEqual(self.workflow["concurrency"], {"group": "store-feeds-${{ github.ref }}",
                                                        "cancel-in-progress": False})
        self.assertNotIn("concurrency", deploy)
        self.assertEqual([step["uses"].split("@")[0] for step in deploy["steps"]], ["actions/deploy-pages"])

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is not installed")
    def test_exported_workflow_passes_actionlint(self):
        result = subprocess.run(["actionlint", ".github/workflows/store.yml"], cwd=self.store,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


LIVE_EXPORT = ("import sys, release, marketplaces; release.verify_live = lambda lock, tag: None; "
               "sys.exit(marketplaces.main(sys.argv[1:]))")


class PublicationVerifierTests(ExportCase):
    PUBLISH = ("--publish", "--repository", "LavX/bazarr-packages", "--ref", "main")

    def live_export(self, name, dirty=False):
        repository = package_repository(self.root / f"{name}-repository")
        if dirty:
            (repository / "packaging/scratch.txt").write_text("local edit\n")
        self.lock.write_text(json.dumps(self.data))
        output = self.root / name
        # Bytecode caches would show up as untracked files and mark the fixture repository dirty.
        result = subprocess.run(
            [sys.executable, "-c", LIVE_EXPORT, "--tag", "v2.7.0", "--lock", str(self.lock),
             "--repository", "LavX/bazarr-packages", "--ref", "main", "--output", str(output), "--verify-live"],
            cwd=repository / "packaging", env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads((output / "EXPORT.json").read_text())["packaging_git"]["dirty"], dirty)
        return output

    def verify(self, store, *args, env=None):
        return subprocess.run([sys.executable, ".github/scripts/verify_export.py", *args], cwd=store,
                              env={**{key: value for key, value in os.environ.items() if key != "GITHUB_OUTPUT"},
                                   **(env or {})}, capture_output=True, text=True, check=False)

    def assertRejected(self, result, reason):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("export verification failed", result.stderr)
        self.assertIn(reason, result.stderr)

    def test_clean_live_export_is_publishable_and_names_its_feed(self):
        store = self.live_export("live")
        outputs = self.root / "github-output"
        result = self.verify(store, *self.PUBLISH, env={"GITHUB_OUTPUT": str(outputs)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs.read_text(), "casaos-feed=https://lavx.github.io/bazarr-packages\n")

    def test_offline_export_builds_but_cannot_publish(self):
        store = self.exported()
        self.assertEqual(self.verify(store).returncode, 0)
        self.assertRejected(self.verify(store, *self.PUBLISH), "not live-verified")

    def test_dirty_export_cannot_publish(self):
        store = self.live_export("dirty", dirty=True)
        self.assertIs(json.loads((store / "EXPORT.json").read_text())["packaging_git"]["dirty"], True)
        self.assertEqual(self.verify(store).returncode, 0)
        self.assertRejected(self.verify(store, *self.PUBLISH), "dirty")

    def test_publication_requires_the_exported_destination(self):
        store = self.live_export("live")
        self.assertRejected(self.verify(store, "--publish", "--repository", "Someone/fork", "--ref", "main"),
                            "repository")
        self.assertRejected(self.verify(store, "--publish", "--repository", "LavX/bazarr-packages", "--ref", "beta"),
                            "ref")

    def test_tampered_trees_are_rejected(self):
        store = self.live_export("live")
        tampering = {
            "changed": lambda root: (root / "Apps/BazarrPlus/docker-compose.yml").write_text("services: {}\n"),
            "extra": lambda root: (root / "Apps/Extra.yml").write_text("x: 1\n"),
            "missing": lambda root: (root / "templates/bazarr-plus.xml").unlink(),
            "workflow": lambda root: (root / ".github/workflows/store.yml").write_text("on: push\n"),
            "symlink": lambda root: (root / "linked").symlink_to("/etc/hostname"),
        }
        for case, tamper in tampering.items():
            with self.subTest(case=case):
                copy = self.root / f"tampered-{case}"
                shutil.copytree(store, copy, symlinks=True)
                tamper(copy)
                self.assertRejected(self.verify(copy), "")
                self.assertRejected(self.verify(copy, *self.PUBLISH), "")

    def test_git_metadata_is_ignored(self):
        store = self.live_export("live")
        (store / ".git").mkdir()
        (store / ".git/HEAD").write_text("ref: refs/heads/main\n")
        self.assertEqual(self.verify(store, *self.PUBLISH).returncode, 0)


class RefusalTests(ExportCase):
    def assertRefused(self, result, output):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("store export refused", result.stderr)
        self.assertFalse(output.exists() or output.is_symlink())
        self.assertEqual([path.name for path in output.parent.iterdir() if path.name.startswith(".bazarr")], [])

    def test_rejects_unsafe_repository_and_ref(self):
        output = self.root / "store"
        for repository in ("LavX", "LavX/bazarr-packages/extra", "../x", "-LavX/store", "LavX-/store", "LavX/..",
                           "LavX/.", "La vX/store", "LavX/store?x", "LavX/store.git", "LavX/st%2fore",
                           "https://github.com/LavX/x"):
            with self.subTest(repository=repository):
                self.assertRefused(self.export(output, repository=repository), output)
        for ref in ("", "-main", "a..b", "main/", "/main", "a//b", "main.lock", "has space", "a@{1}", "a%2fb",
                    "refs/../x", "main?x", "main#x", ".hidden"):
            with self.subTest(ref=ref):
                self.assertRefused(self.export(output, ref=ref), output)

    def test_rejects_malformed_lock(self):
        output = self.root / "store"
        self.data["app"]["digest"] = "sha256:bad"
        self.assertRefused(self.export(output), output)

    def test_never_writes_into_existing_output(self):
        output = self.root / "checkout"
        output.mkdir()
        (output / ".git").write_text("keep\n")
        result = self.export(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("store export refused", result.stderr)
        self.assertEqual(tree(output), {".git": b"keep\n"})

    def test_rejects_symlinked_output_parent(self):
        target = self.root / "target"
        target.mkdir()
        (self.root / "link").symlink_to(target, target_is_directory=True)
        result = self.export(self.root / "link/store")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("store export refused", result.stderr)
        self.assertEqual(list(target.iterdir()), [])

    def test_rejects_symlink_in_package_sources(self):
        repository = package_repository(self.root / "repository")
        (repository / "packaging/casaos/Apps/BazarrPlus/linked.yml").symlink_to("/etc/hostname")
        output = self.root / "store"
        self.assertRefused(self.export(output, command=repository / "packaging/marketplaces.py"), output)

    def test_rejects_symlinked_truenas_fixture(self):
        repository = package_repository(self.root / "repository")
        tests = repository / "packaging/truenas/ix-dev/community/bazarr-plus" / LIBRARY_TESTS
        (tests / "test_linked.py").symlink_to("/etc/hostname")
        output = self.root / "store"
        self.assertRefused(self.export(output, command=repository / "packaging/marketplaces.py"), output)

    def test_missing_fixture_fails_after_staging_without_partial_output(self):
        repository = package_repository(self.root / "repository")
        (repository / "packaging/truenas/ix-dev/community/bazarr-plus/templates/test_values/basic-values.yaml").unlink()
        output = self.root / "store"
        self.assertRefused(self.export(output, command=repository / "packaging/marketplaces.py"), output)

    def test_unknown_fixture_files_are_not_exported(self):
        repository = package_repository(self.root / "repository")
        templates = repository / "packaging/truenas/ix-dev/community/bazarr-plus/templates"
        (templates / "test_values/local-secrets.env").write_text("TOKEN=example\n")
        (templates / "library/base_v2_3_14/tests/scratch.log").write_text("debug\n")
        store = self.root / "store"
        result = self.export(store, command=repository / "packaging/marketplaces.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        exported = set(tree(store))
        self.assertNotIn(f"{TRUENAS_APP}/templates/test_values/local-secrets.env", exported)
        self.assertNotIn(f"{TRUENAS_APP}/{LIBRARY_TESTS}/scratch.log", exported)
        self.assertIs(json.loads((store / "EXPORT.json").read_text())["packaging_git"]["dirty"], True)


class WorkflowExportTests(unittest.TestCase):
    """Run the candidate and export steps against a disposable repository and a recording Docker."""

    STEPS = ("Prepare offline PR candidate", "Export store-root candidates")
    CONDITIONS = {None: lambda event: True, "github.event_name == 'pull_request'": lambda event: event == "pull_request"}
    EXPRESSIONS = {
        "${{ github.event_name != 'pull_request' }}": lambda event: str(event != "pull_request").lower(),
        "${{ github.event_name != 'pull_request' && github.token || '' }}":
            lambda event: "" if event == "pull_request" else "fake-token",
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = package_repository(self.root / "repository")
        (self.repository / "packaging/releases/2.7.1.json").write_text(json.dumps(lock_data("2.7.1", "sha256:" + "a" * 64)))
        self.runner_temp = self.root / "runner"
        self.runner_temp.mkdir()
        tools = self.root / "bin"
        tools.mkdir()
        self.docker_log = self.root / "docker.log"
        docker = tools / "docker"
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\n')
        docker.chmod(0o755)
        self.gh_log = self.root / "gh.log"
        gh = tools / "gh"
        gh.write_text('#!/bin/sh\nprintf "%s %s\\n" "${GH_TOKEN:-none}" "$*" >> "$GH_LOG"\nexit 1\n')
        gh.chmod(0o755)
        self.path = f"{tools}{os.pathsep}{os.environ['PATH']}"
        self.steps = {step["name"]: step for step in yaml.safe_load(WORKFLOW.read_text())["jobs"]["prepare"]["steps"]
                      if "name" in step}

    def evaluate(self, value, event):
        value = str(value)
        if "${{" not in value:
            return value
        self.assertIn(value, self.EXPRESSIONS)
        return self.EXPRESSIONS[value](event)

    def run_step(self, name, event="pull_request"):
        step = self.steps[name]
        self.assertIn(step.get("if"), self.CONDITIONS)
        self.assertTrue(self.CONDITIONS[step.get("if")](event), f"{name} does not run on {event}")
        env = {**{key: value for key, value in os.environ.items() if key != "GH_TOKEN"},
               **{key: self.evaluate(value, event) for key, value in step.get("env", {}).items()},
               "RUNNER_TEMP": str(self.runner_temp), "PATH": self.path, "DOCKER_LOG": str(self.docker_log),
               "GH_LOG": str(self.gh_log)}
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                              cwd=self.repository, env=env, capture_output=True, text=True, check=False)

    def test_every_pull_request_candidate_gets_an_offline_store_export(self):
        for name in self.STEPS:
            result = self.run_step(name)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")
        docker = self.docker_log.read_text().splitlines()
        for version in ("2.7.0", "2.7.1"):
            store = self.runner_temp / f"store-exports/v{version}"
            record = json.loads((store / "EXPORT.json").read_text())
            self.assertEqual((record["tag"], record["repository"], record["ref"], record["live_verified"]),
                             (f"v{version}", "LavX/bazarr-packages", "main", False))
            for compose in ("apps/bazarr-plus/docker-compose.yml", "Apps/BazarrPlus/docker-compose.yml",
                            "Apps/BazarrPlusStack/docker-compose.yml", "stacks/bazarr-plus/compose.yaml"):
                self.assertIn(f"compose -f {store}/{compose} config --quiet", docker)
        self.assertFalse(self.gh_log.exists())

    def test_release_and_manual_exports_verify_live_with_the_job_token(self):
        self.assertEqual(self.run_step(self.STEPS[0]).returncode, 0)
        for event in ("release", "workflow_dispatch"):
            with self.subTest(event=event):
                shutil.rmtree(self.runner_temp / "store-exports", ignore_errors=True)
                self.gh_log.unlink(missing_ok=True)
                result = self.run_step(self.STEPS[1], event=event)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("store export refused: Cannot verify GitHub release v2.7.0", result.stderr)
                self.assertEqual(self.gh_log.read_text(), "fake-token api repos/LavX/bazarr/releases/tags/v2.7.0\n")
                self.assertEqual(list((self.runner_temp / "store-exports").iterdir()), [])

    def test_store_export_artifact_keeps_the_hidden_workflow(self):
        upload = self.steps["Upload reviewable store exports"]
        self.assertIs(upload["with"]["include-hidden-files"], True)


if __name__ == "__main__":
    unittest.main()
