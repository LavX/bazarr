# coding=utf-8
"""The CI planner: which suites a run needs, and on which Python versions.

.github/scripts/ci_plan.py is what lets a documentation-only pull request skip
the backend and the frontend, and what gives a pull request into development
one Python version instead of three. Both are ways to run fewer tests, so the
decisions are pinned here case by case. test_ci_guard_list.py separately checks
that no file a test can depend on is ever classified as documentation.
"""

import importlib.util
import json
import pathlib
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLANNER = REPO_ROOT / ".github" / "scripts" / "ci_plan.py"


def _load():
    spec = importlib.util.spec_from_file_location("_ci_plan_under_test", PLANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_plan = _load()
FULL = list(ci_plan.FULL_MATRIX)


def _plan(event, base, changed, image="3.14"):
    return ci_plan.plan(event, base, changed, image_python=image)


@pytest.mark.parametrize(
    "path",
    [
        "docs/agents/ci.md",
        "docs/release-notes/v2.7.0-atlas.md",
        "docs/images/diagram.png",
        "CONTRIBUTING.md",
        "frontend/README.md",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        "site/index.html",
        "site/js/stats.js",
        "site/guides/upgrading.html",
    ],
)
def test_documentation_is_recognised(path):
    assert ci_plan.is_documentation(path)


@pytest.mark.parametrize(
    "path",
    [
        # Read by tests/bazarr/test_container_hardening.py.
        "README.md",
        "site/install.sh",
        "site/guides/getting-started.html",
        # Test input and source trees, whatever the file is called.
        "tests/bazarr/fixtures/notes.md",
        "custom_libs/subliminal_patch/providers/README.md",
        "bazarr/app/README.md",
        "frontend/src/pages/Help.md",
        # Python is code wherever it lives.
        "docs/tools/render.py",
        "site/build.py",
        # Everything that is not documentation.
        "bazarr/main.py",
        "frontend/package-lock.json",
        "requirements.txt",
        "Dockerfile",
        ".github/workflows/ci.yml",
        ".github/scripts/ci_plan.py",
        "scripts/ci/run-local.sh",
        "",
    ],
)
def test_code_and_tested_documents_are_not_documentation(path):
    assert not ci_plan.is_documentation(path)


def test_a_documentation_only_pull_request_skips_the_suites():
    decision = _plan("pull_request", "development", ["docs/a.md", "site/index.html"])
    assert decision == {"code": False, "docs": True, "python": ["3.14"]}


def test_one_code_file_runs_everything():
    decision = _plan("pull_request", "development", ["docs/a.md", "bazarr/main.py"])
    assert decision["code"] is True
    assert decision["docs"] is True


def test_a_code_only_pull_request_needs_no_docs_job():
    decision = _plan("pull_request", "development", ["bazarr/main.py"])
    assert decision == {"code": True, "docs": False, "python": ["3.14"]}


@pytest.mark.parametrize("base", ["development", "feature/stacked-on-something"])
def test_a_pull_request_not_into_master_runs_the_image_python_only(base):
    assert _plan("pull_request", base, ["bazarr/main.py"])["python"] == ["3.14"]


def test_the_release_pull_request_runs_the_full_matrix():
    assert _plan("pull_request", "master", ["bazarr/main.py"])["python"] == FULL


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch", "merge_group"])
def test_everything_but_a_pull_request_runs_the_full_matrix(event):
    decision = _plan(event, "", ["bazarr/main.py"])
    assert decision["python"] == FULL
    assert decision["code"] is True
    assert decision["docs"] is False


def test_a_documentation_only_push_skips_the_suites():
    assert _plan("push", "", ["docs/a.md"])["code"] is False


@pytest.mark.parametrize("event", ["pull_request", "push", "schedule"])
def test_an_unreadable_change_list_runs_everything(event):
    decision = _plan(event, "development", None)
    assert decision["code"] is True
    assert decision["docs"] is (event == "pull_request")


def test_an_empty_change_list_runs_everything():
    assert _plan("pull_request", "development", [])["code"] is True
    assert _plan("pull_request", "development", ["", "  "])["code"] is True


@pytest.mark.parametrize("image", ["", "3.11", "4.0"])
def test_an_image_version_outside_the_matrix_runs_the_full_matrix(image):
    assert _plan("pull_request", "development", ["bazarr/main.py"], image)["python"] == FULL


def test_the_image_python_is_read_from_the_dockerfile(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-slim AS builder\nFROM python:3.13-slim\n")
    assert ci_plan.docker_python(dockerfile) == "3.13"
    assert ci_plan.docker_python(tmp_path / "missing") == ""


def test_the_real_image_python_is_in_the_matrix():
    """Otherwise pull requests would test a version users never run."""
    assert ci_plan.docker_python() in ci_plan.FULL_MATRIX


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=ci", "-c", "user.email=ci@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "bazarr.py").write_text("one\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "one")
    first = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "page.md").write_text("two\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "two")
    monkeypatch.setattr(ci_plan, "REPO_ROOT", tmp_path)
    return first


def test_a_pull_request_diff_is_against_the_first_parent(repo):
    assert ci_plan.changed_files("pull_request", {}) == ["docs/page.md"]


def test_a_push_diff_is_against_the_previous_tip(repo):
    assert ci_plan.changed_files("push", {"before": repo}) == ["docs/page.md"]


@pytest.mark.parametrize(
    "event,payload",
    [
        ("push", {"before": "0" * 40}),
        ("push", {}),
        ("push", {"before": "not-a-sha"}),
        ("push", {"before": "1" * 40}),
        ("schedule", {}),
        ("workflow_dispatch", {}),
    ],
)
def test_a_change_list_that_cannot_be_read_is_none(repo, event, payload):
    assert ci_plan.changed_files(event, payload) is None


def test_a_shallow_pull_request_checkout_is_unreadable(tmp_path, monkeypatch):
    """With no parent commit to compare against, the planner must not guess."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs.md").write_text("x\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "only")
    monkeypatch.setattr(ci_plan, "REPO_ROOT", tmp_path)
    assert ci_plan.changed_files("pull_request", {}) is None


def test_main_writes_the_outputs_the_workflow_reads(tmp_path, monkeypatch):
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_BASE_REF", "")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "no-event.json"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert ci_plan.main() == 0
    written = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert written["code"] == "true"
    assert written["docs"] == "false"
    assert json.loads(written["python"]) == FULL
    assert "CI plan" in summary.read_text()
