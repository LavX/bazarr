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
    "tests/subliminal_patch/test_video.py": (
        "Video.fromguess drops attributes the test expects, the same class of "
        "defect as the movie-edition scoring bug. Needs triage alongside it."
    ),
}


def _enumerated_paths() -> set:
    """Every tests/... path named anywhere in the workflow."""
    return set(re.findall(r"tests/[\w./-]+\.py", WORKFLOW.read_text()))


def _all_test_files() -> set:
    return {
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "tests").rglob("test_*.py")
    }


def test_workflow_exists():
    assert WORKFLOW.is_file(), f"CI workflow not found at {WORKFLOW}"


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
