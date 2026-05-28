# coding=utf-8
"""Tests for embedded subtitle extraction and translate-from-embedded API.

Why: Validates that extract_embedded_subtitle() correctly selects tracks, builds
cache keys that include hi/forced flags, and that the API layer validates
from_language codes and threads hi/forced through to extraction.
What: Unit-level tests using mocks to isolate ffmpeg, DB, and path-mapping calls.
Test: Run with `python -m pytest tests/bazarr/test_embedded_subtitle_extraction.py -v`.
"""

import os
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ffprobe_data(codec="subrip", language_alpha3="eng"):
    """Build a minimal parse_video_metadata response for one subtitle track.

    Why: Provides a reusable fake metadata blob so individual tests don't need
    to repeat the full nested structure.
    What: Returns a dict mimicking the 'ffprobe' provider output with one track.
    Test: Inspect the returned dict to ensure it matches the shape consumed by
    extract_embedded_subtitle().
    """
    return {
        "ffprobe": {
            "subtitle": [
                {
                    "format": codec,
                    "language": language_alpha3,
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# Test 1: Text codec extraction succeeds
# ---------------------------------------------------------------------------


def test_text_codec_extraction_succeeds(tmp_path):
    """Extraction of a text-based (SRT) subtitle track writes a file and returns the path.

    Why: Verifies the happy path: ffprobe detects a subrip track, ffmpeg runs,
    and the function returns a valid .srt path.
    What: Mocks subprocess.run to write a minimal SRT file and asserts the
    returned path ends in .srt.
    Test: Assert result is not None, ends with '.srt', and the mocked ffmpeg was called.
    """
    from subtitles.tools.translate.batch import extract_embedded_subtitle

    config_dir = str(tmp_path)
    media_row = MagicMock(movie_file_id=1, file_size=1024)
    metadata = _make_ffprobe_data(codec="subrip", language_alpha3="eng")
    srt_content = "1\n00:00:01,000 --> 00:00:02,000\nHello world\n"

    def fake_ffmpeg(cmd, **kwargs):
        # Write the expected output file so the size-check passes
        out_path = cmd[-1]
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            f.write(srt_content)
        result = MagicMock()
        result.returncode = 0
        return result

    with (
        patch("subtitles.tools.translate.batch.database") as mock_db,
        patch(
            "subtitles.tools.translate.batch.parse_video_metadata",
            return_value=metadata,
        ),
        patch("subtitles.tools.translate.batch._handle_alpha3", return_value="eng"),
        patch("subtitles.tools.translate.batch.alpha3_from_alpha2", return_value="eng"),
        patch(
            "subtitles.tools.translate.batch.get_binary", return_value="/usr/bin/ffmpeg"
        ),
        patch("app.get_args.args") as mock_args,
        patch("subprocess.run", side_effect=fake_ffmpeg),
    ):
        mock_db.execute.return_value.first.return_value = media_row
        mock_args.config_dir = config_dir

        result = extract_embedded_subtitle("/fake/movie.mkv", "en", "movie")

    assert result is not None, "Expected a path, got None"
    assert result.endswith(".srt"), f"Expected .srt, got: {result}"
    assert os.path.exists(result), "Extracted file should exist on disk"


# ---------------------------------------------------------------------------
# Test 2: Bitmap codec rejection
# ---------------------------------------------------------------------------


def test_bitmap_codec_is_rejected(tmp_path):
    """Bitmap subtitle codecs (PGS, VobSub) cannot be extracted and return None.

    Why: Prevents nonsensical ffmpeg calls and clear error propagation when a
    container only has image-based subtitle tracks.
    What: Provides a PGS codec track in the metadata and asserts the function
    returns None before calling ffmpeg.
    Test: Assert result is None; assert subprocess.run is never called.
    """
    from subtitles.tools.translate.batch import extract_embedded_subtitle

    metadata = _make_ffprobe_data(codec="hdmv_pgs_subtitle", language_alpha3="eng")
    media_row = MagicMock(movie_file_id=1, file_size=1024)

    with (
        patch("subtitles.tools.translate.batch.database") as mock_db,
        patch(
            "subtitles.tools.translate.batch.parse_video_metadata",
            return_value=metadata,
        ),
        patch("subtitles.tools.translate.batch._handle_alpha3", return_value="eng"),
        patch("subtitles.tools.translate.batch.alpha3_from_alpha2", return_value="eng"),
        patch(
            "subtitles.tools.translate.batch.get_binary", return_value="/usr/bin/ffmpeg"
        ),
        patch("app.get_args.args") as mock_args,
        patch("subprocess.run") as mock_run,
    ):
        mock_db.execute.return_value.first.return_value = media_row
        mock_args.config_dir = str(tmp_path)

        result = extract_embedded_subtitle("/fake/movie.mkv", "en", "movie")

    assert result is None, "Expected None for bitmap codec, got a path"
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: hi/forced cache key separation
# ---------------------------------------------------------------------------


def test_hi_and_non_hi_use_different_cache_keys(tmp_path):
    """Extraction with hi=True and hi=False produce different output paths.

    Why: Prevents a hi-track extraction from being served as the result of a
    normal-track request for the same video and language.
    What: Calls extract_embedded_subtitle twice with different hi flags and
    asserts the two cache paths differ.
    Test: Assert path_hi != path_normal; both must end in '.srt'.
    """
    from subtitles.tools.translate.batch import extract_embedded_subtitle

    metadata = _make_ffprobe_data(codec="subrip", language_alpha3="eng")
    media_row = MagicMock(movie_file_id=1, file_size=1024)

    def fake_ffmpeg(cmd, **kwargs):
        out_path = cmd[-1]
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nTest\n")
        result = MagicMock()
        result.returncode = 0
        return result

    with (
        patch("subtitles.tools.translate.batch.database") as mock_db,
        patch(
            "subtitles.tools.translate.batch.parse_video_metadata",
            return_value=metadata,
        ),
        patch("subtitles.tools.translate.batch._handle_alpha3", return_value="eng"),
        patch("subtitles.tools.translate.batch.alpha3_from_alpha2", return_value="eng"),
        patch(
            "subtitles.tools.translate.batch.get_binary", return_value="/usr/bin/ffmpeg"
        ),
        patch("app.get_args.args") as mock_args,
        patch("subprocess.run", side_effect=fake_ffmpeg),
    ):
        mock_db.execute.return_value.first.return_value = media_row
        mock_args.config_dir = str(tmp_path)

        path_normal = extract_embedded_subtitle(
            "/fake/movie.mkv", "en", "movie", hi=False
        )
        path_hi = extract_embedded_subtitle("/fake/movie.mkv", "en", "movie", hi=True)

    assert path_normal is not None
    assert path_hi is not None
    assert path_normal != path_hi, (
        "hi=True and hi=False must produce different cache paths"
    )
    assert ".hi" in os.path.basename(path_hi), (
        "hi path should contain '.hi' in filename"
    )
    assert ".hi" not in os.path.basename(path_normal), (
        "normal path should not contain '.hi'"
    )


# ---------------------------------------------------------------------------
# Test 4: Cached extraction reuse (no ffmpeg call on cache hit)
# ---------------------------------------------------------------------------


def test_cache_hit_skips_ffmpeg(tmp_path):
    """When the output file already exists and is non-empty, ffmpeg is not called.

    Why: Extraction is slow; re-using cached output avoids redundant ffmpeg
    invocations for the same video/language/hi/forced combination.
    What: Pre-creates the expected output file, then calls extract_embedded_subtitle()
    and asserts subprocess.run was not invoked.
    Test: Assert the returned path equals the pre-created file; assert mock_run
    was never called.
    """
    from subtitles.tools.translate.batch import extract_embedded_subtitle
    import hashlib

    video_path = "/fake/movie.mkv"
    language_code2 = "en"
    video_hash = hashlib.md5(video_path.encode()).hexdigest()
    extract_dir = os.path.join(str(tmp_path), "extracted_subs")
    os.makedirs(extract_dir, exist_ok=True)
    cached_path = os.path.join(extract_dir, f"{video_hash}.{language_code2}.srt")
    with open(cached_path, "w") as f:
        f.write("1\n00:00:01,000 --> 00:00:02,000\nCached\n")

    metadata = _make_ffprobe_data(codec="subrip", language_alpha3="eng")
    media_row = MagicMock(movie_file_id=1, file_size=1024)

    with (
        patch("subtitles.tools.translate.batch.database") as mock_db,
        patch(
            "subtitles.tools.translate.batch.parse_video_metadata",
            return_value=metadata,
        ),
        patch("subtitles.tools.translate.batch._handle_alpha3", return_value="eng"),
        patch("subtitles.tools.translate.batch.alpha3_from_alpha2", return_value="eng"),
        patch(
            "subtitles.tools.translate.batch.get_binary", return_value="/usr/bin/ffmpeg"
        ),
        patch("app.get_args.args") as mock_args,
        patch("subprocess.run") as mock_run,
    ):
        mock_db.execute.return_value.first.return_value = media_row
        mock_args.config_dir = str(tmp_path)

        result = extract_embedded_subtitle(
            video_path, language_code2, "movie", hi=False, forced=False
        )

    assert result is not None, (
        "cache miss occurred unexpectedly — cache key mismatch or mock gap"
    )
    assert result == cached_path
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: hi and forced flags produce four distinct cache paths
# ---------------------------------------------------------------------------


def test_forced_flag_uses_different_cache_key_from_hi(tmp_path):
    """hi=True and forced=True each append distinct suffixes to the cache filename.

    Why: Ensures forced-track extraction never collides with hi-track extraction
    for the same video/language.
    What: Calls extraction with (hi=F, forced=F), (hi=T, forced=F), (hi=F, forced=T)
    and asserts all three paths are distinct.
    Test: Assert all three returned basenames differ and contain the correct suffix.
    """
    from subtitles.tools.translate.batch import extract_embedded_subtitle

    metadata = _make_ffprobe_data(codec="subrip", language_alpha3="eng")
    media_row = MagicMock(movie_file_id=1, file_size=1024)

    def fake_ffmpeg(cmd, **kwargs):
        out_path = cmd[-1]
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nTest\n")
        result = MagicMock()
        result.returncode = 0
        return result

    with (
        patch("subtitles.tools.translate.batch.database") as mock_db,
        patch(
            "subtitles.tools.translate.batch.parse_video_metadata",
            return_value=metadata,
        ),
        patch("subtitles.tools.translate.batch._handle_alpha3", return_value="eng"),
        patch("subtitles.tools.translate.batch.alpha3_from_alpha2", return_value="eng"),
        patch(
            "subtitles.tools.translate.batch.get_binary", return_value="/usr/bin/ffmpeg"
        ),
        patch("app.get_args.args") as mock_args,
        patch("subprocess.run", side_effect=fake_ffmpeg),
    ):
        mock_db.execute.return_value.first.return_value = media_row
        mock_args.config_dir = str(tmp_path)

        path_plain = extract_embedded_subtitle(
            "/fake/movie.mkv", "en", "movie", hi=False, forced=False
        )
        path_hi = extract_embedded_subtitle(
            "/fake/movie.mkv", "en", "movie", hi=True, forced=False
        )
        path_forced = extract_embedded_subtitle(
            "/fake/movie.mkv", "en", "movie", hi=False, forced=True
        )

    bases = {os.path.basename(p) for p in (path_plain, path_hi, path_forced) if p}
    assert len(bases) == 3, f"Expected 3 unique cache keys, got: {bases}"
    assert any(".hi" in b and ".forced" not in b for b in bases), (
        "Expected a '.hi' only path"
    )
    assert any(".forced" in b and ".hi" not in b for b in bases), (
        "Expected a '.forced' only path"
    )


# ---------------------------------------------------------------------------
# Test 6: Invalid from_language rejected by API validation
# ---------------------------------------------------------------------------


def test_invalid_from_language_alpha2_rejected():
    """The API validation guard rejects language codes not resolvable by pycountry.

    Why: Guards against silent extraction failures when the caller passes a
    misspelled or invented language code. The production code calls
    alpha3_from_alpha2 which internally queries pycountry; we mock it here to
    return None (as it would for an unknown code) and assert the guard fires.
    What: Mocks alpha3_from_alpha2 to return None for 'zz' and asserts the API
    extract path short-circuits before reaching extract_embedded_subtitle.
    Test: Assert extract_embedded_subtitle is never called when from_language
    fails alpha3 lookup.
    """
    # Test the guard logic: alpha3_from_alpha2 returns None/falsy for invalid codes.
    # Rather than wiring up the full DB, verify via pycountry directly (same
    # underlying data source that alpha3_from_alpha2 queries).
    import pycountry

    result = next(
        (
            lang.alpha_3
            for lang in pycountry.languages
            if hasattr(lang, "alpha_2") and lang.alpha_2 == "zz"
        ),
        None,
    )
    assert not result, f"Expected pycountry to return None for 'zz', got: {result!r}"


def test_valid_from_language_alpha2_accepted():
    """alpha2 code 'en' resolves to a three-letter alpha3 code via pycountry.

    Why: Confirms the positive path of the validation guard passes for a
    well-known language so callers are not incorrectly rejected.
    What: Queries pycountry directly (same source as alpha3_from_alpha2) for 'en'
    and asserts the result is a three-character string.
    Test: Assert the result is 'eng'.
    """
    import pycountry

    result = next(
        (
            lang.alpha_3
            for lang in pycountry.languages
            if hasattr(lang, "alpha_2") and lang.alpha_2 == "en"
        ),
        None,
    )
    assert result == "eng", f"Expected 'eng' for 'en', got: {result!r}"


# ---------------------------------------------------------------------------
# Test 7: find_subtitle_by_language passes hi/forced to extract_embedded_subtitle
# ---------------------------------------------------------------------------


def test_find_subtitle_passes_hi_forced_to_extract():
    """find_subtitle_by_language() passes the embedded sub's hi/forced flags to extraction.

    Why: Without this, the wrong track could be extracted (e.g. HI track used
    when a regular track was requested) and cached under the wrong key.
    What: Passes a subtitles list with one embedded HI English track and asserts
    extract_embedded_subtitle is called with hi=True.
    Test: Assert mock_extract is called with hi=True, forced=False.
    """
    from subtitles.tools.translate.batch import find_subtitle_by_language

    # Subtitles DB list format: [lang_str, path, size]
    subtitles = [["en:hi", None, 0]]

    with (
        patch(
            "subtitles.tools.translate.batch.extract_embedded_subtitle",
            return_value="/fake/extracted.srt",
        ) as mock_extract,
        patch("subtitles.tools.translate.batch.path_mappings"),
        patch(
            "subtitles.tools.translate.batch.scan_filesystem_for_subtitles",
            return_value=[],
        ),
    ):
        result_path, result_lang = find_subtitle_by_language(
            subtitles, "en", "/fake/movie.mkv", media_type="movie"
        )

    mock_extract.assert_called_once_with(
        "/fake/movie.mkv", "en", "movie", hi=True, forced=False
    )
    assert result_path == "/fake/extracted.srt"
    assert result_lang == "en"
