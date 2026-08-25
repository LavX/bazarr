# coding=utf-8
"""A series episode is mapped with the series path mapping, not the movie one.

Bazarr keeps separate movie and series path mappings, and installs that store
the two libraries on different mounts configure them differently. Re-indexing an
episode through path_replace_movie() therefore hands store_subtitles a path that
does not exist, and the episode's subtitles stay unindexed with nothing in the
log to say why.
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
