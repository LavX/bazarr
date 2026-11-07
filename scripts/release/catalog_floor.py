#!/usr/bin/env python3
# coding=utf-8
"""Pin the official provider catalog into a release image.

The image ships a snapshot of the official catalog (its catalog.json and every
provider bundle file it names), so a first run with no network can still list
and install providers. This script makes that snapshot reproducible and
checked. It uses the standard library and git only, so the Docker stage that
runs it needs nothing else.

The pin is one lock file, docker/catalog-pin.lock.json: the catalog repository,
the branch, one commit and the sha256 of every file taken from that commit.
Any build stage that needs the official catalog bytes reads this same lock, so
a release bumps one pin and records one commit.

  lock   Write the lock for a commit (the branch tip by default). Run it in
         release prep and commit the result.
  build  Check the lock against the live repository and write the snapshot.
  check  Check a written snapshot's modes (and its owner, when asked).

What `build` verifies before it writes a byte:

  * the locked commit is an ancestor of, or equal to, the branch tip. The
    branch may have moved on since the lock was written, which breaks nothing
    and keeps rebuilds byte-identical. A commit that left the branch (a
    force-push, a commit that was never on it) fails the build.
  * every file's blob at the locked commit hashes to the lock's value.
  * the lock names exactly catalog.json plus the files catalog.json declares,
    each blob matches the pin the catalog declares for it, and each bundle's
    bundle_sha256 matches. A file the catalog does not declare cannot ride
    along, and a declared file cannot be missing.
  * every path is a plain relative path and every blob is a regular file.
  * the lock's opaque_files (below) hash to their lock values at the commit.

Opaque files are pinned by hash but not declared by catalog.json: a detached
signature next to it, a key file, anything a later stage must carry verbatim.
They are copied byte for byte into the snapshot and get the same mode checks.
The map is empty today, and a lock without the key reads as empty, so adding
one needs no lock_version bump.

Snapshot layout, under the destination directory, at the same relative paths
the catalog repository uses:

  catalog.json
  providers/<id>/<file>...
  floor.json        {lock_version, repo, branch, commit, files}

The names catalog.json and floor.json are reserved at the snapshot root: the
catalog may not declare a bundle file by either name, and an opaque file may
not use them.

Files are written 0444 and directories 0555, and the modes are asserted.
Nothing in the running application writes there.

Usage:
  scripts/release/catalog_floor.py lock  --out docker/catalog-pin.lock.json [--opaque catalog.json.sig]
  scripts/release/catalog_floor.py build --lock docker/catalog-pin.lock.json --dest /catalog_floor
  scripts/release/catalog_floor.py check --dest /app/bazarr/catalog_floor --expect-root-owner
"""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

LOCK_VERSION = 1
DEFAULT_REPO = 'LavX/bazarr-provider-catalog'
DEFAULT_BRANCH = 'main'
CATALOG_FILE = 'catalog.json'
FLOOR_META_FILE = 'floor.json'

FILE_MODE = 0o444
DIR_MODE = 0o555

_SHA1 = re.compile(r'^[0-9a-f]{40}$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_REGULAR_BLOB_MODES = frozenset({'100644', '100755'})
_LOCK_KEYS = frozenset({'lock_version', 'repo', 'branch', 'commit', 'files'})
_LOCK_OPTIONAL_KEYS = frozenset({'opaque_files'})
# Written by the build itself, or the catalog document: nothing else may take
# these names at the snapshot root.
_RESERVED_PATHS = frozenset({CATALOG_FILE, FLOOR_META_FILE})


class FloorError(Exception):
    """A pin, a hash, an ancestry or a mode check did not hold."""


# --------------------------------------------------------------------------
# git access
# --------------------------------------------------------------------------

def _git(repo, *args, check=True):
    try:
        result = subprocess.run(
            ['git', '-C', str(repo), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except OSError as error:
        raise FloorError(f'git could not be run: {error}') from error
    if check and result.returncode != 0:
        detail = result.stderr.decode('utf-8', 'replace').strip()
        raise FloorError(f'git {" ".join(args)} failed: {detail}')
    return result


def _remote_ref(branch):
    return f'refs/remotes/origin/{branch}'


def fetch_branch(url, branch, work_dir):
    """Fetch the whole history of ``branch`` from ``url`` into a fresh
    repository under ``work_dir`` and return its path. The history matters:
    the ancestry check walks it."""
    repo = Path(work_dir)
    _git(repo, 'init', '--quiet')
    _git(repo, 'fetch', '--quiet', '--no-tags', url, f'+refs/heads/{branch}:{_remote_ref(branch)}')
    return repo


def branch_tip(repo, branch):
    result = _git(repo, 'rev-parse', '--verify', '--quiet', f'{_remote_ref(branch)}^{{commit}}', check=False)
    tip = result.stdout.decode().strip()
    if result.returncode != 0 or not _SHA1.match(tip):
        raise FloorError(f'branch {branch} has no tip in the fetched repository')
    return tip


def assert_ancestor(repo, commit, branch):
    """The locked commit is the branch tip or reachable from it."""
    known = _git(repo, 'cat-file', '-e', f'{commit}^{{commit}}', check=False)
    if known.returncode != 0:
        raise FloorError(
            f'locked commit {commit} is not in the history of {branch}: '
            'the branch was rewritten or the commit was never on it'
        )
    result = _git(repo, 'merge-base', '--is-ancestor', commit, _remote_ref(branch), check=False)
    if result.returncode == 1:
        raise FloorError(f'locked commit {commit} is not an ancestor of {branch}')
    if result.returncode != 0:
        detail = result.stderr.decode('utf-8', 'replace').strip()
        raise FloorError(f'ancestry check against {branch} failed: {detail}')


def _tree_entries(repo, commit):
    """Map every path at ``commit`` to its git file mode."""
    result = _git(repo, 'ls-tree', '-r', '-z', commit)
    entries = {}
    for record in result.stdout.split(b'\0'):
        if not record:
            continue
        meta, _, name = record.partition(b'\t')
        mode = meta.split(b' ', 1)[0].decode()
        entries[name.decode('utf-8')] = mode
    return entries


def _read_blob(repo, commit, path):
    return _git(repo, 'cat-file', 'blob', f'{commit}:{path}').stdout


# --------------------------------------------------------------------------
# paths and hashes
# --------------------------------------------------------------------------

def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _safe_path(path, what):
    if (
        not isinstance(path, str) or not path or path.startswith('/')
        or '\\' in path or '\0' in path
        or any(part in ('', '.', '..') for part in path.split('/'))
    ):
        raise FloorError(f'{what} is not a plain relative path: {path!r}')
    return path


def _bundle_sha256(files):
    """The bundle digest the application checks on install: the sorted
    relative paths and contents, length-prefixed and NUL separated."""
    digest = hashlib.sha256()
    for relative_path in sorted(files):
        data = files[relative_path]
        digest.update(relative_path.encode('utf-8'))
        digest.update(b'\0')
        digest.update(str(len(data)).encode('ascii'))
        digest.update(b'\0')
        digest.update(data)
        digest.update(b'\0')
    return digest.hexdigest()


# --------------------------------------------------------------------------
# what the catalog declares
# --------------------------------------------------------------------------

def declared_bundles(catalog_bytes):
    """Every provider bundle catalog.json declares, as a list of
    ``(provider_id, bundle_sha256, {repo_path: (bundle_relative_path, pin)})``."""
    try:
        catalog = json.loads(catalog_bytes)
    except ValueError as error:
        raise FloorError(f'{CATALOG_FILE} is not valid JSON: {error}') from error
    providers = catalog.get('providers') if isinstance(catalog, dict) else None
    if not isinstance(providers, list) or not providers:
        raise FloorError(f'{CATALOG_FILE} has no providers list')

    bundles = []
    for index, entry in enumerate(providers):
        manifest = entry.get('manifest') if isinstance(entry, dict) else None
        if not isinstance(manifest, dict):
            raise FloorError(f'provider entry {index} has no manifest')
        provider_id = manifest.get('provider_id')
        label = provider_id if isinstance(provider_id, str) and provider_id else f'entry {index}'
        source = manifest.get('source')
        source_path = source.get('path', '') if isinstance(source, dict) else ''
        if source_path:
            _safe_path(source_path, f'{label} source path')
        files = manifest.get('files')
        if not isinstance(files, dict) or not files:
            raise FloorError(f'{label} declares no files')
        bundle_pin = manifest.get('bundle_sha256')
        if not isinstance(bundle_pin, str) or not _SHA256.match(bundle_pin):
            raise FloorError(f'{label} has no valid bundle_sha256')
        mapping = {}
        for relative_path, pin in files.items():
            _safe_path(relative_path, f'{label} file')
            if not isinstance(pin, str) or not _SHA256.match(pin):
                raise FloorError(f'{label} has no valid sha256 for {relative_path}')
            repo_path = '/'.join(part for part in (source_path, relative_path) if part)
            if repo_path in _RESERVED_PATHS:
                raise FloorError(f'{label} declares {repo_path}, a name the snapshot reserves')
            mapping[repo_path] = (relative_path, pin)
        bundles.append((label, bundle_pin, mapping))
    return bundles


def declared_files(catalog_bytes):
    """The repository paths catalog.json declares, mapped to the sha256 it
    pins for each. Two providers that claim one path must agree on its hash."""
    pinned = {}
    for label, _bundle_pin, mapping in declared_bundles(catalog_bytes):
        for repo_path, (_relative, pin) in mapping.items():
            if repo_path in pinned and pinned[repo_path] != pin:
                raise FloorError(f'{label} pins {repo_path} to a different hash than another provider')
            pinned[repo_path] = pin
    return pinned


# --------------------------------------------------------------------------
# the lock
# --------------------------------------------------------------------------

def validate_lock(lock):
    """Shape check for a parsed lock. Returns it, or raises FloorError."""
    if not isinstance(lock, dict):
        raise FloorError('lock is not a JSON object')
    unknown = set(lock) - _LOCK_KEYS - _LOCK_OPTIONAL_KEYS
    missing = _LOCK_KEYS - set(lock)
    if unknown or missing:
        raise FloorError(f'lock keys wrong (unknown {sorted(unknown)}, missing {sorted(missing)})')
    if lock['lock_version'] != LOCK_VERSION:
        raise FloorError(f'lock_version {lock["lock_version"]!r} is not {LOCK_VERSION}')
    if not isinstance(lock['repo'], str) or not re.match(r'^[\w.-]+/[\w.-]+$', lock['repo']):
        raise FloorError(f'lock repo is not owner/name: {lock["repo"]!r}')
    if not isinstance(lock['branch'], str) or not re.match(r'^[\w][\w./-]*$', lock['branch']):
        raise FloorError(f'lock branch is invalid: {lock["branch"]!r}')
    if not isinstance(lock['commit'], str) or not _SHA1.match(lock['commit']):
        raise FloorError('lock commit is not a full lowercase 40-hex sha')
    files = lock['files']
    if not isinstance(files, dict) or CATALOG_FILE not in files:
        raise FloorError(f'lock files must be a map that includes {CATALOG_FILE}')
    for path, digest in files.items():
        _safe_path(path, 'lock file path')
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise FloorError(f'lock hash for {path} is not a lowercase sha256')
    opaque = lock.get('opaque_files', {})
    if not isinstance(opaque, dict):
        raise FloorError('lock opaque_files must be a map of path to sha256')
    for path, digest in opaque.items():
        _safe_path(path, 'lock opaque file path')
        if path in _RESERVED_PATHS:
            raise FloorError(f'opaque file {path} uses a reserved name')
        if path in files:
            raise FloorError(f'opaque file {path} is also listed in files')
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise FloorError(f'lock hash for opaque file {path} is not a lowercase sha256')
    return lock


def load_lock(path):
    try:
        lock = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise FloorError(f'lock {path} could not be read: {error}') from error
    return validate_lock(lock)


def dump_lock(lock):
    return json.dumps(lock, indent=2, sort_keys=True) + '\n'


def _read_declared_tree(repo, commit):
    """Read catalog.json and every file it declares at ``commit``.

    Verifies tree modes, the per-file pins and each bundle digest. Returns
    ``{repo_path: bytes}`` for catalog.json plus the declared files."""
    entries = _tree_entries(repo, commit)

    def read(path):
        mode = entries.get(path)
        if mode is None:
            raise FloorError(f'{path} does not exist at {commit}')
        if mode not in _REGULAR_BLOB_MODES:
            raise FloorError(f'{path} is not a regular file at {commit} (git mode {mode})')
        return _read_blob(repo, commit, path)

    catalog_bytes = read(CATALOG_FILE)
    data = {CATALOG_FILE: catalog_bytes}
    for label, bundle_pin, mapping in declared_bundles(catalog_bytes):
        contents = {}
        for repo_path, (relative_path, pin) in mapping.items():
            if repo_path not in data:
                data[repo_path] = read(repo_path)
            if _sha256(data[repo_path]) != pin:
                raise FloorError(f'{repo_path} does not match the hash catalog.json pins for it')
            contents[relative_path] = data[repo_path]
        if _bundle_sha256(contents) != bundle_pin:
            raise FloorError(f'{label} bundle_sha256 does not match its files')
    return data


def _read_opaque(repo, commit, paths):
    """Read opaque files at ``commit``. They must be regular files."""
    if not paths:
        return {}
    entries = _tree_entries(repo, commit)
    data = {}
    for path in sorted(paths):
        mode = entries.get(path)
        if mode is None:
            raise FloorError(f'opaque file {path} does not exist at {commit}')
        if mode not in _REGULAR_BLOB_MODES:
            raise FloorError(f'opaque file {path} is not a regular file at {commit} (git mode {mode})')
        data[path] = _read_blob(repo, commit, path)
    return data


def make_lock(repo, commit, branch=DEFAULT_BRANCH, repo_name=DEFAULT_REPO, opaque_paths=()):
    data = _read_declared_tree(repo, commit)
    opaque = _read_opaque(repo, commit, set(opaque_paths))
    lock = {
        'lock_version': LOCK_VERSION,
        'repo': repo_name,
        'branch': branch,
        'commit': commit,
        'files': {path: _sha256(content) for path, content in data.items()},
        'opaque_files': {path: _sha256(content) for path, content in opaque.items()},
    }
    return validate_lock(lock)


def verify_lock(lock, repo):
    """Run every check the build makes against a fetched repository and
    return ``{repo_path: bytes}`` for the files to write."""
    validate_lock(lock)
    assert_ancestor(repo, lock['commit'], lock['branch'])
    data = _read_declared_tree(repo, lock['commit'])
    opaque_lock = lock.get('opaque_files', {})
    opaque = _read_opaque(repo, lock['commit'], set(opaque_lock))
    for path, content in opaque.items():
        if path in data:
            raise FloorError(f'opaque file {path} is also declared by {CATALOG_FILE}')
        if _sha256(content) != opaque_lock[path]:
            raise FloorError(f'opaque file {path} does not match the lock hash at {lock["commit"]}')

    extra = sorted(set(lock['files']) - set(data))
    absent = sorted(set(data) - set(lock['files']))
    if extra or absent:
        raise FloorError(
            f'lock and {CATALOG_FILE} disagree on the file set '
            f'(only in lock: {extra[:5]}, only in catalog: {absent[:5]})'
        )
    for path, content in data.items():
        if _sha256(content) != lock['files'][path]:
            raise FloorError(f'{path} does not match the lock hash at {lock["commit"]}')
    return {**data, **opaque}


# --------------------------------------------------------------------------
# writing the snapshot
# --------------------------------------------------------------------------

def _all_dirs_deepest_first(root):
    dirs = [Path(dirpath) for dirpath, _names, _files in os.walk(root)]
    return sorted(dirs, key=lambda p: len(p.parts), reverse=True)


def write_floor(lock, data, dest):
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FloorError(f'{dest} is not empty')
    dest.mkdir(parents=True, exist_ok=True)

    for path in sorted(data):
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data[path])
    meta = {
        'lock_version': lock['lock_version'],
        'repo': lock['repo'],
        'branch': lock['branch'],
        'commit': lock['commit'],
        'files': len(data),
    }
    (dest / FLOOR_META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    for dirpath, _names, filenames in os.walk(dest):
        for name in filenames:
            os.chmod(Path(dirpath) / name, FILE_MODE)
    for directory in _all_dirs_deepest_first(dest):
        os.chmod(directory, DIR_MODE)


def check_modes(dest, expect_root_owner=False):
    """Fail on anything under ``dest`` that is not a read-only regular file or
    directory (and, when asked, not owned by root)."""
    dest = Path(dest)
    if not dest.is_dir():
        raise FloorError(f'{dest} is not a directory')
    problems = []
    for path in [dest, *sorted(dest.rglob('*'))]:
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            problems.append(f'{path}: symlink')
        elif stat.S_ISDIR(info.st_mode):
            if mode != DIR_MODE:
                problems.append(f'{path}: directory mode {mode:04o}, expected {DIR_MODE:04o}')
        elif stat.S_ISREG(info.st_mode):
            if mode != FILE_MODE:
                problems.append(f'{path}: file mode {mode:04o}, expected {FILE_MODE:04o}')
        else:
            problems.append(f'{path}: not a regular file or directory')
        if expect_root_owner and info.st_uid != 0:
            problems.append(f'{path}: owned by uid {info.st_uid}, expected root')
    if problems:
        raise FloorError('snapshot modes wrong:\n  ' + '\n  '.join(problems[:20]))


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def _resolve_repo(args, work_dir, branch, default_url):
    if args.repo_dir:
        return Path(args.repo_dir)
    return fetch_branch(args.repo_url or default_url, branch, work_dir)


def _cmd_lock(args):
    with tempfile.TemporaryDirectory(prefix='catalog-lock-') as work_dir:
        url = args.repo_url or f'https://github.com/{args.repo}.git'
        repo = _resolve_repo(args, work_dir, args.branch, url)
        commit = args.commit or branch_tip(repo, args.branch)
        if not _SHA1.match(commit):
            raise FloorError('--commit must be a full lowercase 40-hex sha')
        assert_ancestor(repo, commit, args.branch)
        # A rewrite keeps the opaque files the current lock already carries.
        opaque_paths = set(args.opaque)
        if Path(args.out).exists():
            opaque_paths |= set(load_lock(args.out).get('opaque_files', {}))
        lock = make_lock(repo, commit, args.branch, args.repo, opaque_paths)
    Path(args.out).write_text(dump_lock(lock), encoding='utf-8')
    print(f'locked {lock["repo"]}@{commit[:12]} ({len(lock["files"])} files) -> {args.out}')


def _cmd_build(args):
    lock = load_lock(args.lock)
    with tempfile.TemporaryDirectory(prefix='catalog-floor-') as work_dir:
        url = args.repo_url or f'https://github.com/{lock["repo"]}.git'
        repo = _resolve_repo(args, work_dir, lock['branch'], url)
        data = verify_lock(lock, repo)
        tip = branch_tip(repo, lock['branch'])
        write_floor(lock, data, args.dest)
    check_modes(args.dest, expect_root_owner=args.expect_root_owner)
    state = 'at the branch tip' if tip == lock['commit'] else f'behind the branch tip {tip[:12]}'
    print(f'floor written: {lock["repo"]}@{lock["commit"][:12]} ({len(data)} files), {state}')


def _cmd_check(args):
    check_modes(args.dest, expect_root_owner=args.expect_root_owner)
    print(f'{args.dest}: modes ok')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)

    def add_source(command):
        group = command.add_mutually_exclusive_group()
        group.add_argument('--repo-url', help='where to fetch the catalog from (default: the lock repo on GitHub)')
        group.add_argument('--repo-dir', help='an existing clone with origin/<branch> fetched')

    lock = commands.add_parser('lock', help='write the lock for a commit')
    add_source(lock)
    lock.add_argument('--repo', default=DEFAULT_REPO)
    lock.add_argument('--branch', default=DEFAULT_BRANCH)
    lock.add_argument('--commit', help='default: the branch tip')
    lock.add_argument('--opaque', action='append', default=[], metavar='PATH',
                      help='pin a repository file catalog.json does not declare (repeatable); '
                           'the opaque files of an existing --out lock are kept')
    lock.add_argument('--out', required=True)
    lock.set_defaults(func=_cmd_lock)

    build = commands.add_parser('build', help='verify the lock and write the snapshot')
    add_source(build)
    build.add_argument('--lock', required=True)
    build.add_argument('--dest', required=True)
    build.add_argument('--expect-root-owner', action='store_true')
    build.set_defaults(func=_cmd_build)

    check = commands.add_parser('check', help='assert the snapshot modes')
    check.add_argument('--dest', required=True)
    check.add_argument('--expect-root-owner', action='store_true')
    check.set_defaults(func=_cmd_check)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except FloorError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
