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

Later audits found five more of exactly that kind, all of which left the guard
green: a step that mentions neither pytest nor tests/ writing
`PYTEST_ADDOPTS=--collect-only` into `$GITHUB_ENV`, which GitHub then applies to
every later step in the job; `if: false` on a job that runs no tests but that
the test job waits on, because a skipped dependency skips everything downstream
and a skipped job fails nothing; `on.pull_request` narrowed by a `paths:` filter
that a code change never matches, which stops the whole workflow without
changing a line the guard reads; `-p neuter`, a whitelisted option whose value
nobody inspected; and this file itself being dropped from the workflow and added
to its own exclusion list, which switched off every check here in two edits.

This version does not guess. It refuses.

A `run:` script that mentions pytest or a tests/ path has to be built entirely
out of a small whitelisted grammar: `set`, `pip install`, `echo`, `printf`,
`ruff`, `cd .`, a `for VAR in <paths>; do ... done` loop, and a pytest command.
Every pytest option has to be one the guard knows. Every workflow, job and step
key on the path to that script has to be either on a safe list or read: `if`,
`continue-on-error`, `working-directory`, `shell`, `defaults`, `runs-on`,
`strategy` and `services` are all read, because each of them decides something
with its value rather than with its presence. Any shell operator at all,
anything else, and the script is reported as UNVERIFIABLE and grants no
coverage whatsoever.

Refusal has to be applied all the way down, not only to the shapes that look
dangerous. `set` takes -e, -u, -x and -o pipefail, and nothing else, because
`set -n` makes bash read a whole script without executing a line of it and
still exit 0, and `set +e` lets a failing pytest leave the step green.

And refusal has to stop there, which is the other half of the design and the
half a later audit found missing. Ten of eighteen ordinary maintainer edits
were refused: `set -euo pipefail`, which is stricter than the `set -e` the
guard accepted; `--cov=bazarr`, which cannot drop a test; a workflow-level
`concurrency:` group, which emptied coverage entirely and printed all 168 test
files as unrun. A guard that goes red on routine, correct work gets deleted by
the first person it obstructs, and a deleted guard reopens every hole above at
once, so the friction is not a side effect of the safety, it is a threat to it.
The rule is therefore: refuse a shape that can stop tests running, skip them,
deselect them, or mask an exit code. Accept a key or an option that is inert
with respect to whether assertions execute. Where the answer depends on the
value, read the value. TestOrdinaryEditsStayGreen holds that line from the
other side, and it is as load-bearing as the bypass cases.

A whitelisted option whose value nobody inspects is as good as no whitelist at
all, so `-o`, `--override-ini` and `-c` are refused outright: each can carry
`addopts=--collect-only`, which is the PYTEST_ADDOPTS hole with a different
spelling. `--ignore-glob` is refused because pytest matches it recursively
during collection while a filesystem glob does not, so the guard would subtract
far less than pytest really skips.

A `file.py::case` node selector is refused for the same reason: it exits 0
having run one case, which says nothing about the rest of the module, and
coverage here is counted per file.

And `$(`, backticks and parameter expansions are refused inside a token, not
only as standalone punctuation, because quoting hides them from the tokenizer:
shlex keeps `"$(printf '%s' --collect-only)"` as one word, so no operator check
sees it, and bash expands it before pytest starts.

The consequence of a shape nobody anticipated is therefore a red build asking a
human to look at it, not a green build hiding a suite that stopped running.
That is the whole design: the failure mode is refusal, never a guess in the
permissive direction. It costs some friction when the workflow grows a new
shape, and it buys the property the five previous versions did not have.

Coverage is also grounded in the filesystem rather than in string matching. A
positional argument is expanded against the repo: `tests/compat/` covers the
files that are really under it, a glob covers what it really matches, and a
path that no longer exists is refused, taking the whole step's coverage with it.
That last one is not a technicality: pytest exits 4 on a missing path, so the
step really does fail and really does run none of the files it names. That is
why there is no list of "directories CI runs wholesale" any more. The old list
was matched as a substring, so a single-file run, or even
`--ignore=tests/compat/`, credited all 55 files under it.

The expansion has to mean what the runner's bash means, not what pathlib means.
`**` is refused for that reason: globstar is off in a non-interactive shell, so
bash hands pytest one level of directories where pathlib matches the whole
tree. Reading `tests/**/test_*.py` as recursive credited 185 files against the
130 bash really expands, and the 55 in between are the same tests/compat/ tree
the substring hole used to give away.

What this cannot see
--------------------
The guard proves one thing: that CI hands pytest a path, in a step and a job
whose result binds, under configuration that does not stop pytest asserting.
It does not prove a single assertion ran. These are outside what reading a
workflow can reach, and no amount of parsing gets to them:

- A dependency that changes what pytest does. A plugin in dev-requirements.txt
  that skips, deselects or exits early is installed by the same
  `pip install -r` line the grammar accepts, and nothing in the workflow says
  what it does. A `container:` image is the same claim by another route: it
  decides which python and which site-packages the run gets. Both are accepted
  for that reason, not overlooked.
- A test file that skips itself. Coverage here means a path was handed to
  pytest, and a module-level skip, or a fixture calling pytest.skip, leaves that
  true while nothing is asserted. The one case the workflow says enough about to
  check is encoded in _REQUIRES_SERVICE: the Postgres cutover suite skips unless
  the job really defines the service it connects to. Nothing catches the next
  one automatically.
- A conftest.py that empties the run. It is Python the guard does not execute,
  and one module-level skip leaves a green run with nothing in it.
- A test file that has been emptied, or whose assertions stopped asserting.
  Coverage here is counted per file: what is inside the file is a review
  question, not a parsing one.
- Tests moved into a composite action or a second workflow file. Neither grants
  coverage, so the files show up as unaccounted, but the guard reports them as
  unrun rather than as moved, and it reads no other workflow file at all.
- Whether the run passed, or how many tests it collected. A path handed to
  pytest and a suite that really executed are not the same claim.
"""

import configparser
import pathlib
import re
import shlex
import sys
import tomllib

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
# This file, repo-relative. Every check below runs only because CI enumerates
# this path, so the path itself is part of what has to be guarded.
SELF = pathlib.Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

# Files deliberately kept out of CI, grouped by cause, each with the reason it
# cannot run. Every reason below was verified by running the file twice, once
# normally and once inside a network namespace with no route out: a file whose
# two runs are identical is not network-dependent, whatever it looks like.
EXCLUDED = {
    # Built-in provider tests that really do reach the provider's site, so a
    # third party being down would turn every pull request in the repo red.
    # The built-in providers are being retired in favour of catalog plugins,
    # which are tested in their own repo.
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
        "six of its seven cases call www.feliratok.eu for real. The file has no "
        "cassette directory and no requests_mock at all: 7 passed in 18s online "
        "against 6 failed, 1 passed in 61s offline, the extra minute being "
        "RetryingSession.retry_method, which is retry_call(tries=3, delay=5) at "
        "custom_libs/subliminal_patch/http.py:217. The provider writes `from "
        "requests import Session`, but the patched Provider base rebinds the "
        "module's Session to RetryingSession at import time "
        "(custom_libs/subliminal_patch/providers/__init__.py:133), which is why "
        "the retries happen at all. Only test_subtitle_reprs is offline-safe: "
        "it builds a SuperSubtitlesSubtitle by hand and asserts on it, and it "
        "is the one that passes offline. test_list_video_2 is NOT offline-safe, "
        "whatever its name suggests; it calls provider.list_subtitles. This is "
        "also NOT the CFSession bug that test_prijevodi.py and test_titlovi.py "
        "have: RetryingSession is not a CFSession, so fixing cloudscraper does "
        "not make this file safe to run."
    ),
    # Not network-dependent at all, despite what they look like. These seven
    # were previously all labelled "live network call to the provider", which
    # was wrong and hid seven different real problems. Each needs a fix, not an
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
        "replays recorded HTTP and touches no network: 3 failed, 4 passed, "
        "identical online and inside a network namespace. The three failures "
        "are CannotOverwriteExistingCassetteException, because the cassettes "
        "only recorded the pTitle=org query while the provider now issues org, "
        "en and pl. Two of the four that pass do so vacuously, one asserting "
        "only isinstance(subs, list) and one looping over a list that is empty."
    ),
    "tests/subliminal_patch/test_napiprojekt.py": (
        "replays recorded HTTP and touches no network, but subliminal 2.6.0 "
        "moved server_url to https while the cassettes were recorded against "
        "http, and vcrpy matches on scheme and port, so every request misses. "
        "Both cases also burn 30 seconds each of ProviderRetryMixin sleep."
    ),
    "tests/subliminal_patch/test_core.py": (
        "no network at all: 8 failed, 14 passed in under a fifth of a second, "
        "identical online and offline. Four pool tests compare SZProviderPool.providers against a "
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
    "tests/subliminal_patch/test_video.py": (
        "three failures, none of them a network problem, and none of them what "
        "this entry claimed until now. test_video_fromguess_episode and "
        "test_video_fromguess_movie hand fromguess a guess dict with no title "
        "key, so subliminal raises GuessingError, 'Insufficient data to process "
        "the guess', before any attribute is assigned: the tests are wrong, not "
        "the code. Only test_video_fromname_movie is a dropped attribute, "
        "video.other is None where it expects 'Proper', and it is dropped by "
        "fromguess: Video.fromname is one line, `return cls.fromguess(name, "
        "guessit(name))`, and Movie.fromguess never passes `other` through. "
        "Written the other way round here twice; the direction is fromname to "
        "fromguess, so a reader sent to fromname will find nothing to fix."
    ),
    # These two are fully requests_mock-ed, so they are deterministic and touch
    # no network, and they still fail for a real reason: a product bug in
    # CFSession. Excluded only so the guard-list backfill is not blocked by it.
    #
    # WATCH OUT before lifting either: both PASS on a machine with an older
    # cloudscraper still installed, so "it is green locally" is not evidence.
    # requirements.txt pins cloudscraper<=1.2.71, which resolves to 1.2.71, and
    # 1.2.71 no longer has is_Challenge_Request. CFSession._request calls it at
    # custom_libs/subliminal_patch/http.py:95, outside anything that catches
    # AttributeError, so every request through CFSession dies there. Verified by
    # running both files against 1.2.71 (5 failed, 1 failed) and against a
    # locally installed 1.2.58, which still has the method (5 passed, 1 passed).
    #
    # test_supersubtitles.py used to sit in this block, wrongly. Its provider
    # builds a plain requests.Session, so fixing cloudscraper does not make it
    # safe to enumerate: lifting these two must not lift that one.
    "tests/subliminal_patch/test_prijevodi.py": (
        "CFSession._request calls is_Challenge_Request, which cloudscraper "
        "1.2.71 removed, so all five cases raise AttributeError even though "
        "requests_mock means they never reach a socket. Product bug in "
        "custom_libs/subliminal_patch/http.py, tracked separately."
    ),
    "tests/subliminal_patch/test_titlovi.py": (
        "same missing is_Challenge_Request as test_prijevodi.py: one case, "
        "fully requests_mock-ed, fails on cloudscraper 1.2.71."
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
# Commands that print and do nothing else. `printf` is here for the same reason
# `echo` is: the workflow groups its per-file loop output, and which of the two
# builtins does the printing cannot change what pytest collects.
_INERT_COMMANDS = frozenset({"echo", "printf"})
# Commands that are not this guard's business but that a test-bearing step can
# legitimately contain, because they name a tests/ path in passing. `ruff check
# tests/ bazarr/` is the case that mattered: it mentions tests/, so the step
# became test-bearing, and the guard then demanded the linter be rewritten into
# a pytest grammar. It grants no coverage, and it cannot change what a later
# pytest line does without a redirect or an expansion, both of which are still
# refused on the line.
_NON_PYTEST_COMMANDS = frozenset({"ruff"})
# The `set` modes the guard has reasoned about. `-e` exits on the first failure,
# `-u` aborts on an unset variable, `-x` only traces: each of the three either
# leaves every later command running or stops the script loudly. Every other
# mode is refused, because a mode that changes whether commands run at all is
# precisely how a script can name every test file and execute none. `set -n`
# makes bash read the rest of the script without executing it and still exit 0,
# and any `+` form turns a mode back off, so `set +e` lets a failing pytest
# leave the step green.
_SET_MODES = frozenset("eux")
# The long-form options, and the letter each one corresponds to. `pipefail` has
# no letter, and it is the reason this exists: `set -euo pipefail` is strictly
# stricter than the `set -e` the workflow uses, because it stops a failure being
# swallowed by a pipeline, and refusing it while accepting `set -e` told
# maintainers to write the weaker line. Every `+` spelling is still refused.
_SET_OPTIONS = {
    "pipefail": "pipefail",
    "errexit": "e",
    "nounset": "u",
    "xtrace": "x",
}
# The commands a `shell:` value may name. GitHub's own `bash` and `sh` keywords
# select the same shell the grammar below describes.
_SHELL_COMMANDS = frozenset({"bash", "sh"})

# Options pytest takes that consume the following token. A test path sitting in
# one of these positions is an argument to the option, not something pytest
# collects, so it must never be read as coverage.
_PYTEST_VALUE_OPTIONS = {
    "-p", "-k", "-m", "-n", "-W", "-r",
    "--tb", "--rootdir", "--basetemp", "--color", "--durations", "--maxfail",
    "--log-level", "--junitxml", "--import-mode",
    "--ignore", "--deselect",
    # Coverage and timeouts. Neither can drop a test or hide a failure: a
    # coverage run collects and asserts exactly what the same command collects
    # and asserts without it, and --cov-fail-under and --timeout can only turn a
    # green run red. `--cov` takes an optional value, and pytest-cov really does
    # swallow the following token when it is not an option, so consuming one
    # here is what pytest does rather than a guess.
    "--cov", "--cov-report", "--cov-config", "--cov-fail-under",
    "--timeout", "--timeout-method",
}
# Options that take no value.
_PYTEST_FLAGS = {
    "-v", "-vv", "-vvv", "-q", "-qq", "-s", "-x", "-l", "-ra", "-rA", "-rf",
    "-rN", "--verbose", "--quiet", "--exitfirst", "--showlocals",
    "--full-trace", "--no-header", "--no-summary", "--disable-warnings",
    "--strict-markers", "--strict-config", "--continue-on-collection-errors",
    "--collect-only", "--co", "--setup-only", "--setup-plan", "--fixtures",
    "--markers", "--help", "-h",
    "--cov-branch", "--cov-append", "--no-cov", "--no-cov-on-fail",
}
# The short options that may appear inside a combined cluster such as `-svx`.
# Only the flags: a cluster carrying a letter that takes a value, or one that is
# refused on its own, is refused as a cluster too, so `-sk nothing_matches`
# cannot smuggle a -k filter past the check that refuses `-k`.
_PYTEST_CLUSTERABLE = frozenset(
    option for option in _PYTEST_FLAGS if len(option) == 2 and option[1].isalpha()
)
# Options whose paths pytest is told NOT to collect. They subtract.
_PYTEST_SUBTRACTING = {"--ignore", "--deselect"}
# Options that stop pytest asserting anything, or narrow it to an arbitrary
# subset. A command carrying one of these proves nothing about coverage.
_PYTEST_NEUTERING = {
    "--collect-only", "--co", "--setup-only", "--setup-plan", "--fixtures",
    "--markers", "--help", "-h", "-k", "-m",
}
# Options refused outright on a test-bearing command, each with the reason.
# Refused rather than modelled: what each of these does to a run is decided
# somewhere the guard cannot read, so accepting the option with its value
# uninspected is the same hole PYTEST_ADDOPTS already closes.
_PYTEST_REFUSED = {
    "-o": (
        "overrides an ini option, so `-o addopts=--collect-only` collects and "
        "exits 0 while asserting nothing"
    ),
    "--override-ini": "is the long form of -o, with the same reach over addopts",
    "-c": (
        "loads configuration from an arbitrary file, which can carry exactly "
        "that addopts"
    ),
    "--ignore-glob": (
        "excludes recursively by pattern during collection, which is not "
        "filesystem glob semantics. `--ignore-glob=*ui.py` really does drop "
        "tests/bazarr/test_ui.py, while the same pattern against the repo root "
        "matches nothing, so the guard would subtract far less than pytest skips"
    ),
}

# Whitelisted options whose value the guard does inspect, rather than refusing
# the option outright. `-p` loads a pytest plugin, and a plugin decides what
# collection returns: `pytest -p neuter tests/...` can empty the run and still
# exit 0, which is the hole -o, --override-ini and -c are refused for, reached
# through an option the guard was letting past with its value unread. Only the
# `no:` forms named here are accepted; the workflow uses one of them.
_PYTEST_INSPECTED_VALUES = {
    "-p": (
        frozenset({"no:cacheprovider"}),
        "loads a pytest plugin, and a plugin can empty collection while the "
        "command still exits 0. Only a `no:` form the guard has reasoned about "
        "is accepted",
    ),
}

# The configuration files pytest itself reads, and the section each one keeps
# its settings in. They are searched from the common ancestor of the arguments
# upward, and every path CI passes is under tests/, so both directories count.
# Nothing in the repo defines any of them today, which is the point: `-o
# addopts=--collect-only` is refused on the command line, and committing the
# same setting to a file pytest loads by itself would have been invisible.
_PYTEST_CONFIG_SECTIONS = {
    "pytest.ini": "pytest",
    ".pytest.ini": "pytest",
    "tox.ini": "pytest",
    "setup.cfg": "tool:pytest",
}
_PYTEST_CONFIG_DIRECTORIES = (".", "tests")
# Settings in those files that decide what a run collects and what it asserts.
_NEUTERING_INI_KEYS = ("addopts", "testpaths")

# Workflow, job and step keys the guard has reasoned about. A key that is on
# neither this list nor _CHECKED_KEYS below is absent because nobody has
# reasoned about it yet, which is exactly when the guard should stop rather than
# assume.
# Keys that cannot decide whether the tests run or whether their result binds,
# whatever their value is. `concurrency` cancels superseded runs, and a cancelled
# run is not a successful one, so it can never stand in for a suite that passed:
# the run that gates the merge is the last one, and it runs in full. `outputs`
# hands values to a downstream job and reaches nothing in this one. Refusing
# either was friction with no safety behind it, and friction is how a guard gets
# deleted, which reopens every hole in this file at once.
_SAFE_WORKFLOW_KEYS = {
    "name", "run-name", "on", True, "permissions", "env", "jobs", "concurrency",
}
# `container` is on this list for a reason worth writing down, because it looks
# like it belongs with PYTHONPATH: an image decides which python and which
# site-packages pytest runs against, so an image carrying a sitecustomize.py or
# a collection-emptying plugin reaches a run the same way. It is accepted
# because that is the class of thing this guard already says it cannot see: the
# `pip install -r requirements.txt` line the grammar accepts installs whatever
# those files name, and a plugin that skips everything arrives that way today.
# Refusing the key would not close that route, it would only refuse an ordinary
# edit while the route stayed open through pip. See "What this cannot see".
_SAFE_JOB_KEYS = {
    "name", "runs-on", "needs", "permissions", "strategy", "services", "steps",
    "env", "timeout-minutes", "concurrency", "outputs", "container",
}
_SAFE_STEP_KEYS = {"name", "id", "run", "env", "timeout-minutes"}
# Keys whose value decides, rather than their presence. `if` and
# `continue-on-error` say whether the result binds, `working-directory` changes
# what the paths mean, `shell` and `defaults` change what the script means,
# `runs-on` says whether the job is ever picked up, `strategy` says how many
# times it runs, and `services` says whether the suites that need one assert
# anything.
_CHECKED_KEYS = {
    "if", "continue-on-error", "working-directory", "shell", "defaults",
}
# `if` values that are exactly, unconditionally true, plus success(), which is
# what a step does by default: it runs unless an earlier step in the same job
# already failed, and a job with a failed step is red either way. So a step
# carrying it cannot hide a failing suite, while `failure()` and `cancelled()`
# can and stay refused. An exact literal match on a small set, not an expression
# evaluator.
_ALWAYS_RUNS = {
    True, "true", "always()", "${{ always() }}", "${{always()}}",
    "success()", "${{ success() }}", "${{success()}}",
}
# `working-directory` values that leave every path in the script meaning what it
# says. Anything else is refused, because the guard resolves paths against the
# repository root.
_REPOSITORY_ROOT_DIRECTORIES = {".", "./"}
# Runner labels the guard knows GitHub schedules. A label nobody provides is the
# quietest way to stop a job: the job is never picked up, so it never fails, it
# just sits queued, and `development` has no required status checks to notice.
# GitHub-hosted labels only: a self-hosted pool is a claim about infrastructure
# this file cannot check, so naming one is a decision to record here.
# `strategy` keys the guard has reasoned about. `fail-fast` and `max-parallel`
# change how the rows are scheduled, never whether they run.
_SAFE_STRATEGY_KEYS = {"matrix", "fail-fast", "max-parallel"}
_SAFE_RUNNERS = {
    "ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04",
    "windows-latest", "windows-2025", "windows-2022",
    "macos-latest", "macos-15", "macos-14",
}
# Keys under `on.pull_request` that cannot stop an ordinary code change running
# the workflow. `branches` is here on purpose: which branches are gated is an
# ordinary decision, and hardcoding a list would fight every change to it.
# `paths` and `paths-ignore` are not, and neither is `types`, which can narrow
# the event to something a pushed commit never raises.
_PULL_REQUEST_SAFE_KEYS = {"branches", "branches-ignore"}

# Environment names that decide what a pytest run does before pytest is in a
# position to be asked. PYTEST_* is read by pytest itself. The three PYTHON*
# names sit one step further back: PYTHONPATH puts a directory ahead of
# site-packages, and a sitecustomize.py found anywhere on that path is imported
# by the interpreter before pytest parses its first argument, so it can set
# PYTEST_ADDOPTS from inside the repository. PYTHONSTARTUP and PYTHONHOME reach
# the same place by other routes. Refused wherever PYTEST_* is refused: workflow,
# job and step `env:`, and a leading assignment on the command itself.
_NEUTERING_ENV_NAMES = frozenset({"PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME"})
# Files that are handed to pytest and still assert nothing unless something
# outside pytest is up. Each one calls pytest.skip when its dependency is
# unreachable, so the coverage this guard counts, a path handed to pytest, is
# not enough on its own.
#
# tests/bazarr/test_arr_pg_cutover_migration.py is the whole list: all five of
# its cases take the pg_bind fixture, which calls pytest.skip when nothing
# answers on the URL. Deleting the `services:` block was therefore a one-line
# edit that turned the native-Postgres cutover suite into five skips, with the
# file still enumerated and the guard still green. The general case, a file that
# skips itself for a reason the workflow does not mention, is outside what
# reading a workflow can reach and is listed under "What this cannot see"; this
# is the one instance the workflow does say enough about to check.
#
# name -> (environment variable naming the service, image prefix, port inside
# the container, what happens when it is not there).
_REQUIRES_SERVICE = {
    "tests/bazarr/test_arr_pg_cutover_migration.py": (
        "BAZARR_PG_TEST_URL",
        "postgres",
        5432,
        "every case takes the pg_bind fixture, which calls pytest.skip when no "
        "Postgres answers, so the file is collected, reports five skips and "
        "asserts nothing at all",
    ),
}
# The only `env:` key a job or step that runs tests needs in this repo. An env
# key is as good as a command-line option for deciding what a run does, so a new
# one is a decision, not a detail: name it here once you have made it.
_ALLOWED_TEST_ENV = frozenset({"BAZARR_PG_TEST_URL"})

# A step writing to $GITHUB_ENV or $GITHUB_PATH changes the environment of every
# later step in the same job. The step doing it need not mention pytest or a
# tests/ path, so it is invisible to _MENTIONS_TESTS: `echo
# "PYTEST_ADDOPTS=--collect-only" >> $GITHUB_ENV` in a step called "Configure
# environment" silences the suite three steps further down. That is the fourth
# way into the same hole PYTEST_ADDOPTS already closes at workflow, job, step and
# inline-assignment scope, so it is refused wherever it appears in a job that
# runs tests.
_EXPORTS_ENVIRONMENT = re.compile(r"\bGITHUB_(ENV|PATH)\b")

# Every shell operator, all of them refused inside a test-bearing script. Each
# one is a way for a command to name a path without running it, or to run it
# without its exit status binding.
#
# Recognised as "a token made only of shell punctuation" rather than as a list
# of spellings. shlex with punctuation_chars merges a run of these characters
# into one token of whatever length the run happens to be, so `|&`, `;&`, `&>`
# and `>|` each arrive as a single token that an exact-string set has to have
# listed to catch. `pytest X |& true` is the one that mattered: the default step
# shell is `bash -e {0}` with no pipefail, so pytest's exit status is thrown
# away and the step is green whatever the suite did.
_SHELL_PUNCTUATION = frozenset("();<>|&")


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


def _is_shell_operator(token: str) -> bool:
    """Whether shlex handed back a run of shell punctuation rather than a word."""
    return bool(token) and set(token) <= _SHELL_PUNCTUATION


def _reject_operators(tokens: list, line: str, allow_semicolon: bool = False) -> None:
    permitted = {";"} if allow_semicolon else set()
    for token in tokens:
        if _is_shell_operator(token) and token not in permitted:
            raise _Unverifiable(
                f"shell operator {token!r} in {line!r}: the guard cannot tell "
                "whether the command runs, or whether its exit status binds"
            )


def _reject_expansions(
    tokens: list,
    line: str,
    allowed: frozenset = frozenset(),
    parameters_are_inert: bool = False,
) -> None:
    """Refuse whatever the shell would rewrite before the command sees it.

    Checked inside every token, not only as standalone punctuation, because
    quoting hides a substitution from the tokenizer:
    `pytest tests/x.py "$(printf '%s' --collect-only)"` survives shlex as one
    token, no operator check fires, and bash then hands pytest --collect-only.

    A plain parameter expansion is refused for the same reason wherever it could
    change what runs: the guard cannot see what `pytest tests/x.py $EXTRA`
    becomes. `allowed` carries the loop variable, the one expansion the loop
    grammar has to read. `parameters_are_inert` is passed only for `echo`, whose
    arguments cannot change what pytest does, so the workflow's own
    `echo "::group::$f"` stays legal without widening anything else.
    """
    for token in tokens:
        if "`" in token or "$(" in token:
            raise _Unverifiable(
                f"command substitution in {line!r}: quoted or not, the guard "
                "cannot read the token the shell will produce"
            )
        if "${{" in token:
            raise _Unverifiable(
                f"workflow expression in {line!r}: it can expand to anything, so "
                "the guard cannot read the command it produces"
            )
        if "$" not in token or token in allowed or parameters_are_inert:
            continue
        raise _Unverifiable(
            f"parameter expansion {token!r} in {line!r}: the guard cannot see "
            "what the shell puts there, and an expansion in this position can "
            "become an option that stops pytest asserting anything"
        )


def _inside_repo(path: pathlib.Path) -> bool:
    """Whether a match really lives in the repository, with symlinks resolved.

    The check on the token itself is lexical, so it cannot see a committed
    symlink: `tests/linkdir/test_outside.py` reads as a repo-relative path and
    is a file somewhere else on disk. Crediting it would vouch for a file the
    repository does not contain, and does not run.
    """
    try:
        return path.resolve().is_relative_to(REPO_ROOT.resolve())
    except (ValueError, OSError):
        return False


def _glob(pattern: str) -> list:
    """REPO_ROOT.glob with symlink recursion off on every version CI runs.

    `recurse_symlinks` arrived in 3.13, and the default changed with it: 3.12
    follows a directory symlink inside a `**` and 3.13 onward does not. CI runs
    3.12, 3.13 and 3.14, so the argument is passed where it exists, and
    _inside_repo covers the version that has no such argument. Without both, the
    same workflow means different things on different rows of the matrix.

    Errors are turned into a refusal rather than an exception: a NUL byte in a
    path makes glob raise ValueError, and the guard reporting a stack trace
    instead of a reason is the failure mode this file exists to avoid.
    """
    try:
        try:
            matches = REPO_ROOT.glob(pattern, recurse_symlinks=False)
        except TypeError:  # Python 3.12 has no recurse_symlinks argument
            matches = REPO_ROOT.glob(pattern)
        return sorted(match for match in matches if _inside_repo(match))
    except (ValueError, OSError) as error:
        raise _Unverifiable(
            f"{pattern!r} cannot be matched against the repository: {error}"
        )


def _path_kind(path: pathlib.Path) -> str:
    """"dir", "file" or "", refusing rather than raising out of the guard."""
    try:
        if path.is_dir():
            return "dir"
        return "file" if path.is_file() else ""
    except (ValueError, OSError) as error:
        raise _Unverifiable(f"{str(path)!r} cannot be read: {error}")


def _expand(token: str, allow_node_selector: bool = False) -> set:
    """The repo-relative test files a pytest path argument really names.

    Grounded in the filesystem rather than in string matching: a directory
    covers what is under it, and a glob covers what it matches, with bash's
    semantics rather than pathlib's.

    A `tests/...py` path that no longer exists is refused, not quietly dropped,
    and the refusal costs the whole step its coverage. That is not an
    overreaction: pytest exits 4 on a path it cannot find, so the step really
    does fail and really does run none of the files it names. The report says
    which name is wrong.

    A `file.py::case` node selector is refused, because coverage here is counted
    per file: `pytest tests/bazarr/test_ui.py::test_one_case` exits 0 having run
    one case, and crediting the module would vouch for every other assertion in
    it. `allow_node_selector` is passed only where a node id subtracts, where
    collapsing it to its module removes more coverage than pytest really skips,
    which is the safe direction.
    """
    path, selector, _ = token.partition("::")
    if selector and not allow_node_selector:
        raise _Unverifiable(
            f"{token!r} selects a single test node, so the rest of the module "
            "does not run. The guard credits whole files, so it refuses rather "
            "than claim the file ran"
        )
    if not path or path.startswith("-"):
        return set()
    if path.startswith("/") or ".." in pathlib.PurePosixPath(path).parts:
        raise _Unverifiable(f"path {token!r} escapes the repository")
    if "\x00" in path:
        raise _Unverifiable(
            f"path {token!r} contains a NUL byte, which nothing in this "
            "repository is named. Matching it raises out of the filesystem "
            "rather than answering"
        )
    glob = any(char in path for char in "*?[")
    if "**" in path:
        raise _Unverifiable(
            f"{token!r} uses `**`, which the runner's bash does not expand "
            "recursively: globstar is off in a non-interactive shell, so bash "
            "hands pytest one level of directories while pathlib matches the "
            "whole tree. Reading it as recursive credits files pytest is never "
            "given. Name the directory instead"
        )
    matches = _glob(path) if glob else [REPO_ROOT / path]
    found = set()
    for match in matches:
        kind = _path_kind(match)
        if kind and not _inside_repo(match):
            raise _Unverifiable(
                f"{token!r} resolves outside the repository, through a symlink. "
                "The repository does not contain that file, so nothing here can "
                "vouch for what it asserts"
            )
        if kind == "dir":
            relative = match.relative_to(REPO_ROOT).as_posix()
            prefix = "" if relative == "." else relative + "/"
            found |= {name for name in _all_test_files() if name.startswith(prefix)}
        elif kind == "file" and match.suffix == ".py":
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
        if token.startswith("@"):
            # pytest sets fromfile_prefix_chars="@", so it reads the rest of its
            # arguments out of that file before parsing any of them. Every
            # option the guard refuses on the command line can sit in there, and
            # the token itself reads as an ordinary positional.
            raise _Unverifiable(
                f"{token!r} in {line!r} is an arguments file: pytest reads the "
                "rest of its options out of it before parsing, so it can carry "
                "--collect-only, -k or -p and the guard cannot see any of it"
            )
        if not token.startswith("-") or token == "-":
            positional.append(token)
            continue
        name, separator, inline = token.partition("=")
        if name in _PYTEST_REFUSED:
            raise _Unverifiable(
                f"{name} in {line!r} {_PYTEST_REFUSED[name]}, so the command "
                "cannot be read as coverage"
            )
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
                subtracted |= _expand(value, allow_node_selector=True)
            inspected = _PYTEST_INSPECTED_VALUES.get(name)
            if inspected and value not in inspected[0]:
                raise _Unverifiable(
                    f"{name} {value!r} in {line!r} {inspected[1]}, so the "
                    "command cannot be read as coverage"
                )
            continue
        if name in _PYTEST_FLAGS and not separator:
            continue
        if not separator and re.fullmatch(r"-[A-Za-z]{2,}", token):
            _check_option_cluster(token, line)
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


def _check_option_cluster(token: str, line: str) -> None:
    """`-svx` is three flags, and only flags may be written that way.

    pytest reads a cluster of short options exactly as the same letters written
    apart, so refusing the spelling was friction rather than safety. The letters
    are checked one at a time so nothing changes about what is accepted: a
    cluster carrying a letter that takes a value, or one that is refused on its
    own, is refused as a cluster.
    """
    bad = sorted(
        "-" + letter
        for letter in token[1:]
        if "-" + letter not in _PYTEST_CLUSTERABLE
        or "-" + letter in _PYTEST_NEUTERING
    )
    if bad:
        raise _Unverifiable(
            f"the combined short options {token!r} in {line!r} include "
            f"{', '.join(bad)}, which the guard does not accept on its own "
            "either, so it cannot accept it hidden in a cluster"
        )


def _strip_assignments(tokens: list, line: str) -> list:
    """Drop leading VAR=value assignments, refusing any that neuter pytest."""
    index = 0
    while index < len(tokens):
        match = _ASSIGNMENT.match(tokens[index])
        if not match:
            break
        if _neuters_pytest(match.group(1)):
            raise _Unverifiable(
                f"{match.group(1)} is set on {line!r}; it decides what pytest "
                "does before pytest can be asked, while the command still looks "
                "like it runs the tests. PYTEST_* is read by pytest itself, and "
                "PYTHONPATH puts a directory ahead of site-packages where a "
                "committed sitecustomize.py runs before pytest parses its first "
                "argument"
            )
        # The value too: `FOO="$(...)" pytest X` keeps the substitution inside a
        # single token, so nothing else would look at it.
        _reject_expansions([tokens[index]], line)
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

        if head == "set":
            _check_set_modes(tokens, line)
            continue
        if head in {"pip", "pip3"} and tokens[1:2] == ["install"]:
            _reject_operators(tokens, line)
            _reject_expansions(tokens, line)
            continue
        if head in _INERT_COMMANDS:
            _reject_operators(tokens, line)
            _reject_expansions(tokens, line, parameters_are_inert=True)
            continue
        if head == "cd":
            _check_directory_change(tokens, line)
            continue
        if head in _NON_PYTEST_COMMANDS:
            _reject_operators(tokens, line)
            _reject_expansions(tokens, line)
            continue
        if head == "pytest":
            _reject_operators(tokens, line)
            _reject_expansions(tokens, line)
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
            "steps must use only `set`, `pip install`, `echo`, `printf`, "
            "`ruff`, `cd .`, `for VAR in <paths>; do ... done` and pytest, so "
            "that what runs is readable without interpreting shell"
        )
    return covered


def _check_directory_change(tokens: list, line: str) -> None:
    """`cd` back to where the script already is, and nothing else.

    The paths in this file are resolved against the repository root, so a `cd`
    anywhere else makes every later path mean something the guard cannot check.
    `cd .` means nothing at all, which is why it is allowed rather than refused:
    a line that changes nothing should not turn a build red.
    """
    _reject_operators(tokens, line)
    _reject_expansions(tokens, line)
    if len(tokens) != 2 or tokens[1] not in _REPOSITORY_ROOT_DIRECTORIES:
        raise _Unverifiable(
            f"{line!r} moves the shell out of the repository root, so every "
            "path after it means something the guard cannot resolve"
        )


def _shell_modes(words: list) -> tuple:
    """(modes these words turn on, why the guard cannot accept them).

    Shared by `set` and by a `shell:` value, because they are the same grammar:
    `set -euo pipefail` and `bash -eo pipefail {0}` say the same thing in two
    places. Both are stricter than what the workflow uses today, and both were
    refused by a check that only recognised a single `-eux` cluster.
    """
    modes = set()
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if word.startswith("+"):
            return modes, (
                f"{word!r} turns a shell mode back off, and a mode that can be "
                "turned off can be turned off around a failing pytest: `set +e` "
                "leaves the step green whatever the suite did"
            )
        if not word.startswith("-") or len(word) < 2:
            return modes, (
                f"{word!r} is not a `set` mode the guard has modelled. Only -e, "
                "-u, -x and the long forms of those, plus -o pipefail, are "
                "accepted"
            )
        letters = word[1:]
        if "o" in letters:
            if not letters.endswith("o"):
                return modes, (
                    f"{word!r} puts -o inside a cluster, where the guard cannot "
                    "tell which token is the option name"
                )
            if index >= len(words):
                return modes, f"{word!r} names a shell option with no value"
            option = words[index]
            index += 1
            if option not in _SET_OPTIONS:
                return modes, (
                    f"`set -o {option}` is not an option the guard has modelled. "
                    "Only pipefail, errexit, nounset and xtrace are, because "
                    "each of those either leaves every later command running or "
                    "stops the script loudly"
                )
            modes.add(_SET_OPTIONS[option])
            letters = letters[:-1]
        unknown = sorted(set(letters) - _SET_MODES)
        if unknown:
            return modes, (
                f"{word!r} is not a `set` mode the guard has modelled. Only -e, "
                "-u, -x and the long forms of those, plus -o pipefail, are "
                "accepted: `set -n` reads the rest of the script without "
                "executing a line of it and still exits 0"
            )
        modes |= set(letters)
    return modes, ""


def _check_set_modes(tokens: list, line: str) -> None:
    """`set` carrying only modes that leave every later command running."""
    words = tokens[1:]
    if not words:
        raise _Unverifiable(
            f"`set` with no mode in {line!r} prints the shell's variables and "
            "decides nothing, so the guard has nothing to reason about"
        )
    _, problem = _shell_modes(words)
    if problem:
        raise _Unverifiable(f"{line!r} cannot be read: {problem}")


def _unmodelled_shell(value) -> bool:
    """Whether a `shell:` value means something other than the grammar below.

    GitHub's `bash` and `sh` keywords, and any bash or sh invocation whose flags
    are the modes above with `-e` among them, run the script the same way the
    default `bash -e {0}` does. `bash -eo pipefail {0}` is one of those, and it
    is strictly stricter than the default, so refusing it while accepting the
    default was backwards. A form with no `-e` is still refused: without it a
    failing pytest in the middle of a script leaves the step's status to be
    decided by whatever ran last.
    """
    if value is None or value in _SHELL_COMMANDS:
        return False
    if not isinstance(value, str):
        return True
    words = value.split()
    if len(words) < 2 or words[0] not in _SHELL_COMMANDS or words[-1] != "{0}":
        return True
    modes, problem = _shell_modes(words[1:-1])
    return bool(problem) or "e" not in modes


def _for_header(tokens: list, line: str) -> tuple:
    """(files the list names, loop variable). The list must be test paths only."""
    _reject_operators(tokens, line, allow_semicolon=True)
    # Shape before content, so a one-line loop is reported as a one-line loop.
    # The expansion check used to run first, and the `$f` in the body of the
    # one-liner a maintainer would really write tripped it, so the sentence
    # about the body sitting on the header line was never the one anybody saw.
    if len(tokens) < 4 or tokens[2] != "in" or not _IDENTIFIER.match(tokens[1]):
        raise _Unverifiable(f"{line!r} is not a `for VAR in ...` header")
    if "do" in tokens and tokens.index("do") != len(tokens) - 1:
        raise _Unverifiable(
            f"{line!r} puts the loop body on the header line. Spread the loop "
            "over several lines so the guard can read the body that runs pytest"
        )
    _reject_expansions(tokens, line)
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
        if tokens[0] in _INERT_COMMANDS:
            _reject_expansions(tokens, line, parameters_are_inert=True)
            continue
        if tokens[0] == "pytest":
            _reject_expansions(tokens, line, allowed=frozenset(reference))
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


def _neuters_pytest(name: str) -> bool:
    """Whether setting this environment variable can decide what pytest does."""
    return name.startswith("PYTEST_") or name in _NEUTERING_ENV_NAMES


def _scope_problem(
    kind: str, name: str, scope: dict, safe_keys: set, restrict_env: bool = False
) -> str:
    """Why this workflow, job or step cannot be counted as guaranteed coverage."""
    if not isinstance(scope, dict):
        return f"{kind} {name} is not a mapping"

    unknown = sorted(str(key) for key in scope if key not in safe_keys | _CHECKED_KEYS)
    if unknown:
        return (
            f"{kind} {name} uses {', '.join(unknown)}, which the guard has not "
            "reasoned about. Work out whether it changes what runs or whether "
            "the result binds, then add it to the safe list or check its value"
        )

    condition = scope.get("if")
    if condition is not None and condition not in _ALWAYS_RUNS:
        return (
            f"{kind} {name} carries `if: {condition}`, so it may not run and "
            "cannot count as guaranteed coverage. Only an exactly unconditional "
            "`if` counts, such as always()"
        )
    if scope.get("continue-on-error"):
        return (
            f"{kind} {name} sets continue-on-error, so it runs but its failures "
            "cannot fail the workflow. Its assertions gate nothing, which is the "
            "state this guard exists to prevent"
        )
    directory = scope.get("working-directory")
    if directory is not None and str(directory).strip() not in _REPOSITORY_ROOT_DIRECTORIES:
        return (
            f"{kind} {name} sets working-directory: {directory!r}, so the guard "
            "cannot tell which files its paths name. Only the repository root "
            "leaves every path meaning what it says"
        )
    if _unmodelled_shell(scope.get("shell")):
        return (
            f"{kind} {name} runs under shell {scope['shell']!r}, whose semantics "
            "the guard does not model"
        )
    defaults_problem = _defaults_problem(kind, name, scope.get("defaults"))
    if defaults_problem:
        return defaults_problem

    environment = [str(key) for key in (scope.get("env") or {})]
    neutering = sorted(key for key in environment if _neuters_pytest(key))
    if neutering:
        return (
            f"{kind} {name} sets {', '.join(neutering)}, which decides what "
            "pytest does before pytest can be asked, while every command still "
            "looks like it runs the tests. PYTEST_* is read by pytest itself; "
            "PYTHONPATH puts a directory ahead of site-packages, where a "
            "committed sitecustomize.py is imported before pytest parses its "
            "first argument and can set PYTEST_ADDOPTS from there"
        )
    if restrict_env:
        unknown = sorted(key for key in environment if key not in _ALLOWED_TEST_ENV)
        if unknown:
            return (
                f"{kind} {name} sets {', '.join(unknown)}, which the guard has "
                "not reasoned about. An env key reaches the run as surely as a "
                "command-line option does, so decide what this one does and add "
                "it to _ALLOWED_TEST_ENV"
            )
    if kind == "job":
        return _runs_on_problem(name, scope) or _strategy_problem(name, scope)
    return ""


def _defaults_problem(kind: str, name: str, defaults) -> str:
    """`defaults:` decides the shell and directory of every `run:` under it.

    Safe-listing the key would hand `defaults: run: shell: pwsh` a script the
    grammar below cannot read, and refusing the key outright turned
    `defaults: run: shell: bash`, which is what the steps already get, into a
    red build.
    """
    if defaults is None:
        return ""
    if not isinstance(defaults, dict) or set(defaults) - {"run"}:
        return (
            f"{kind} {name} carries a `defaults:` the guard cannot read: "
            f"{defaults!r}. It decides how every `run:` under it is executed"
        )
    run = defaults.get("run") or {}
    if not isinstance(run, dict) or set(run) - {"shell", "working-directory"}:
        return (
            f"{kind} {name} carries a `defaults.run` the guard cannot read: "
            f"{run!r}"
        )
    if _unmodelled_shell(run.get("shell")):
        return (
            f"{kind} {name} defaults every run to shell {run['shell']!r}, whose "
            "semantics the guard does not model"
        )
    directory = run.get("working-directory")
    if directory is not None and str(directory).strip() not in _REPOSITORY_ROOT_DIRECTORIES:
        return (
            f"{kind} {name} defaults every run to working-directory: "
            f"{directory!r}, so the guard cannot tell which files the paths name"
        )
    return ""


def _runs_on_problem(name: str, job: dict) -> str:
    """A label no runner answers to leaves the job queued, and queued is not red.

    The key was safe-listed with its value unread, so one word turned every
    check in this file into a claim about a job that never started. Nothing
    downstream catches it either: a queued job reports no conclusion, and
    `development` has no required status checks.
    """
    if "runs-on" not in job:
        return (
            f"job {name} names no runs-on, so GitHub has nowhere to schedule it"
        )
    label = job["runs-on"]
    if isinstance(label, str) and label in _SAFE_RUNNERS:
        return ""
    return (
        f"job {name} runs on {label!r}, which is not a runner label the guard "
        "knows GitHub provides. A label no runner answers to leaves the job "
        "queued forever: it never fails, so nothing goes red, and every path it "
        "enumerates would still read as coverage. Add the label to "
        "_SAFE_RUNNERS once you know a runner really picks it up"
    )


def _strategy_problem(name: str, job: dict) -> str:
    """A matrix with no combination left runs the job zero times.

    `strategy` was safe-listed with its value unread, so emptying
    `matrix.python-version`, or excluding every version in it, skipped the whole
    job while the guard still credited all 168 files to it.
    """
    if "strategy" not in job:
        return ""
    strategy = job["strategy"]
    if not isinstance(strategy, dict):
        return f"job {name} carries a `strategy:` the guard cannot read: {strategy!r}"
    unknown = sorted(str(key) for key in strategy if key not in _SAFE_STRATEGY_KEYS)
    if unknown:
        return (
            f"job {name} uses strategy.{', strategy.'.join(unknown)}, which the "
            "guard has not reasoned about. Work out whether it can stop the job "
            "running, then add it to _SAFE_STRATEGY_KEYS"
        )
    if "matrix" not in strategy:
        return ""
    combinations, problem = _matrix_combinations(strategy["matrix"])
    if problem:
        return f"job {name} carries a matrix the guard cannot read: {problem}"
    if not combinations:
        return (
            f"job {name} has a matrix that produces no combination, so GitHub "
            "runs the job zero times. It does not fail, it simply never happens, "
            "and every path it enumerates would still read as coverage"
        )
    return ""


def _matrix_combinations(matrix) -> tuple:
    """(combinations the matrix really produces, why it cannot be read).

    Only what decides whether the job runs at all: the product of the dimensions,
    minus the `exclude` entries that cover a whole combination. `include` is not
    modelled, so a matrix that ends up empty and carries one is refused rather
    than guessed at.
    """
    if not isinstance(matrix, dict):
        return [], f"{matrix!r} is not a mapping"
    dimensions = {}
    for key, value in matrix.items():
        if key in {"include", "exclude"}:
            continue
        if isinstance(value, str) and "${{" in value:
            return [], (
                f"dimension {key} is the expression {value!r}, which can expand "
                "to an empty list"
            )
        if not isinstance(value, list):
            return [], f"dimension {key} is {value!r}, which is not a list"
        dimensions[key] = value
    combinations = [{}]
    for key, values in dimensions.items():
        combinations = [
            dict(combination, **{key: value})
            for combination in combinations
            for value in values
        ]
    excluded = matrix.get("exclude") or []
    if not isinstance(excluded, list):
        return [], f"`exclude` is {excluded!r}, which is not a list"
    for entry in excluded:
        if not isinstance(entry, dict):
            return [], f"`exclude` entry {entry!r} is not a mapping"
        combinations = [
            combination
            for combination in combinations
            if not all(combination.get(key) == value for key, value in entry.items())
        ]
    if not combinations and matrix.get("include"):
        return [], (
            "every combination is excluded and an `include` may or may not add "
            "one back, which the guard does not model"
        )
    return combinations, ""


def _exported_environment_problems(job_name: str, steps: list) -> list:
    """Steps that hand a changed environment to the steps after them.

    Every other check here looks only at steps that mention pytest or a tests/
    path, which is right for shell: a step that runs no tests cannot run them
    wrong. $GITHUB_ENV is the exception, because its effect outlives the step.
    """
    problems = []
    for position, step in enumerate(steps or []):
        script = step.get("run") if isinstance(step, dict) else None
        if not isinstance(script, str) or not _EXPORTS_ENVIRONMENT.search(script):
            continue
        label = f"{job_name!r} step {step.get('name') or position!r}"
        problems.append(
            f"{label} writes to GITHUB_ENV or GITHUB_PATH, which GitHub applies "
            "to every later step in the job. The guard cannot see what value "
            "lands there, and PYTEST_ADDOPTS=--collect-only put there from a "
            "step that mentions neither pytest nor tests/ silences the suite "
            "while every command still looks like it runs the tests. No step in "
            "this job counts while that line is here"
        )
    return problems


def _service_problems(job_name: str, job: dict, steps: list, covered: set) -> list:
    """Files whose assertions need a service container the job may not define.

    Called with the coverage the job's steps produced, and it takes credit away:
    a file that skips itself is not covered, so leaving it in the set would put
    the guard back to vouching for assertions that did not run.
    """
    problems = []
    for name in sorted(_REQUIRES_SERVICE):
        if name not in covered:
            continue
        problem = _service_problem(job_name, job, steps, name)
        if problem:
            problems.append(problem)
            covered.discard(name)
    return problems


def _service_problem(job_name: str, job: dict, steps: list, name: str) -> str:
    variable, image, container_port, consequence = _REQUIRES_SERVICE[name]
    label = f"{name} runs in job {job_name!r}, where {consequence}"

    scopes = [job.get("env") or {}]
    scopes += [step.get("env") or {} for step in steps or [] if isinstance(step, dict)]
    values = [scope[variable] for scope in scopes if variable in scope]
    if not values:
        return (
            f"{label}. Nothing in the job sets {variable}, so the file falls "
            "back to its own default and skips"
        )

    for value in values:
        location = re.search(r"://(?:[^@/\s]*@)?([^:/\s]+):(\d+)/", str(value))
        if not location:
            return (
                f"{label}. {variable} is {value!r}, which the guard cannot read "
                f"a host and port out of"
            )
        host, port = location.group(1), location.group(2)
        if host not in {"localhost", "127.0.0.1"}:
            return (
                f"{label}. {variable} points at {host}, which is not a service "
                "container of this job, so whether anything answers is decided "
                "outside the workflow"
            )
        services = job.get("services") or {}
        if not isinstance(services, dict):
            return f"{label}. The job's `services:` is {services!r}, which the guard cannot read"
        wanted = f"{port}:{container_port}"
        matching = [
            service
            for service in services.values()
            if isinstance(service, dict)
            and str(service.get("image", "")).startswith(image)
            and wanted in [str(mapping) for mapping in (service.get("ports") or [])]
        ]
        if not matching:
            return (
                f"{label}. {variable} points at port {port}, and the job "
                f"defines no {image} service publishing {wanted}. Removing the "
                "service leaves the file enumerated, collected and silent"
            )
    return ""


def _dependency_problems(job_name: str, jobs: dict) -> list:
    """The job-scope checks, applied over the whole `needs:` closure.

    Checking only the job that carries the pytest steps leaves the hole one
    level up: GitHub skips a job whose dependency was skipped, and a skipped job
    does not fail the workflow. So `if: false` on a job that runs no tests at
    all, but that the test job waits on, stops the tests as dead as putting it
    on the test job itself. The same goes for a `needs:` naming a job that does
    not exist, which GitHub never schedules.
    """
    problems = []
    seen = {job_name}
    queue = [job_name]
    while queue:
        current = queue.pop()
        job = jobs.get(current)
        needs = job.get("needs") if isinstance(job, dict) else None
        if needs is None:
            continue
        names = [needs] if isinstance(needs, str) else needs
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            problems.append(
                f"job {current} carries a `needs:` the guard cannot read: "
                f"{needs!r}. It decides whether this job runs at all"
            )
            continue
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            if name not in jobs:
                problems.append(
                    f"job {current} needs {name}, which this workflow does not "
                    "define, so GitHub never schedules it and the tests "
                    "downstream of it never run"
                )
                continue
            problem = _scope_problem(
                "job", f"{name}, which {current} needs,", jobs[name], _SAFE_JOB_KEYS
            )
            if problem:
                problems.append(problem)
            queue.append(name)
    return problems


def _ini_settings(path: pathlib.Path, section: str) -> dict:
    """The pytest section of an ini-style file, or {} if it has none."""
    parser = configparser.ConfigParser()
    try:
        parser.read_string(path.read_text(), source=str(path))
    except (configparser.Error, OSError, UnicodeDecodeError) as error:
        raise _Unverifiable(f"{path.name} cannot be parsed: {error}")
    return dict(parser[section]) if parser.has_section(section) else {}


def _toml_settings(path: pathlib.Path) -> dict:
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as error:
        raise _Unverifiable(f"{path.name} cannot be parsed: {error}")
    section = (data.get("tool") or {}).get("pytest") or {}
    return section.get("ini_options") or {}


def _pytest_configuration_problems() -> list:
    """Settings committed to a file pytest reads, that decide what a run does.

    `-o addopts=--collect-only` is refused on the command line, and so is `-c`,
    because either can stop pytest asserting anything. Both were refused while
    the files pytest loads on its own went unread, so committing `addopts` to a
    pytest.ini did the same thing with nothing on the command line to see. The
    repository defines none of these files today.

    `testpaths` is here for the same reason: it replaces the arguments pytest
    collects when none are given, and it is the setting a future `pytest` with
    no path would silently obey.
    """
    problems = []
    for directory in _PYTEST_CONFIG_DIRECTORIES:
        for name, section in _PYTEST_CONFIG_SECTIONS.items():
            path = REPO_ROOT / directory / name
            if not path.is_file():
                continue
            settings = _ini_settings(path, section)
            problems += _neutering_settings(directory, name, f"[{section}]", settings)
        path = REPO_ROOT / directory / "pyproject.toml"
        if path.is_file():
            problems += _neutering_settings(
                directory,
                "pyproject.toml",
                "[tool.pytest.ini_options]",
                _toml_settings(path),
            )
    return problems


def _neutering_settings(directory: str, name: str, section: str, settings: dict) -> list:
    where = name if directory == "." else f"{directory}/{name}"
    return [
        f"{where} sets {section} {key} = {settings[key]!r}. pytest reads that "
        "file by itself, so the setting applies to every run in CI without "
        "appearing on any command line the guard can read, and it can stop "
        "pytest asserting anything at all"
        for key in _NEUTERING_INI_KEYS
        if key in settings
    ]


def _trigger_problem(workflow: dict) -> str:
    """Whether the workflow really fires on the pull requests it is meant to gate.

    `on:` was safe-listed wholesale, so its contents were never read. Narrowing
    `on.pull_request` with a `paths:` filter that a code change never matches
    switches off everything below without touching a single line the guard
    inspects: the steps still enumerate every path, the shell still parses, and
    the workflow simply never runs. Every check in this file then vouches for a
    run that did not happen.

    What has to hold is only that an ordinary pull request reaches this workflow.
    Which branches it gates is left alone deliberately.
    """
    triggers = workflow.get("on", workflow.get(True))
    if isinstance(triggers, str):
        triggers = {triggers: None}
    elif isinstance(triggers, list) and all(isinstance(item, str) for item in triggers):
        triggers = {item: None for item in triggers}
    if not isinstance(triggers, dict):
        return (
            f"`on:` is {triggers!r}, which the guard cannot read. It decides "
            "whether any of this runs at all"
        )
    if "pull_request" not in triggers:
        return (
            "`on:` does not include pull_request, so this workflow does not gate "
            "a pull request and the checks in it block nothing from merging"
        )
    filters = triggers["pull_request"] or {}
    if not isinstance(filters, dict):
        return f"`on.pull_request` is {filters!r}, which the guard cannot read"
    for key in ("paths", "paths-ignore"):
        if key in filters:
            return (
                f"`on.pull_request` carries a {key} filter, so a pull request "
                "that changes code outside it never runs this workflow at all. "
                "Every path enumerated below would still read as coverage while "
                "nothing ran"
            )
    unknown = sorted(str(key) for key in filters if key not in _PULL_REQUEST_SAFE_KEYS)
    if unknown:
        return (
            f"`on.pull_request` uses {', '.join(unknown)}, which the guard has "
            "not reasoned about. Work out whether it can stop an ordinary pull "
            "request running the workflow, then add it to _PULL_REQUEST_SAFE_KEYS"
        )
    return ""


def _read_workflow() -> tuple:
    """(files CI really runs, problems that stop the guard vouching for a step)."""
    workflow = _workflow()
    problems = []
    covered = set()

    top = _scope_problem(
        "workflow", WORKFLOW.name, workflow, _SAFE_WORKFLOW_KEYS
    ) or _trigger_problem(workflow)
    if top:
        return covered, [top]

    jobs = workflow.get("jobs") or {}
    for job_name, job in jobs.items():
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

        job_problem = _scope_problem(
            "job", job_name, job, _SAFE_JOB_KEYS, restrict_env=True
        )
        if job_problem:
            problems.append(job_problem)
            continue

        scope_problems = _dependency_problems(job_name, jobs)
        scope_problems += _exported_environment_problems(job_name, steps)
        if scope_problems:
            problems.extend(scope_problems)
            continue

        job_covered = set()
        for position, step, script in bearing:
            label = f"{job_name!r} step {step.get('name') or position!r}"
            step_problem = _scope_problem(
                "step", label, step, _SAFE_STEP_KEYS, restrict_env=True
            )
            if step_problem:
                problems.append(step_problem)
                continue
            try:
                job_covered |= _script_coverage(script)
            except _Unverifiable as refusal:
                problems.append(f"{label}: {refusal}")

        problems += _service_problems(job_name, job, steps, job_covered)
        covered |= job_covered

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
        for path in _glob("tests/**/" + pattern)
    }


# ---------------------------------------------------------------------------
# The guard itself.
# ---------------------------------------------------------------------------


def test_workflow_exists():
    assert WORKFLOW.is_file(), f"CI workflow not found at {WORKFLOW}"


def _guard_disabled_problem() -> str:
    """Why the checks in this file are switched off, or "" while they are live.

    A function rather than two asserts inside the test, so the test below can be
    exercised against a workflow that really has dropped this file. Written as
    asserts, deleting the second one was a mutation nothing caught.
    """
    consequence = (
        f" Removing {SELF} from {WORKFLOW.name}, or excluding it, disables EVERY "
        "other check in this file: the guard only ever runs because CI hands "
        "pytest this path, so a disabled guard reports nothing while looking "
        "exactly as green as a working one."
    )
    if SELF in EXCLUDED:
        return f"{SELF} is in EXCLUDED, so it does not run." + consequence
    if SELF not in _enumerated_paths():
        return f"{SELF} is not enumerated in {WORKFLOW.name}." + consequence
    return ""


def test_this_guard_runs_in_ci_and_is_not_excluded():
    """The guard has to guard itself, or two edits switch off all of the rest.

    Everything else in this file runs only because the workflow enumerates this
    path. Delete that one line, add one EXCLUDED entry for this file, and the
    whole guard stops running in CI, stays green locally, and never reports
    another unrun test file again. Nothing else here would notice, because
    nothing else here runs.
    """
    assert not _guard_disabled_problem(), _guard_disabled_problem()


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

    if problems and not covered:
        # A refusal at workflow scope stops the guard reading anything, so every
        # test file in the repository lands in `unaccounted` at once. Printing
        # all 168 of them says "none of your tests run", which is false and
        # buries the one line that is true. Report the cause and nothing else.
        pytest.fail(
            "The guard could not read the workflow at all, so it credits no "
            "file and a list of unrun tests would be meaningless here. Fix "
            "this first:\n  " + "\n  ".join(problems)
        )

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


def test_no_committed_pytest_config_changes_what_ci_runs():
    """The guard refuses -o and -c, so it has to read the files pytest loads.

    Both options are refused because either can carry addopts, and the same
    setting committed to a pytest.ini, a tox.ini, a setup.cfg or a pyproject
    reaches every run without appearing on a command line at all. The repository
    defines none of those files, and this is what keeps it that way.
    """
    problems = _pytest_configuration_problems()
    assert not problems, (
        "Configuration pytest reads by itself decides what CI runs:\n  "
        + "\n  ".join(problems)
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

# case -> (script, the reason the guard has to give). The second half matters as
# much as the first: a bare `pytest.raises(_Unverifiable)` passes when the script
# is refused for some entirely different reason, so a mutation that deletes one
# check survives as long as any other check still fires on the same line. Every
# entry pins the sentence that names the shape it is about.
_SCRIPT_REFUSALS = {
    "failure is masked with || true": (
        f"pytest {_REAL} || true\n",
        r"shell operator '\|\|'",
    ),
    # `|&` pipes stderr as well as stdout, and the default step shell carries no
    # pipefail, so pytest's exit status is discarded exactly as `|| true`
    # discards it. shlex merges it into one two-character token, which is why the
    # operator check reads punctuation runs rather than a list of spellings.
    "failure is masked by a pipeline that swallows the status": (
        f"pytest {_REAL} |& true\n",
        r"shell operator '\|&'",
    ),
    "a fallthrough separator hides a second command": (
        f"pytest {_REAL} ;& true\n",
        r"shell operator ';&'",
    ),
    "command sits in a branch that is never taken": (
        f"if false; then\n  pytest {_REAL}\nfi\n",
        r"'if false; then' is not a command the guard understands",
    ),
    "command sits in a function nothing calls": (
        f"run_them() {{\n  pytest {_REAL}\n}}\necho done\n",
        r"is not a command the guard understands",
    ),
    "command sits in a heredoc body": (
        f"cat <<EOF\npytest {_REAL}\nEOF\n",
        r"'cat <<EOF' is not a command the guard understands",
    ),
    "command sits after exit 0": (
        f"exit 0\npytest {_REAL}\n",
        r"'exit 0' is not a command the guard understands",
    ),
    "command is backgrounded": (
        f"pytest {_REAL} &\n",
        r"shell operator '&'",
    ),
    "command is piped into something else": (
        f"pytest {_REAL} | tee log.txt\n",
        r"shell operator '\|'",
    ),
    "output is redirected away": (
        f"pytest {_REAL} > /dev/null\n",
        r"shell operator '>'",
    ),
    # Reported as the redirect it is. It used to be reported as "path
    # '/dev/null' escapes the repository", which sends the reader looking for a
    # path problem that is not there.
    "output and errors are both redirected away": (
        f"pytest {_REAL} &> /dev/null\n",
        r"shell operator '&>'",
    ),
    "an existing file is clobbered by a redirect": (
        f"pytest {_REAL} >| log.txt\n",
        r"shell operator '>\|'",
    ),
    "path is produced by command substitution": (
        f"pytest $(echo {_REAL})\n",
        r"shell operator '\('",
    ),
    "pytest is neutered inline": (
        f'PYTEST_ADDOPTS="--collect-only" pytest {_REAL}\n',
        r"PYTEST_ADDOPTS is set on",
    ),
    "pytest is asked only to collect": (
        f"pytest --collect-only {_REAL}\n",
        r"--collect-only in .* stops pytest asserting anything",
    ),
    "pytest is narrowed by an arbitrary -k filter": (
        f"pytest {_REAL} -k nothing_matches\n",
        r"-k in .* stops pytest asserting anything",
    ),
    "an unknown option might change what runs": (
        f"pytest {_REAL} --some-new-flag\n",
        r"pytest option '--some-new-flag' .* is not one the guard knows",
    ),
    "the command is reached through another tool": (
        f"echo {_REAL} | xargs pytest\n",
        r"shell operator '\|'",
    ),
    "the directory is changed to somewhere else first": (
        f"cd tests\npytest {_REAL}\n",
        r"'cd tests' moves the shell out of the repository root",
    ),
    "a loop iterates over something that is not a test path": (
        'for f in one two ; do\n  pytest "$f"\ndone\n',
        r"'one' in .* names no test file",
    ),
    # Reachable, and now reached by the case named after it. The check on where
    # `do` sits used to run after the expansion check, so the one-liner that a
    # maintainer would really write was refused for its `$f` and this sentence
    # was never the one a reader saw.
    "a loop is written on one line": (
        'for f in tests/bazarr/test_pretty_date.py; do pytest "$f"; done\n',
        r"puts the loop body on the header line",
    ),
    "the path names a test file that was deleted": (
        "pytest tests/bazarr/test_deleted_in_some_other_branch.py\n",
        r"names a test file that does not exist",
    ),
    # shlex keeps a quoted substitution as a single token, so no operator check
    # sees it, and bash expands it to --collect-only before pytest starts.
    "command substitution hides inside a quoted token": (
        f"""pytest {_REAL} "$(printf '%s' --collect-only)"\n""",
        r"command substitution in",
    ),
    "backticks hide inside a quoted token": (
        f'pytest {_REAL} "`printf -- --collect-only`"\n',
        r"command substitution in",
    ),
    "an option is hidden behind a variable": (
        f"pytest {_REAL} $EXTRA\n",
        r"parameter expansion '\$EXTRA'",
    ),
    "an expansion is concatenated onto a path": (
        f"pytest {_REAL}$SUFFIX\n",
        r"parameter expansion 'tests/bazarr/test_pretty_date.py\$SUFFIX'",
    ),
    "an assignment value is produced by command substitution": (
        f'FOO="$(echo --collect-only)" pytest {_REAL}\n',
        r"command substitution in",
    ),
    # A node selector runs one case and exits 0. Crediting its module would
    # vouch for every other assertion in the file.
    "the run is narrowed to a single test node": (
        f"pytest {_OTHER}::test_one_case\n",
        r"selects a single test node",
    ),
    "a node selector hides in a longer list": (
        f"pytest {_REAL} {_OTHER}::test_one_case\n",
        r"selects a single test node",
    ),
    "a loop iterates over node selectors": (
        f'for f in \\\n  {_REAL}::test_one_case \\\n  ; do\n  pytest "$f"\ndone\n',
        r"selects a single test node",
    ),
    # pytest matches --ignore-glob recursively during collection, so
    # `--ignore-glob=*ui.py` really drops tests/bazarr/test_ui.py while the same
    # pattern against the repo root matches nothing at all.
    "an ignore glob is matched by pytest, not by the filesystem": (
        "pytest tests/bazarr/ --ignore-glob=*ui.py\n",
        r"--ignore-glob in .* excludes recursively by pattern",
    ),
    # `**` is the mirror image: pathlib recurses and the runner's bash does not,
    # so reading it as recursive credits 55 files under tests/compat/ that bash
    # never hands to pytest.
    "a recursive glob means one level to the runner's bash": (
        "pytest tests/**/test_*.py\n",
        r"uses `\*\*`, which the runner's bash does not expand recursively",
    ),
    # pytest's fromfile_prefix_chars is "@", so the file's contents become
    # arguments before pytest parses anything, and the token reads as a path.
    "arguments are read out of a file pytest expands": (
        f"pytest {_REAL} @pytest.args\n",
        r"is an arguments file",
    ),
    # -o, --override-ini and -c all reach addopts, which is the PYTEST_ADDOPTS
    # hole spelled as a command-line option.
    "an ini option is overridden by a separated value": (
        f"pytest {_REAL} -o addopts=--collect-only\n",
        r"-o in .* overrides an ini option",
    ),
    "the long option overrides the same ini setting inline": (
        f"pytest {_REAL} --override-ini=addopts=--collect-only\n",
        r"--override-ini in .* is the long form of -o",
    ),
    "config is loaded from an arbitrary file": (
        f"pytest {_REAL} -c neutered.ini\n",
        r"-c in .* loads configuration from an arbitrary file",
    ),
    # `set -n` makes bash read the script and execute none of it, exiting 0, so
    # every file the script names would be credited by a step that ran nothing.
    # `set +e` leaves a failing pytest unable to fail the step.
    "execution is switched off by set -n": (
        f"set -n\npytest {_REAL}\n",
        r"'-n' is not a `set` mode the guard has modelled",
    ),
    "a set mode is turned back off": (
        f"set +e\npytest {_REAL}\n",
        r"'\+e' turns a shell mode back off",
    ),
    "a set option is turned back off by its long name": (
        f"set +o pipefail\npytest {_REAL}\n",
        r"'\+o' turns a shell mode back off",
    ),
    "an unmodelled set option is used": (
        f"set -o noexec\npytest {_REAL}\n",
        r"`set -o noexec` is not an option the guard has modelled",
    ),
    "set is given no modes at all": (
        f"set\npytest {_REAL}\n",
        r"`set` with no mode",
    ),
    # This one was blessed as legitimate by the guard's own test data until now,
    # as a leading `PYTHONPATH=. pytest "${f}"` in the loop-body cases. It is not
    # legitimate: `PYTHONPATH=.` puts a repository directory ahead of
    # site-packages, and a sitecustomize.py committed anywhere on that path is
    # imported before pytest parses its first argument, so it can set
    # PYTEST_ADDOPTS without the workflow ever spelling it. The workflow does not
    # use the shape, so refusing it costs nothing today.
    "the interpreter's import path is set on the command": (
        f"PYTHONPATH=. pytest {_REAL}\n",
        r"PYTHONPATH is set on",
    ),
    "an interpreter startup file is set on the command": (
        f"PYTHONSTARTUP=tests/helpers/start.py pytest {_REAL}\n",
        r"PYTHONSTARTUP is set on",
    ),
    # -p was whitelisted with its value never read, which is the same hole -o,
    # --override-ini and -c are refused for: a plugin decides what collection
    # returns, so `-p neuter` can empty the run and still exit 0.
    "a plugin is loaded whose behaviour nobody inspected": (
        f"pytest -p neuter {_REAL}\n",
        r"-p 'neuter' .* loads a pytest plugin",
    ),
    "a plugin disable the guard has not reasoned about": (
        f"pytest -p no:python {_REAL}\n",
        r"-p 'no:python' .* loads a pytest plugin",
    ),
    # Combined short options are accepted only when every letter in the cluster
    # is one of the inert ones, so `-sk` cannot smuggle a -k filter past the
    # check that refuses `-k` on its own.
    "a combined short option hides a filter": (
        f"pytest {_REAL} -sk nothing_matches\n",
        r"combined short options '-sk' .* include -k",
    ),
    "a combined short option hides an ini override": (
        f"pytest {_REAL} -so addopts=--collect-only\n",
        r"combined short options '-so' .* include -o",
    ),
    # A NUL byte used to raise ValueError out of glob, so the guard died with a
    # stack trace instead of reporting a refusal a human could act on.
    # The escape check is lexical and runs before anything touches the
    # filesystem. Deleting it survived a mutation run with every test green.
    "an absolute path is handed to pytest": (
        "pytest /tmp/test_somewhere_else.py\n",
        r"escapes the repository",
    ),
    "a path climbs out of the repository": (
        "pytest tests/../../elsewhere/test_x.py\n",
        r"escapes the repository",
    ),
    "a path carries a NUL byte": (
        f"pytest {_REAL}\x00\n",
        r"contains a NUL byte",
    ),
    "a glob carries a NUL byte": (
        "pytest tests/bazarr/*\x00/test_ui.py\n",
        r"contains a NUL byte",
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


@pytest.mark.parametrize(
    "case,script,reason",
    [(case,) + value for case, value in sorted(_SCRIPT_REFUSALS.items())],
)
def test_script_the_guard_cannot_read_is_refused(case, script, reason):
    """Refusal, not a quiet zero, and refusal for the reason that applies.

    Returning an empty set here would be safe for coverage and useless in
    practice, because the workflow would go red with no idea why. Matching the
    message matters just as much: without it, a mutation that deletes one check
    stays green as long as some other check happens to fire on the same line,
    which is how the `shell:` check and the --ignore subtraction survived a
    mutation run with every test passing.
    """
    with pytest.raises(_Unverifiable, match=reason):
        _script_coverage(script)


@pytest.mark.parametrize(
    "body",
    [
        'pytest "$f"',
        "pytest $f",
        'pytest -q "$f" -p no:cacheprovider',
        'pytest "${f}"',
    ],
)
def test_loop_list_with_pytest_on_the_variable_is_coverage(body):
    script = f"for f in \\\n  {_REAL} \\\n  ; do\n  {body}\ndone\n"
    assert _script_coverage(script) == {_REAL}


def test_the_plugin_disable_the_workflow_uses_is_still_coverage():
    """`-p no:cacheprovider` is the one -p value CI needs, and it must survive."""
    assert _script_coverage(f"pytest {_REAL} -p no:cacheprovider -q\n") == {_REAL}


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


@pytest.fixture
def constructed_repo(tmp_path, monkeypatch):
    """Run the guard against a repository built in the test, not this one.

    The same seam the workflow fixture uses: REPO_ROOT is read off the module,
    so pointing it somewhere else is enough to exercise the filesystem rules
    without committing a symlink or a pytest.ini to the real repository to prove
    they are caught.
    """

    def build(files: dict) -> pathlib.Path:
        for name, content in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
        return tmp_path

    return build


@pytest.mark.parametrize(
    "name,body",
    [
        ("pytest.ini", "[pytest]\naddopts = --collect-only\n"),
        (".pytest.ini", "[pytest]\naddopts = -k nothing_matches\n"),
        ("tox.ini", "[pytest]\ntestpaths = docs\n"),
        ("setup.cfg", "[tool:pytest]\naddopts = --collect-only\n"),
        ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "--collect-only"\n'),
        # pytest searches upward from the common ancestor of its arguments, and
        # every path CI passes is under tests/, so a file there is read first.
        ("tests/pytest.ini", "[pytest]\naddopts = --collect-only\n"),
    ],
)
def test_committed_pytest_config_is_reported(constructed_repo, name, body):
    """The setting -o and -c are refused for, arriving in a file instead.

    Nothing about the workflow changes: the steps still enumerate every path and
    still parse. pytest reads the file on its own and collects nothing.
    """
    constructed_repo({name: body})
    problems = _pytest_configuration_problems()
    assert any(name in problem for problem in problems), (
        f"{name} can stop pytest asserting anything and the guard did not say so"
    )


def test_pytest_config_without_the_dangerous_settings_is_fine(constructed_repo):
    """Refusing every config file outright would be friction with no safety."""
    constructed_repo(
        {
            "pyproject.toml": '[tool.pytest.ini_options]\nmarkers = ["slow"]\n',
            "setup.cfg": "[metadata]\nname = bazarr\n",
        }
    )
    assert not _pytest_configuration_problems()


def test_symlink_out_of_the_repository_is_not_coverage(constructed_repo, tmp_path):
    """The escape check is lexical, so only resolving the path catches this one.

    A committed symlink under tests/ is a repo-relative path by inspection and a
    file somewhere else on disk in fact. Crediting it vouches for assertions the
    repository does not contain. It also read differently across the CI matrix:
    3.12 follows directory symlinks inside a recursive glob and 3.13 onward does
    not, so the same workflow meant two things on two rows.
    """
    root = constructed_repo({"tests/bazarr/test_real.py": ""})
    outside = tmp_path.parent / "outside_of_the_repository"
    outside.mkdir(exist_ok=True)
    (outside / "test_away.py").write_text("")
    (root / "tests" / "linkdir").symlink_to(outside, target_is_directory=True)

    assert _all_test_files() == {"tests/bazarr/test_real.py"}
    with pytest.raises(_Unverifiable):
        _expand("tests/linkdir/test_away.py")
    assert _expand("tests/") == {"tests/bazarr/test_real.py"}


def _one_job(*steps: dict, **job) -> dict:
    """A workflow with one job, triggered and scheduled the way `on:` and
    `runs-on:` now both have to be."""
    job.setdefault("runs-on", "ubuntu-latest")
    return {
        "on": {"pull_request": None},
        "jobs": {"backend": dict(job, steps=list(steps))},
    }


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
        ("PYTHONPATH", {"env": {"PYTHONPATH": "tests/helpers"}, "run": f"pytest {_OTHER}\n"}),
        ("PYTHONSTARTUP", {"env": {"PYTHONSTARTUP": "s.py"}, "run": f"pytest {_OTHER}\n"}),
        ("SOME_NEW_VAR", {"env": {"SOME_NEW_VAR": "1"}, "run": f"pytest {_OTHER}\n"}),
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
        ("PYTHONPATH", {"env": {"PYTHONPATH": "tests/helpers"}}),
        ("SOME_NEW_VAR", {"env": {"SOME_NEW_VAR": "1"}}),
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


@pytest.mark.parametrize("name", ["PYTEST_ADDOPTS", "PYTHONPATH", "PYTHONHOME"])
def test_workflow_level_environment_that_neuters_pytest_is_not_coverage(
    constructed_workflow, name
):
    """PYTHONPATH reaches the same place PYTEST_ADDOPTS does, one step earlier.

    The interpreter imports sitecustomize.py from the first directory on
    PYTHONPATH before pytest parses an argument, and a repository is a place
    where a file like that can be committed. Screening env keys for a PYTEST_
    prefix alone left that route open at all three scopes.
    """
    workflow = _one_job({"run": f"pytest {_REAL}\n"})
    workflow["env"] = {name: "whatever"}
    constructed_workflow(workflow)
    covered, problems = _read_workflow()
    assert not covered
    assert any(name in problem for problem in problems)


def test_workflow_level_unrelated_environment_is_still_coverage(constructed_workflow):
    """The env allowlist binds test-bearing jobs and steps, not the whole file.

    The real workflow sets four UI paths at workflow scope, which have nothing to
    do with the backend suite. Forcing those through a backend allowlist would be
    friction with no safety behind it, so only the scopes that carry the tests
    are restricted; the dangerous names are refused everywhere.
    """
    workflow = _one_job({"run": f"pytest {_REAL}\n"})
    workflow["env"] = {"UI_DIRECTORY": "./frontend"}
    constructed_workflow(workflow)
    covered, problems = _read_workflow()
    assert covered == {_REAL} and not problems


def test_the_one_allowed_test_environment_key_still_counts(constructed_workflow):
    """The Postgres URL the cutover migration test needs is the allowlist."""
    constructed_workflow(
        _one_job(
            {
                "env": {"BAZARR_PG_TEST_URL": "postgresql+psycopg://postgres@localhost/x"},
                "run": f"pytest {_REAL}\n",
            }
        )
    )
    covered, problems = _read_workflow()
    assert covered == {_REAL} and not problems


@pytest.mark.parametrize(
    "case,triggers",
    [
        ("paths", {"pull_request": {"paths": ["docs/**"]}}),
        ("paths-ignore", {"pull_request": {"paths-ignore": ["**"]}}),
        ("types", {"pull_request": {"types": ["labeled"]}}),
        ("pull_request", {"push": {"branches": ["development"]}}),
        ("pull_request", ["push"]),
        ("pull_request", "push"),
        ("on:", {}),
    ],
)
def test_workflow_that_never_fires_on_a_pull_request_is_not_coverage(
    constructed_workflow, case, triggers
):
    """`on:` was safe-listed wholesale, so nobody read what was in it.

    A `paths:` filter under pull_request that a code change never matches
    disables every check this file vouches for, without changing one line the
    guard inspects. The workflow simply never runs, and the guard stays green.
    """
    workflow = _one_job({"run": f"pytest {_REAL}\n"})
    workflow["on"] = triggers
    constructed_workflow(workflow)
    covered, problems = _read_workflow()
    assert not covered, f"a workflow narrowed by {case} was counted as coverage"
    assert any(case in problem for problem in problems)


@pytest.mark.parametrize(
    "triggers",
    [
        {"pull_request": None},
        {"pull_request": {"branches": ["master", "development"]}},
        # A separate branch removes the branch filter. Nothing here fights that.
        {"pull_request": {}},
        {"push": {"paths": ["bazarr/**"]}, "pull_request": None},
        ["push", "pull_request"],
    ],
)
def test_workflow_that_fires_on_any_pull_request_is_still_coverage(
    constructed_workflow, triggers
):
    """Which branches are gated is an ordinary decision, and stays unchecked.

    A `paths:` filter on push is fine too: what has to hold is that an ordinary
    pull request reaches the workflow, whatever it touches.
    """
    workflow = _one_job({"run": f"pytest {_REAL}\n"})
    workflow["on"] = triggers
    constructed_workflow(workflow)
    covered, problems = _read_workflow()
    assert covered == {_REAL} and not problems


@pytest.mark.parametrize("condition", ["always()", "${{ always() }}", True])
def test_step_that_always_runs_is_still_coverage(constructed_workflow, condition):
    """`if: always()` really does always run, so refusing it would be a false alarm.

    An exact literal match on a tiny set, not an expression evaluator: anything
    the guard cannot read off by sight is still refused.
    """
    constructed_workflow(_one_job({"if": condition, "run": f"pytest {_REAL}\n"}))
    covered, problems = _read_workflow()
    assert covered == {_REAL} and not problems


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


@pytest.mark.parametrize(
    "case,script",
    [
        ("GITHUB_ENV", 'echo "PYTEST_ADDOPTS=--collect-only" >> $GITHUB_ENV\n'),
        ("GITHUB_PATH", 'echo "$PWD/fake-bin" >> $GITHUB_PATH\n'),
    ],
)
def test_environment_exported_by_an_earlier_step_is_not_coverage(
    constructed_workflow, case, script
):
    """The step doing this mentions neither pytest nor tests/, and still decides.

    GitHub applies a line written to $GITHUB_ENV to every later step in the job,
    so this is PYTEST_ADDOPTS delivered by a route that no test-bearing step
    check can see. It was the fourth way into a hole the guard had already
    closed three times.
    """
    constructed_workflow(
        _one_job(
            {"name": "Configure environment", "run": script},
            {"run": f"pytest {_REAL}\n"},
        )
    )
    covered, problems = _read_workflow()
    assert not covered, f"a job containing a {case} write was counted as coverage"
    assert any(case in problem for problem in problems)


@pytest.mark.parametrize(
    "case,upstream",
    [
        ("if", {"if": "false"}),
        ("if", {"if": "github.event_name == 'schedule'"}),
        ("some-new-key", {"some-new-key": 1}),
    ],
)
def test_job_reached_through_needs_is_checked_too(constructed_workflow, case, upstream):
    """`if: false` one job away skips the test job just as dead.

    The job-scope checks used to run only on jobs that carry a test-bearing step
    themselves. Frontend carries none, so `if: false` on Frontend skipped
    Frontend, which skipped Backend through its `needs:`, and a skipped job does
    not fail the workflow. The guard credited every path Backend named.
    """
    constructed_workflow(
        {
            "on": {"pull_request": None},
            "jobs": {
                "Frontend": dict(
                    upstream, **{"runs-on": "ubuntu-latest", "steps": [{"run": "npm run build\n"}]}
                ),
                "Backend": {
                    "runs-on": "ubuntu-latest",
                    "needs": "Frontend",
                    "steps": [{"run": f"pytest {_REAL}\n"}],
                },
            }
        }
    )
    covered, problems = _read_workflow()
    assert not covered, f"a job needing a {case} job was counted as coverage"
    assert any(case in problem for problem in problems)


def test_needs_naming_a_job_that_does_not_exist_is_not_coverage(constructed_workflow):
    """GitHub never schedules a job whose dependency is missing."""
    constructed_workflow(_one_job({"run": f"pytest {_REAL}\n"}, needs="NoSuchJob"))
    covered, problems = _read_workflow()
    assert not covered
    assert any("NoSuchJob" in problem for problem in problems)


def test_needs_a_healthy_job_is_still_coverage(constructed_workflow):
    """The real workflow's Backend needs Frontend, so this must not false-alarm."""
    constructed_workflow(
        {
            "on": {"pull_request": None},
            "jobs": {
                "Frontend": {"runs-on": "ubuntu-latest", "steps": [{"run": "npm run build\n"}]},
                "Backend": {
                    "runs-on": "ubuntu-latest",
                    "needs": ["Frontend"],
                    "steps": [{"run": f"pytest {_REAL}\n"}],
                },
            }
        }
    )
    covered, problems = _read_workflow()
    assert covered == {_REAL} and not problems


@pytest.mark.parametrize(
    "case,label",
    [
        ("self-hosted-nope", "self-hosted-nope"),
        ("no runner at all", None),
    ],
)
def test_job_no_runner_picks_up_is_not_coverage(constructed_workflow, case, label):
    """`runs-on:` was safe-listed with its value never read.

    One word, and the job is never scheduled. It does not fail: it sits queued,
    reports no conclusion, and `development` has no required status checks to
    notice. Every path the job enumerated still read as guaranteed coverage.
    """
    job = {} if label is None else {"runs-on": label}
    workflow = _one_job({"run": f"pytest {_REAL}\n"})
    workflow["jobs"]["backend"].pop("runs-on")
    workflow["jobs"]["backend"].update(job)
    constructed_workflow(workflow)
    covered, problems = _read_workflow()
    assert not covered, f"a job with {case} was counted as coverage"
    assert any("runs-on" in problem or "runner" in problem for problem in problems)


@pytest.mark.parametrize(
    "case,strategy",
    [
        ("empty dimension", {"matrix": {"python-version": []}}),
        (
            "every row excluded",
            {
                "matrix": {
                    "python-version": ["3.12", "3.13"],
                    "exclude": [{"python-version": "3.12"}, {"python-version": "3.13"}],
                }
            },
        ),
        ("dimension is an expression", {"matrix": {"python-version": "${{ fromJSON(x) }}"}}),
        ("unknown strategy key", {"matrix": {"python-version": ["3.13"]}, "some-new-key": 1}),
    ],
)
def test_matrix_that_produces_no_row_is_not_coverage(
    constructed_workflow, case, strategy
):
    """A matrix with nothing left in it runs the job zero times.

    Not once with a default: zero. The job does not fail, it never happens, and
    `strategy` sat on the job safe list with its value unread, so emptying
    `python-version` was a one-line edit that left the guard reporting 168 files
    covered and no problems.
    """
    constructed_workflow(_one_job({"run": f"pytest {_REAL}\n"}, strategy=strategy))
    covered, problems = _read_workflow()
    assert not covered, f"a matrix with {case} was counted as coverage"
    assert problems


def test_a_matrix_with_rows_left_is_still_coverage(constructed_workflow):
    """Adding or removing a python version must not fight the guard."""
    constructed_workflow(
        _one_job(
            {"run": f"pytest {_REAL}\n"},
            strategy={
                "fail-fast": False,
                "max-parallel": 2,
                "matrix": {
                    "python-version": ["3.12", "3.13", "3.14"],
                    "exclude": [{"python-version": "3.12"}],
                },
            },
        )
    )
    covered, problems = _read_workflow()
    assert covered == {_REAL} and not problems


_PG_FILE = "tests/bazarr/test_arr_pg_cutover_migration.py"
_PG_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/bazarr_test"
_PG_SERVICE = {
    "postgres": {"image": "postgres:16-alpine", "ports": ["5432:5432"]}
}


@pytest.mark.parametrize(
    "case,job",
    [
        ("no service at all", {}),
        (
            "a service on another port",
            {"services": {"postgres": {"image": "postgres:16-alpine", "ports": ["55432:5432"]}}},
        ),
        (
            "a service that is not postgres",
            {"services": {"db": {"image": "redis:7", "ports": ["5432:5432"]}}},
        ),
    ],
)
def test_a_suite_that_skips_without_its_service_is_not_coverage(
    constructed_workflow, case, job
):
    """Being handed to pytest is not enough when the file skips itself.

    All five cases in the Postgres cutover suite take the pg_bind fixture, which
    calls pytest.skip when nothing answers. So deleting the `services:` block
    was a one-line edit that turned the file into five skips while it stayed
    enumerated, and `services` sat on the job safe list with its value unread.
    """
    constructed_workflow(
        _one_job({"env": {"BAZARR_PG_TEST_URL": _PG_URL}, "run": f"pytest {_PG_FILE}\n"}, **job)
    )
    covered, problems = _read_workflow()
    assert _PG_FILE not in covered, f"with {case}, a suite that skips was credited"
    assert any(_PG_FILE in problem for problem in problems)


def test_the_service_the_workflow_defines_is_still_coverage(constructed_workflow):
    """The real shape has to pass, or the check is just an outage."""
    constructed_workflow(
        _one_job(
            {"env": {"BAZARR_PG_TEST_URL": _PG_URL}, "run": f"pytest {_PG_FILE}\n"},
            services=_PG_SERVICE,
        )
    )
    covered, problems = _read_workflow()
    assert covered == {_PG_FILE} and not problems


def test_the_url_must_point_at_this_job_s_service(constructed_workflow):
    """A URL pointing somewhere else is a claim the workflow cannot support."""
    constructed_workflow(
        _one_job(
            {
                "env": {"BAZARR_PG_TEST_URL": _PG_URL.replace("localhost", "db.example.com")},
                "run": f"pytest {_PG_FILE}\n",
            },
            services=_PG_SERVICE,
        )
    )
    covered, problems = _read_workflow()
    assert _PG_FILE not in covered
    assert any("db.example.com" in problem for problem in problems)


@pytest.mark.parametrize(
    "case,defaults",
    [
        ("pwsh", {"run": {"shell": "pwsh"}}),
        ("python", {"run": {"shell": "python"}}),
        ("bash with no -e", {"run": {"shell": "bash {0}"}}),
        ("a directory that is not the root", {"run": {"working-directory": "tests"}}),
        ("a key nobody has read", {"run": {"some-new-key": 1}}),
    ],
)
def test_defaults_that_change_what_a_run_means_is_not_coverage(
    constructed_workflow, case, defaults
):
    """`defaults:` sets the shell and directory of every `run:` under it.

    Safe-listing the key hands the grammar a script it cannot read; refusing the
    key turns `defaults: run: shell: bash`, which is what the steps effectively
    already get, into a red build. So the value is read.
    """
    workflow = _one_job({"run": f"pytest {_REAL}\n"})
    workflow["defaults"] = defaults
    constructed_workflow(workflow)
    covered, problems = _read_workflow()
    assert not covered, f"defaults naming {case} was counted as coverage"
    assert any("defaults" in problem for problem in problems)


@pytest.mark.parametrize("shell", ["pwsh", "python", "bash {0}", "sh {0}", "bash -n {0}"])
def test_step_shell_the_grammar_does_not_describe_is_not_coverage(
    constructed_workflow, shell
):
    """`bash {0}` is on this list on purpose: no -e.

    Without it a failing pytest halfway through a script leaves the step's
    status to whatever ran last, which is `|| true` spelled as a shell option.
    """
    constructed_workflow(_one_job({"shell": shell, "run": f"pytest {_REAL}\n"}))
    covered, problems = _read_workflow()
    assert not covered, f"shell {shell!r} was counted as coverage"
    assert any("shell" in problem for problem in problems)


@pytest.mark.parametrize(
    "shell", ["bash", "sh", "bash -e {0}", "bash -eo pipefail {0}", "bash -euo pipefail {0}"]
)
def test_a_stricter_shell_than_the_default_is_still_coverage(
    constructed_workflow, shell
):
    """Every one of these is at least as strict as the `bash -e {0}` default.

    Refusing `bash -eo pipefail {0}` while accepting `bash -e {0}` told
    maintainers to write the spelling that lets `pytest X | tee log` pass.
    """
    constructed_workflow(_one_job({"shell": shell, "run": f"pytest {_REAL}\n"}))
    covered, problems = _read_workflow()
    assert covered == {_REAL} and not problems


@pytest.mark.parametrize("condition", ["failure()", "cancelled()", "${{ cancelled() }}"])
def test_a_condition_that_needs_something_to_go_wrong_is_not_coverage(
    constructed_workflow, condition
):
    """`success()` is accepted and these are not, which is the whole distinction.

    A step carrying `success()` runs unless an earlier step already failed, and
    a job with a failed step is red either way, so it cannot hide anything. A
    step carrying `failure()` or `cancelled()` runs only when the run is already
    going wrong, which is never in the case the guard is vouching for.
    """
    constructed_workflow(_one_job({"if": condition, "run": f"pytest {_REAL}\n"}))
    covered, problems = _read_workflow()
    assert not covered, f"a step gated on {condition} was counted as coverage"
    assert any(condition in problem for problem in problems)


def test_ignore_inside_the_directory_it_runs_really_subtracts():
    """The --ignore shape that matters, which no case covered until now.

    Every earlier --ignore case put the ignored path outside the positional set,
    so `collected - subtracted` equalled `collected` in all of them and emptying
    _PYTEST_SUBTRACTING left every test green.
    """
    covered = _script_coverage("pytest tests/compat/ --ignore=tests/compat/contract/\n")
    under = {name for name in _all_test_files() if name.startswith("tests/compat/")}
    contract = {name for name in under if name.startswith("tests/compat/contract/")}
    assert contract, "the fixture directory this case relies on has moved"
    assert covered == under - contract


# ---------------------------------------------------------------------------
# Ordinary maintainer edits, which have to stay green.
#
# This half matters as much as the half above. A guard that goes red on routine,
# correct changes gets deleted by the first person it obstructs, and deleting it
# reopens every shape above at once. An audit of eighteen everyday edits found
# ten of them refused, including `set -euo pipefail`, which is stricter than the
# `set -e` the guard accepted, and a workflow-level `concurrency:` group, which
# emptied coverage entirely and printed 168 files as unrun.
#
# The line is: refusal is for shapes that can stop tests running or mask a
# failure. A key or an option that is inert with respect to whether assertions
# execute is accepted. Each case below is one edit somebody plausibly makes,
# asserted to leave coverage exactly as it was, so a later narrowing pass cannot
# quietly bring the hostility back.
# ---------------------------------------------------------------------------


def _test_bearing_job(workflow: dict) -> dict:
    """The job whose steps run tests, found rather than named."""
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            script = step.get("run") if isinstance(step, dict) else None
            if isinstance(script, str) and _MENTIONS_TESTS.search(script):
                return job
    raise AssertionError("the workflow has no test-bearing job any more")


def _first_test_step(workflow: dict) -> dict:
    for step in _test_bearing_job(workflow).get("steps") or []:
        script = step.get("run") if isinstance(step, dict) else None
        if isinstance(script, str) and _MENTIONS_TESTS.search(script):
            return step
    raise AssertionError("the workflow has no test-bearing step any more")


def _rewrite_runs(workflow: dict, old: str, new: str) -> dict:
    """Rewrite every `run:` containing `old`, refusing to be a silent no-op.

    A case that stops matching would otherwise keep passing while testing
    nothing, which is the failure this whole file is about.
    """
    changed = 0
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            script = step.get("run") if isinstance(step, dict) else None
            if isinstance(script, str) and old in script:
                step["run"] = script.replace(old, new)
                changed += 1
    assert changed, (
        f"no `run:` in the workflow contains {old!r} any more, so this case "
        "tests nothing. Re-anchor it on what the workflow says now"
    )
    return workflow


def _add_workflow_concurrency(workflow: dict) -> dict:
    workflow["concurrency"] = {
        "group": "ci-${{ github.ref }}",
        "cancel-in-progress": True,
    }
    return workflow


def _add_job_concurrency(workflow: dict) -> dict:
    _test_bearing_job(workflow)["concurrency"] = "backend"
    return workflow


def _add_defaults_shell(workflow: dict) -> dict:
    workflow["defaults"] = {"run": {"shell": "bash"}}
    return workflow


def _add_container(workflow: dict) -> dict:
    _test_bearing_job(workflow)["container"] = "python:3.13-slim"
    return workflow


def _add_job_outputs(workflow: dict) -> dict:
    _test_bearing_job(workflow)["outputs"] = {"result": "${{ steps.one.outputs.result }}"}
    return workflow


def _add_job_timeout(workflow: dict) -> dict:
    _test_bearing_job(workflow)["timeout-minutes"] = 45
    return workflow


def _add_step_timeout(workflow: dict) -> dict:
    _first_test_step(workflow)["timeout-minutes"] = 20
    return workflow


def _add_max_parallel(workflow: dict) -> dict:
    _test_bearing_job(workflow)["strategy"]["max-parallel"] = 2
    return workflow


def _add_python_version(workflow: dict) -> dict:
    matrix = _test_bearing_job(workflow)["strategy"]["matrix"]
    matrix["python-version"] = list(matrix["python-version"]) + ["3.15"]
    return workflow


def _newer_runner(workflow: dict) -> dict:
    _test_bearing_job(workflow)["runs-on"] = "ubuntu-24.04"
    return workflow


def _add_step_success_condition(workflow: dict) -> dict:
    _first_test_step(workflow)["if"] = "success()"
    return workflow


def _add_root_working_directory(workflow: dict) -> dict:
    _first_test_step(workflow)["working-directory"] = "."
    return workflow


def _add_pipefail_shell(workflow: dict) -> dict:
    _first_test_step(workflow)["shell"] = "bash -eo pipefail {0}"
    return workflow


def _add_coverage(workflow: dict) -> dict:
    return _rewrite_runs(
        workflow, "-v --tb=short", "-v --tb=short --cov=bazarr --cov-report=xml"
    )


def _add_timeout_option(workflow: dict) -> dict:
    return _rewrite_runs(workflow, "-v --tb=short", "-v --tb=short --timeout=300")


def _combine_short_options(workflow: dict) -> dict:
    return _rewrite_runs(workflow, "-v --tb=short", "-svx --tb=short")


def _quieten(workflow: dict) -> dict:
    return _rewrite_runs(workflow, "-v --tb=short", "-q --tb=short")


def _stricter_set(workflow: dict) -> dict:
    return _rewrite_runs(workflow, "set -e\n", "set -euo pipefail\n")


def _separated_set_modes(workflow: dict) -> dict:
    return _rewrite_runs(workflow, "set -e\n", "set -e -u\n")


def _printf_instead_of_echo(workflow: dict) -> dict:
    return _rewrite_runs(
        workflow, 'echo "::group::$f"', 'printf "::group::%s\\n" "$f"'
    )


def _leading_cd(workflow: dict) -> dict:
    return _rewrite_runs(workflow, "pip install pytest", "cd .\npip install pytest")


def _lint_the_test_tree(workflow: dict) -> dict:
    return _rewrite_runs(workflow, "ruff check .", "ruff check tests/ bazarr/")


def _enumerate_another_file(workflow: dict) -> dict:
    """A file already covered elsewhere, added to a second step.

    Coverage is a set, so the total cannot move; what this pins is that adding a
    positional argument is still readable as one.
    """
    return _rewrite_runs(
        workflow, "pytest tests/compat/", "pytest tests/compat/ " + _REAL
    )


_ORDINARY_EDITS = {
    "a workflow-level concurrency group is added": _add_workflow_concurrency,
    "a job-level concurrency group is added": _add_job_concurrency,
    "the default shell is stated explicitly": _add_defaults_shell,
    "the job moves into a container image": _add_container,
    "the job gains an output": _add_job_outputs,
    "the job gains a timeout": _add_job_timeout,
    "the step gains a timeout": _add_step_timeout,
    "the matrix gains max-parallel": _add_max_parallel,
    "a new python version is added to the matrix": _add_python_version,
    "the runner label is pinned to a version": _newer_runner,
    "a step is marked to run only after the earlier ones passed":
        _add_step_success_condition,
    "a step states the repository root as its working directory":
        _add_root_working_directory,
    "a step asks for pipefail explicitly": _add_pipefail_shell,
    "coverage is measured": _add_coverage,
    "a per-test timeout is added": _add_timeout_option,
    "short options are combined": _combine_short_options,
    "the output is made quiet": _quieten,
    "the script is made stricter with set -euo pipefail": _stricter_set,
    "the set modes are written separately": _separated_set_modes,
    "printf replaces echo": _printf_instead_of_echo,
    "the script changes to the directory it is already in": _leading_cd,
    "the linter is pointed at the test tree": _lint_the_test_tree,
    "another file is enumerated": _enumerate_another_file,
}


@pytest.fixture
def edited_workflow(constructed_workflow):
    """Apply one edit to the repository's real workflow, and report the baseline.

    Built from the real ci.yml rather than a toy, because the claim being made
    is about this workflow: an edit somebody really makes to the file that is
    really there leaves the same 168 files covered.
    """

    def build(edit):
        baseline = _enumerated_paths()
        assert baseline, "the real workflow covers nothing, so the case proves nothing"
        constructed_workflow(edit(_workflow()))
        return baseline

    return build


class TestOrdinaryEditsStayGreen:
    """Everyday edits, each asserted to leave the guard exactly where it was.

    Refusal costs something every time it happens, and it is only worth paying
    where a shape can really stop tests running or mask a failure. None of these
    can. If one of them starts failing, the fix is in the guard, not in the
    workflow: narrowing the guard until ordinary work goes red is how a control
    like this gets deleted, and a deleted guard protects nothing at all.
    """

    @pytest.mark.parametrize("case,edit", sorted(_ORDINARY_EDITS.items()))
    def test_the_edit_is_accepted(self, edited_workflow, case, edit):
        baseline = edited_workflow(edit)
        covered, problems = _read_workflow()
        assert not problems, (
            f"when {case}, the guard refused to read the workflow:\n  "
            + "\n  ".join(problems)
            + "\n\nNothing about this edit can stop a test running or hide a "
            "failure, so refusing it is friction the guard cannot afford."
        )
        assert covered == baseline, (
            f"when {case}, the set of files the guard reads as covered changed "
            f"by {sorted(baseline ^ covered)}"
        )


@pytest.mark.parametrize("case", ["dropped from the workflow", "added to EXCLUDED"])
def test_a_workflow_that_switches_this_guard_off_is_reported(
    constructed_workflow, monkeypatch, case
):
    """The two-edit self-removal, exercised rather than assumed.

    The self-check passes today whatever it says, so nothing noticed when its
    second half was deleted. These two build the states it exists to catch.
    """
    if case == "dropped from the workflow":
        constructed_workflow(_one_job({"run": f"pytest {_REAL}\n"}))
    else:
        constructed_workflow(_one_job({"run": f"pytest {SELF}\n"}))
        monkeypatch.setitem(EXCLUDED, SELF, "excluded with a plausible sentence")
    assert _guard_disabled_problem(), (
        f"with the guard {case}, every check in this file is off and it said so "
        "to nobody"
    )


def test_the_guard_enumerated_and_not_excluded_is_not_reported(constructed_workflow):
    """The state the repository is really in has to read as healthy."""
    constructed_workflow(_one_job({"run": f"pytest {SELF}\n"}))
    assert not _guard_disabled_problem()


def test_a_file_named_the_other_way_round_is_enumerated(constructed_repo):
    """pytest collects `*_test.py` too, and nothing here narrows python_files.

    A file named that way would otherwise get no CI coverage and no complaint,
    which is the exact state this whole file exists to make impossible.
    """
    constructed_repo({"tests/bazarr/test_one.py": "", "tests/bazarr/two_test.py": ""})
    assert _all_test_files() == {"tests/bazarr/test_one.py", "tests/bazarr/two_test.py"}


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
