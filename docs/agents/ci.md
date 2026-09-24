# Continuous integration

Everything a pull request is checked against lives in one workflow,
`.github/workflows/ci.yml`. It ends in one job, `ci-ok`, which is green only
when every other job either passed or was skipped because the change did not
need it. That is the check to require in a branch rule.

## What runs

| Job | What it does |
| --- | --- |
| Plan | Reads the change and decides what the run needs (`.github/scripts/ci_plan.py`) |
| Docs | Release-notes render check, and no em or en dash or broken relative link on an added line (`.github/scripts/docs_check.py`) |
| Frontend build | `npm run build:ci`, uploaded for the backend jobs |
| Frontend checks | Types, ESLint and Prettier |
| Frontend tests 1/3 to 3/3 | Vitest in three shards, each recording coverage |
| Frontend coverage | Merges the three shards and enforces the coverage floor |
| Backend core | ruff, the compat subpackage and the dependency, Provider Hub and Subliminal rebase guards |
| Backend suites | The previously unguarded suites, then the application startup check |
| Backend isolation 1/5 to 5/5 | The cross-instance isolation guards, one pytest process per file, against PostgreSQL |
| ci-ok | The verdict |

Plan decides three things:

- **Documentation only.** When every changed file is documentation (`docs/`,
  Markdown files, `site/`), the frontend and backend jobs are skipped and the
  Docs job decides. `README.md`, `site/install.sh` and
  `site/guides/getting-started.html` are read by a test, so they count as code,
  and so does anything under `tests/`, `bazarr/`, `custom_libs/`,
  `migrations/` and `frontend/src/`, and every `.py` file.
- **Python versions.** A pull request into development, or into any branch but
  master, runs the backend on the version the Docker image ships. A push to
  development or master, the release pull request into master, the weekly
  schedule (which GitHub runs on master, the default branch) and a manual run
  use 3.12, 3.13 and 3.14, with the same jobs and steps on each.
- **The Docs job** runs when a pull request touches documentation.

When Plan cannot read the change, it runs everything.

A pull request's superseded runs are cancelled when a new commit lands, and so
are superseded development runs. Runs on master are never cancelled.

CodeQL is GitHub's default setup, not a file in this repository. It runs on the
pull request into master and on master itself, and takes about two minutes.

## Adding a test file

`tests/bazarr/test_ci_guard_list.py` fails when a test file is not run by CI.
Name the file in one of the pytest steps of `ci.yml`:

- Most files go in a list in Backend core or Backend suites.
- A file that leaks module state into other files, or needs PostgreSQL, goes in
  one of the Backend isolation loops. The five loops are balanced by run time,
  about five minutes each; add the file to whichever is shortest in the last
  run's job timings.

The guard reads the workflow strictly. Its docstring explains what it accepts
and why, including the one condition it allows on a job: Plan's `code` output.

## Running it locally

```sh
scripts/ci/run-local.sh                  # everything a pull request runs
scripts/ci/run-local.sh --backend-only   # ruff, pytest and the startup check
scripts/ci/run-local.sh --frontend-only  # build, checks, tests and coverage
scripts/ci/run-local.sh --python 3.12    # the backend on another version
scripts/ci/run-local.sh --jobs 8         # fewer processes at a time
```

It reads the suites out of `ci.yml`, so it cannot drift from CI. It needs the
Python version (found through `uv` when installed), node and npm, and docker.
It starts a throwaway PostgreSQL 16 on a random local port, gives each parallel
worker its own database, and removes the container on exit. It never touches
another container, including `bazarr`, `bazarr-standalone` and `bazarr-atlas`.
The startup check runs on a free port with a temporary configuration
directory, so an instance already running on 6767 is not involved.

The virtualenv is cached under `~/.cache/bazarr-ci` and rebuilt when the
requirements change; logs of every run are kept there too. The isolation files
run in parallel rather than in five serial loops, and each run records how
long every task took so the next one starts the longest first. The summary
lists every CI job with its result and time, and the script exits non-zero
when anything failed.
