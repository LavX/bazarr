# coding=utf-8
"""A series episode is mapped as a series, and by the instance that owns it.

Bazarr keeps separate movie and series path mappings, and installs that store
the two libraries on different mounts configure them differently. Re-indexing an
episode through path_replace_movie() therefore hands store_subtitles a path that
does not exist, and the episode's subtitles stay unindexed with nothing in the
log to say why.

A secondary Sonarr instance can carry its own path mappings on top of that, so
the episode row's own arr_instance_id decides the mapping rather than the global
one. path_replace_instance() falls back to the global mapping when the instance
has none configured, which is every single-instance install.
"""

import types

import pytest

import app.database  # noqa: F401


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


@pytest.fixture
def episode_module(monkeypatch):
    from subtitles.mass_download import series as module

    unindexed = types.SimpleNamespace(
        arr_instance_id=None,
        path="/tv/Show/Season 1/Show.S01E01.mkv",
        subtitles=None,
        missing_subtitles=None,
        sonarrSeriesId=1,
        audio_language="English",
        seriesType="standard",
        title="Show",
        episodeTitle="One",
        season=1,
        episode=1,
        profileId=1,
        tags="[]",
    )

    monkeypatch.setattr(module.database, "execute", lambda *a, **k: _Result(unindexed))
    monkeypatch.setattr(module.jobs_queue, "update_job_progress", lambda **k: None)
    monkeypatch.setattr(module.path_mappings, "path_replace",
                        lambda path: path.replace("/tv/", "/mnt/series/"))
    monkeypatch.setattr(module.path_mappings, "path_replace_movie",
                        lambda path: path.replace("/tv/", "/mnt/movies/"))
    monkeypatch.setattr(
        module.path_mappings, "path_replace_instance",
        # What the real helper does: the instance's own mapping when it has one,
        # the global mapping for its media type otherwise.
        lambda path, arr_instance_id, media_type: (
            path.replace("/tv/", "/mnt/instance7/") if arr_instance_id == 7
            else path.replace("/tv/", f"/mnt/{media_type}/")
        ),
    )

    return module, unindexed


def test_reindexing_an_episode_uses_the_series_path_mapping(episode_module, monkeypatch):
    module, unindexed = episode_module
    indexed = []
    monkeypatch.setattr(module, "store_subtitles", lambda path, mapped: indexed.append((path, mapped)))

    # The episode file is not on this machine, so the call stops right after the
    # re-index. That is the whole region under test.
    with pytest.raises(OSError):
        module.episode_download_subtitles(1, job_id=1, job_sub_function=True)

    assert indexed == [(unindexed.path, "/mnt/series/Show/Season 1/Show.S01E01.mkv")]


def test_the_owning_instance_path_mapping_wins(episode_module, monkeypatch):
    module, unindexed = episode_module
    unindexed.arr_instance_id = 7
    indexed = []
    monkeypatch.setattr(module, "store_subtitles", lambda path, mapped: indexed.append((path, mapped)))

    with pytest.raises(OSError):
        module.episode_download_subtitles(1, job_id=1, job_sub_function=True)

    assert indexed == [(unindexed.path, "/mnt/instance7/Show/Season 1/Show.S01E01.mkv")]
