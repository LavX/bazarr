#!/usr/bin/env python3
# coding=utf-8
"""Decide what one CI run has to do, and hand the answer to the jobs after it.

Every job in .github/workflows/ci.yml that runs tests waits on this script's
`plan` step and reads three outputs from it:

- `code`: "true" unless every file the change touches is documentation. Only
  then are the backend and frontend jobs skipped, and the docs job alone
  decides the pull request.
- `docs`: "true" when a pull request touches documentation, which starts the
  docs job.
- `python`: the JSON list of Python versions the backend jobs run on. A pull
  request into anything but master gets one version, the one the Docker image
  ships, because that is the interpreter users run. Everything else gets the
  full matrix: pushes to development and master, the release pull request into
  master, the weekly schedule and a manual run.

The script fails towards running more. Anything it cannot work out, a missing
parent commit, an unreadable event payload, an empty change list, a Dockerfile
it cannot read a version out of, yields `code=true` and the full matrix. The
only thing that can switch tests off is a change list the script read in full
and found to be documentation from end to end.

tests/bazarr/test_ci_guard_list.py imports this file and checks that no test
file, no Python file and none of the files the tests read can ever be
classified as documentation, and that the version list is never empty.
tests/bazarr/test_ci_plan.py checks the decisions themselves.
"""

import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Every version the backend supports. The Docker image's version must be one
# of them; the guard checks that too.
FULL_MATRIX = ("3.12", "3.13", "3.14")

# What counts as documentation. fnmatch semantics, so `*` crosses directories.
DOCS_PATTERNS = (
    "docs/*",
    "*.md",
    "site/*",
)

# Files that look like documentation and are read by a test, so a change to
# one of them has to run the backend. tests/bazarr/test_container_hardening.py
# parses the compose block out of the README and checks the installer and the
# getting-started guide for the same hardening.
TESTED_DOCUMENTS = frozenset({
    "README.md",
    "site/install.sh",
    "site/guides/getting-started.html",
})

# Trees that are never documentation, whatever the file inside them is called:
# a Markdown fixture under tests/ is test input, and a README inside a source
# tree ships with the code it sits next to.
CODE_TREES = ("tests/", "bazarr/", "custom_libs/", "migrations/", "frontend/src/")


def is_documentation(path: str) -> bool:
    """Whether a change to this repository-relative path cannot affect a test."""
    path = path.strip()
    if not path or path in TESTED_DOCUMENTS:
        return False
    if path.startswith(CODE_TREES) or path.endswith(".py"):
        return False
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in DOCS_PATTERNS)


def docker_python(dockerfile: pathlib.Path = REPO_ROOT / "Dockerfile") -> str:
    """The Python version the production image is built on, or "" if unreadable."""
    try:
        text = dockerfile.read_text()
    except OSError:
        return ""
    match = re.search(r"^FROM\s+python:(\d+\.\d+)", text, flags=re.MULTILINE)
    return match.group(1) if match else ""


def plan(event: str, base_ref: str, changed, image_python: str = None) -> dict:
    """What a run for this event and change list has to do.

    `changed` is the list of changed paths, or None when it could not be read.
    """
    if image_python is None:
        image_python = docker_python()
    files = [path for path in (changed or []) if path.strip()]
    documentation = [path for path in files if is_documentation(path)]
    docs_only = bool(files) and len(documentation) == len(files)

    one_version = (
        event == "pull_request"
        and base_ref != "master"
        and image_python in FULL_MATRIX
    )
    return {
        "code": not docs_only,
        # An unreadable change list starts the docs job too, which then fails
        # loudly for the same reason instead of being skipped in silence.
        "docs": event == "pull_request" and (changed is None or bool(documentation)),
        "python": [image_python] if one_version else list(FULL_MATRIX),
    }


def _git(*args: str):
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else None


def changed_files(event: str, payload: dict):
    """Paths this run's change touches, or None when they cannot be read in full.

    A pull request is checked out as GitHub's merge commit, whose first parent
    is the tip of the base branch, so HEAD^1..HEAD is exactly what merging the
    pull request would change. A push compares against the commit the branch
    pointed at before, fetched on demand when it is not in the shallow clone.
    """
    if event == "pull_request":
        base = "HEAD^1"
    elif event == "push":
        base = str(payload.get("before") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", base) or set(base) == {"0"}:
            return None
        if _git("cat-file", "-e", base + "^{commit}") is None:
            _git("fetch", "--no-tags", "--depth=1", "origin", base)
    else:
        return None
    listing = _git("diff", "--name-only", "--no-renames", base, "HEAD")
    if listing is None:
        return None
    return [line for line in listing.splitlines() if line.strip()]


def main() -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    base_ref = os.environ.get("GITHUB_BASE_REF", "")
    payload = {}
    try:
        with open(os.environ.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        pass

    changed = changed_files(event, payload)
    decision = plan(event, base_ref, changed)

    outputs = {
        "code": "true" if decision["code"] else "false",
        "docs": "true" if decision["docs"] else "false",
        "python": json.dumps(decision["python"]),
    }
    lines = [f"{key}={value}" for key, value in outputs.items()]
    print("\n".join(lines))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        read = "could not be read, so everything runs" if changed is None else (
            f"{len(changed)} changed file(s)"
        )
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"### CI plan\n\n- Event: `{event}`, base `{base_ref or '-'}`\n"
                f"- Change: {read}\n"
                f"- Backend and frontend: {'run' if decision['code'] else 'skipped, documentation only'}\n"
                f"- Docs checks: {'run' if decision['docs'] else 'not needed'}\n"
                f"- Python: {', '.join(decision['python'])}\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
