#!/usr/bin/env python3
# coding=utf-8
"""Checks for a change that touches documentation.

The CI Docs job runs this on every pull request that changes a file
.github/scripts/ci_plan.py classifies as documentation. It checks only what
the change adds, so an old page is never failed for text nobody touched:

- Release notes under docs/release-notes/ render with pandoc GFM, through
  `scripts/release/notes.py check`, the same check the release process runs.
- Added lines carry no em or en dash.
- A relative Markdown link on an added line points at a file that exists.

Usage:
  .github/scripts/docs_check.py              # CI: the change HEAD^1..HEAD
  .github/scripts/docs_check.py --base REF   # locally: REF's merge base to the working tree
"""

import argparse
import importlib.util
import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "ci_plan", pathlib.Path(__file__).with_name("ci_plan.py")
)
ci_plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_plan)

# Spelled as code points so this file does not contain the characters it bans.
DASHES = {chr(0x2014): "em dash", chr(0x2013): "en dash"}
LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def changed(base: str, head: list) -> list:
    listing = git("diff", "--name-only", "--no-renames", "--diff-filter=d", base, *head)
    files = [line for line in listing.splitlines() if line.strip()]
    if not head:
        # Comparing with the working tree: new files git does not track yet
        # are part of the change too.
        files += git("ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(files))


def added_lines(base: str, head: list, path: str) -> list:
    """(line number, text) for every line the change adds to `path`."""
    if not git("ls-files", "--", path).strip():
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        return list(enumerate(text.splitlines(), 1))
    lines = []
    number = 0
    for line in git("diff", "-U0", "--no-color", base, *head, "--", path).splitlines():
        hunk = HUNK.match(line)
        if hunk:
            number = int(hunk.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            lines.append((number, line[1:]))
            number += 1
    return lines


def link_problem(path: str, target: str) -> str:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith(("#", "/")):
        return ""
    if "{{" in target or "{%" in target:
        return ""
    relative = target.split("#", 1)[0].split("?", 1)[0]
    if not relative:
        return ""
    resolved = (REPO_ROOT / path).parent / relative
    return "" if resolved.exists() else f"links to {target}, which does not exist"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", help="compare the working tree with this ref's merge base")
    args = parser.parse_args()

    if args.base:
        base, head = git("merge-base", args.base, "HEAD").strip(), []
    else:
        base, head = "HEAD^1", ["HEAD"]

    documents = [
        path
        for path in changed(base, head)
        if ci_plan.is_documentation(path) and (REPO_ROOT / path).is_file()
    ]
    if not documents:
        print("No documentation in this change.")
        return 0

    problems = []
    for path in documents:
        if re.fullmatch(r"docs/release-notes/[^/]+\.md", path):
            result = subprocess.run(
                [sys.executable, "scripts/release/notes.py", "check", "--file", path],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                problems.append(f"{path}: does not render cleanly\n    {detail}")

        if not path.endswith((".md", ".html", ".txt")):
            continue
        for number, text in added_lines(base, head, path):
            for dash, name in DASHES.items():
                if dash in text:
                    problems.append(f"{path}:{number}: adds an {name}: {text.strip()[:120]}")
            if path.endswith(".md"):
                for target in LINK.findall(text):
                    problem = link_problem(path, target)
                    if problem:
                        problems.append(f"{path}:{number}: {problem}")

    print(f"Checked {len(documents)} changed document(s).")
    if problems:
        print("\n".join(["", "Documentation problems:"] + problems))
        return 1
    print("No problems found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
