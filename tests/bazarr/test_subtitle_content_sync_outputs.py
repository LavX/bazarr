# coding=utf-8

import importlib
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import app.database  # noqa: F401


def _real_module(module_name):
    module = sys.modules.get(module_name)
    if module is not None and not getattr(module, "__file__", None):
        sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def test_sync_modifier_language_code_is_valid():
    content = _real_module("api.subtitles.content")

    assert content._is_valid_language_code("hu:sync-ffsubsync")
    assert content._is_valid_language_code("hu:hi:sync-ffsubsync")
    assert content._is_valid_language_code("hu:forced:sync-alass")
    assert content._is_valid_language_code("pt-BR:sync-autosubsync")
    assert content._is_valid_language_code("eng:sync-alass")


def test_promote_sync_output_overwrites_target_atomically(tmp_path, monkeypatch):
    content = _real_module("api.subtitles.content")

    original = tmp_path / "Movie.hu.srt"
    generated = tmp_path / "Movie.hu.ffsubsync.srt"
    original.write_text("original subtitle", encoding="utf-8")
    generated.write_text("synced subtitle", encoding="utf-8")

    metadata = {"mediaPath": "/movies/Movie.mkv", "mediaId": 1203}

    def fake_resolve(media_type, media_id, language_code):
        if language_code == "hu":
            return str(original), metadata
        if language_code == "hu:sync-ffsubsync":
            return str(generated), metadata
        raise AssertionError(language_code)

    store_calls = []
    events = []
    history_calls = []
    monkeypatch.setattr(content, "resolve_subtitle_path", fake_resolve)
    monkeypatch.setattr(
        content, "language_from_alpha2", lambda value: {"hu": "Hungarian"}[value]
    )
    monkeypatch.setattr(
        content.path_mappings, "path_replace_movie", lambda value: value
    )
    monkeypatch.setattr(
        content,
        "store_subtitles_movie",
        lambda *args, **kwargs: store_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        content,
        "history_log_movie",
        lambda *args, **kwargs: history_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(content, "event_stream", lambda **kwargs: events.append(kwargs))

    response, status_code = content.promote_sync_subtitle(
        media_type="movie",
        media_id=1203,
        target_language="hu",
        source_language="hu:sync-ffsubsync",
    )

    assert status_code == 200
    assert response["sourceLanguage"] == "hu:sync-ffsubsync"
    assert response["targetLanguage"] == "hu"
    assert original.read_text(encoding="utf-8") == "synced subtitle"
    assert store_calls == [
        ((metadata["mediaPath"], metadata["mediaPath"]), {"use_cache": False})
    ]
    assert history_calls[0][1]["action"] == 5
    assert history_calls[0][1]["radarr_id"] == 1203
    assert history_calls[0][1]["result"].subs_path == str(original)
    assert "ffsubsync" in history_calls[0][1]["result"].message
    assert events == [{"type": "movie", "payload": 1203}]


def test_promote_sync_output_rejects_different_base_language(monkeypatch):
    content = _real_module("api.subtitles.content")

    def fail_resolve(*args, **kwargs):
        raise AssertionError("path resolution should not run")

    monkeypatch.setattr(content, "resolve_subtitle_path", fail_resolve)

    response, status_code = content.promote_sync_subtitle(
        media_type="movie",
        media_id=1203,
        target_language="hu",
        source_language="en:sync-ffsubsync",
    )

    assert status_code == 400
    assert response == "Source and target languages do not match"


def test_postprocess_preserves_sync_language_modifier(monkeypatch):
    utils = _real_module("api.utils")

    monkeypatch.setattr(utils.path_mappings, "path_replace_movie", lambda value: value)
    monkeypatch.setattr(
        utils,
        "language_from_alpha2",
        lambda value: {"hu": "Hungarian"}[value],
    )
    monkeypatch.setattr(utils, "alpha3_from_alpha2", lambda value: {"hu": "hun"}[value])

    item = utils.postprocess(
        {
            "radarrId": 1203,
            "title": "Movie",
            "subtitles": "[['hu:sync-ffsubsync', '/movies/Movie.hu.ffsubsync.srt', 128]]",
        }
    )

    assert item["subtitles"] == [
        {
            "path": "/movies/Movie.hu.ffsubsync.srt",
            "name": "Hungarian",
            "code2": "hu",
            "code3": "hun",
            "language": "hu:sync-ffsubsync",
            "modifier": "sync-ffsubsync",
            "forced": False,
            "hi": False,
            "file_size": 128,
        }
    ]


def test_sync_status_marks_subtitle_unconfirmed_when_file_changed_after_sync(
    tmp_path, monkeypatch
):
    content = _real_module("api.subtitles.content")

    subtitle = tmp_path / "Movie.hu.srt"
    subtitle.write_text("edited subtitle", encoding="utf-8")
    modified_at = datetime(2026, 5, 27, 12, 0, 10).timestamp()
    os.utime(subtitle, (modified_at, modified_at))

    monkeypatch.setattr(
        content,
        "resolve_subtitle_path",
        lambda *args, **kwargs: (str(subtitle), {"mediaPath": "/movies/Movie.mkv"}),
    )
    monkeypatch.setattr(
        content,
        "_latest_sync_history_for_path",
        lambda *args, **kwargs: SimpleNamespace(
            timestamp=datetime(2026, 5, 27, 12, 0, 0)
        ),
    )

    response, status_code = content.get_subtitle_sync_status(
        media_type="movie",
        media_id=1203,
        language_code="hu",
    )

    assert status_code == 200
    assert response["synced"] is True
    assert response["confirmed"] is False
    assert response["editedAfterSync"] is True
    assert response["lastSyncTimestamp"] == "2026-05-27T12:00:00"


def test_sync_status_confirms_subtitle_when_sync_history_is_newer_than_file(
    tmp_path, monkeypatch
):
    content = _real_module("api.subtitles.content")

    subtitle = tmp_path / "Movie.hu.srt"
    subtitle.write_text("synced subtitle", encoding="utf-8")
    modified_at = datetime(2026, 5, 27, 12, 0, 0).timestamp()
    os.utime(subtitle, (modified_at, modified_at))

    monkeypatch.setattr(
        content,
        "resolve_subtitle_path",
        lambda *args, **kwargs: (str(subtitle), {"mediaPath": "/movies/Movie.mkv"}),
    )
    monkeypatch.setattr(
        content,
        "_latest_sync_history_for_path",
        lambda *args, **kwargs: SimpleNamespace(
            timestamp=datetime(2026, 5, 27, 12, 0, 10)
        ),
    )

    response, status_code = content.get_subtitle_sync_status(
        media_type="movie",
        media_id=1203,
        language_code="hu",
    )

    assert status_code == 200
    assert response["synced"] is True
    assert response["confirmed"] is True
    assert response["editedAfterSync"] is False


def test_sync_status_reports_running_job_as_unconfirmed(tmp_path, monkeypatch):
    content = _real_module("api.subtitles.content")

    subtitle = tmp_path / "Movie.hu.srt"
    subtitle.write_text("subtitle", encoding="utf-8")
    modified_at = datetime(2026, 5, 27, 12, 0, 0).timestamp()
    os.utime(subtitle, (modified_at, modified_at))

    monkeypatch.setattr(
        content,
        "resolve_subtitle_path",
        lambda *args, **kwargs: (str(subtitle), {"mediaPath": "/movies/Movie.mkv"}),
    )
    monkeypatch.setattr(
        content,
        "_latest_sync_history_for_path",
        lambda *args, **kwargs: SimpleNamespace(
            timestamp=datetime(2026, 5, 27, 12, 0, 10)
        ),
    )
    monkeypatch.setattr(
        content,
        "jobs_queue",
        SimpleNamespace(
            jobs_pending_queue=[],
            jobs_running_queue=[
                SimpleNamespace(
                    job_id=42,
                    status="running",
                    job_name=f"Syncing {subtitle}",
                    kwargs={"srt_path": str(subtitle)},
                )
            ],
        ),
    )

    response, status_code = content.get_subtitle_sync_status(
        media_type="movie",
        media_id=1203,
        language_code="hu",
    )

    assert status_code == 200
    assert response["jobStatus"] == "running"
    assert response["jobId"] == 42
    assert response["confirmed"] is False
