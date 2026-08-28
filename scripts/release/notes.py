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
import json
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
    """PR numbers merged in previous..head, oldest first.

    Release-cut merges (a development->master merge names LavX/development as
    its source) are excluded: they are the previous cut itself, not content.
    """
    result = run(['git', '-C', str(REPO_ROOT), 'log', '--merges',
                  '--pretty=%s', f'{previous}..{head}'])
    if result.returncode != 0:
        fail(f'git log {previous}..{head} failed: {result.stderr.strip()}')
    numbers = []
    for subject in result.stdout.splitlines():
        match = MERGE_RE.match(subject)
        if not match:
            continue
        if match.group(2) == 'LavX/development':
            continue
        numbers.append(int(match.group(1)))
    return sorted(set(numbers))


def pr_titles(numbers):
    """Best-effort PR titles via gh; empty dict when gh is unavailable."""
    titles = {}
    for number in numbers:
        result = run(['gh', 'pr', 'view', str(number), '-R', REPO_SLUG,
                      '--json', 'title', '--jq', '.title'])
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

def render_check(markdown):
    """Return a list of problems; empty means the draft renders correctly."""
    problems = []
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as source:
        source.write(markdown)
        source_path = source.name
    result = run(['pandoc', '-f', 'gfm', '-t', 'html', source_path])
    pathlib.Path(source_path).unlink()
    if result.returncode != 0:
        return [f'pandoc failed: {result.stderr.strip()}']
    html = result.stdout

    has_hero_line = any(line.startswith('![') for line in markdown.splitlines())
    if has_hero_line and '<img' not in html:
        problems.append('a hero line is present but no <img> rendered: '
                        'the image line does not parse as an image')

    if '```' in html:
        problems.append('literal triple backticks survive in the rendered '
                        'HTML: a code fence degraded to text')

    fence_count = markdown.count('```')
    if fence_count % 2 != 0:
        problems.append(f'odd number of code fences ({fence_count}): one is unclosed')

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

    problems = render_check(markdown)
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
    p_check.set_defaults(func=check)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
