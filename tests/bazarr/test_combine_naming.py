# coding=utf-8

import os
import pytest

from subtitles.tools.combine.naming import (
    CombinedFileInfo,
    compose_combined_filename,
    parse_combined_filename,
)


class TestParseCombinedFilename:
    def test_parses_two_language_srt(self):
        info = parse_combined_filename("/movies/Movie (2024)/Movie (2024).en.combined-hu.srt")
        assert info == CombinedFileInfo(primary="en", secondaries=["hu"], format="srt")

    def test_parses_three_language_ass(self):
        info = parse_combined_filename("/movies/Movie/Movie.de.combined-es-zh.ass")
        assert info == CombinedFileInfo(primary="de", secondaries=["es", "zh"], format="ass")

    def test_rejects_regular_subtitle(self):
        assert parse_combined_filename("/movies/Movie/Movie.en.srt") is None

    def test_rejects_hi_subtitle(self):
        assert parse_combined_filename("/movies/Movie/Movie.en.hi.srt") is None

    def test_rejects_forced_subtitle(self):
        assert parse_combined_filename("/movies/Movie/Movie.en.forced.srt") is None

    def test_rejects_uppercase_codes(self):
        assert parse_combined_filename("/movies/Movie/Movie.EN.combined-HU.srt") is None

    def test_rejects_three_letter_codes(self):
        assert parse_combined_filename("/movies/Movie/Movie.eng.combined-hun.srt") is None


class TestComposeCombinedFilename:
    def test_two_language_srt(self, monkeypatch):
        monkeypatch.setattr(
            "subtitles.tools.combine.naming.get_external_subtitles_path",
            lambda file, subtitle: f"/movies/Movie/{subtitle}",
        )
        path = compose_combined_filename(
            video_path="/movies/Movie/Movie.mkv",
            primary="en",
            secondaries=["hu"],
            format="srt",
        )
        assert path == "/movies/Movie/Movie.en.combined-hu.srt"

    def test_three_language_ass(self, monkeypatch):
        monkeypatch.setattr(
            "subtitles.tools.combine.naming.get_external_subtitles_path",
            lambda file, subtitle: f"/movies/Movie/{subtitle}",
        )
        path = compose_combined_filename(
            video_path="/movies/Movie/Movie.mkv",
            primary="de",
            secondaries=["es", "zh"],
            format="ass",
        )
        assert path == "/movies/Movie/Movie.de.combined-es-zh.ass"


class TestRoundTrip:
    def test_roundtrip_two_lang(self, monkeypatch):
        monkeypatch.setattr(
            "subtitles.tools.combine.naming.get_external_subtitles_path",
            lambda file, subtitle: f"/movies/Movie/{subtitle}",
        )
        path = compose_combined_filename("/movies/Movie/Movie.mkv", "en", ["hu"], "srt")
        info = parse_combined_filename(path)
        assert info.primary == "en"
        assert info.secondaries == ["hu"]
        assert info.format == "srt"
