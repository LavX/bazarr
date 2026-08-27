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
