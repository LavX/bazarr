# coding=utf-8
"""The catalog release floor build: scripts/release/catalog_floor.py.

The image ships a snapshot of the official provider catalog, pinned by one
lock file. The build must accept a lock whose commit is the branch tip or an
ancestor of it (the branch moves on between release prep and the build), must
refuse a commit that left the branch, and must refuse any file that does not
hash to what the lock and the catalog say. The tests run the real script
against a fake catalog repository built with git in a temporary directory.
"""
import hashlib
import importlib.util
import json
import os
import stat
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, 'scripts', 'release', 'catalog_floor.py')
LOCK = os.path.join(ROOT, 'docker', 'catalog-pin.lock.json')
DOCKERFILE = os.path.join(ROOT, 'Dockerfile')


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


def _build(catalog, lock, tmp_path, name='out'):
    lock_path = tmp_path / f'{name}.lock.json'
    lock_path.write_text(floor.dump_lock(lock))
    dest = tmp_path / name
    code = floor.main([
        'build', '--lock', str(lock_path), '--repo-url', str(catalog.path), '--dest', str(dest),
    ])
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
    assert 'COPY --from=catalog-floor /catalog_floor ./catalog_floor' in text
    for assertion in ('-type f ! -perm 0444', '-type d ! -perm 0555', '! -user root'):
        assert assertion in text, f'the production stage no longer asserts {assertion}'


def test_the_floor_is_copied_into_the_application_tree_not_the_config_volume():
    text = _dockerfile()
    workdir = text.index('WORKDIR /app/bazarr')
    copy = text.index('COPY --from=catalog-floor')
    ownership_check = text.index('find /app \\( ! -user root')
    assert workdir < copy < ownership_check, (
        'the floor must land under /app/bazarr before the whole-tree ownership assertion')
