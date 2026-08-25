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
import sys

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
# `for <var> in`, so the body check can look for that exact variable rather than
# assuming the workflow always spells it `f`.
_FOR_IN = re.compile(r"^for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")


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


def _continued_paths(lines: list, start: int) -> tuple:
    """Paths on the line at `start` plus its backslash continuations.

    Returns the paths and the index of the first line after the run, which is
    how the workflow spells its multi-line pytest invocations and loop headers.
    """
    paths = set()
    index = start
    while index < len(lines):
        line = lines[index]
        paths.update(_TEST_PATH.findall(line))
        index += 1
        if not line.endswith("\\"):
            break
    return paths, index


def _body_runs_pytest_on(lines: list, start: int, variable: str) -> bool:
    """Whether the loop body beginning at `start` hands `$variable` to pytest.

    Anything else the body does with the list runs no tests: `echo "$f"` prints
    it, and `pytest tests/other.py` tests a file the list never named.
    """
    reference = re.compile(
        r"\$(?:\{%s\}|%s(?![A-Za-z0-9_]))" % (re.escape(variable), re.escape(variable))
    )
    for line in lines[start:]:
        if re.match(r"done\b", line):
            break
        if _PYTEST_COMMAND.match(line) and reference.search(line):
            return True
    return False


def _paths_in_script(script: str) -> set:
    """Paths that a pytest command in this script actually receives.

    Argument lists continue while lines end in a backslash, which is how the
    workflow spells its multi-line invocations. `for f in ... ; do pytest "$f"`
    counts too, but only when the body really passes the iterator variable to
    pytest: a header listing twenty files proves nothing on its own, so deleting
    the one pytest line inside `do ... done` must stop the list counting.
    """
    lines = [re.sub(r"#.*$", "", raw).strip() for raw in script.splitlines()]
    paths = set()
    index = 0
    while index < len(lines):
        loop = _FOR_IN.match(lines[index])
        if loop:
            listed, index = _continued_paths(lines, index)
            if _body_runs_pytest_on(lines, index, loop.group(1)):
                paths |= listed
        elif _PYTEST_COMMAND.match(lines[index]):
            found, index = _continued_paths(lines, index)
            paths |= found
        else:
            index += 1
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


# Scripts that name a test path without ever running it. Every one of these
# shapes has been mistaken for coverage by some version of this parser, so each
# stays encoded rather than checked by hand once. Each entry is the script, the
# paths the parser must NOT count, and the paths it must count: the second half
# matters as much as the first, since a parser that returned nothing at all
# would sail through every bypass case while covering nothing.
_SCRIPT_BYPASSES = {
    "path sits in a trailing comment": (
        """
        pytest tests/bazarr/test_real.py -q  # dropped: tests/bazarr/test_commented.py
        """,
        ("tests/bazarr/test_commented.py",),
        ("tests/bazarr/test_real.py",),
    ),
    "path is echoed by a non-pytest command": (
        """
        echo tests/bazarr/test_echoed.py
        pytest tests/bazarr/test_real.py
        """,
        ("tests/bazarr/test_echoed.py",),
        ("tests/bazarr/test_real.py",),
    ),
    "pytest is an argument to echo, not the command": (
        """
        echo pytest tests/bazarr/test_announced.py
        pytest tests/bazarr/test_real.py
        """,
        ("tests/bazarr/test_announced.py",),
        ("tests/bazarr/test_real.py",),
    ),
    "loop body only echoes the file": (
        """
        set -e
        for f in \\
          tests/bazarr/test_editor_api.py \\
          tests/bazarr/test_mass_operations.py \\
          ; do
          echo "::group::$f"
          echo "$f"
          echo "::endgroup::"
        done
        pytest tests/bazarr/test_real.py
        """,
        ("tests/bazarr/test_editor_api.py", "tests/bazarr/test_mass_operations.py"),
        ("tests/bazarr/test_real.py",),
    ),
    "loop body runs pytest on an unrelated file": (
        """
        set -e
        for f in \\
          tests/bazarr/test_editor_api.py \\
          tests/bazarr/test_mass_operations.py \\
          ; do
          pytest tests/bazarr/test_something_else.py -q
        done
        """,
        ("tests/bazarr/test_editor_api.py", "tests/bazarr/test_mass_operations.py"),
        ("tests/bazarr/test_something_else.py",),
    ),
    "loop body mentions the variable but not to pytest": (
        """
        for candidate in \\
          tests/bazarr/test_editor_api.py \\
          ; do
          test -f "$candidate" || exit 1
          pytest --collect-only
        done
        pytest tests/bazarr/test_real.py
        """,
        ("tests/bazarr/test_editor_api.py",),
        ("tests/bazarr/test_real.py",),
    ),
}


@pytest.mark.parametrize(
    "case,script,ignored,counted",
    [(case,) + value for case, value in sorted(_SCRIPT_BYPASSES.items())],
)
def test_script_that_only_names_a_path_is_not_coverage(case, script, ignored, counted):
    found = _paths_in_script(script)

    wrongly_counted = sorted(found & set(ignored))
    assert not wrongly_counted, (
        f"the guard counted a path as coverage when the {case}, so CI could stop "
        "running these files while the guard stayed green:\n  "
        + "\n  ".join(wrongly_counted)
    )

    assert set(counted) <= found, (
        f"the {case} case proves nothing: the parser missed the paths that script "
        "really does hand to pytest, so it would pass even if it counted nothing"
    )


@pytest.mark.parametrize(
    "body",
    [
        'pytest "$f"',
        "pytest $f",
        'pytest -q "$f" -p no:cacheprovider',
        'PYTHONPATH=. pytest "${f}"',
    ],
)
def test_loop_list_with_pytest_on_the_variable_is_coverage(body):
    script = (
        "for f in \\\n"
        "  tests/bazarr/test_editor_api.py \\\n"
        "  ; do\n"
        f"  {body}\n"
        "done\n"
    )
    assert _paths_in_script(script) == {"tests/bazarr/test_editor_api.py"}


@pytest.fixture
def constructed_workflow(tmp_path, monkeypatch):
    """Run the guard against a workflow built in the test, not the repo's own.

    The helpers read WORKFLOW from disk on purpose, so the path itself is the
    seam: nothing test-only has to be threaded through them.
    """

    def build(workflow: dict) -> pathlib.Path:
        path = tmp_path / "ci.yml"
        path.write_text(yaml.safe_dump(workflow, sort_keys=False))
        monkeypatch.setattr(sys.modules[__name__], "WORKFLOW", path)
        return path

    return build


def _one_job(*steps: dict) -> dict:
    return {"jobs": {"backend": {"steps": list(steps)}}}


def test_path_only_in_a_step_name_is_not_coverage(constructed_workflow):
    """A step called after the file it used to run is not running it."""
    constructed_workflow(
        _one_job(
            {
                "name": "pytest tests/bazarr/test_named_only.py",
                "run": "pytest tests/bazarr/test_real.py\n",
            }
        )
    )
    enumerated = _enumerated_paths()
    assert "tests/bazarr/test_named_only.py" not in enumerated, (
        "a path in a step's name was counted as coverage, so renaming a step "
        "could keep the guard green after its pytest line was gone"
    )
    assert "tests/bazarr/test_real.py" in enumerated, "the script itself must still count"


def test_path_in_a_conditional_step_is_not_coverage(constructed_workflow):
    """`if: false` really does invoke pytest, and really never runs."""
    constructed_workflow(
        _one_job(
            {
                "name": "disabled suite",
                "if": "false",
                "run": "pytest tests/bazarr/test_disabled.py\n",
            },
            {"name": "backend", "run": "pytest tests/bazarr/test_real.py\n"},
        )
    )
    enumerated = _enumerated_paths()
    assert "tests/bazarr/test_disabled.py" not in enumerated, (
        "a step carrying an `if:` was counted as coverage, so disabling a pytest "
        "step with `if: false` would stop the tests without failing this guard"
    )
    assert "tests/bazarr/test_real.py" in enumerated, "the unconditional step must still count"


def test_deleted_directory_run_is_not_coverage(constructed_workflow):
    """The wholesale `tests/compat/` run is the only thing covering that tree."""
    constructed_workflow(_one_job({"run": "pytest tests/bazarr/test_real.py\n"}))
    assert not _directory_is_run("tests/compat/"), (
        "the directory guard reported tests/compat/ as run by a workflow that "
        "never mentions it"
    )


def test_directory_named_by_a_non_pytest_command_is_not_coverage(constructed_workflow):
    """Mentioning the directory in an echo is not running it."""
    constructed_workflow(
        _one_job({"run": 'echo "skipping tests/compat/"\npytest tests/bazarr/test_real.py\n'})
    )
    assert not _directory_is_run("tests/compat/"), (
        "an echoed directory name was taken for a wholesale run"
    )
    assert _directory_is_run("tests/bazarr/"), "a real pytest run must still count"
