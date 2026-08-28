#!/usr/bin/env python3
# coding=utf-8
"""Release-notes scaffold and GFM render check.

scaffold: emits the class-appropriate skeleton (feature versus patch) with
the mechanical parts computed rather than typed: the complete Included Pull
Requests list and the Full Changelog compare link derived from the git range,
and the Docker section with the unprefixed version tag, latest pull command
and the post-upgrade check line. Prose sections are clearly marked
placeholders for a human.

check: renders a draft (a file, or a GitHub release's body) with pandoc GFM
and fails loudly when the hero image line does not parse as an image or any
code fence degrades to literal backticks. The draft editor's preview is not
the public page and has lied before.

Usage:
  scripts/release/notes.py scaffold --version 2.6.0 --codename Clockwork \
      --previous v2.5.2 [--head development] [--size feature|patch]
  scripts/release/notes.py check --file docs/release-notes/v2.6.0-clockwork.md
  scripts/release/notes.py check --release v2.6.0
"""

import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REPO_SLUG = 'LavX/bazarr'

MERGE_RE = re.compile(r'^Merge pull request #(\d+) from (\S+)')


def fail(message):
    print(f'FAIL: {message}', file=sys.stderr)
    sys.exit(1)


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


# ---------------------------------------------------------------------------
# scaffold
# ---------------------------------------------------------------------------

def merged_prs(previous, head):
    """PR numbers merged in previous..head, ascending.

    Both merge shapes are collected: GitHub merge-commit subjects and
    squash-merge subjects carrying the trailing "(#N)" suffix (this repo has
    both). Release-cut merges (a development->master merge names
    LavX/development as its source) are excluded: they are the previous cut
    itself, not content.
    """
    result = run(['git', '-C', str(REPO_ROOT), 'log',
                  '--pretty=%s', f'{previous}..{head}'])
    if result.returncode != 0:
        fail(f'git log {previous}..{head} failed: {result.stderr.strip()}')
    numbers = []
    for subject in result.stdout.splitlines():
        match = MERGE_RE.match(subject)
        if match:
            if match.group(2) != 'LavX/development':
                numbers.append(int(match.group(1)))
            continue
        squash = re.search(r'\(#(\d+)\)\s*$', subject)
        if squash:
            numbers.append(int(squash.group(1)))
    return sorted(set(numbers))


def pr_titles(numbers):
    """Best-effort PR titles via gh; empty dict when gh is unavailable."""
    titles = {}
    for number in numbers:
        try:
            result = run(['gh', 'pr', 'view', str(number), '-R', REPO_SLUG,
                          '--json', 'title', '--jq', '.title'])
        except FileNotFoundError:
            # No gh on this machine: every title becomes a placeholder.
            return {}
        if result.returncode == 0 and result.stdout.strip():
            titles[number] = result.stdout.strip().rstrip('.')
    return titles


def included_prs_block(numbers, size):
    if not numbers:
        return '(none found in the range; check --previous/--head)'
    if size == 'patch':
        return '\n\n'.join(f'#{n}' for n in numbers)
    titles = pr_titles(numbers)
    return '\n'.join(
        f'- #{n}: {titles[n]}.' if n in titles else f'- #{n}: PLACEHOLDER title.'
        for n in numbers)


def scaffold(args):
    version = args.version
    codename = args.codename
    tag = f'v{version}'
    compare = (f'https://github.com/{REPO_SLUG}/compare/'
               f'{args.previous}...{tag}')
    numbers = merged_prs(args.previous, args.head)
    prs = included_prs_block(numbers, args.size)

    hero = (f'![Bazarr+ {tag} - {codename}]'
            f'(https://raw.githubusercontent.com/{REPO_SLUG}/development/'
            f'screenshot/hero-{codename.lower()}-{tag}.gif)\n\n'
            if args.size == 'feature' else '')

    other = ('## Other Improvements & Fixes\n\n'
             'PLACEHOLDER: grouped smaller items.\n\n---\n\n'
             if args.size == 'feature' else '')

    body = f"""{hero}# Bazarr+ {tag} ({codename})

Codename: **{codename}**

PLACEHOLDER: intro lead paragraph. What this release is, in two or three
sentences a user cares about.

---

## Headline: PLACEHOLDER main item

PLACEHOLDER: real prose. What was wrong, the root cause, what changes for
the user.

---

{other}## CI / Docker

PLACEHOLDER: workflow and image changes, or state there were none.

---

## Dependency Updates

PLACEHOLDER: notable bumps, or state there were none.

---

## Database Migrations

PLACEHOLDER: name the migrations, or state "No schema changes."

---

## Included Pull Requests

{prs}

---

## Upgrade / Migration Notes

- PLACEHOLDER: breaking changes or "No breaking changes. Update and restart as usual."
- **PostgreSQL remains fully supported and first-class.**

---

## Docker

```bash
docker pull ghcr.io/lavx/bazarr:{version}
docker pull ghcr.io/lavx/bazarr:latest
```

After upgrade, confirm the UI loads and `/api/system/status` reports `{version}`.

---

## Contributors

PLACEHOLDER: name external reporters and contributors, or thank the community.

---

**Full Changelog**: {compare}
"""
    print(body, end='')


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def render_check(markdown, require_hero=False):
    """Return a list of problems; empty means the draft renders correctly.

    require_hero: a feature release must open with a hero image; without the
    flag a hero is only validated when present (patch releases have none)."""
    problems = []
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as source:
        source.write(markdown)
        source_path = source.name
    result = run(['pandoc', '-f', 'gfm', '-t', 'html', source_path])
    pathlib.Path(source_path).unlink()
    if result.returncode != 0:
        return [f'pandoc failed: {result.stderr.strip()}']
    html = result.stdout

    # The hero line is checked in isolation: any other valid screenshot in
    # the body would otherwise mask a malformed hero. Indented hero lines are
    # still detected (indentation is itself a way a hero breaks).
    hero_lines = [line for line in markdown.splitlines()
                  if line.lstrip().startswith('![')]
    if require_hero:
        # The hero must be the OPENING line: a valid screenshot later in the
        # body must not satisfy the requirement.
        first_substantive = next(
            (line for line in markdown.splitlines() if line.strip()), '')
        if not first_substantive.lstrip().startswith('!['):
            problems.append('the notes must open with the hero image line; '
                            f'first content is: {first_substantive[:60]!r}')
    for hero_line in hero_lines[:1]:
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as hero_source:
            hero_source.write(hero_line + '\n')
            hero_path = hero_source.name
        hero_result = run(['pandoc', '-f', 'gfm', '-t', 'html', hero_path])
        pathlib.Path(hero_path).unlink()
        if hero_result.returncode != 0 or '<img' not in hero_result.stdout:
            problems.append('the hero line does not parse as an image: '
                            + hero_line[:80])

    if '```' in html:
        problems.append('literal triple backticks survive in the rendered '
                        'HTML: a code fence degraded to text')

    # Both GFM fence forms, paired by character AND length: a closing fence
    # must use the opener's character and be at least as long (~~~~bash is
    # not closed by ~~~), and a fence line of the other character inside an
    # open block is content, not a delimiter.
    open_fence = None
    for line in markdown.splitlines():
        stripped = line.lstrip()
        for char in ('`', '~'):
            if stripped.startswith(char * 3):
                run_length = len(stripped) - len(stripped.lstrip(char))
                if open_fence is None:
                    open_fence = (char, run_length)
                elif open_fence[0] == char and run_length >= open_fence[1]:
                    open_fence = None
                break
    if open_fence is not None:
        problems.append(f'unclosed {open_fence[0] * 3} fence: the rest of '
                        'the document renders as code')

    return problems


def check(args):
    if args.file:
        markdown = pathlib.Path(args.file).read_text()
        source = args.file
    else:
        result = run(['gh', 'api', f'repos/{REPO_SLUG}/releases/tags/{args.release}',
                      '--jq', '.body'])
        if result.returncode != 0:
            # Draft releases are not addressable by tag; fall back to the list.
            result = run(['gh', 'api', f'repos/{REPO_SLUG}/releases',
                          '--jq',
                          f'.[] | select(.tag_name == "{args.release}") | .body'])
        if result.returncode != 0 or not result.stdout.strip():
            fail(f'could not fetch release body for {args.release}')
        markdown = result.stdout
        source = f'release {args.release}'

    problems = render_check(markdown, require_hero=args.require_hero)
    if problems:
        for problem in problems:
            print(f'FAIL: {problem}', file=sys.stderr)
        sys.exit(1)
    print(f'PASS: {source} renders correctly '
          '(hero parses as an image where present, code fences intact)')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)

    p_scaffold = sub.add_parser('scaffold', help='emit a release-notes skeleton')
    p_scaffold.add_argument('--version', required=True, help='bare semver, e.g. 2.6.0')
    p_scaffold.add_argument('--codename', required=True)
    p_scaffold.add_argument('--previous', required=True,
                            help='previous release tag, e.g. v2.5.2')
    p_scaffold.add_argument('--head', default='development',
                            help='end of the range (default: development)')
    p_scaffold.add_argument('--size', choices=['feature', 'patch'], default='feature')
    p_scaffold.set_defaults(func=scaffold)

    p_check = sub.add_parser('check', help='pandoc GFM render check of a draft')
    group = p_check.add_mutually_exclusive_group(required=True)
    group.add_argument('--file', help='markdown file to check')
    group.add_argument('--release', help='release tag whose body to check, e.g. v2.6.0')
    p_check.add_argument('--require-hero', action='store_true',
                         help='fail when no hero image line is present '
                              '(feature releases open with one)')
    p_check.set_defaults(func=check)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
