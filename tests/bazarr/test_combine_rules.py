# coding=utf-8


from subtitles.tools.combine.rules import (
    SourcePaths,
    resolve_source_paths,
)


def make_video_dir(tmp_path, files):
    """Create empty subtitle files in tmp_path so resolve can find them."""
    for name in files:
        (tmp_path / name).write_text("")
    return tmp_path


def test_finds_sources_in_relative_subfolder(tmp_path, monkeypatch):
    # Video alongside the .mkv, but the SRTs live in a configured subfolder.
    (tmp_path / "Movie.mkv").write_text("")
    subs = tmp_path / "Subs"
    subs.mkdir()
    (subs / "Movie.en.srt").write_text("")
    (subs / "Movie.hu.srt").write_text("")
    monkeypatch.setattr(
        "subtitles.tools.combine.naming.external_subtitles_search_dirs",
        lambda video_path: [str(tmp_path), str(subs)],
    )
    result = resolve_source_paths(
        video_path=str(tmp_path / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result is not None
    assert result.primary == str(subs / "Movie.en.srt")
    assert result.secondaries == [str(subs / "Movie.hu.srt")]


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


def test_picks_hi_when_only_hi_exists(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.hi.srt",
        "Movie.hu.hi.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result.primary == str(base / "Movie.en.hi.srt")
    assert result.secondaries == [str(base / "Movie.hu.hi.srt")]


def test_prefers_plain_over_hi_when_both_exist(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.en.hi.srt",
        "Movie.hu.hi.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result.primary == str(base / "Movie.en.srt")
    assert result.secondaries == [str(base / "Movie.hu.hi.srt")]


def test_prefers_hi_over_forced(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.forced.srt",
        "Movie.en.hi.srt",
        "Movie.hu.forced.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result.primary == str(base / "Movie.en.hi.srt")
    assert result.secondaries == [str(base / "Movie.hu.forced.srt")]


def test_skips_chained_modifier_sync_outputs(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.hi.ffsubsync.srt",
        "Movie.hu.hi.srt",
    ])
    # en.hi.ffsubsync is a sync output of an HI subtitle; not a valid source.
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result is None
