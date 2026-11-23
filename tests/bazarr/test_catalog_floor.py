# coding=utf-8
"""The catalog release floor build: scripts/release/catalog_floor.py.

The image ships a snapshot of the official provider catalog, pinned by one
lock file. The build must accept a lock whose commit is the branch tip or an
ancestor of it (the branch moves on between release prep and the build), must
refuse a commit that left the branch, and must refuse any file that does not
hash to what the lock and the catalog say. The tests run the real script
against a fake catalog repository built with git in a temporary directory.
"""
import base64
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, 'scripts', 'release', 'catalog_floor.py')
LOCK = os.path.join(ROOT, 'docker', 'catalog-pin.lock.json')
DOCKERFILE = os.path.join(ROOT, 'Dockerfile')
PROVIDER_HUB = os.path.join(ROOT, 'bazarr', 'provider_hub')


def _load():
    spec = importlib.util.spec_from_file_location('_catalog_floor_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


floor = _load()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _bundle_digest(files):
    digest = hashlib.sha256()
    for name in sorted(files):
        data = files[name]
        digest.update(name.encode())
        digest.update(b'\0')
        digest.update(str(len(data)).encode())
        digest.update(b'\0')
        digest.update(data)
        digest.update(b'\0')
    return digest.hexdigest()


def _entry(provider_id, files, path=None):
    return {'manifest': {
        'provider_id': provider_id,
        'schema_version': 1,
        'files': {name: _sha(data) for name, data in files.items()},
        'bundle_sha256': _bundle_digest(files),
        'source': {'path': path if path is not None else f'providers/{provider_id}'},
    }}


class FakeCatalog:
    """A git repository shaped like the catalog repository."""

    def __init__(self, path):
        self.path = path
        os.makedirs(path)
        self.git('init', '--quiet', '-b', 'main')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'commit.gpgsign', 'false')
        self.bundles = {
            'alpha': {'provider.py': b'print("alpha")\n'},
            'beta': {'provider.py': b'print("beta")\n', 'helper.py': b'X = 1\n'},
        }
        # Plugin bundles sit in the tree but never in the legacy catalog.json.
        self.plugins = {}

    def git(self, *args):
        return subprocess.run(
            ['git', '-C', str(self.path), *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def write(self, relative, data):
        target = os.path.join(self.path, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as handle:
            handle.write(data)

    def publish(self, message='update'):
        """Write catalog.json and the bundles, then commit. Returns the sha."""
        providers = []
        for provider_id, files in self.bundles.items():
            providers.append(_entry(provider_id, files))
            for name, data in files.items():
                self.write(f'providers/{provider_id}/{name}', data)
        for plugin_id, files in self.plugins.items():
            for name, data in files.items():
                self.write(f'plugins/{plugin_id}/{name}', data)
        catalog = {'name': 'Fake', 'schema_version': 1, 'providers': providers}
        self.write('catalog.json', json.dumps(catalog, indent=1).encode())
        self.git('add', '-A')
        self.git('commit', '--quiet', '-m', message)
        return self.git('rev-parse', 'HEAD')


@pytest.fixture
def catalog(tmp_path):
    return FakeCatalog(tmp_path / 'catalog')


def _lock_for(catalog, commit):
    """Lock a commit exactly as the lock command does, from a fetched clone."""
    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    return floor.make_lock(clone, commit)


_counter = [0]


def _fresh_dir(catalog):
    _counter[0] += 1
    target = os.path.join(os.path.dirname(str(catalog.path)), f'clone-{_counter[0]}')
    os.makedirs(target)
    return target


def _build(catalog, lock, tmp_path, name='out', signing_dir=None):
    lock_path = tmp_path / f'{name}.lock.json'
    lock_path.write_text(floor.dump_lock(lock))
    dest = tmp_path / name
    command = ['build', '--lock', str(lock_path), '--repo-url', str(catalog.path), '--dest', str(dest)]
    if signing_dir is not None:
        command += ['--signing-dir', str(signing_dir)]
    code = floor.main(command)
    return code, dest


def _tree(dest):
    out = {}
    for dirpath, _dirs, names in os.walk(dest):
        for name in names:
            full = os.path.join(dirpath, name)
            with open(full, 'rb') as handle:
                out[os.path.relpath(full, dest)] = handle.read()
    return out


@pytest.fixture(autouse=True)
def _make_trees_removable(tmp_path):
    yield
    for dirpath, dirs, _names in os.walk(tmp_path):
        for name in dirs:
            try:
                os.chmod(os.path.join(dirpath, name), 0o755)
            except OSError:
                pass


# ---- the build ----------------------------------------------------------

def test_a_lock_at_the_branch_tip_builds_a_read_only_snapshot(catalog, tmp_path):
    commit = catalog.publish()
    code, dest = _build(catalog, _lock_for(catalog, commit), tmp_path)

    assert code == 0
    tree = _tree(dest)
    assert set(tree) == {
        'catalog.json', 'floor.json',
        'providers/alpha/provider.py',
        'providers/beta/provider.py', 'providers/beta/helper.py',
    }
    assert tree['providers/beta/helper.py'] == b'X = 1\n'
    meta = json.loads(tree['floor.json'])
    assert meta['commit'] == commit
    # floor.json lists every other file with its hash.
    assert meta['files'] == {
        path: hashlib.sha256(data).hexdigest() for path, data in tree.items() if path != 'floor.json'
    }
    for dirpath, dirs, names in os.walk(dest):
        assert stat.S_IMODE(os.stat(dirpath).st_mode) == 0o555
        for name in names:
            assert stat.S_IMODE(os.stat(os.path.join(dirpath, name)).st_mode) == 0o444
    floor.check_modes(dest)


def test_the_branch_moving_on_after_the_lock_changes_nothing(catalog, tmp_path):
    locked = catalog.publish('locked')
    lock = _lock_for(catalog, locked)
    code, first = _build(catalog, lock, tmp_path, 'first')

    catalog.bundles['gamma'] = {'provider.py': b'print("gamma")\n'}
    catalog.bundles['alpha'] = {'provider.py': b'print("alpha v2")\n'}
    catalog.publish('main moved')
    code_again, second = _build(catalog, lock, tmp_path, 'second')

    assert code == 0 and code_again == 0
    assert _tree(first) == _tree(second)
    assert 'providers/gamma/provider.py' not in _tree(second)
    assert _tree(second)['providers/alpha/provider.py'] == b'print("alpha")\n'


def test_a_commit_that_was_never_on_the_branch_is_refused(catalog, tmp_path, capsys):
    catalog.publish('base')
    catalog.git('checkout', '--quiet', '-b', 'side')
    catalog.bundles['alpha'] = {'provider.py': b'print("side")\n'}
    side = catalog.publish('side only')
    catalog.git('checkout', '--quiet', 'main')

    # Lock it from a clone that can see the side branch, then build from main.
    clone = floor.fetch_branch(str(catalog.path), 'side', _fresh_dir(catalog))
    lock = floor.make_lock(clone, side)
    code, dest = _build(catalog, lock, tmp_path)

    assert code == 1
    assert 'not in the history of main' in capsys.readouterr().err
    assert not dest.exists()


def test_a_rewritten_branch_that_dropped_the_commit_is_refused(catalog, tmp_path, capsys):
    catalog.publish('first')
    catalog.bundles['alpha'] = {'provider.py': b'print("two")\n'}
    locked = catalog.publish('second')
    lock = _lock_for(catalog, locked)

    catalog.git('reset', '--quiet', '--hard', 'HEAD~1')
    catalog.bundles['alpha'] = {'provider.py': b'print("rewritten")\n'}
    catalog.publish('rewritten second')
    code, _dest = _build(catalog, lock, tmp_path)

    assert code == 1
    assert 'main' in capsys.readouterr().err


def test_a_commit_in_history_but_not_reachable_from_the_tip_is_refused(catalog, tmp_path, capsys):
    """The commit exists in the fetched repository (a merged-away branch tip
    is reachable here through the merge parent, so build a case that is not:
    the repository fetched the branch, and the locked commit is in the object
    store from an earlier fetch but no longer an ancestor)."""
    catalog.publish('base')
    catalog.git('checkout', '--quiet', '-b', 'side')
    catalog.bundles['alpha'] = {'provider.py': b'print("side")\n'}
    side = catalog.publish('side only')
    catalog.git('checkout', '--quiet', 'main')
    catalog.bundles['alpha'] = {'provider.py': b'print("main")\n'}
    catalog.publish('main only')

    clone = floor.fetch_branch(str(catalog.path), 'side', _fresh_dir(catalog))
    lock = floor.make_lock(clone, side)
    # Same repository, now also holding main: the commit is known, not an ancestor.
    floor._git(clone, 'fetch', '--quiet', '--no-tags', str(catalog.path), '+refs/heads/main:refs/remotes/origin/main')
    with pytest.raises(floor.FloorError, match='not an ancestor'):
        floor.verify_lock(lock, clone)


def test_a_tampered_lock_hash_is_refused(catalog, tmp_path, capsys):
    commit = catalog.publish()
    lock = _lock_for(catalog, commit)
    lock['files']['providers/alpha/provider.py'] = '0' * 64
    code, dest = _build(catalog, lock, tmp_path)

    assert code == 1
    assert 'does not match the lock hash' in capsys.readouterr().err
    assert not dest.exists()


def test_a_file_edited_without_updating_the_catalog_pin_is_refused(catalog, tmp_path):
    catalog.publish()
    catalog.write('providers/alpha/provider.py', b'print("edited")\n')
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'edit without pin')
    commit = catalog.git('rev-parse', 'HEAD')

    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    with pytest.raises(floor.FloorError, match='does not match the hash catalog.json pins'):
        floor.make_lock(clone, commit)


def test_a_bundle_digest_that_does_not_match_is_refused(catalog, tmp_path):
    catalog.publish()
    document = json.loads(open(os.path.join(catalog.path, 'catalog.json')).read())
    document['providers'][0]['manifest']['bundle_sha256'] = '1' * 64
    catalog.write('catalog.json', json.dumps(document).encode())
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'bad digest')
    commit = catalog.git('rev-parse', 'HEAD')

    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    with pytest.raises(floor.FloorError, match='bundle_sha256'):
        floor.make_lock(clone, commit)


def test_a_lock_with_a_file_the_catalog_does_not_declare_is_refused(catalog, tmp_path, capsys):
    commit = catalog.publish()
    lock = _lock_for(catalog, commit)
    catalog.write('providers/alpha/extra.py', b'x = 1\n')
    lock['files']['providers/alpha/extra.py'] = _sha(b'x = 1\n')
    code, _dest = _build(catalog, lock, tmp_path)

    assert code == 1
    assert 'disagree on the file set' in capsys.readouterr().err


def test_a_lock_missing_a_declared_file_is_refused(catalog, tmp_path, capsys):
    commit = catalog.publish()
    lock = _lock_for(catalog, commit)
    del lock['files']['providers/beta/helper.py']
    code, _dest = _build(catalog, lock, tmp_path)

    assert code == 1
    assert 'disagree on the file set' in capsys.readouterr().err


def test_a_source_path_that_climbs_out_is_refused(catalog):
    providers = [_entry('evil', {'provider.py': b'x\n'}, path='../outside')]
    catalog.write('catalog.json', json.dumps({'providers': providers}).encode())
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'evil')
    commit = catalog.git('rev-parse', 'HEAD')

    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    with pytest.raises(floor.FloorError, match='plain relative path'):
        floor.make_lock(clone, commit)


def test_a_symlink_in_the_tree_is_refused(catalog):
    catalog.publish()
    target = os.path.join(catalog.path, 'providers', 'alpha', 'provider.py')
    os.remove(target)
    os.symlink('../beta/provider.py', target)
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'symlink')
    commit = catalog.git('rev-parse', 'HEAD')

    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    with pytest.raises(floor.FloorError, match='not a regular file'):
        floor.make_lock(clone, commit)


def test_a_non_empty_destination_is_refused(catalog, tmp_path, capsys):
    commit = catalog.publish()
    lock = _lock_for(catalog, commit)
    dest = tmp_path / 'out'
    dest.mkdir()
    (dest / 'leftover').write_text('x')
    code, _ = _build(catalog, lock, tmp_path)

    assert code == 1
    assert 'is not empty' in capsys.readouterr().err


# ---- opaque files and reserved names -----------------------------------

def test_opaque_files_are_pinned_copied_verbatim_and_mode_checked(catalog, tmp_path):
    commit = catalog.publish()
    catalog.write('catalog.json.sig', b'\x00signature bytes\xff')
    catalog.write('keys/catalog-signing-keys.json', b'{"keys": []}\n')
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'signature and keys')
    commit = catalog.git('rev-parse', 'HEAD')

    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    lock = floor.make_lock(clone, commit, opaque_paths=['catalog.json.sig', 'keys/catalog-signing-keys.json'])
    assert lock['opaque_files'] == {
        'catalog.json.sig': _sha(b'\x00signature bytes\xff'),
        'keys/catalog-signing-keys.json': _sha(b'{"keys": []}\n'),
    }
    code, dest = _build(catalog, lock, tmp_path)

    assert code == 0
    tree = _tree(dest)
    assert tree['catalog.json.sig'] == b'\x00signature bytes\xff'
    assert tree['keys/catalog-signing-keys.json'] == b'{"keys": []}\n'
    meta_files = json.loads(tree['floor.json'])['files']
    assert len(meta_files) == 6
    assert meta_files['catalog.json.sig'] == hashlib.sha256(tree['catalog.json.sig']).hexdigest()
    floor.check_modes(dest)
    assert stat.S_IMODE(os.stat(dest / 'keys').st_mode) == 0o555
    assert stat.S_IMODE(os.stat(dest / 'catalog.json.sig').st_mode) == 0o444


def test_an_opaque_file_that_changed_since_the_lock_is_refused(catalog, tmp_path, capsys):
    catalog.publish()
    catalog.write('catalog.json.sig', b'one')
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'sig')
    commit = catalog.git('rev-parse', 'HEAD')
    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    lock = floor.make_lock(clone, commit, opaque_paths=['catalog.json.sig'])
    lock['opaque_files']['catalog.json.sig'] = _sha(b'two')

    code, dest = _build(catalog, lock, tmp_path)

    assert code == 1
    assert 'opaque file catalog.json.sig does not match the lock hash' in capsys.readouterr().err
    assert not dest.exists()


def test_an_opaque_file_missing_at_the_commit_is_refused(catalog):
    commit = catalog.publish()
    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    with pytest.raises(floor.FloorError, match='does not exist'):
        floor.make_lock(clone, commit, opaque_paths=['catalog.json.sig'])


def test_an_opaque_file_the_catalog_also_declares_is_refused(catalog):
    commit = catalog.publish()
    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    lock = floor.make_lock(clone, commit)
    lock['opaque_files'] = {'providers/alpha/provider.py': _sha(b'print("alpha")\n')}
    # Taken out of files so only the opaque listing claims it.
    del lock['files']['providers/alpha/provider.py']

    with pytest.raises(floor.FloorError, match='also declared by catalog.json'):
        floor.verify_lock(lock, clone)


def test_the_lock_command_pins_opaque_files_and_keeps_them_on_a_rewrite(catalog, tmp_path):
    catalog.publish()
    catalog.write('catalog.json.sig', b'sig one')
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'sig')
    out = tmp_path / 'pin.lock.json'

    assert floor.main(['lock', '--repo-url', str(catalog.path), '--opaque', 'catalog.json.sig', '--out', str(out)]) == 0
    assert floor.load_lock(out)['opaque_files'] == {'catalog.json.sig': _sha(b'sig one')}

    catalog.write('catalog.json.sig', b'sig two')
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'new sig')
    assert floor.main(['lock', '--repo-url', str(catalog.path), '--out', str(out)]) == 0
    assert floor.load_lock(out)['opaque_files'] == {'catalog.json.sig': _sha(b'sig two')}


def test_a_lock_without_opaque_files_is_still_valid_and_reads_as_empty(catalog, tmp_path):
    commit = catalog.publish()
    lock = _lock_for(catalog, commit)
    del lock['opaque_files']
    assert floor.validate_lock(lock)
    code, dest = _build(catalog, lock, tmp_path)
    assert code == 0 and 'floor.json' in _tree(dest)


@pytest.mark.parametrize('opaque', [
    {'floor.json': 'c' * 64},
    {'catalog.json': 'c' * 64},
    {'../escape': 'c' * 64},
    {'ok.sig': 'C' * 64},
    {'catalog.json.sig': 'c' * 64, 'x/../y': 'c' * 64},
    ['not', 'a', 'map'],
])
def test_a_malformed_opaque_map_is_refused(opaque):
    with pytest.raises(floor.FloorError):
        floor.validate_lock({**_good_lock(), 'opaque_files': opaque})


def test_an_opaque_path_that_is_also_in_files_is_refused():
    lock = {**_good_lock(), 'files': {'catalog.json': 'b' * 64, 'a.py': 'd' * 64}, 'opaque_files': {'a.py': 'd' * 64}}
    with pytest.raises(floor.FloorError, match='also listed in files'):
        floor.validate_lock(lock)


@pytest.mark.parametrize('path', ['floor.json', 'catalog.json'])
def test_a_catalog_that_declares_a_reserved_root_name_is_refused(catalog, path):
    providers = [_entry('clash', {path: b'{}\n'}, path='')]
    catalog.write('catalog.json', json.dumps({'providers': providers}).encode())
    if path != 'catalog.json':
        catalog.write(path, b'{}\n')
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'clash')
    commit = catalog.git('rev-parse', 'HEAD')

    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    with pytest.raises(floor.FloorError, match='reserves'):
        floor.make_lock(clone, commit)


# ---- destination safety -------------------------------------------------

def _locked(catalog):
    commit = catalog.publish()
    return _lock_for(catalog, commit)


def test_a_destination_that_is_a_symlink_is_refused(catalog, tmp_path, capsys):
    lock = _locked(catalog)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    link = tmp_path / 'link'
    os.symlink(elsewhere, link)

    code, _dest = _build(catalog, lock, tmp_path, 'link')

    assert code == 1
    assert 'is a symlink' in capsys.readouterr().err
    assert list(elsewhere.iterdir()) == []
    assert stat.S_IMODE(os.stat(elsewhere).st_mode) != 0o555


def test_a_destination_with_a_symlinked_ancestor_is_refused(catalog, tmp_path, capsys):
    lock = _locked(catalog)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    os.symlink(elsewhere, tmp_path / 'ancestor')
    lock_path = tmp_path / 'pin.lock.json'
    lock_path.write_text(floor.dump_lock(lock))

    code = floor.main([
        'build', '--lock', str(lock_path), '--repo-url', str(catalog.path),
        '--dest', str(tmp_path / 'ancestor' / 'nested' / 'out'),
    ])

    assert code == 1
    assert 'is a symlink' in capsys.readouterr().err
    assert list(elsewhere.iterdir()) == []


def _swap_directory_for_link_on_creation(monkeypatch, name, target):
    """Make the directory called ``name`` become a link to ``target`` right
    after it is created, the way a concurrent process could."""
    real_mkdir = os.mkdir
    swapped = []

    def mkdir(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)
        if path == name:
            os.rmdir(path, dir_fd=dir_fd)
            os.symlink(target, path, dir_fd=dir_fd)
            swapped.append(path)

    monkeypatch.setattr(os, 'mkdir', mkdir)
    return swapped


def test_a_symlink_swapped_in_for_a_snapshot_directory_never_leaves_the_destination(tmp_path, monkeypatch):
    """A directory inside the snapshot became a link before it was opened:
    the write must fail instead of following it."""
    outside = tmp_path / 'outside'
    outside.mkdir()
    dest = tmp_path / 'dest'
    swapped = _swap_directory_for_link_on_creation(monkeypatch, 'providers', outside)

    with pytest.raises(floor.FloorError, match='could not be written'):
        floor.write_floor(_good_lock(), {'providers/alpha/provider.py': b'x\n'}, dest)
    assert swapped == ['providers']
    assert list(outside.iterdir()) == []


def test_a_symlink_swapped_in_for_a_destination_ancestor_never_leaves_the_destination(tmp_path, monkeypatch):
    """An ancestor of the destination became a link while the destination path
    was still being created: the write must be refused and nothing may land in
    the link's target."""
    outside = tmp_path / 'outside'
    outside.mkdir()
    dest = tmp_path / 'ancestor' / 'nested' / 'out'
    swapped = _swap_directory_for_link_on_creation(monkeypatch, 'nested', outside)

    with pytest.raises(floor.FloorError, match='is a symlink'):
        floor.write_floor(_good_lock(), {'providers/alpha/provider.py': b'x\n'}, dest)
    assert swapped == ['nested']
    assert list(outside.iterdir()) == []


def test_a_snapshot_directory_that_already_exists_is_refused(tmp_path, monkeypatch):
    """Someone else created a directory inside the snapshot after the empty
    check: it is not adopted."""
    dest = tmp_path / 'dest'
    real_mkdir = os.mkdir

    def mkdir_after_a_rival(path, mode=0o777, *, dir_fd=None):
        if path == 'providers':
            real_mkdir(path, mode, dir_fd=dir_fd)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'mkdir', mkdir_after_a_rival)

    with pytest.raises(floor.FloorError, match='could not be written'):
        floor.write_floor(_good_lock(), {'providers/alpha/provider.py': b'x\n'}, dest)


def test_an_existing_ancestor_that_is_a_link_is_refused(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'real').mkdir()
    os.symlink(outside, tmp_path / 'real' / 'nested')

    with pytest.raises(floor.FloorError, match='is a symlink'):
        floor.write_floor(_good_lock(), {'a': b'x\n'}, tmp_path / 'real' / 'nested' / 'out')
    assert list(outside.iterdir()) == []


def test_a_file_and_a_directory_claiming_one_path_are_refused(tmp_path):
    dest = tmp_path / 'dest'
    lock = {**_good_lock()}
    with pytest.raises(floor.FloorError):
        floor.write_floor(lock, {'a': b'1', 'a/b': b'2'}, dest)


# ---- first-component reserved names -------------------------------------

@pytest.mark.parametrize('path', ['floor.json/signature', 'floor.json/a/b', 'catalog.json/x'])
def test_an_opaque_path_beneath_a_reserved_name_is_refused(path):
    with pytest.raises(floor.FloorError, match='reserved'):
        floor.validate_lock({**_good_lock(), 'opaque_files': {path: 'c' * 64}})


def test_a_lock_file_beneath_a_reserved_name_is_refused():
    lock = {**_good_lock(), 'files': {'catalog.json': 'b' * 64, 'floor.json/x': 'c' * 64}}
    with pytest.raises(floor.FloorError, match='reserved'):
        floor.validate_lock(lock)


@pytest.mark.parametrize('path', ['floor.json/signature', 'catalog.json/x'])
def test_a_catalog_that_declares_a_path_beneath_a_reserved_name_is_refused(catalog, path):
    providers = [_entry('clash', {path: b'{}\n'}, path='')]
    catalog.write('catalog.json', json.dumps({'providers': providers}).encode())
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', 'clash')
    commit = catalog.git('rev-parse', 'HEAD')

    clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
    with pytest.raises(floor.FloorError, match='reserves'):
        floor.make_lock(clone, commit)


# ---- the lock command ---------------------------------------------------

def test_the_lock_command_defaults_to_the_branch_tip_and_round_trips(catalog, tmp_path):
    catalog.publish('older')
    catalog.bundles['alpha'] = {'provider.py': b'print("newer")\n'}
    tip = catalog.publish('tip')
    out = tmp_path / 'pin.lock.json'

    code = floor.main(['lock', '--repo-url', str(catalog.path), '--out', str(out)])

    assert code == 0
    lock = floor.load_lock(out)
    assert lock['commit'] == tip
    assert lock['repo'] == floor.DEFAULT_REPO and lock['branch'] == 'main'
    assert set(lock['files']) == {
        'catalog.json', 'providers/alpha/provider.py',
        'providers/beta/provider.py', 'providers/beta/helper.py',
    }
    assert out.read_text() == floor.dump_lock(lock)


def test_the_lock_command_refuses_a_commit_that_is_not_on_the_branch(catalog, tmp_path, capsys):
    catalog.publish('base')
    catalog.git('checkout', '--quiet', '-b', 'side')
    catalog.bundles['alpha'] = {'provider.py': b'print("side")\n'}
    side = catalog.publish('side only')
    catalog.git('checkout', '--quiet', 'main')
    out = tmp_path / 'pin.lock.json'

    code = floor.main(['lock', '--repo-url', str(catalog.path), '--commit', side, '--out', str(out)])

    assert code == 1
    assert not out.exists()


# ---- lock shape ---------------------------------------------------------

def _good_lock():
    return {
        'lock_version': 1, 'repo': 'o/r', 'branch': 'main',
        'commit': 'a' * 40, 'files': {'catalog.json': 'b' * 64},
    }


@pytest.mark.parametrize('change', [
    {'commit': 'A' * 40},
    {'commit': 'a' * 39},
    {'lock_version': 2},
    {'repo': 'no-slash'},
    {'files': {}},
    {'files': {'catalog.json': 'b' * 64, '../x': 'c' * 64}},
    {'files': {'catalog.json': 'B' * 64}},
    {'files': {'other.json': 'b' * 64}},
    {'extra': 1},
])
def test_a_malformed_lock_is_refused(change):
    lock = {**_good_lock(), **change}
    with pytest.raises(floor.FloorError):
        floor.validate_lock(lock)


def test_a_good_lock_validates():
    assert floor.validate_lock(_good_lock())


def test_the_committed_lock_is_well_formed():
    lock = floor.load_lock(LOCK)
    assert lock['repo'] == 'LavX/bazarr-provider-catalog'
    assert lock['branch'] == 'main'
    assert 'catalog.json' in lock['files']
    assert list(lock['files']) == sorted(lock['files'])
    with open(LOCK, encoding='utf-8') as handle:
        assert handle.read() == floor.dump_lock(lock), 'rewrite the lock with the lock command'


# ---- the signed feed ----------------------------------------------------

SIGNED_PATHS = ['signed/main/catalog.json', 'signed/main/catalog.json.sig', 'keys/catalog-signing-keys.json']


def _stamp(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


@pytest.fixture
def keys():
    from test_provider_hub_signing import Keys
    return Keys()


def _signing_dir(tmp_path, roots, name='signing'):
    """The verifier and a root file naming ``roots``, laid out the way the image stage lays them out."""
    target = tmp_path / name
    target.mkdir()
    shutil.copy(os.path.join(PROVIDER_HUB, 'signing.py'), target / 'signing.py')
    lines = ''.join(f'    {key!r}: base64.b64decode({base64.b64encode(raw).decode()!r}),\n' for key, raw in roots.items())
    (target / 'signing_roots.py').write_text(
        f'import base64\n\nSIGNING_ROOTS = {{\n{lines}}}\n', encoding='utf-8')
    return target


@pytest.fixture
def signing_dir(tmp_path, keys):
    return _signing_dir(tmp_path, keys.roots)


def _plugin_entry(plugin_id, files):
    entry = _entry(plugin_id, files, path=f'plugins/{plugin_id}')
    manifest = entry['manifest']
    manifest.pop('provider_id')
    manifest['id'] = plugin_id
    manifest['schema_version'] = 2
    return entry


def _sign_into_tree(catalog, keys, source_commit, *, sign_bytes=None, key_file=None, expires_in=89, signed_ago=1,
                    channel='main', directory='main', providers=None, message='sign main'):
    """Commit a signed feed for ``source_commit`` on top of whatever the tree holds, under signed/<directory>.
    Returns the sha."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    body = {
        'schema_version': 2, 'name': 'Fake', 'channel': channel, 'signed_version': 1000,
        'not_before': _stamp(now - timedelta(days=signed_ago)),
        'expires': _stamp(now + timedelta(days=expires_in)),
        'source_commit': source_commit,
        'providers': ([_entry(pid, files) for pid, files in catalog.bundles.items()]
                      if providers is None else providers),
        'plugins': [_plugin_entry(pid, files) for pid, files in catalog.plugins.items()],
    }
    raw = json.dumps(body, sort_keys=True).encode()
    from test_provider_hub_signing import envelope_for
    envelope = json.dumps(envelope_for(raw, keys.ci)).encode()
    catalog.write(f'signed/{directory}/catalog.json', raw)
    catalog.write(f'signed/{directory}/catalog.json.sig', sign_bytes(raw, envelope) if sign_bytes else envelope)
    catalog.write('keys/catalog-signing-keys.json',
                  key_file if key_file is not None else json.dumps(keys.key_file()).encode())
    catalog.git('add', '-A')
    catalog.git('commit', '--quiet', '-m', message)
    return catalog.git('rev-parse', 'HEAD')


def _signed_lock(catalog, head, signing_dir, paths=SIGNED_PATHS, branch='main'):
    clone = floor.fetch_branch(str(catalog.path), branch, _fresh_dir(catalog))
    return floor.make_lock(clone, head, branch, opaque_paths=paths, signing_dir=str(signing_dir))


def _signed_catalog(catalog, keys, **options):
    """A source commit, then a later head that changes a bundle and carries the signed feed for the source."""
    catalog.plugins['gamma'] = {'service.py': b'print("gamma")\n'}
    source = catalog.publish('source')
    catalog.bundles['alpha'] = {'provider.py': b'print("alpha, after the source")\n'}
    catalog.publish('main moved on')
    # The signed body is built from the source commit's bundles, so it is signed from a tree that has them.
    return source, _sign_with_bundles_of(catalog, keys, source, **options)


def _sign_with_bundles_of(catalog, keys, source, **options):
    moved = dict(catalog.bundles)
    catalog.bundles = {'alpha': {'provider.py': b'print("alpha")\n'}, 'beta': moved['beta']}
    try:
        return _sign_into_tree(catalog, keys, source, **options)
    finally:
        catalog.bundles = moved


def test_a_lock_ahead_of_the_source_commit_bakes_the_bundles_from_the_source_commit(
        catalog, keys, signing_dir, tmp_path, capsys):
    source, head = _signed_catalog(catalog, keys)
    assert source != head
    lock = _signed_lock(catalog, head, signing_dir)

    code, dest = _build(catalog, lock, tmp_path, signing_dir=signing_dir)

    assert code == 0
    tree = _tree(dest)
    # Bundles and catalog.json come from the source commit, not the head.
    assert tree['providers/alpha/provider.py'] == b'print("alpha")\n'
    assert tree['plugins/gamma/service.py'] == b'print("gamma")\n'
    meta = json.loads(tree['floor.json'])
    assert meta['commit'] == source and lock['commit'] == head
    # The signed files ride along byte for byte from the head, listed with their hashes.
    for path in SIGNED_PATHS:
        assert tree[path] == catalog.git('show', f'{head}:{path}').encode()
        assert meta['files'][path] == hashlib.sha256(tree[path]).hexdigest()
    assert json.loads(tree['signed/main/catalog.json'])['source_commit'] == source
    floor.check_modes(dest)
    out = capsys.readouterr().out
    assert 'signed feed baked: channel main' in out and source[:12] in out


def test_the_image_stage_command_builds_a_signed_snapshot_in_isolated_mode(catalog, keys, signing_dir, tmp_path):
    """The Dockerfile runs the script as `python -I` with --signing-dir, so no path but the arguments is trusted."""
    import sys
    _source, head = _signed_catalog(catalog, keys)
    lock_path = tmp_path / 'stage.lock.json'
    lock_path.write_text(floor.dump_lock(_signed_lock(catalog, head, signing_dir)))
    dest = tmp_path / 'stage-out'

    run = subprocess.run(
        [sys.executable, '-I', SCRIPT, 'build', '--lock', str(lock_path), '--repo-url', str(catalog.path),
         '--signing-dir', str(signing_dir), '--dest', str(dest)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    assert run.returncode == 0, run.stderr.decode()
    assert 'signed feed baked' in run.stdout.decode()
    assert 'signed/main/catalog.json' in _tree(dest)


def _run_stage_command(script, *arguments):
    import sys
    return subprocess.run([sys.executable, '-I', str(script), *arguments],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def test_the_script_runs_from_a_copy_outside_the_repository_with_and_without_a_signed_lock(catalog, keys, signing_dir, tmp_path):
    """A copy beside nothing else, run as `python -I`: it cannot lean on the repository layout around it."""
    outside = tmp_path / 'outside'
    outside.mkdir()
    script = outside / 'catalog_floor.py'
    shutil.copy(SCRIPT, script)
    unsigned_commit = catalog.publish('no signed feed yet')
    unsigned_lock = tmp_path / 'unsigned.lock.json'
    unsigned_lock.write_text(floor.dump_lock(_lock_for(catalog, unsigned_commit)))

    plain = _run_stage_command(script, 'build', '--lock', str(unsigned_lock), '--repo-url', str(catalog.path),
                               '--dest', str(tmp_path / 'plain-out'))

    assert plain.returncode == 0, plain.stderr.decode()
    assert 'no signed feed pinned' in plain.stdout.decode()

    _source, head = _signed_catalog(catalog, keys)
    signed_lock = tmp_path / 'signed.lock.json'
    signed_lock.write_text(floor.dump_lock(_signed_lock(catalog, head, signing_dir)))

    signed = _run_stage_command(script, 'build', '--lock', str(signed_lock), '--repo-url', str(catalog.path),
                                '--signing-dir', str(signing_dir), '--dest', str(tmp_path / 'signed-out'))

    assert signed.returncode == 0, signed.stderr.decode()
    assert 'signed feed baked' in signed.stdout.decode()


def test_the_script_does_not_work_out_a_verifier_path_at_import(tmp_path):
    """A script two directories deep, as in the image, loads, and only a signed lock without --signing-dir asks."""
    if os.path.realpath('/opt') != '/opt':
        pytest.skip('/opt is a link to a deeper path here, so a script at /opt/catalog_floor.py is not shallow')
    namespace = {'__name__': '_shallow_catalog_floor', '__file__': '/opt/catalog_floor.py'}
    with open(SCRIPT, encoding='utf-8') as handle:
        exec(compile(handle.read(), '/opt/catalog_floor.py', 'exec'), namespace)

    with pytest.raises(namespace['FloorError'], match='--signing-dir'):
        namespace['_default_signing_dir']()


def test_unsigned_mode_is_not_reachable_by_dropping_pins_from_the_lock(catalog, keys, signing_dir, tmp_path, capsys):
    _source, head = _signed_catalog(catalog, keys)
    good = _signed_lock(catalog, head, signing_dir)
    for kept in (['keys/catalog-signing-keys.json'], []):
        dropped = json.loads(json.dumps(good))
        dropped['opaque_files'] = {path: digest for path, digest in good['opaque_files'].items() if path in kept}

        code, dest = _build(catalog, dropped, tmp_path, name=f'dropped-{len(kept)}', signing_dir=signing_dir)

        assert code == 1 and not dest.exists()
        assert 'signed/main/catalog.json' in capsys.readouterr().err
        clone = floor.fetch_branch(str(catalog.path), 'main', _fresh_dir(catalog))
        with pytest.raises(floor.FloorError, match='must pin all of'):
            floor.make_lock(clone, head, 'main', opaque_paths=kept, signing_dir=str(signing_dir))


def test_a_commit_before_the_first_signed_feed_still_builds_on_a_branch_that_has_one_later(
        catalog, keys, signing_dir, tmp_path):
    before = catalog.publish('before any signing')
    _signed_catalog(catalog, keys)

    code, dest = _build(catalog, _lock_for(catalog, before), tmp_path, signing_dir=signing_dir)

    assert code == 0 and not any(path.startswith('signed/') for path in _tree(dest))


def test_a_signed_body_without_providers_is_refused_as_the_reader_refuses_it(catalog, keys, signing_dir):
    catalog.plugins['gamma'] = {'service.py': b'print("gamma")\n'}
    source = catalog.publish('source')
    head = _sign_into_tree(catalog, keys, source, providers=[])

    with pytest.raises(floor.FloorError, match='the signed catalog has no providers list'):
        _signed_lock(catalog, head, signing_dir)


def test_a_lock_without_signed_files_still_builds_and_says_none_is_baked(catalog, tmp_path, capsys):
    commit = catalog.publish()

    code, dest = _build(catalog, _lock_for(catalog, commit), tmp_path)

    assert code == 0
    assert not any(path.startswith(('signed/', 'keys/')) for path in _tree(dest))
    assert json.loads(_tree(dest)['floor.json'])['commit'] == commit
    assert 'no signed feed pinned' in capsys.readouterr().out


def test_the_lock_command_reports_whether_the_signed_feed_is_pinned(catalog, keys, signing_dir, tmp_path, capsys):
    _source, head = _signed_catalog(catalog, keys)
    out = tmp_path / 'pin.lock.json'

    assert floor.main(['lock', '--repo-url', str(catalog.path), '--commit', head, '--signing-dir', str(signing_dir),
                       *[arg for path in SIGNED_PATHS for arg in ('--opaque', path)], '--out', str(out)]) == 0

    assert 'signed feed pinned' in capsys.readouterr().out
    assert set(floor.load_lock(out)['opaque_files']) == set(SIGNED_PATHS)


def _flip_a_signature_byte(raw, envelope):
    document = json.loads(envelope)
    signature = bytearray(base64.b64decode(document['signatures'][0]['signature']))
    signature[0] ^= 0xFF
    document['signatures'][0]['signature'] = base64.b64encode(bytes(signature)).decode()
    return json.dumps(document).encode()


def _relock_at(catalog, lock, head):
    """A lock whose opaque hashes match the tree at ``head``, as a release prep that skipped the check would write."""
    lock = json.loads(json.dumps(lock))
    lock['commit'] = head
    for path in lock['opaque_files']:
        lock['opaque_files'][path] = _sha(catalog.git('show', f'{head}:{path}').encode())
    return lock


def test_the_build_refuses_a_bad_signature(catalog, keys, signing_dir, tmp_path, capsys):
    source, good_head = _signed_catalog(catalog, keys)
    lock = _signed_lock(catalog, good_head, signing_dir)
    bad_head = _sign_with_bundles_of(catalog, keys, source, sign_bytes=_flip_a_signature_byte, message='bad sig')
    # The tampered envelope is binary-safe JSON, so the hash read through git show is the file's own.
    bad_lock = _relock_at(catalog, lock, bad_head)

    code, dest = _build(catalog, bad_lock, tmp_path, signing_dir=signing_dir)

    assert code == 1
    assert 'does not verify (bad_signature)' in capsys.readouterr().err
    assert not dest.exists()


def test_the_lock_command_refuses_a_bad_signature(catalog, keys, signing_dir, tmp_path):
    source, _head = _signed_catalog(catalog, keys)
    bad_head = _sign_with_bundles_of(catalog, keys, source, sign_bytes=_flip_a_signature_byte, message='bad sig')

    with pytest.raises(floor.FloorError, match='does not verify'):
        _signed_lock(catalog, bad_head, signing_dir)


def test_the_build_refuses_a_feed_signed_under_another_root(catalog, keys, tmp_path, capsys):
    from test_provider_hub_signing import Keys
    _source, head = _signed_catalog(catalog, keys)
    right = _signing_dir(tmp_path, keys.roots, 'right')
    lock = _signed_lock(catalog, head, right)
    wrong = _signing_dir(tmp_path, Keys().roots, 'wrong')

    code, dest = _build(catalog, lock, tmp_path, signing_dir=wrong)

    assert code == 1
    assert 'does not verify (bad_signature)' in capsys.readouterr().err
    assert not dest.exists()


def test_the_build_refuses_when_no_root_is_baked(catalog, keys, tmp_path, capsys):
    _source, head = _signed_catalog(catalog, keys)
    lock = _signed_lock(catalog, head, _signing_dir(tmp_path, keys.roots, 'right'))

    code, _dest = _build(catalog, lock, tmp_path, signing_dir=_signing_dir(tmp_path, {}, 'empty'))

    assert code == 1
    assert 'does not verify' in capsys.readouterr().err


def test_a_lock_that_misses_the_key_file_is_refused(catalog, keys, signing_dir, tmp_path):
    _source, head = _signed_catalog(catalog, keys)

    with pytest.raises(floor.FloorError, match='must pin all of.*missing keys/catalog-signing-keys.json'):
        _signed_lock(catalog, head, signing_dir, paths=SIGNED_PATHS[:2])


def test_a_signed_feed_whose_key_file_is_not_in_the_tree_is_refused(catalog, keys, signing_dir, tmp_path):
    _source, head = _signed_catalog(catalog, keys)
    catalog.git('rm', '--quiet', 'keys/catalog-signing-keys.json')
    catalog.git('commit', '--quiet', '-m', 'drop the key file')
    head = catalog.git('rev-parse', 'HEAD')

    with pytest.raises(floor.FloorError, match='does not exist'):
        _signed_lock(catalog, head, signing_dir)


def test_a_key_file_the_root_did_not_sign_is_refused(catalog, keys, signing_dir, tmp_path):
    from test_provider_hub_signing import Keys
    source, _head = _signed_catalog(catalog, keys)
    stranger = Keys().key_file()
    head = _sign_with_bundles_of(catalog, keys, source, key_file=json.dumps(stranger).encode(), message='foreign keys')

    with pytest.raises(floor.FloorError, match='does not verify'):
        _signed_lock(catalog, head, signing_dir)


def test_a_feed_signed_for_another_channel_is_refused(catalog, keys, signing_dir):
    source, _head = _signed_catalog(catalog, keys)
    # A body signed for beta, published under main's path.
    head = _sign_with_bundles_of(catalog, keys, source, channel='beta', directory='main', message='beta body in main')

    with pytest.raises(floor.FloorError, match='channel_mismatch'):
        _signed_lock(catalog, head, signing_dir)


def test_the_baked_check_ignores_the_expiry_window(catalog, keys, signing_dir, tmp_path):
    catalog.plugins['gamma'] = {'service.py': b'print("gamma")\n'}
    source = catalog.publish('source')
    head = _sign_into_tree(catalog, keys, source, expires_in=-10, signed_ago=200)

    lock = _signed_lock(catalog, head, signing_dir)
    code, dest = _build(catalog, lock, tmp_path, signing_dir=signing_dir)

    assert code == 0 and 'signed/main/catalog.json' in _tree(dest)


def test_a_source_commit_that_is_not_behind_the_locked_commit_is_refused(catalog, keys, signing_dir):
    catalog.plugins['gamma'] = {'service.py': b'print("gamma")\n'}
    catalog.publish('base')
    catalog.git('checkout', '--quiet', '-b', 'side')
    catalog.bundles['alpha'] = {'provider.py': b'print("side")\n'}
    ahead = catalog.publish('a commit the signed head has not reached')
    catalog.git('checkout', '--quiet', 'main')
    # The sign commit names a commit that only joins main afterwards.
    head = _sign_into_tree(catalog, keys, ahead, message='sign a commit that is not behind it')
    catalog.git('merge', '--quiet', '--no-ff', '-m', 'merge', 'side')

    with pytest.raises(floor.FloorError, match='not the locked commit or an ancestor'):
        _signed_lock(catalog, head, signing_dir)


def test_a_source_commit_that_is_not_a_commit_is_refused(catalog, keys, signing_dir):
    catalog.plugins['gamma'] = {'service.py': b'print("gamma")\n'}
    catalog.publish('base')
    head = _sign_into_tree(catalog, keys, 'e' * 40)

    with pytest.raises(floor.FloorError, match='not in the history'):
        _signed_lock(catalog, head, signing_dir)


def test_a_bundle_file_that_does_not_match_the_signed_pin_is_refused(catalog, keys, signing_dir):
    original = {'service.py': b'print("gamma")\n'}
    catalog.plugins['gamma'] = {'service.py': b'print("gamma, edited")\n'}
    source = catalog.publish('the plugin file differs from what gets signed')
    catalog.plugins['gamma'] = original
    head = _sign_into_tree(catalog, keys, source)

    with pytest.raises(floor.FloorError, match='does not match the hash the signed catalog pins'):
        _signed_lock(catalog, head, signing_dir)


def test_the_builder_and_the_reader_name_the_same_signed_paths():
    from provider_hub import catalog_feed
    assert floor.SIGNED_CATALOG_PATH == catalog_feed.SIGNED_CATALOG_PATH
    assert floor.SIGNED_ENVELOPE_PATH == catalog_feed.SIGNED_ENVELOPE_PATH
    assert floor.KEY_FILE_PATH == catalog_feed.KEY_FILE_PATH
    assert floor.SIGNED_CHANNELS == (catalog_feed.CHANNEL_MAIN, catalog_feed.CHANNEL_BETA)


def test_the_dockerfile_verifies_the_signed_feed_with_the_applications_own_verifier():
    text = _dockerfile()
    stage = text[text.index('AS catalog-floor'):text.index('AS production')]
    assert 'COPY bazarr/provider_hub/signing.py bazarr/provider_hub/signing_roots.py /opt/provider_hub/' in stage
    assert '--signing-dir /opt/provider_hub' in stage
    with open(os.path.join(ROOT, 'requirements.txt'), encoding='utf-8') as handle:
        requirement = next(line.strip() for line in handle if line.startswith('cryptography'))
    assert f'pip install "{requirement}"' in stage, 'the floor stage must install the same cryptography requirement'


# ---- modes --------------------------------------------------------------

def test_a_writable_file_fails_the_mode_check(catalog, tmp_path):
    commit = catalog.publish()
    _code, dest = _build(catalog, _lock_for(catalog, commit), tmp_path)
    target = dest / 'catalog.json'
    os.chmod(target, 0o644)

    with pytest.raises(floor.FloorError, match='file mode 0644'):
        floor.check_modes(dest)


def test_a_writable_directory_fails_the_mode_check(catalog, tmp_path):
    commit = catalog.publish()
    _code, dest = _build(catalog, _lock_for(catalog, commit), tmp_path)
    os.chmod(dest / 'providers', 0o755)

    with pytest.raises(floor.FloorError, match='directory mode 0755'):
        floor.check_modes(dest)


def test_a_symlink_fails_the_mode_check(catalog, tmp_path):
    commit = catalog.publish()
    _code, dest = _build(catalog, _lock_for(catalog, commit), tmp_path)
    os.chmod(dest, 0o755)
    os.symlink('catalog.json', dest / 'link')
    os.chmod(dest, 0o555)

    with pytest.raises(floor.FloorError, match='symlink'):
        floor.check_modes(dest)


@pytest.mark.skipif(os.geteuid() == 0, reason='root owns everything it creates')
def test_the_root_owner_check_fails_for_another_user(catalog, tmp_path):
    commit = catalog.publish()
    _code, dest = _build(catalog, _lock_for(catalog, commit), tmp_path)

    with pytest.raises(floor.FloorError, match='expected root'):
        floor.check_modes(dest, expect_root_owner=True)


# ---- the Dockerfile -----------------------------------------------------

def _dockerfile():
    with open(DOCKERFILE, encoding='utf-8') as handle:
        return handle.read()


def test_the_dockerfile_builds_the_floor_from_the_lock_and_asserts_the_modes():
    text = _dockerfile()
    assert 'AS catalog-floor' in text
    assert 'COPY docker/catalog-pin.lock.json' in text
    assert 'COPY scripts/release/catalog_floor.py' in text
    assert 'catalog_floor.py build' in text and '--expect-root-owner' in text
    assert 'COPY --from=catalog-floor /app/bazarr/catalog_floor ./catalog_floor' in text
    assert '--runtime-seed' in text and 'check_image_interpreter' in text
    for assertion in ('-type f ! -perm 0444', '-type d ! -perm 0555', '! -user root'):
        assert assertion in text, f'the production stage no longer asserts {assertion}'


def test_the_floor_is_copied_into_the_application_tree_not_the_config_volume():
    text = _dockerfile()
    workdir = text.index('WORKDIR /app/bazarr')
    copy = text.index('COPY --from=catalog-floor')
    ownership_check = text.index('find /app \\( ! -user root')
    assert workdir < copy < ownership_check, (
        'the floor must land under /app/bazarr before the whole-tree ownership assertion')


def test_the_runtime_builder_refuses_a_destination_another_user_could_redirect(tmp_path):
    shared = tmp_path / 'shared'
    shared.mkdir()
    shared.chmod(0o777)
    lock = {'lock_version': 1, 'repo': floor.DEFAULT_REPO, 'branch': 'main', 'commit': 'a' * 40}
    try:
        with pytest.raises(floor.FloorError, match='other users can redirect'):
            floor.write_floor(lock, {'catalog.json': b'{}'}, shared / 'floor',
                              build_runtime=lambda *_: pytest.fail('runtime build must not start'))
    finally:
        shared.chmod(0o755)
