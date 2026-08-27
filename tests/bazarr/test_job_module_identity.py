# coding=utf-8
"""A queued job must re-import the module it was queued from, not a copy of it.

`add_job_from_function` records the caller's module as a dotted path derived
from its filename relative to `bazarr_dir`, and `_run_job` later resolves that
string with `importlib.import_module`. `bazarr_dir` was computed one directory
too high, so it named the repository root rather than the package directory, and
every derived string came out prefixed: `bazarr.subtitles.wanted.movies` where
the running process had already imported `subtitles.wanted.movies`.

`app/libs.py` puts the parent directory on `sys.path` too, so both names resolve.
Python then holds two module objects for the same file, each with its own
module-level state.

That is not theoretical. `subtitles/cache.py` carries a comment describing this
exact split and a workaround for it, because the subtitle UUID stored by a
manual search was written into one copy and looked for in the other, so users
got "Subtitle not found in cache" on every manual download. The workaround
stores the dict on `sys.modules` under a fixed key and is unaffected by this fix.

The part nobody connected: `subtitles/pool.py` holds `_pools` at module level,
and Provider Hub spawns a worker subprocess per pool and provider. Two copies of
that module means two pool registries and, on a warm install, two worker fleets.

The invariant is simply that the string is the name the module already has.
"""
import os

import pytest


@pytest.mark.parametrize("module_name", [
    "subtitles.wanted.series",
    "subtitles.wanted.movies",
    "subtitles.pool",
    "sonarr.sync.series",
    "radarr.sync.movies",
])
def test_the_derived_job_module_is_the_name_the_module_already_has(module_name):
    import importlib

    from app import jobs_queue

    module = importlib.import_module(module_name)

    # Exactly what add_job_from_function does with the caller's filename.
    relative = os.path.relpath(module.__file__, start=jobs_queue.bazarr_dir)
    derived = os.path.splitext(relative)[0].replace(os.sep, '.')

    assert derived == module.__name__, (
        f'a job queued from {module.__name__} would be re-imported as {derived!r}, '
        'which is a second copy of the same file with its own module-level state'
    )


def test_bazarr_dir_is_the_package_directory():
    """The directory the top-level modules live in, not its parent."""
    from app import jobs_queue

    for expected in ('subtitles', 'sonarr', 'radarr', 'app'):
        assert os.path.isdir(os.path.join(jobs_queue.bazarr_dir, expected)), (
            f'{expected!r} is not directly under bazarr_dir '
            f'({jobs_queue.bazarr_dir}), so derived module paths will be wrong')
