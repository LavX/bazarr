# coding=utf-8
"""The auto-sync step that runs after a subtitle download must not hijack the
parent job's progress bar.

When ``process_subtitle`` is invoked as part of a larger job (a wanted/mass
download whose ``progress_max`` is the episode/provider count), the follow-up
``sync_subtitles`` call must run with ``track_job_progress=False`` so it does not
overwrite that job's ``progress_max`` with the small sync-engine count (which
produced rings well over 100%). This mirrors the existing pattern in
``mass_operations``.
"""
from unittest.mock import patch, MagicMock

import pytest


class _Stop(Exception):
    """Sentinel raised by the mocked sync to stop process_subtitle early."""


def _fake_subtitle():
    sub = MagicMock()
    sub.provider_name = "opensubtitles"
    sub.uploader = "uploader"
    sub.release_info = "release"
    sub.score = 80
    sub.id = 1
    sub.storage_path = "/tmp/x.en.srt"
    sub.language.hi = False
    sub.language.forced = False
    sub.matches = set()
    return sub


def _run(media_type, meta, **kwargs):
    from subtitles import processing
    with patch.object(processing, "database") as db, \
            patch.object(processing, "_defaul_sync_checker", return_value=True), \
            patch.object(processing, "_get_download_code3", return_value="eng"), \
            patch.object(processing, "language_from_alpha3", return_value="English"), \
            patch.object(processing, "alpha2_from_alpha3", return_value="en"), \
            patch.object(processing, "alpha2_from_language", return_value="en"), \
            patch.object(processing, "alpha3_from_language", return_value="eng"), \
            patch("subtitles.sync.sync_subtitles", side_effect=_Stop) as sync_mock:
        db.execute.return_value.first.return_value = meta
        with pytest.raises(_Stop):
            processing.process_subtitle(_fake_subtitle(), media_type, "English",
                                        "/media/file.mkv", max_score=100, **kwargs)
    return sync_mock


def test_series_substep_sync_does_not_track_parent_job_progress():
    meta = MagicMock(sonarrSeriesId=7, sonarrEpisodeId=70, imdbId="tt1", tvdbId=1, season=1, episode=13)
    sync_mock = _run("series", meta, job_id=42)
    assert sync_mock.call_args.kwargs.get("job_id") == 42
    # Sub-step must NOT own the parent job's progress (no value/max hijack)...
    assert sync_mock.call_args.kwargs.get("owns_job_progress") is False
    # ...but tracking stays on so cancellation checkpoints survive during sync.
    assert sync_mock.call_args.kwargs.get("track_job_progress") is not False


def test_movie_substep_sync_does_not_track_parent_job_progress():
    meta = MagicMock(radarrId=9, imdbId="tt2", tmdbId=2)
    sync_mock = _run("movie", meta, job_id=99)
    assert sync_mock.call_args.kwargs.get("job_id") == 99
    assert sync_mock.call_args.kwargs.get("owns_job_progress") is False
    assert sync_mock.call_args.kwargs.get("track_job_progress") is not False
