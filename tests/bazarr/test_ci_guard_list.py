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

Why this reads the workflow the way it does
-------------------------------------------
Five earlier versions of this guard worked out what CI runs by recognising the
shell they understood and ignoring the rest. Every one of them was holed,
because the set of shell shapes that name a path without running it is open
ended. `pytest X || true`, `if false; then pytest X; fi`, a function body that
nothing calls, a heredoc, a line after `exit 0`, `pytest X &`,
`PYTEST_ADDOPTS=--collect-only pytest X`, `--ignore=X`, a job carrying
`if: false`, a step carrying `continue-on-error: true`: each of those read as
coverage to a parser that guesses, and each was found by a reviewer rather than
by the guard. Patching them one at a time only ever bought time until the next
shape.

This version does not guess. It refuses.

A `run:` script that mentions pytest or a tests/ path has to be built entirely
out of a small whitelisted grammar: `set`, `pip install`, `echo`, a
`for VAR in <paths>; do ... done` loop, and a pytest command. Every pytest
option has to be one the guard knows. Every workflow, job and step key on the
path to that script has to be on a safe list, which deliberately excludes `if`,
`continue-on-error` and `working-directory`. Any shell operator at all,
anything else, and the script is reported as UNVERIFIABLE and grants no
coverage whatsoever.

The consequence of a shape nobody anticipated is therefore a red build asking a
human to look at it, not a green build hiding a suite that stopped running.
That is the whole design: the failure mode is refusal, never a guess in the
permissive direction. It costs some friction when the workflow grows a new
shape, and it buys the property the five previous versions did not have.

Coverage is also grounded in the filesystem rather than in string matching. A
positional argument is expanded against the repo: `tests/compat/` covers the
files that are really under it, a glob covers what it really matches, and a
path that no longer exists covers nothing. That is why there is no list of
"directories CI runs wholesale" any more. The old list was matched as a
substring, so a single-file run, or even `--ignore=tests/compat/`, credited all
55 files under it.
"""

import pathlib
import re
import shlex
import sys

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Files deliberately kept out of CI, each with the reason. Keep sorted.
EXCLUDED = {
    # Built-in provider tests that really do reach the provider's site, so a
    # third party being down would turn every pull request in the repo red.
    # The built-in providers are being retired in favour of catalog plugins,
    # which are tested in their own repo.
    #
    # Each reason below was verified by running the file twice, once normally
    # and once inside a network namespace with no route out. A file whose two
    # runs are identical is not network-dependent, whatever it looks like.
    "tests/subliminal_patch/test_subclub.py": (
        "four vcr-marked cases have no recorded cassette, so vcr records live "
        "against subclub.eu on every run. The site currently answers 500, so "
        "they fail online as well as offline. The other seven cases are "
        "offline-safe and could be split out."
    ),
    "tests/subliminal_patch/test_subf2m.py": (
        "23 of its 29 cases hit subf2m.co for real, and each burns ten seconds "
        "of retry sleep when it is unreachable: 222s offline against 30s "
        "online. Separately, test_list_and_download_subtitles_complete_series_pack "
        "fails online too, because Episode.name has no setter."
    ),
    "tests/subliminal_patch/test_subtitrarinoi.py": (
        "two of its eight cases call subtitrari-noi.ro for real. The other six "
        "are pure get_matches checks, so splitting them out would let this file "
        "be enumerated. test_provider_download_subtitle fails online as well, "
        "with a pysubs2 FormatAutodetectionError."
    ),
    "tests/subliminal_patch/test_gestdown.py": (
        "five of its cases call api.gestdown.info for real, so a provider "
        "outage would turn every pull request in the repo red. The offline "
        "cases in it are worth keeping; splitting them out would let this file "
        "be enumerated."
    ),
    "tests/subliminal_patch/test_supersubtitles.py": (
        "all seven cases reach www.feliratok.eu for real over a plain "
        "requests.Session, with no cassettes and no requests_mock anywhere in "
        "the file: 7 passed online, 6 failed offline. A feliratok.eu outage "
        "would turn every pull request in the repo red."
    ),
    # Not network-dependent at all, despite what they look like. These were
    # previously all labelled "live network call to the provider", which was
    # wrong and hid four different real problems. Each needs a fix, not an
    # exemption.
    "tests/subliminal_patch/test_addic7ed.py": (
        "skipped in full, so it protects nothing: a module-level skipif needs "
        "ADDIC7ED_USERNAME and ADDIC7ED_PASSWORD plus two ANTICAPTCHA "
        "variables, and CI sets none of them. Both cases would go live if it did."
    ),
    "tests/subliminal_patch/test_karagarga.py": (
        "skipped in full: a module-level skipif needs KARAGARGA_USER and "
        "KARAGARGA_PASSWORD, which CI does not set, so all eight cases skip. "
        "Two of them need no network and could be split out. Note the "
        "import-time region.configure(), which is a co-collection hazard."
    ),
    "tests/subliminal_patch/test_subdl.py": (
        "errors before it reaches a socket: the session fixture reads "
        "os.environ['SUBDL_TOKEN'], which CI does not set, so both cases error "
        "with KeyError at setup. Red in CI, not skipped, and not flaky."
    ),
    "tests/subliminal_patch/test_animesubinfo.py": (
        "replays recorded HTTP and touches no network: identical results online "
        "and inside a network namespace. Three of its seven cases fail because "
        "the cassettes only recorded the pTitle=org query while the provider "
        "now issues org, en and pl. Two of the four that pass do so vacuously, "
        "asserting only isinstance(subs, list)."
    ),
    "tests/subliminal_patch/test_hosszupuska.py": (
        "hosszupuskasub.com is dead, so the provider is being retired. Until "
        "that lands: replays recorded HTTP, touches no network, and both cases "
        "fail because the recorded page now yields adf.ly-wrapped links while "
        "the assertions expect bare download URLs."
    ),
    "tests/subliminal_patch/test_napiprojekt.py": (
        "replays recorded HTTP and touches no network, but subliminal 2.6.0 "
        "moved server_url to https while the cassettes were recorded against "
        "http, and vcrpy matches on scheme and port, so every request misses. "
        "Both cases also burn 30 seconds each of ProviderRetryMixin sleep."
    ),
    "tests/subliminal_patch/test_core.py": (
        "no network at all: 8 failed, 14 passed in 0.16s, identical online and "
        "offline. Four pool tests compare SZProviderPool.providers against a "
        "set, but it became an ordered list in af95a58bf. Three language-equals "
        "tests build the pool with an empty opensubtitlescom config, so it "
        "raises ConfigurationError before any request. test_scan_video_episode "
        "depends on the pytest tmpdir counter: guessit parses the parent "
        "directory, and from pytest-10 onward it drops the title and subliminal "
        "raises GuessingError, so it passes on a fresh runner and fails on any "
        "machine that has run pytest more than ten times."
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
    # These two replay recorded HTTP, so they are deterministic, and they fail
    # for a real reason: CFSession's request override calls a cloudscraper method
    # that no longer exists in the version the image actually installs, so every
    # request through it raises AttributeError. Tracked separately; excluded here
    # only so the guard-list backfill is not blocked by an unrelated product bug.
    # test_supersubtitles.py used to sit here too, wrongly: its provider never
    # touches CFSession, and lifting these two must not lift that one.
    "tests/subliminal_patch/test_prijevodi.py": (
        "CFSession request override calls a cloudscraper method missing in the "
        "installed version; every request raises AttributeError. Product bug, "
        "tracked separately."
    ),
    "tests/subliminal_patch/test_titlovi.py": (
        "same CFSession AttributeError as test_prijevodi.py"
    ),
    "tests/subliminal_patch/test_video.py": (
        "three failures, none of them a network problem. "
        "test_video_fromguess_episode and test_video_fromguess_movie both raise "
        "subliminal.exceptions.GuessingError, 'Insufficient data to process the "
        "guess', before Video.fromguess sets any attribute at all. "
        "test_video_fromname_movie is the only dropped attribute, and it is "
        "dropped by Video.fromname, not fromguess. Needs triage."
    ),
}


# ---------------------------------------------------------------------------
# The whitelisted shell grammar.
#
# Everything below exists to say no. A construct that is not named here does not
# get interpreted charitably, it gets reported, so the guard cannot be holed by
# a shape nobody thought of.
# ---------------------------------------------------------------------------

# `for VAR in`, so the body check can look for that exact variable rather than
# assuming the workflow always spells it `f`.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A leading VAR=value assignment on a command, as in `PYTHONPATH=. pytest ...`.
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
# Whether a script has anything to do with running tests at all. Scripts that do
# not are left alone: `ruff check .` and `npm run build` are not this guard's
# business, and holding them to a shell grammar would be pointless friction.
_MENTIONS_TESTS = re.compile(r"(?<![\w./-])pytest\b|(?<![\w./-])tests/")
# `set -e`, and nothing more elaborate.
_SET_FLAGS = re.compile(r"^[-+][A-Za-z]+$")

# Options pytest takes that consume the following token. A test path sitting in
# one of these positions is an argument to the option, not something pytest
# collects, so it must never be read as coverage.
_PYTEST_VALUE_OPTIONS = {
    "-p", "-k", "-m", "-o", "-c", "-n", "-W", "-r",
    "--tb", "--rootdir", "--basetemp", "--color", "--durations", "--maxfail",
    "--log-level", "--junitxml", "--override-ini", "--import-mode",
    "--ignore", "--ignore-glob", "--deselect",
}
# Options that take no value.
_PYTEST_FLAGS = {
    "-v", "-vv", "-vvv", "-q", "-qq", "-s", "-x", "-l", "-ra", "-rA", "-rf",
    "-rN", "--verbose", "--quiet", "--exitfirst", "--showlocals",
    "--full-trace", "--no-header", "--no-summary", "--disable-warnings",
    "--strict-markers", "--strict-config", "--continue-on-collection-errors",
    "--collect-only", "--co", "--setup-only", "--setup-plan", "--fixtures",
    "--markers", "--help", "-h",
}
# Options whose paths pytest is told NOT to collect. They subtract.
_PYTEST_SUBTRACTING = {"--ignore", "--ignore-glob", "--deselect"}
# Options that stop pytest asserting anything, or narrow it to an arbitrary
# subset. A command carrying one of these proves nothing about coverage.
_PYTEST_NEUTERING = {
    "--collect-only", "--co", "--setup-only", "--setup-plan", "--fixtures",
    "--markers", "--help", "-h", "-k", "-m",
}

# Workflow, job and step keys the guard has reasoned about. `if`,
# `continue-on-error` and `working-directory` are absent on purpose: the first
# two decide whether a step's result binds, the third changes what its paths
# mean. Anything else absent is absent because nobody has reasoned about it yet,
# which is exactly when the guard should stop rather than assume.
_SAFE_WORKFLOW_KEYS = {"name", "run-name", "on", True, "permissions", "env", "jobs"}
_SAFE_JOB_KEYS = {
    "name", "runs-on", "needs", "permissions", "strategy", "services", "steps",
    "env", "timeout-minutes",
}
_SAFE_STEP_KEYS = {"name", "id", "run", "env", "shell", "timeout-minutes"}
# Shells whose semantics the grammar below describes.
_SAFE_SHELLS = {"bash", "sh", "bash -e {0}"}

# Every shell operator, all of them refused inside a test-bearing script. Each
# one is a way for a command to name a path without running it, or to run it
# without its exit status binding.
_SHELL_OPERATORS = {
    "|", "||", "&", "&&", ";", ";;", "(", ")", "()", "<", ">", "<<", ">>", "<&", ">&",
}


class _Unverifiable(Exception):
    """The guard cannot prove what this construct does, so it grants nothing.

    Raised rather than swallowed: an unverifiable step has to reach a human, or
    the guard is back to guessing.
    """


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _tokenize(line: str) -> list:
    """Shell words plus separated operator tokens, so `||` cannot hide in a word.

    shlex with punctuation_chars splits `();<>|&` off as their own tokens while
    leaving quoted text alone, which is what lets the grammar below reject every
    operator without also rejecting `echo "a|b"`.
    """
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as error:  # unbalanced quote, unterminated expansion
        raise _Unverifiable(f"cannot tokenize {line!r}: {error}")


def _logical_lines(script: str) -> list:
    """Backslash continuations joined, whole-line comments dropped.

    Trailing comments are left to the tokenizer, which knows what is quoted.
    """
    lines = []
    pending = ""
    for raw in script.splitlines():
        stripped = raw.strip()
        if not pending and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        lines.append(pending + stripped)
        pending = ""
    if pending:
        lines.append(pending)
    return [line for line in lines if line.strip()]


def _reject_operators(tokens: list, line: str, allow_semicolon: bool = False) -> None:
    permitted = {";"} if allow_semicolon else set()
    for token in tokens:
        if token in _SHELL_OPERATORS and token not in permitted:
            raise _Unverifiable(
                f"shell operator {token!r} in {line!r}: the guard cannot tell "
                "whether the command runs, or whether its exit status binds"
            )
        if "`" in token:
            raise _Unverifiable(f"command substitution in {line!r}")
        if "${{" in token:
            raise _Unverifiable(
                f"workflow expression in {line!r}: it can expand to anything, so "
                "the guard cannot read the command it produces"
            )


def _expand(token: str) -> set:
    """The repo-relative test files a pytest path argument really names.

    Grounded in the filesystem rather than in string matching: a directory
    covers what is under it, a glob covers what it matches, and a path that no
    longer exists covers nothing.
    """
    path = token.split("::", 1)[0]
    if not path or path.startswith("-"):
        return set()
    if path.startswith("/") or ".." in pathlib.PurePosixPath(path).parts:
        raise _Unverifiable(f"path {token!r} escapes the repository")
    glob = any(char in path for char in "*?[")
    matches = sorted(REPO_ROOT.glob(path)) if glob else [REPO_ROOT / path]
    found = set()
    for match in matches:
        if match.is_dir():
            relative = match.relative_to(REPO_ROOT).as_posix()
            prefix = "" if relative == "." else relative + "/"
            found |= {name for name in _all_test_files() if name.startswith(prefix)}
        elif match.is_file() and match.suffix == ".py":
            found.add(match.relative_to(REPO_ROOT).as_posix())
        elif not glob and re.fullmatch(r"tests/.*\.py", path):
            raise _Unverifiable(
                f"{token!r} names a test file that does not exist, so it runs "
                "nothing. Remove it, or restore the file"
            )
    return found


def _pytest_command(tokens: list, line: str) -> tuple:
    """(files this command collects, its positional tokens, files it ignores).

    Every option has to be one of the known sets. An unknown one is refused
    rather than skipped, because skipping it is how `--ignore=` came to count as
    coverage: the guard had no idea the flag before a path changed its meaning.
    """
    positional = []
    subtracted = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("-") or token == "-":
            positional.append(token)
            continue
        name, separator, inline = token.partition("=")
        if name in _PYTEST_NEUTERING:
            raise _Unverifiable(
                f"{name} in {line!r} stops pytest asserting anything, or narrows "
                "it to an arbitrary subset, so the command proves no coverage"
            )
        if name in _PYTEST_VALUE_OPTIONS:
            if separator:
                value = inline
            elif index < len(tokens):
                value = tokens[index]
                index += 1
            else:
                raise _Unverifiable(f"{name} in {line!r} has no value")
            if name in _PYTEST_SUBTRACTING:
                subtracted |= _expand(value)
            continue
        if name in _PYTEST_FLAGS and not separator:
            continue
        raise _Unverifiable(
            f"pytest option {token!r} in {line!r} is not one the guard knows. "
            "Add it to _PYTEST_FLAGS, _PYTEST_VALUE_OPTIONS or _PYTEST_NEUTERING "
            "once you have decided whether it changes what runs"
        )
    collected = set()
    for token in positional:
        collected |= _expand(token)
    return collected - subtracted, positional, subtracted


def _strip_assignments(tokens: list, line: str) -> list:
    """Drop leading VAR=value assignments, refusing any that neuter pytest."""
    index = 0
    while index < len(tokens):
        match = _ASSIGNMENT.match(tokens[index])
        if not match:
            break
        if match.group(1).startswith("PYTEST_"):
            raise _Unverifiable(
                f"{match.group(1)} is set on {line!r}; it can silence pytest "
                "while the command still looks like it runs the tests"
            )
        index += 1
    return tokens[index:]


def _command_words(tokens: list) -> list:
    """`python -m pytest` and `python3 -m pytest` read as `pytest`."""
    if len(tokens) >= 3 and re.fullmatch(r"python3?", tokens[0]) and tokens[1] == "-m":
        return [tokens[2]] + tokens[3:]
    return tokens


def _script_coverage(script: str) -> set:
    """Test files this `run:` script really hands to pytest.

    Raises _Unverifiable for anything outside the grammar, which is the whole
    point: refusing is safe, guessing is what kept holing the previous versions.
    """
    if not _MENTIONS_TESTS.search(re.sub(r"#.*$", "", script, flags=re.MULTILINE)):
        return set()

    lines = _logical_lines(script)
    covered = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        tokens = _command_words(_strip_assignments(_tokenize(line), line))
        if not tokens:
            continue
        head = tokens[0]

        if head == "set" and len(tokens) == 2 and _SET_FLAGS.match(tokens[1]):
            continue
        if head in {"pip", "pip3"} and tokens[1:2] == ["install"]:
            _reject_operators(tokens, line)
            continue
        if head == "echo":
            _reject_operators(tokens, line)
            continue
        if head == "pytest":
            _reject_operators(tokens, line)
            collected, _, _ = _pytest_command(tokens[1:], line)
            covered |= collected
            continue
        if head == "for":
            listed, variable = _for_header(tokens, line)
            index = _skip_optional_do(lines, index)
            index, body_covered, subtracted, runs = _loop_body(lines, index, variable)
            covered |= body_covered
            if runs:
                covered |= listed - subtracted
            continue
        raise _Unverifiable(
            f"{line!r} is not a command the guard understands. Test-bearing "
            "steps must use only `set`, `pip install`, `echo`, "
            "`for VAR in <paths>; do ... done` and pytest, so that what runs is "
            "readable without interpreting shell"
        )
    return covered


def _for_header(tokens: list, line: str) -> tuple:
    """(files the list names, loop variable). The list must be test paths only."""
    _reject_operators(tokens, line, allow_semicolon=True)
    if len(tokens) < 4 or tokens[2] != "in" or not _IDENTIFIER.match(tokens[1]):
        raise _Unverifiable(f"{line!r} is not a `for VAR in ...` header")
    if "do" in tokens and tokens.index("do") != len(tokens) - 1:
        raise _Unverifiable(
            f"{line!r} puts the loop body on the header line. Spread the loop "
            "over several lines so the guard can read the body that runs pytest"
        )
    words = [token for token in tokens[3:] if token not in {";", "do"}]
    if not words:
        raise _Unverifiable(f"{line!r} iterates over nothing")
    listed = set()
    for word in words:
        found = _expand(word)
        if not found:
            raise _Unverifiable(
                f"{word!r} in {line!r} names no test file, so the guard cannot "
                "tell what the loop runs"
            )
        listed |= found
    return listed, tokens[1]


def _skip_optional_do(lines: list, index: int) -> int:
    if index < len(lines) and lines[index].strip() == "do":
        return index + 1
    return index


def _loop_body(lines: list, index: int, variable: str) -> tuple:
    """(index after `done`, files the body runs outright, files it ignores, runs $VAR).

    The body has to hand `$VAR` to pytest as a positional argument. A header
    listing twenty files proves nothing on its own: `echo "$f"` prints them, and
    `pytest tests/other.py` tests a file the list never named.
    """
    reference = {"$" + variable, "${" + variable + "}"}
    body_covered = set()
    subtracted = set()
    runs = False
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip() == "done":
            return index, body_covered, subtracted, runs
        tokens = _command_words(_strip_assignments(_tokenize(line), line))
        if not tokens:
            continue
        _reject_operators(tokens, line)
        if tokens[0] == "echo":
            continue
        if tokens[0] == "pytest":
            collected, positional, ignored = _pytest_command(tokens[1:], line)
            if reference & set(positional):
                runs = True
            body_covered |= collected
            subtracted |= ignored
            continue
        raise _Unverifiable(
            f"{line!r} inside a loop body is not a command the guard understands"
        )
    raise _Unverifiable("a `for` loop is never closed by `done`")


# ---------------------------------------------------------------------------
# Reading the workflow. Scopes that can stop a step counting are refused here,
# before any shell is looked at.
# ---------------------------------------------------------------------------


def _scope_problem(kind: str, name: str, scope: dict, safe_keys: set) -> str:
    if not isinstance(scope, dict):
        return f"{kind} {name} is not a mapping"
    unknown = sorted(str(key) for key in scope if key not in safe_keys)
    if unknown:
        return (
            f"{kind} {name} uses {', '.join(unknown)}, which the guard has not "
            "reasoned about. `if` and `continue-on-error` decide whether the "
            "step's result binds, `working-directory` changes what its paths "
            "mean. Decide which it is, then add the key to the safe list"
        )
    neutering = sorted(
        str(key) for key in (scope.get("env") or {}) if str(key).startswith("PYTEST_")
    )
    if neutering:
        return (
            f"{kind} {name} sets {', '.join(neutering)}, which can silence pytest "
            "while every command still looks like it runs the tests"
        )
    shell = scope.get("shell")
    if shell is not None and shell not in _SAFE_SHELLS:
        return f"{kind} {name} runs under shell {shell!r}, whose semantics the guard does not model"
    return ""


def _read_workflow() -> tuple:
    """(files CI really runs, problems that stop the guard vouching for a step)."""
    workflow = _workflow()
    problems = []
    covered = set()

    top = _scope_problem("workflow", WORKFLOW.name, workflow, _SAFE_WORKFLOW_KEYS)
    if top:
        return covered, [top]

    for job_name, job in (workflow.get("jobs") or {}).items():
        steps = (job or {}).get("steps") if isinstance(job, dict) else None
        bearing = []
        for position, step in enumerate(steps or []):
            script = step.get("run") if isinstance(step, dict) else None
            if isinstance(script, str) and _MENTIONS_TESTS.search(
                re.sub(r"#.*$", "", script, flags=re.MULTILINE)
            ):
                bearing.append((position, step, script))
        if not bearing:
            continue

        job_problem = _scope_problem("job", job_name, job, _SAFE_JOB_KEYS)
        if job_problem:
            problems.append(job_problem)
            continue

        for position, step, script in bearing:
            label = f"{job_name!r} step {step.get('name') or position!r}"
            step_problem = _scope_problem("step", label, step, _SAFE_STEP_KEYS)
            if step_problem:
                problems.append(step_problem)
                continue
            try:
                covered |= _script_coverage(script)
            except _Unverifiable as refusal:
                problems.append(f"{label}: {refusal}")

    return covered, problems


def _enumerated_paths() -> set:
    return _read_workflow()[0]


def _all_test_files() -> set:
    """Both default pytest filename patterns, not just the common one.

    pytest collects *_test.py as well, and nothing in this repo narrows
    python_files. A file named that way would otherwise get no CI coverage and
    no complaint from this guard.
    """
    return {
        path.relative_to(REPO_ROOT).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for path in (REPO_ROOT / "tests").rglob(pattern)
    }


# ---------------------------------------------------------------------------
# The guard itself.
# ---------------------------------------------------------------------------


def test_workflow_exists():
    assert WORKFLOW.is_file(), f"CI workflow not found at {WORKFLOW}"


def test_every_test_bearing_step_is_verifiable():
    """The guard must be able to say what each pytest step runs, or say nothing.

    This is the check that closes the class of bug the earlier versions kept
    hitting. When CI grows a shape the grammar does not cover, this fails and
    asks for a decision, instead of quietly reading the shape as coverage.
    """
    problems = _read_workflow()[1]
    assert not problems, (
        "The guard cannot verify what these parts of the workflow run, so it "
        "grants them no coverage:\n  " + "\n  ".join(problems)
    )


def test_every_test_file_runs_in_ci_or_is_excluded():
    covered, problems = _read_workflow()
    unaccounted = sorted(_all_test_files() - covered - set(EXCLUDED))

    message = (
        "These test files are not run by CI and are not listed as deliberate "
        "exclusions, so their assertions protect nothing:\n  "
        + "\n  ".join(unaccounted)
        + "\n\nEnumerate each as a positional pytest argument in "
        ".github/workflows/ci.yml, or add it to EXCLUDED in this file with the "
        "reason it cannot run. A path only counts when pytest is handed it to "
        "collect: naming it in a comment, a step name, an --ignore= argument or "
        "a step that may not run is not coverage."
    )
    if problems:
        message += (
            "\n\nSome of these may already be enumerated in a step the guard "
            "refused to read:\n  " + "\n  ".join(problems)
        )
    assert not unaccounted, message


def test_exclusions_still_exist():
    """An exclusion for a deleted file is stale and hides the next mistake."""
    missing = sorted(name for name in EXCLUDED if not (REPO_ROOT / name).is_file())
    assert not missing, (
        "EXCLUDED names files that no longer exist; remove them:\n  "
        + "\n  ".join(missing)
    )


def test_exclusions_are_not_also_enumerated():
    """A file cannot be both excluded and run; that hides which one is intended."""
    both = sorted(set(EXCLUDED) & _enumerated_paths())
    assert not both, (
        "These files are excluded but also enumerated in the workflow:\n  "
        + "\n  ".join(both)
    )


@pytest.mark.parametrize("name,reason", sorted(EXCLUDED.items()))
def test_every_exclusion_states_a_reason(name, reason):
    assert reason and len(reason) > 15, f"{name} needs a real reason, got {reason!r}"


# ---------------------------------------------------------------------------
# Shapes that name a test path without running it, or without its result
# binding. Every one of these was mistaken for coverage by some version of this
# parser, so each stays encoded rather than checked by hand once.
#
# `refused` means the guard must raise rather than return a smaller set: a
# script it cannot read must reach a human, not be silently discounted.
# ---------------------------------------------------------------------------

_REAL = "tests/bazarr/test_pretty_date.py"
_OTHER = "tests/bazarr/test_ui.py"

_SCRIPT_BYPASSES = {
    "path sits in a trailing comment": (
        f"pytest {_REAL} -q  # dropped: {_OTHER}\n",
        {_REAL},
    ),
    "path is echoed by a non-pytest command": (
        f"echo {_OTHER}\npytest {_REAL}\n",
        {_REAL},
    ),
    "pytest is an argument to echo, not the command": (
        f"echo pytest {_OTHER}\npytest {_REAL}\n",
        {_REAL},
    ),
    "loop body only echoes the file": (
        "set -e\n"
        f"for f in \\\n  {_OTHER} \\\n  ; do\n"
        '  echo "::group::$f"\n  echo "$f"\n  echo "::endgroup::"\ndone\n'
        f"pytest {_REAL}\n",
        {_REAL},
    ),
    "loop body runs pytest on an unrelated file": (
        "set -e\n"
        f"for f in \\\n  {_OTHER} \\\n  ; do\n  pytest {_REAL} -q\ndone\n",
        {_REAL},
    ),
    "path is only an --ignore argument": (
        f"pytest {_REAL} --ignore={_OTHER}\n",
        {_REAL},
    ),
    "path is only a --deselect argument": (
        f"pytest {_REAL} --deselect={_OTHER}\n",
        {_REAL},
    ),
    "path is the value of a separated --ignore": (
        f"pytest {_REAL} --ignore {_OTHER}\n",
        {_REAL},
    ),
    "directory run is narrowed to one file": (
        "pytest tests/compat/contract/test_plugin_contract.py\n",
        {"tests/compat/contract/test_plugin_contract.py"},
    ),
    "directory is only an --ignore argument": (
        f"pytest {_REAL} --ignore=tests/compat/\n",
        {_REAL},
    ),
}

_SCRIPT_REFUSALS = {
    "failure is masked with || true": f"pytest {_REAL} || true\n",
    "command sits in a branch that is never taken": f"if false; then\n  pytest {_REAL}\nfi\n",
    "command sits in a function nothing calls": (
        f"run_them() {{\n  pytest {_REAL}\n}}\necho done\n"
    ),
    "command sits in a heredoc body": f"cat <<EOF\npytest {_REAL}\nEOF\n",
    "command sits after exit 0": f"exit 0\npytest {_REAL}\n",
    "command is backgrounded": f"pytest {_REAL} &\n",
    "command is piped into something else": f"pytest {_REAL} | tee log.txt\n",
    "output is redirected away": f"pytest {_REAL} > /dev/null\n",
    "path is produced by command substitution": f"pytest $(echo {_REAL})\n",
    "pytest is neutered inline": f'PYTEST_ADDOPTS="--collect-only" pytest {_REAL}\n',
    "pytest is asked only to collect": f"pytest --collect-only {_REAL}\n",
    "pytest is narrowed by an arbitrary -k filter": f"pytest {_REAL} -k nothing_matches\n",
    "an unknown option might change what runs": f"pytest {_REAL} --some-new-flag\n",
    "the command is reached through another tool": f"echo {_REAL} | xargs pytest\n",
    "the directory is changed first": f"cd . && pytest {_REAL}\n",
    "a loop iterates over something that is not a test path": (
        'for f in one two ; do\n  pytest "$f"\ndone\n'
    ),
    "a loop is written on one line": (
        'for f in tests/bazarr/test_pretty_date.py; do pytest "$f"; done\n'
    ),
    "the path names a test file that was deleted": (
        "pytest tests/bazarr/test_deleted_in_some_other_branch.py\n"
    ),
}


@pytest.mark.parametrize(
    "case,script,expected",
    [(case,) + value for case, value in sorted(_SCRIPT_BYPASSES.items())],
)
def test_script_that_only_names_a_path_is_not_coverage(case, script, expected):
    """The path the script really runs counts, the one it only names does not.

    Both halves matter: a parser that returned nothing at all would sail through
    every bypass case while vouching for nothing.
    """
    assert _script_coverage(script) == expected, (
        f"when the {case}, the guard read the wrong set of files as covered, so "
        "CI could stop running a file while the guard stayed green"
    )


@pytest.mark.parametrize("case,script", sorted(_SCRIPT_REFUSALS.items()))
def test_script_the_guard_cannot_read_is_refused(case, script):
    """Refusal, not a quiet zero: an unreadable step has to reach a human.

    Returning an empty set here would be safe for coverage and useless in
    practice, because the workflow would go red with no idea why.
    """
    with pytest.raises(_Unverifiable):
        _script_coverage(script)


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
    script = f"for f in \\\n  {_REAL} \\\n  ; do\n  {body}\ndone\n"
    assert _script_coverage(script) == {_REAL}


def test_wholesale_directory_run_covers_the_tree():
    """`pytest tests/compat/` is the only thing covering those 55 files."""
    covered = _script_coverage("pytest tests/compat/ -v --tb=short\n")
    under = {name for name in _all_test_files() if name.startswith("tests/compat/")}
    assert under and covered == under


def test_a_single_file_does_not_credit_its_whole_directory():
    """Deliberately the opposite of what the guard used to assert.

    Until this commit the directory check was a substring match, so
    `pytest tests/compat/test_one.py`, and even
    `pytest tests/bazarr/ --ignore=tests/compat/`, credited every file under
    tests/compat/. That is 55 files, 30% of the suite, vouched for by a run that
    executed one of them or none.
    """
    one = "tests/compat/contract/test_plugin_contract.py"
    assert _script_coverage(f"pytest {one}\n") == {one}

    ignored = _script_coverage(f"pytest {_REAL} --ignore=tests/compat/\n")
    assert not {name for name in ignored if name.startswith("tests/compat/")}


def test_directory_named_by_a_non_pytest_command_is_not_coverage():
    """Mentioning the directory in an echo is not running it."""
    covered = _script_coverage(f'echo "skipping tests/compat/"\npytest {_REAL}\n')
    assert covered == {_REAL}


# ---------------------------------------------------------------------------
# Workflow-level shapes: things that stop a step counting before its shell is
# even read.
# ---------------------------------------------------------------------------


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


def _one_job(*steps: dict, **job) -> dict:
    return {"jobs": {"backend": dict(job, steps=list(steps))}}


def test_path_only_in_a_step_name_is_not_coverage(constructed_workflow):
    """A step called after the file it used to run is not running it."""
    constructed_workflow(
        _one_job({"name": f"pytest {_OTHER}", "run": f"pytest {_REAL}\n"})
    )
    covered, problems = _read_workflow()
    assert covered == {_REAL}, "a path in a step's name was counted as coverage"
    assert not problems


@pytest.mark.parametrize(
    "case,step",
    [
        ("if", {"if": "false", "run": f"pytest {_OTHER}\n"}),
        ("continue-on-error", {"continue-on-error": True, "run": f"pytest {_OTHER}\n"}),
        ("working-directory", {"working-directory": "tests", "run": f"pytest {_OTHER}\n"}),
        ("PYTEST_ADDOPTS", {"env": {"PYTEST_ADDOPTS": "--collect-only"}, "run": f"pytest {_OTHER}\n"}),
    ],
)
def test_step_that_may_not_bind_is_not_coverage(constructed_workflow, case, step):
    """`if: false` really does invoke pytest, and really never runs.

    `continue-on-error: true` really does run it, and its failures really cannot
    fail the workflow, which is the same thing from the merge gate's point of
    view. `working-directory:` changes what the paths mean.
    `PYTEST_ADDOPTS=--collect-only` leaves pytest asserting nothing.
    """
    constructed_workflow(_one_job(step, {"run": f"pytest {_REAL}\n"}))
    covered, problems = _read_workflow()
    assert _OTHER not in covered, f"a step carrying {case} was counted as coverage"
    assert _REAL in covered, "the ordinary step must still count"
    assert any(case in problem for problem in problems), (
        f"the guard dropped the {case} step without saying so, which would leave "
        "a contributor with no idea why their file is reported as unrun"
    )


@pytest.mark.parametrize(
    "case,job",
    [
        ("if", {"if": "false"}),
        ("continue-on-error", {"continue-on-error": True}),
        ("PYTEST_ADDOPTS", {"env": {"PYTEST_ADDOPTS": "--collect-only"}}),
    ],
)
def test_job_that_may_not_bind_is_not_coverage(constructed_workflow, case, job):
    """Moving one `if:` up one level used to defeat the whole guard.

    The previous version filtered steps and never looked at the job, so a job
    gated `if: false` still contributed every path it named as guaranteed
    coverage.
    """
    constructed_workflow(_one_job({"run": f"pytest {_REAL}\n"}, **job))
    covered, problems = _read_workflow()
    assert not covered, f"a job carrying {case} was counted as coverage"
    assert any(case in problem for problem in problems)


def test_workflow_level_pytest_addopts_is_not_coverage(constructed_workflow):
    workflow = _one_job({"run": f"pytest {_REAL}\n"})
    workflow["env"] = {"PYTEST_ADDOPTS": "--collect-only"}
    constructed_workflow(workflow)
    covered, problems = _read_workflow()
    assert not covered and problems


def test_unknown_job_key_stops_the_guard_vouching(constructed_workflow):
    """A key nobody has reasoned about is refused, not assumed harmless.

    This is the check that is meant to survive the next GitHub Actions feature:
    the guard cannot be holed by a key it has never heard of, because it stops
    at one.
    """
    constructed_workflow(_one_job({"run": f"pytest {_REAL}\n"}, **{"some-new-key": 1}))
    covered, problems = _read_workflow()
    assert not covered
    assert any("some-new-key" in problem for problem in problems)


def test_unknown_step_key_stops_the_guard_vouching(constructed_workflow):
    constructed_workflow(
        _one_job({"run": f"pytest {_REAL}\n", "some-new-key": 1}, {"run": f"pytest {_OTHER}\n"})
    )
    covered, problems = _read_workflow()
    assert covered == {_OTHER}
    assert any("some-new-key" in problem for problem in problems)


def test_non_test_steps_are_left_alone(constructed_workflow):
    """Steps that run no tests are not this guard's business.

    Holding `npm run build` or a conditional deploy step to a shell grammar
    would be friction with no safety behind it.
    """
    constructed_workflow(
        _one_job(
            {"if": "github.ref == 'refs/heads/master'", "run": "npm run build && ./deploy.sh\n"},
            {"uses": "actions/checkout@v7"},
            {"run": f"pytest {_REAL}\n"},
        )
    )
    covered, problems = _read_workflow()
    assert covered == {_REAL}
    assert not problems
