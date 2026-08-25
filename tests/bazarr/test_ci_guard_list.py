# coding=utf-8
"""Guard: every backend test file must actually run in CI, or be excluded on purpose.

The workflow enumerates test paths explicitly rather than running the whole tree,
which is deliberate: some files need their own process, and some reach the
network. The cost of that design is silent rot. A contributor adds a test file,
CI never collects it, and the file's assertions protect nothing while looking
like they do. Several real defects reached users that way, including regression
tests that shipped attached to a bug fix and were never executed once.

So: a test file under tests/ is either enumerated in the workflow, or listed
below with a reason. Adding a new test file forces that choice instead of
letting the file quietly do nothing.

Exclusions are not a dumping ground. If a file is here because it is broken,
there should be a ticket to fix it, and the reason should say so.
"""

import pathlib
import re

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Directories the workflow runs wholesale, so their contents need no enumeration.
DIRECTORY_RUNS = ("tests/compat/",)

# Files deliberately kept out of CI, each with the reason. Keep sorted.
EXCLUDED = {
    # Legacy built-in provider tests that perform live HTTP against the
    # provider's real site. Non-deterministic and dependent on a third party
    # being up, so they cannot gate a merge. The built-in providers are being
    # retired in favour of catalog plugins, which are tested in their own repo.
    "tests/subliminal_patch/test_addic7ed.py": "live network call to the provider",
    "tests/subliminal_patch/test_animesubinfo.py": "live network call to the provider",
    "tests/subliminal_patch/test_hosszupuska.py": "live network call to the provider",
    "tests/subliminal_patch/test_karagarga.py": "live network call to the provider",
    "tests/subliminal_patch/test_napiprojekt.py": "live network call to the provider",
    "tests/subliminal_patch/test_subclub.py": "live network call to the provider",
    "tests/subliminal_patch/test_subdl.py": "live network call to the provider",
    "tests/subliminal_patch/test_subf2m.py": "live network call to the provider",
    "tests/subliminal_patch/test_subtitrarinoi.py": "live network call to the provider",
    "tests/subliminal_patch/test_core.py": "live network call plus environment-dependent guessit fixtures",
    "tests/subliminal_patch/test_gestdown.py": (
        "five of its cases call api.gestdown.info for real, so a provider "
        "outage would turn every pull request in the repo red. The offline "
        "cases in it are worth keeping; splitting them out would let this file "
        "be enumerated."
    ),
    # Currently failing for reasons that are not network flakiness. Each needs a
    # fix, not an exemption; the reason names what is wrong so it cannot be
    # forgotten again.
    "tests/bazarr/test_connection_tester.py": (
        "stale harness: the /test endpoint was hardened and now answers 401, so "
        "the login helper no longer bypasses auth. Fix the helper, then remove "
        "this entry."
    ),
    "tests/bazarr/test_database_sqlite_maintenance.py": (
        "asserts the SQLite maintenance pragmas are issued and observes none. "
        "Either the pragmas regressed or the test is stale; needs triage."
    ),
    # These three replay recorded HTTP, so they are deterministic, and they fail
    # for a real reason: CFSession's request override calls a cloudscraper method
    # that no longer exists in the version the image actually installs, so every
    # request through it raises AttributeError. Tracked separately; excluded here
    # only so the guard-list backfill is not blocked by an unrelated product bug.
    "tests/subliminal_patch/test_prijevodi.py": (
        "CFSession request override calls a cloudscraper method missing in the "
        "installed version; every request raises AttributeError. Product bug, "
        "tracked separately."
    ),
    "tests/subliminal_patch/test_supersubtitles.py": (
        "same CFSession AttributeError as test_prijevodi.py"
    ),
    "tests/subliminal_patch/test_titlovi.py": (
        "same CFSession AttributeError as test_prijevodi.py"
    ),
    "tests/subliminal_patch/test_video.py": (
        "Video.fromguess drops attributes the test expects, the same class of "
        "defect as the movie-edition scoring bug. Needs triage alongside it."
    ),
}


# A line whose COMMAND is pytest, allowing leading VAR=value assignments.
# Substring matching is not enough: "echo pytest ...", a step name mentioning
# pytest, or a step disabled with `if:` would all count as coverage.
_PYTEST_COMMAND = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*pytest\b")
_TEST_PATH = re.compile(r"tests/[\w./-]+\.py")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _unconditional_run_scripts() -> list:
    """Every `run:` script that always executes.

    A step carrying an `if:` is skipped deliberately. It may not run, so it
    cannot be counted as guaranteed coverage: disabling a pytest step with
    `if: false` would otherwise leave the guard green while the tests stopped.
    """
    scripts = []
    for job in (_workflow().get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if not isinstance(step, dict) or "if" in step:
                continue
            script = step.get("run")
            if isinstance(script, str):
                scripts.append(script)
    return scripts


def _paths_in_script(script: str) -> set:
    """Paths that a pytest command in this script actually receives.

    Argument lists continue while lines end in a backslash, which is how the
    workflow spells its multi-line invocations. `for f in ... ; do pytest "$f"`
    counts too, since that list is what pytest iterates.
    """
    paths = set()
    collecting = False
    for raw in script.splitlines():
        line = re.sub(r"#.*$", "", raw).strip()
        if _PYTEST_COMMAND.match(line) or line.startswith("for f in"):
            collecting = True
        if collecting:
            paths.update(_TEST_PATH.findall(line))
            if not line.endswith("\\"):
                collecting = False
    return paths


def _enumerated_paths() -> set:
    """Every tests/... path actually handed to pytest by a step that always runs."""
    paths = set()
    for script in _unconditional_run_scripts():
        paths |= _paths_in_script(script)
    return paths


def _directory_is_run(directory: str) -> bool:
    return any(
        directory in re.sub(r"#.*$", "", line)
        for script in _unconditional_run_scripts()
        for line in script.splitlines()
        if _PYTEST_COMMAND.match(re.sub(r"#.*$", "", line).strip())
    )


def _all_test_files() -> set:
    """Both default pytest filename patterns, not just the common one.

    pytest collects *_test.py as well, and nothing in this repo narrows
    python_files. A file named that way would otherwise get no CI coverage and
    no complaint from this guard.
    """
    return {
        str(path.relative_to(REPO_ROOT))
        for pattern in ("test_*.py", "*_test.py")
        for path in (REPO_ROOT / "tests").rglob(pattern)
    }


def test_workflow_exists():
    assert WORKFLOW.is_file(), f"CI workflow not found at {WORKFLOW}"


@pytest.mark.parametrize("directory", DIRECTORY_RUNS)
def test_directory_runs_are_still_in_the_workflow(directory):
    """Files under these directories are covered only because CI runs the whole
    directory. If that step is deleted, they would silently stop running while
    this guard still counted them as covered."""
    assert _directory_is_run(directory), (
        f"CI no longer runs {directory} wholesale, so the files under it are "
        "not covered. Enumerate them or restore the directory run."
    )


def test_every_test_file_runs_in_ci_or_is_excluded():
    enumerated = _enumerated_paths()
    covered = set(enumerated)
    for path in _all_test_files():
        if path.startswith(DIRECTORY_RUNS):
            covered.add(path)

    unaccounted = sorted(_all_test_files() - covered - set(EXCLUDED))

    assert not unaccounted, (
        "These test files are not run by CI and are not listed as deliberate "
        "exclusions, so their assertions protect nothing:\n  "
        + "\n  ".join(unaccounted)
        + "\n\nAdd each to .github/workflows/ci.yml, or add it to EXCLUDED in "
        "this file with the reason it cannot run."
    )


def test_exclusions_still_exist():
    """An exclusion for a deleted file is stale and hides the next mistake."""
    missing = sorted(name for name in EXCLUDED if not (REPO_ROOT / name).is_file())
    assert not missing, (
        "EXCLUDED names files that no longer exist; remove them:\n  "
        + "\n  ".join(missing)
    )


def test_exclusions_are_not_also_enumerated():
    """A file cannot be both excluded and run; that hides which one is intended."""
    enumerated = _enumerated_paths()
    both = sorted(set(EXCLUDED) & enumerated)
    assert not both, (
        "These files are excluded but also enumerated in the workflow:\n  "
        + "\n  ".join(both)
    )


@pytest.mark.parametrize("name,reason", sorted(EXCLUDED.items()))
def test_every_exclusion_states_a_reason(name, reason):
    assert reason and len(reason) > 15, f"{name} needs a real reason, got {reason!r}"
