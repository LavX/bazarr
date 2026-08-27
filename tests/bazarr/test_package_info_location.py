# coding=utf-8
"""The packaged version has to reach the running app.

``package_info`` is what tells System Status it is running "Bazarr+ v2.6.0" and
what sets ``updatemethod=External`` so a container never offers an in-app
update. ``init`` reads it from a path derived from its own file, and that
derivation was one directory too high for this repository's layout: it looked
beside the repository rather than inside it.

The whole block sits in a bare ``try/except``, so getting it wrong fails
silently. In the shipped image the file is at ``/app/bazarr/package_info`` while
the lookup asked for ``/app/package_info``, so the version never appeared and
the update method never applied, with nothing in the log either way.
"""
import os


def test_the_package_info_path_points_at_a_file_that_exists():
    from utilities.package import package_info_path

    path = package_info_path()
    assert os.path.isfile(path), (
        f'package_info was looked for at {path}, where there is no such file. '
        'Everything it configures is silently skipped when that happens.')


def test_the_packaged_version_is_readable():
    """Not just present: parsed. A file we can find but not read is the same
    outcome for the user."""
    from utilities.package import read_package_info

    info = read_package_info()
    assert info.get('packageversion', '').startswith('Bazarr+ v'), (
        f'packageversion did not parse out of package_info: {info!r}')


def test_the_external_update_method_is_recognised(tmp_path):
    from utilities.package import updates_are_externally_managed

    packaged = tmp_path / 'package_info'
    packaged.write_text('packageversion=x\nupdatemethod=External\n')
    assert updates_are_externally_managed(path=str(packaged))

    unmanaged = tmp_path / 'other_info'
    unmanaged.write_text('packageversion=x\n')
    assert not updates_are_externally_managed(path=str(unmanaged))
    assert not updates_are_externally_managed(path=str(tmp_path / 'absent'))


def _reload_get_args(monkeypatch, externally_managed):
    """Re-parse arguments as a CLI start would, with the packager's answer fixed."""
    import importlib
    import sys

    from utilities import package as package_mod

    monkeypatch.setenv('NO_CLI', 'false')
    monkeypatch.setenv('NO_UPDATE', 'false')
    monkeypatch.setattr(sys, 'argv', ['bazarr'])
    monkeypatch.setattr(package_mod, 'updates_are_externally_managed',
                        lambda *a, **kw: externally_managed)
    return importlib.reload(importlib.import_module('app.get_args'))


def test_an_externally_updated_package_does_not_run_the_in_app_updater(monkeypatch):
    """`updatemethod=External` is the documented way a packager turns the
    updater off. It only set an environment variable nothing reads, so every
    check, the scheduled update job and the Updates settings section all stayed
    live for any package that did not also pass --no-update."""
    try:
        assert _reload_get_args(monkeypatch, externally_managed=True).args.no_update is True
    finally:
        monkeypatch.undo()
        _restore_get_args()


def test_an_ordinary_install_still_updates_itself(monkeypatch):
    try:
        assert _reload_get_args(monkeypatch, externally_managed=False).args.no_update is False
    finally:
        monkeypatch.undo()
        _restore_get_args()


def _restore_get_args():
    """Put the module back the way the suite found it: NO_CLI, no real argv."""
    import importlib

    importlib.reload(importlib.import_module('app.get_args'))


def test_a_source_checkout_ignores_the_tracked_external_marker(tmp_path):
    """The repository tracks a package_info that declares updatemethod=External
    for the shipped image. A git checkout following the source-install docs is
    not that image: forcing no_update there disables in-app updates and shows
    the Docker-specific message to the wrong audience."""
    from utilities.package import updates_are_externally_managed

    packaged = tmp_path / 'package_info'
    packaged.write_text('packageversion=x\nupdatemethod=External\n')
    (tmp_path / '.git').mkdir()
    assert not updates_are_externally_managed(path=str(packaged))


def test_an_unreadable_package_info_is_treated_as_absent(tmp_path):
    """A package with wrong permissions must not abort startup at import time;
    the old parsing lived inside init's broad guard."""
    import os as os_mod

    from utilities.package import read_package_info

    packaged = tmp_path / 'package_info'
    packaged.write_text('packageversion=x\n')
    os_mod.chmod(packaged, 0o000)
    try:
        assert read_package_info(path=str(packaged)) == {}
    finally:
        os_mod.chmod(packaged, 0o644)


def test_an_undecodable_package_info_is_treated_as_absent(tmp_path):
    from utilities.package import read_package_info

    packaged = tmp_path / 'package_info'
    packaged.write_bytes(b'packageversion=\xff\xfe\x00garbage\n')
    assert read_package_info(path=str(packaged)) == {}


def test_get_args_imports_under_the_root_launcher():
    """The root bazarr.py imports bazarr.app.get_args BEFORE anything puts the
    bazarr/ directory on sys.path; main.py gets that for free as the subprocess
    entry script. get_args must therefore resolve its own imports either way,
    or the shipped image crash-loops at boot."""
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    result = subprocess.run(
        [sys.executable, '-c', 'import bazarr.app.get_args'],
        cwd=root,
        env={'NO_CLI': 'true', 'PATH': os.environ.get('PATH', '')},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
