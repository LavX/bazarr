# coding=utf-8
"""A new external subtitle has to land in the configured subtitle folder.

get_external_subtitles_path() is a lookup: it returns a path only for a file that
already exists. translate and mods used it to build the path of the file they were
about to WRITE, so in the absolute and relative subfolder modes a first-time
translation got None and crashed on save. get_subtitle_destination_path() keeps the
lookup's preference for an existing file and otherwise resolves the configured folder.
"""
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def subtitle_folder(monkeypatch):
    from app import config as app_config
    from subtitles.indexer import utils as indexer_utils

    # Startup modules write the config to disk when first imported. If that first
    # import happens while these values are patched, the patched folder would be
    # persisted into the local test config and break every later run.
    monkeypatch.setattr(app_config, "write_config", lambda: None)

    def _configure(mode, custom=""):
        monkeypatch.setattr(indexer_utils.settings.general, "subfolder", mode)
        monkeypatch.setattr(indexer_utils.settings.general, "subfolder_custom", custom)

    return _configure


def test_current_mode_writes_next_to_the_video(tmp_path, subtitle_folder):
    from subtitles.indexer.utils import get_subtitle_destination_path

    subtitle_folder("current")
    video = tmp_path / "video.mkv"

    assert get_subtitle_destination_path(str(video), "video.hu.srt") == str(tmp_path / "video.hu.srt")


def test_absolute_mode_resolves_and_creates_the_custom_folder(tmp_path, subtitle_folder):
    from subtitles.indexer.utils import get_subtitle_destination_path

    custom = tmp_path / "subs"
    subtitle_folder("absolute", str(custom))
    video = tmp_path / "video.mkv"

    destination = get_subtitle_destination_path(str(video), "video.hu.srt")

    assert destination == str(custom / "video.hu.srt")
    assert custom.is_dir(), "the write target folder has to exist, the way downloads create it"


def test_relative_mode_resolves_under_the_video_folder(tmp_path, subtitle_folder):
    from subtitles.indexer.utils import get_subtitle_destination_path

    subtitle_folder("relative", "Subs")
    video = tmp_path / "video.mkv"

    assert get_subtitle_destination_path(str(video), "video.hu.srt") == str(tmp_path / "Subs" / "video.hu.srt")


def test_an_existing_file_next_to_the_video_keeps_precedence(tmp_path, subtitle_folder):
    from subtitles.indexer.utils import get_subtitle_destination_path

    subtitle_folder("absolute", str(tmp_path / "subs"))
    video = tmp_path / "video.mkv"
    existing = tmp_path / "video.hu.srt"
    existing.write_text("1\n00:00:00,000 --> 00:00:01,000\nSzia\n", encoding="utf-8")

    assert get_subtitle_destination_path(str(video), "video.hu.srt") == str(existing)


def test_translate_passes_a_custom_folder_destination_to_the_translator(tmp_path, monkeypatch, subtitle_folder):
    from subtitles.tools.translate import main as translate_main
    from subzero.language import Language

    custom = tmp_path / "subs"
    subtitle_folder("absolute", str(custom))
    video = tmp_path / "video.mkv"

    monkeypatch.setattr(translate_main, "validate_translation_params", lambda *a, **kw: None)
    monkeypatch.setattr(translate_main, "convert_language_codes",
                        lambda to_lang, forced, hi: (Language("hun"), to_lang))
    monkeypatch.setattr(translate_main, "get_subtitle_path", lambda *a, **kw: str(tmp_path / "video.hu.srt"))
    monkeypatch.setattr(translate_main, "alpha3_from_alpha2", lambda code: "hun")

    seen = {}
    translator = MagicMock()
    translator.translate.return_value = True

    def _create(kind, **kwargs):
        seen.update(kwargs)
        return translator

    monkeypatch.setattr(translate_main.TranslatorFactory, "create_translator", staticmethod(_create))

    with (
        patch("api.subtitles.subtitles.postprocess_subtitles", lambda *a, **kw: None),
        patch("subtitles.tools.combine.main.try_combine_for_video", lambda **kw: None),
    ):
        translate_main.translate_subtitles_file(
            video_path=str(video), source_srt_file=str(tmp_path / "video.en.srt"),
            from_lang="en", to_lang="hu", forced=False, hi=False,
            media_type="movies", sonarr_series_id=None, sonarr_episode_id=None,
            radarr_id=9, metadata={}, job_id="job-1")

    assert seen["dest_srt_file"] == os.path.join(str(custom), "video.hu.srt"), (
        "a first-time translation in absolute subfolder mode must target the custom folder, not None")


@pytest.mark.parametrize("mode", ["absolute", "relative"])
@pytest.mark.parametrize("status", ["partial", "completed"])
@pytest.mark.parametrize("failure", [None, "footer", "replace"])
def test_translation_saves_atomic_results_in_configured_folder(
        tmp_path, monkeypatch, subtitle_folder, mode, status, failure):
    import sys
    from types import SimpleNamespace

    import pysubs2

    from languages import get_languages
    from subtitles.tools.translate import main as translate_main
    from subtitles.tools.translate.services import openrouter_translator as service_module

    monkeypatch.setattr(get_languages, "languages_dict", [
        {"code2": "en", "code3": "eng", "code3b": "eng", "name": "English"},
        {"code2": "hu", "code3": "hun", "code3b": "hun", "name": "Hungarian"},
    ], raising=False)
    video = tmp_path / "video.mkv"
    video.touch()
    source = tmp_path / "video.en.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nOne\n\n"
                      "2\n00:00:03,000 --> 00:00:04,000\nTwo\n", encoding="utf-8")
    original_source = source.read_bytes()
    custom = tmp_path / "Subs"
    destination = custom / "video.hu.srt"
    subtitle_folder(mode, str(custom) if mode == "absolute" else "Subs")
    if failure:
        custom.mkdir()
        destination.write_bytes(b"existing subtitle")

    monkeypatch.setattr(service_module.settings.translator, "translator_info", True)
    monkeypatch.setattr(service_module.settings.translator, "translator_type", "openrouter")
    monkeypatch.setattr(service_module, "get_title", lambda **kwargs: "Example")
    monkeypatch.setattr(service_module, "get_translator_auth_headers", lambda: {})
    monkeypatch.setattr(service_module.OpenRouterTranslatorService, "_get_api_key_value", lambda self: "")
    monkeypatch.setattr(service_module.requests, "post", lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: {"jobId": "fixture-job"}))
    lines = [{"position": 0, "line": "Egy"}]
    if status == "completed":
        lines.append({"position": 1, "line": "Kettő"})
    monkeypatch.setattr(service_module.requests, "get", lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: {"status": status, "result": {"lines": lines},
                                      "error": "Some batches failed" if failure else None}))
    for name in ("show_progress", "hide_progress", "show_message"):
        monkeypatch.setattr(service_module, name, lambda *args, **kwargs: None)
    history = []
    monkeypatch.setattr(service_module, "history_log_movie", lambda **kwargs: history.append(kwargs))
    monkeypatch.setattr(service_module.jobs_queue, "update_job_progress", lambda **kwargs: None)
    names = []
    monkeypatch.setattr(translate_main.jobs_queue, "get_job_name", lambda job_id: "Translating Example")
    monkeypatch.setattr(translate_main.jobs_queue, "update_job_name",
                        lambda **kwargs: names.append(kwargs["new_job_name"]))
    monkeypatch.setattr(translate_main.TranslatorFactory, "create_translator",
                        staticmethod(lambda kind, **kwargs: service_module.OpenRouterTranslatorService(**kwargs)))
    postprocessed = []

    def fail_write(*args, **kwargs):
        raise OSError("synthetic write failure")

    if failure == "footer":
        monkeypatch.setattr(service_module, "add_translator_info", fail_write)
    elif failure == "replace":
        monkeypatch.setattr(service_module.os, "replace", fail_write)

    monkeypatch.setitem(sys.modules, "api.subtitles.subtitles", SimpleNamespace(
        postprocess_subtitles=lambda *args, **kwargs: postprocessed.append(args[0])))
    monkeypatch.setitem(sys.modules, "subtitles.tools.combine.main", SimpleNamespace(
        try_combine_for_video=lambda **kwargs: None))
    kwargs = dict(video_path=str(video), source_srt_file=str(source), from_lang="en", to_lang="hu",
                  forced=False, hi=False, media_type="movies", sonarr_series_id=None,
                  sonarr_episode_id=None, radarr_id=9, metadata={}, job_id="fixture-job", arr_instance_id=3)
    if failure:
        with pytest.raises(RuntimeError, match="failed translation result"):
            translate_main.translate_subtitles_file(**kwargs)
    else:
        assert translate_main.translate_subtitles_file(**kwargs) == str(destination)

    assert source.read_bytes() == original_source
    assert not list(custom.glob(".bazarr-translate-*"))
    if failure:
        assert destination.read_bytes() == b"existing subtitle"
        assert not history
        assert not postprocessed
        assert names[-1].startswith("Failed")
    else:
        output = pysubs2.load(destination)
        assert [cue.plaintext for cue in output[:2]] == (["Egy", "Two"] if status == "partial" else ["Egy", "Kettő"])
        assert [(cue.start, cue.end) for cue in output[:2]] == [(1000, 2000), (3000, 4000)]
        assert ("partially translated" in output[-1].plaintext.lower()) == (status == "partial")
        assert ("partial" in history[0]["result"].message.lower()) == (status == "partial")
        assert names[-1].startswith("Partially translated" if status == "partial" else "Translated")
        assert postprocessed == [str(destination)]
