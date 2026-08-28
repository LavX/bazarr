#!/usr/bin/env python3
# coding=utf-8
"""Release-prep version stamp.

Produces the release-prep edit in the shape the way of work defines (stable
across the v2.5.1 and v2.5.2 prep commits): the package_info version line,
the What's New data (latest-version token bumped and the release's slide
entries scaffolded), and the site landing page's JSON-LD softwareVersion.

The What's New slide prose stays human-written: the scaffold emits clearly
marked placeholder slides, one per --slide item, that typecheck against the
WhatsNewSlide interface. The tool edits the working tree and prints the
resulting diff; it never commits.

Usage:
  scripts/release/stamp.py --version 2.7.0 --codename Atlas \
      --slide "Discover page" --slide "Another user-visible change"
"""

import argparse
import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

PACKAGE_INFO = REPO_ROOT / 'package_info'
WHATS_NEW = REPO_ROOT / 'frontend' / 'src' / 'data' / 'whatsNew.ts'
SITE_INDEX = REPO_ROOT / 'site' / 'index.html'

# The scaffold's placeholder icon; added to the import block when missing.
PLACEHOLDER_ICON = 'faStar'

VERSION_RE = re.compile(r'^\d+\.\d+\.\d+$')


def fail(message):
    print(f'ERROR: {message}', file=sys.stderr)
    sys.exit(1)


def stamp_package_info(version):
    text = PACKAGE_INFO.read_text()
    new_text, count = re.subn(r'(?m)^packageversion=.*$',
                              f'packageversion=Bazarr+ v{version}', text)
    if count != 1:
        fail(f'expected exactly one packageversion line in {PACKAGE_INFO}, found {count}')
    PACKAGE_INFO.write_text(new_text)


def scaffold_slides(items):
    slides = []
    for item in items:
        slides.append(
            '    {\n'
            f'      title: "PLACEHOLDER: {item}",\n'
            '      body: "PLACEHOLDER: one to three lines, plain user language, '
            'written by a human before the cut.",\n'
            f'      icon: {PLACEHOLDER_ICON},\n'
            '      // cta: { label: "Open ...", to: "/..." },\n'
            '    },'
        )
    return '\n'.join(slides)


def stamp_whats_new(version, items):
    text = WHATS_NEW.read_text()

    new_text, count = re.subn(r'export const latestWhatsNewVersion = "[^"]+";',
                              f'export const latestWhatsNewVersion = "{version}";',
                              text)
    if count != 1:
        fail('latestWhatsNewVersion token not found in whatsNew.ts')
    text = new_text

    if f'"{version}": [' in text:
        fail(f'whatsNew.ts already has an entry for {version}')

    marker = 'export const whatsNew: Record<string, WhatsNewSlide[]> = {\n'
    if marker not in text:
        fail('whatsNew record marker not found in whatsNew.ts')
    entry = f'  "{version}": [\n{scaffold_slides(items)}\n  ],\n'
    text = text.replace(marker, marker + entry, 1)

    # The placeholder icon must be imported for the scaffold to typecheck.
    if PLACEHOLDER_ICON not in text.split('} from "@fortawesome/free-solid-svg-icons";')[0]:
        text = re.sub(r'(import \{\n)',
                      r'\g<1>  ' + PLACEHOLDER_ICON + ',\n', text, count=1)

    WHATS_NEW.write_text(text)


def stamp_site(version):
    text = SITE_INDEX.read_text()
    new_text, count = re.subn(r'"softwareVersion": "[^"]+"',
                              f'"softwareVersion": "{version}"', text)
    if count != 1:
        fail(f'expected exactly one softwareVersion in {SITE_INDEX}, found {count}')
    SITE_INDEX.write_text(new_text)
    # The redesigned landing page's roadmap is themed prose (Shipped / Next /
    # v3.0.0), not a per-version list, so it cannot be stamped mechanically.
    print('NOTE: site/index.html roadmap ("Shipped" horizon) is prose; '
          'update it by hand as part of the prep review.')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--version', required=True,
                        help='bare semver, e.g. 2.7.0 (no v prefix)')
    parser.add_argument('--codename', required=True,
                        help='release codename, e.g. Atlas')
    parser.add_argument('--slide', action='append', default=[], dest='slides',
                        metavar='ITEM',
                        help='one user-visible item per flag; each becomes a '
                             'placeholder What\'s New slide (at least one required)')
    args = parser.parse_args()

    if not VERSION_RE.match(args.version):
        fail('--version must be bare semver like 2.7.0 (no v prefix)')
    if not args.slides:
        fail('at least one --slide item is required: the What\'s New entry is '
             'a content deliverable of every release, not a version bump')

    stamp_package_info(args.version)
    stamp_whats_new(args.version, args.slides)
    stamp_site(args.version)

    print(f'Stamped Bazarr+ v{args.version} ({args.codename}). Working-tree diff:\n')
    sys.stdout.flush()
    subprocess.run(['git', '-C', str(REPO_ROOT), '--no-pager', 'diff', '--stat',
                    str(PACKAGE_INFO), str(WHATS_NEW), str(SITE_INDEX)], check=False)
    subprocess.run(['git', '-C', str(REPO_ROOT), '--no-pager', 'diff',
                    str(PACKAGE_INFO), str(WHATS_NEW), str(SITE_INDEX)], check=False)
    print('\nReview, replace every PLACEHOLDER, then commit as '
          f'"release: Bazarr+ v{args.version} ({args.codename})". '
          'This tool never commits.')


if __name__ == '__main__':
    main()
