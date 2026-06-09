"""Regression tests for get_video scene-name handling (issue #198).

Some indexers store a URL-encoded release title as the scene name
(e.g. ``Rick%20and%20Morty%20S09E02%20...``). guessit can't parse that and
subliminal raises ``GuessingError``. Before the fix, that exception was caught
by the single outer ``except`` in ``get_video`` and the whole function returned
``None`` - discarding the perfectly good Video already parsed from the on-disk
filename and aborting subtitle search entirely.

The desired behaviour:
  * an unparsable scene name must never discard the on-disk parse; fall back to
    the on-disk Video instead of returning None.
  * a merely URL-encoded scene name should be URL-decoded and retried so we
    still harvest its hints (release group, codec, ...).
"""
from unittest.mock import patch

from subliminal.video import Episode

try:
    from subliminal.exceptions import GuessingError
except ImportError:  # older subliminal
    GuessingError = ValueError


PATH = "/tv/Rick and Morty (2013)/Season 09/Rick and Morty (2013) - S09E02 - Ricks Days Seven Nights [HDTV-1080p][AC3 5.1][x265]-ELiTE.mkv"
# Scene name as stored by the indexer, without extension (get_video appends the
# on-disk file's extension itself).
ENCODED_SCENE = "Rick%20and%20Morty%20S09E02%201080p%20x265-ELiTE%20%5B%20UIndex.org%20%5D"


def _on_disk_episode():
    ep = Episode(PATH, "Rick and Morty", season=9, episodes=[2])
    ep.release_group = None
    ep.video_codec = None
    return ep


def _scene_episode():
    ep = Episode("scene", "Rick and Morty", season=9, episodes=[2])
    ep.release_group = "ELiTE"
    ep.video_codec = "H.265"
    return ep


def test_unparsable_scene_name_falls_back_to_on_disk_video():
    """A scene name guessit can't parse must not nuke the on-disk parse."""
    import subtitles.utils as u

    on_disk = _on_disk_episode()

    def fake_parse_video(name, hints=None, skip_hashing=False, dry_run=False, providers=None):
        if not dry_run:
            return on_disk          # the real on-disk file parses fine
        raise GuessingError(f"Insufficient data to process the guess for {name!r}")

    with patch.object(u, "parse_video", side_effect=fake_parse_video), \
            patch.object(u, "registered_refiners", {}):
        result = u.get_video(PATH, "Rick and Morty", "garbage_scene_name_no_episode",
                             providers=["dummy"], media_type="series")

    assert result is on_disk, "get_video should return the on-disk Video, not None"


def test_url_encoded_scene_name_is_decoded_and_refines_video():
    """A URL-encoded scene name should be decoded, retried, and used to refine."""
    import subtitles.utils as u

    on_disk = _on_disk_episode()
    scene = _scene_episode()
    decoded_names = []

    def fake_parse_video(name, hints=None, skip_hashing=False, dry_run=False, providers=None):
        if not dry_run:
            return on_disk
        if "%" in name:                      # encoded form: guessit gives up
            raise GuessingError(f"Insufficient data to process the guess for {name!r}")
        decoded_names.append(name)           # decoded form: parses fine
        return scene

    with patch.object(u, "parse_video", side_effect=fake_parse_video), \
            patch.object(u, "registered_refiners", {}):
        result = u.get_video(PATH, "Rick and Morty", ENCODED_SCENE,
                             providers=["dummy"], media_type="series")

    assert result is on_disk
    # release_group was empty on the on-disk parse and should be filled from the
    # decoded scene name.
    assert result.release_group == "ELiTE"
    assert decoded_names, "scene name should have been retried URL-decoded"
    assert all("%" not in n for n in decoded_names)


def test_real_guessit_recovers_url_encoded_scene_name():
    """End to end with the real guessit/subliminal stack: the encoded name fails
    raw but the helper recovers it once URL-decoded (no mocking of parse_video)."""
    import subtitles.utils as u

    hints = {"title": "Rick and Morty", "type": "episode"}
    video = u._parse_scenename_video(ENCODED_SCENE, ".mkv", hints)

    assert video is not None, "URL-decoding should let guessit parse the scene name"
    assert video.series == "Rick and Morty"
    assert video.episodes == [2]
    assert video.release_group == "ELiTE"
