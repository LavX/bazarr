# coding=utf-8

import os

from subtitles.tools.combine.rules import (
    SourcePaths,
    resolve_source_paths,
)


def make_video_dir(tmp_path, files):
    """Create empty subtitle files in tmp_path so resolve can find them."""
    for name in files:
        (tmp_path / name).write_text("")
    return tmp_path


def test_all_sources_present(tmp_path):
    base = make_video_dir(tmp_path, ["Movie.en.srt", "Movie.hu.srt", "Movie.mkv"])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert isinstance(result, SourcePaths)
    assert result.primary == str(base / "Movie.en.srt")
    assert result.secondaries == [str(base / "Movie.hu.srt")]


def test_missing_source_returns_none(tmp_path):
    base = make_video_dir(tmp_path, ["Movie.en.srt", "Movie.mkv"])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result is None


def test_skips_combined_and_modifier_files(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.hu.srt",
        "Movie.en.combined-hu.srt",
        "Movie.en.hi.srt",
        "Movie.en.forced.srt",
        # Sync-engine outputs from PR 158 must not be picked as combine sources.
        "Movie.en.ffsubsync.srt",
        "Movie.en.autosubsync.srt",
        "Movie.en.alass.srt",
        "Movie.hu.ffsubsync.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result.primary == str(base / "Movie.en.srt")
    assert result.secondaries == [str(base / "Movie.hu.srt")]


def test_three_languages(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv", "Movie.de.srt", "Movie.es.srt", "Movie.zh.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["de", "es", "zh"],
    )
    assert result.primary == str(base / "Movie.de.srt")
    assert result.secondaries == [
        str(base / "Movie.es.srt"),
        str(base / "Movie.zh.srt"),
    ]
