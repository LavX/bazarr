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


def image_config_version(token, tag):
    """The org.opencontainers.image.version label of a tag, without pulling
    any layer: manifest (or index -> first manifest) then the config blob."""
    headers = {'Authorization': f'Bearer {token}', 'Accept': MANIFEST_TYPES}
    manifest = json.loads(http_get(
        f'https://ghcr.io/v2/{IMAGE}/manifests/{tag}', headers))
    if 'manifests' in manifest:
        digest = next(
            entry['digest'] for entry in manifest['manifests']
            if entry.get('platform', {}).get('os') != 'unknown')
        manifest = json.loads(http_get(
            f'https://ghcr.io/v2/{IMAGE}/manifests/{digest}', headers))
    config_digest = manifest['config']['digest']
    config = json.loads(http_get(
        f'https://ghcr.io/v2/{IMAGE}/blobs/{config_digest}',
        {'Authorization': f'Bearer {token}'}))
    labels = config.get('config', {}).get('Labels') or {}
    return labels.get('org.opencontainers.image.version')


def check_image_tag(token, tag, expected_version, results):
    name = f'image ghcr.io/{IMAGE}:{tag}'
    try:
        observed = image_config_version(token, tag)
    except urllib.error.HTTPError as error:
        results.append((name, False, f'tag does not resolve (HTTP {error.code})'))
        return
    except Exception as error:  # network shapes vary; report, do not crash
        results.append((name, False, f'lookup failed: {error}'))
        return
    if observed is None:
        results.append((name, True,
                        'tag resolves; image carries no version label to compare'))
    elif expected_version in observed:
        results.append((name, True, f'resolves, version label {observed!r}'))
    else:
        results.append((name, False,
                        f'resolves but version label is {observed!r}, '
                        f'expected it to contain {expected_version!r}'))


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


def check_release_page(tag, results):
    name = f'release {tag}'
    fetched = subprocess.run(
        ['gh', 'api', f'repos/{REPO_SLUG}/releases/tags/{tag}',
         '--jq', '{draft: .draft, body: .body}'],
        capture_output=True, text=True)
    if fetched.returncode != 0:
        results.append((name, False, 'release page does not exist'))
        return
    release = json.loads(fetched.stdout)
    if release.get('draft'):
        results.append((name, False, 'release exists but is still a draft'))
        return

    # Same render verification as the draft check in notes.py.
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
    from notes import render_check
    problems = render_check(release['body'])
    if problems:
        results.append((name, False, '; '.join(problems)))
    else:
        results.append((name, True,
                        'public, hero renders as an image where present, '
                        'code fences intact'))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--version', required=True, help='bare semver, e.g. 2.6.0')
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
    check_release_page(tag, results)

    failed = False
    for name, passed, detail in results:
        marker = 'PASS' if passed else 'FAIL'
        print(f'{marker}  {name}: {detail}')
        failed = failed or not passed
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
