#!/usr/bin/env python3
# coding=utf-8
"""Post-cut release verifier.

Read-only checks that a cut actually reached users on every surface the way
of work names: the versioned and latest image tags resolve on the registry
and carry the expected version, the Pages site's JSON-LD reports the new
softwareVersion, and the public release page exists and its body passes the
same render verification as the draft check. Each check reports pass or fail
independently with what it observed.

Usage:
  scripts/release/verify.py --version 2.6.0
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO_SLUG = 'LavX/bazarr'
IMAGE = 'lavx/bazarr'
PAGES_URL = 'https://lavx.github.io/bazarr/'

# Pages deploys propagate; retry briefly before declaring failure.
PAGES_ATTEMPTS = 6
PAGES_DELAY_SECONDS = 10

MANIFEST_TYPES = ('application/vnd.oci.image.index.v1+json, '
                  'application/vnd.docker.distribution.manifest.list.v2+json, '
                  'application/vnd.oci.image.manifest.v1+json, '
                  'application/vnd.docker.distribution.manifest.v2+json')


def http_get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode('utf-8', 'replace')


def registry_token():
    payload = http_get(f'https://ghcr.io/token?scope=repository:{IMAGE}:pull')
    return json.loads(payload)['token']


def _manifest_version_label(token, manifest):
    headers = {'Authorization': f'Bearer {token}'}
    config_digest = manifest['config']['digest']
    config = json.loads(http_get(
        f'https://ghcr.io/v2/{IMAGE}/blobs/{config_digest}', headers))
    labels = config.get('config', {}).get('Labels') or {}
    return labels.get('org.opencontainers.image.version')


def image_config_versions(token, tag):
    """(platform, version-label) per real platform of a tag, without pulling
    any layer: manifest (or index -> every non-attestation manifest) plus the
    config blob each. Every platform is checked: one stale architecture in a
    multi-arch index must not hide behind another's correct label."""
    headers = {'Authorization': f'Bearer {token}', 'Accept': MANIFEST_TYPES}
    manifest = json.loads(http_get(
        f'https://ghcr.io/v2/{IMAGE}/manifests/{tag}', headers))
    if 'manifests' not in manifest:
        return [('single', _manifest_version_label(token, manifest))]
    results = []
    for entry in manifest['manifests']:
        platform = entry.get('platform', {})
        if platform.get('os') == 'unknown':
            continue  # attestation manifest
        child = json.loads(http_get(
            f'https://ghcr.io/v2/{IMAGE}/manifests/{entry["digest"]}', headers))
        name = f"{platform.get('os', '?')}/{platform.get('architecture', '?')}"
        results.append((name, _manifest_version_label(token, child)))
    return results


def check_image_tag(token, tag, expected_version, results):
    name = f'image ghcr.io/{IMAGE}:{tag}'
    try:
        versions = image_config_versions(token, tag)
    except urllib.error.HTTPError as error:
        results.append((name, False, f'tag does not resolve (HTTP {error.code})'))
        return
    except Exception as error:  # network shapes vary; report, do not crash
        results.append((name, False, f'lookup failed: {error}'))
        return
    if not versions:
        results.append((name, False, 'index carries no platform manifests'))
        return
    # Exact match: a substring test would let 2.6.0-rc1 or 12.6.0 satisfy a
    # 2.6.0 verification, and the manual Docker workflow can produce exactly
    # that mismatch. A missing label is a failure too: without it the
    # verifier cannot establish the tag contains the requested release.
    wrong = [(platform, observed) for platform, observed in versions
             if observed != expected_version]
    if wrong:
        detail = ', '.join(f'{platform}: {observed!r}' for platform, observed in wrong)
        results.append((name, False,
                        f'expected every platform label to equal '
                        f'{expected_version!r}; got {detail}'))
    else:
        detail = ', '.join(f'{platform}: {observed!r}'
                           for platform, observed in versions)
        results.append((name, True, detail))


def check_pages(expected_version, results):
    name = f'pages {PAGES_URL}'
    observed = None
    for attempt in range(1, PAGES_ATTEMPTS + 1):
        try:
            html = http_get(PAGES_URL)
        except Exception as error:
            observed = f'fetch failed: {error}'
        else:
            match = re.search(r'"softwareVersion":\s*"([^"]+)"', html)
            observed = match.group(1) if match else 'no softwareVersion found'
            if observed == expected_version:
                results.append((name, True, f'softwareVersion is {observed!r} '
                                            f'(attempt {attempt})'))
                return
        if attempt < PAGES_ATTEMPTS:
            time.sleep(PAGES_DELAY_SECONDS)
    results.append((name, False,
                    f'softwareVersion never became {expected_version!r}; '
                    f'last observed: {observed!r}'))


def check_release_page(tag, results, require_hero=False):
    name = f'release {tag}'
    try:
        fetched = subprocess.run(
            ['gh', 'api', f'repos/{REPO_SLUG}/releases/tags/{tag}',
             '--jq', '{draft: .draft, prerelease: .prerelease, body: .body}'],
            capture_output=True, text=True)
    except FileNotFoundError:
        # Independent reporting: a machine without gh still gets the other
        # surfaces' results plus an explicit failure here.
        results.append((name, False, 'gh executable not available'))
        return
    if fetched.returncode != 0:
        results.append((name, False, 'release page does not exist'))
        return
    release = json.loads(fetched.stdout)
    if release.get('draft'):
        results.append((name, False, 'release exists but is still a draft'))
        return
    if release.get('prerelease'):
        # The stable updater selects only prerelease=false entries, so a cut
        # left marked prerelease has not reached stable users.
        results.append((name, False, 'release is marked as a prerelease'))
        return
    if not (release.get('body') or '').strip():
        # An empty body renders "correctly"; require actual notes.
        results.append((name, False, 'release page is public but its body is empty'))
        return

    # Same render verification as the draft check in notes.py.
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
    from notes import render_check
    problems = render_check(release['body'], require_hero=require_hero)
    if problems:
        results.append((name, False, '; '.join(problems)))
    else:
        results.append((name, True,
                        'public, hero renders as an image where present, '
                        'code fences intact'))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--version', required=True, help='bare semver, e.g. 2.6.0')
    parser.add_argument('--feature', action='store_true',
                        help='feature release: the release page must open '
                             'with a hero image')
    args = parser.parse_args()
    version = args.version
    tag = f'v{version}'

    results = []
    try:
        token = registry_token()
    except Exception as error:
        results.append((f'registry ghcr.io/{IMAGE}', False,
                        f'token request failed: {error}'))
        token = None
    if token:
        check_image_tag(token, version, version, results)
        check_image_tag(token, 'latest', version, results)
    check_pages(version, results)
    check_release_page(tag, results, require_hero=args.feature)

    failed = False
    for name, passed, detail in results:
        marker = 'PASS' if passed else 'FAIL'
        print(f'{marker}  {name}: {detail}')
        failed = failed or not passed
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
