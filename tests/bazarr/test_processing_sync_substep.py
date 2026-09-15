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


def test_sports_media_refresh_fires_only_for_configured_sports_libraries(monkeypatch):
    """A sports write must refresh a server only when it has a sports library
    configured: the Plex and Jellyfin refresh helpers are gated on their sports
    library settings, and the Emby and Silo dispatcher notification is gated on
    those master switches."""
    from app.config import settings
    from subtitles import processing

    calls = []
    notified = []
    monkeypatch.setattr(processing, "plex_update_sports_library", lambda: calls.append("plex"))
    monkeypatch.setattr(processing, "jellyfin_update_sports_library", lambda: calls.append("jellyfin"))
    monkeypatch.setattr(processing, "notify_subtitle_mutation", notified.append)
    monkeypatch.setattr(settings.general, "use_plex", True)
    monkeypatch.setattr(settings.general, "use_jellyfin", True)
    monkeypatch.setattr(settings.general, "use_emby", False)
    monkeypatch.setattr(settings.general, "use_silo", False)
    monkeypatch.setattr(settings.plex, "sports_library", [])
    monkeypatch.setattr(settings.jellyfin, "sports_library_ids", [])

    processing.refresh_sports_media_servers("/sports/Event.mkv", "/sports/Event.en.srt", 1)
    assert calls == []
    assert notified == []

    monkeypatch.setattr(settings.plex, "sports_library", ["Sports"])
    monkeypatch.setattr(settings.jellyfin, "sports_library_ids", ["10"])
    processing.refresh_sports_media_servers("/sports/Event.mkv", "/sports/Event.en.srt", 1)
    assert calls == ["plex", "jellyfin"]
    assert notified == []

    monkeypatch.setattr(settings.general, "use_emby", True)
    monkeypatch.setattr(settings.general, "use_silo", True)
    calls.clear()
    processing.refresh_sports_media_servers("/sports/Event.mkv", "/sports/Event.en.srt", 1)
    assert calls == ["plex", "jellyfin"]
    assert len(notified) == 1
    assert notified[0].media_type == "sports"
    assert notified[0].operation == "download"
    assert notified[0].arr_instance_id == 1
    assert notified[0].video_path == "/sports/Event.mkv"


def test_sports_process_subtitle_calls_the_media_server_refresh(monkeypatch):
    """Processing refreshes configured libraries and reports the exact file owner."""
    from types import SimpleNamespace
    from contextlib import nullcontext
    from subzero.language import Language
    from app.config import settings
    from jellyfin import operations as jellyfin
    from plex import operations as plex
    from languages import get_languages
    from subtitles import processing

    refreshed, published = [], []
    monkeypatch.setattr(settings.general, "use_plex", True)
    monkeypatch.setattr(settings.general, "use_jellyfin", True)
    monkeypatch.setattr(settings.general, "use_emby", True)
    monkeypatch.setattr(settings.plex, "sports_library", ["Sports"])
    monkeypatch.setattr(settings.jellyfin, "sports_library_ids", ["sports-id"])
    monkeypatch.setattr(plex, "get_plex_server", lambda: SimpleNamespace(library=SimpleNamespace(
        section=lambda name: SimpleNamespace(update=lambda: refreshed.append(("plex", name))))))
    monkeypatch.setattr(jellyfin, "get_jellyfin_client", lambda: SimpleNamespace(
        refresh_item=lambda library: refreshed.append(("jellyfin", library))))
    monkeypatch.setattr(processing, "notify_subtitle_mutation", published.append)
    monkeypatch.setattr(processing, "_defaul_sync_checker", lambda subtitle: False)
    monkeypatch.setattr(processing, "_postprocessing_config", lambda *args: (False, "", False, 0))
    monkeypatch.setattr(processing, "call_external_webhook", lambda **kwargs: None)
    monkeypatch.setattr(get_languages, "languages_dict", [
        {"code2": "en", "code3": "eng", "code3b": "eng", "name": "English"},
    ], raising=False)
    context = SimpleNamespace(mapped_path="/tmp/x.mkv", arr_instance_id=42)
    instance = SimpleNamespace(path_mappings='[["/sports", "/tmp"]]')
    subtitle = _fake_subtitle()
    subtitle.language = Language("eng")

    result, = processing.process_subtitle(
        subtitle, "sports", "English", context.mapped_path, max_score=100,
        context=context, validate=lambda: instance, publication_guard=nullcontext)

    assert result.path == "/sports/x.mkv"
    assert result.subs_path == "/sports/x.en.srt"
    assert refreshed == [("plex", "Sports"), ("jellyfin", "sports-id")]
    assert len(published) == 1
    assert published[0].arr_instance_id == 42
    assert published[0].video_path == "/tmp/x.mkv"
    assert published[0].subtitle_path == "/tmp/x.en.srt"
    assert published[0].media_type == "sports"
    assert published[0].operation == "download"
