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
