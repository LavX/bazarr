"""Tests for the compat _build_video enrichment path.

The compat endpoint has no real video file - it builds a virtual Video from
whatever the client and local library can provide. Providers score heavily on
fields like source, release_group, resolution, so enriching the Video makes
the difference between 0 and dozens of useful results.
"""
from unittest.mock import patch
import pytest


@pytest.fixture(autouse=True)
def _no_library(monkeypatch):
    """Force the library metadata lookup to return empty by default; tests
    that care about it patch the function explicitly."""
    from compat import service
    monkeypatch.setattr(service, "_lookup_library_metadata",
                        lambda imdb_id, media_type, season=None, episode=None: {})


def test_movie_without_query_is_bare_but_has_imdb_id():
    from compat.service import _build_video
    from subliminal.video import Movie
    v = _build_video("tt0111161", None, None, "movie")
    assert isinstance(v, Movie)
    assert v.imdb_id == "tt0111161"
    assert v.source is None
    assert v.release_group is None


def test_movie_with_filename_extracts_release_metadata():
    """guessit should populate source/release_group/resolution/codec."""
    from compat.service import _build_video
    v = _build_video(
        "tt0111161", None, None, "movie",
        query="The.Shawshank.Redemption.1994.1080p.BluRay.x264-RARBG.mkv",
    )
    assert v.resolution == "1080p"
    assert v.source in ("Blu-ray", "BluRay")
    assert v.release_group == "RARBG"
    assert v.video_codec in ("H.264", "h264", "x264")
    # guessit should recover the year even when the library lookup is empty
    assert v.year == 1994


def test_movie_uses_library_title_when_available():
    from compat import service
    from compat.service import _build_video
    with patch.object(service, "_lookup_library_metadata",
                       return_value={"title": "The Shawshank Redemption", "year": "1994"}):
        v = _build_video("tt0111161", None, None, "movie")
    assert v.title == "The Shawshank Redemption"
    assert v.year == 1994


def test_episode_sets_series_imdb_and_season_episode():
    from compat.service import _build_video
    from subliminal.video import Episode
    v = _build_video("tt0903747", 1, 2, "episode",
                     query="Breaking.Bad.S01E02.720p.HDTV.x264-GROUP.mkv")
    assert isinstance(v, Episode)
    assert v.series_imdb_id == "tt0903747"
    assert v.season == 1 and v.episode == 2
    assert v.resolution == "720p"
    assert v.release_group == "GROUP"


def test_moviehash_is_wired_for_opensubtitles_providers():
    """Client-supplied hashes enable exact-hash matching on hash providers."""
    from compat.service import _build_video
    v = _build_video("tt0111161", None, None, "movie",
                     moviehash="8e245d9679d31e12")
    assert v.hashes.get("bsplayer") == "8e245d9679d31e12"
    assert v.hashes.get("napiprojekt") == "8e245d9679d31e12"
    assert v.hashes.get("opensubtitles") == "8e245d9679d31e12"
    assert v.hashes.get("opensubtitlescom") == "8e245d9679d31e12"


def test_moviebytesize_is_wired_for_hash_size_providers():
    """BSPlayer needs both the OS hash and byte size on virtual searches."""
    from compat.service import _build_video
    v = _build_video(
        "tt0111161", None, None, "movie",
        moviehash="8e245d9679d31e12",
        moviebytesize=123456789,
    )
    assert v.size == 123456789
    assert v.hashes.get("bsplayer") == "8e245d9679d31e12"


def test_anidb_ids_are_wired_for_anime_providers():
    """AnimeTosho needs AniDB ids on the worker video payload."""
    from compat.service import _build_video
    v = _build_video(
        "tt21209876", 1, 12, "episode",
        query="Solo.Leveling.S01E12.2160p.WEB-ToonsHub.mkv",
        series_anidb_id=17495,
        series_anidb_episode_id=277518,
    )
    assert v.series_anidb_id == 17495
    assert v.series_anidb_series_id == 17495
    assert v.series_anidb_episode_id == 277518


def test_imdb_id_normalized_to_tt_prefix():
    """OS-compat clients (Jellyfin plugin) strip 'tt' before sending.
    OMDB / TVDB v1 / v4 all reject the bare numeric form, so we normalize
    at Video construction and carry the tt-prefixed value downstream."""
    from compat.service import _tt, _build_video
    assert _tt("9198004") == "tt9198004"
    assert _tt("tt9198004") == "tt9198004"
    assert _tt("TT9198004") == "tt9198004"
    assert _tt(9198004) == "tt9198004"
    assert _tt(None) == ""
    assert _tt("") == ""
    assert _tt("notanid") == ""
    # Video inherits the normalized form
    v = _build_video("9198004", 1, 1, "episode",
                     query="For.All.Mankind.S01E01.mkv")
    assert v.series_imdb_id == "tt9198004"


def test_library_title_wins_over_guessit_title_but_guessit_fills_gaps():
    """When both sources have info, library title wins (curated); guessit
    provides the release-quality fields library lookup can't supply."""
    from compat import service
    from compat.service import _build_video
    with patch.object(service, "_lookup_library_metadata",
                       return_value={"title": "The Shawshank Redemption", "year": "1994"}):
        v = _build_video(
            "tt0111161", None, None, "movie",
            query="shawshank.1994.2160p.UHD.BluRay.x265-TERMiNAL.mkv",
        )
    assert v.title == "The Shawshank Redemption"  # library beats guessit
    assert v.resolution == "2160p"  # guessit still fills release quality
    assert v.release_group == "TERMiNAL"


def test_library_path_delegates_to_parse_video():
    """When the library has a real path on disk, compat delegates to
    Bazarr's parse_video pipeline (same scoring intelligence as the
    native manual search). Falls back to virtual Video when the file is
    missing or parse_video fails."""
    from unittest.mock import MagicMock  # noqa: F401
    from compat import service
    from compat.service import _build_video
    from subliminal.video import Movie

    fake_video = Movie(name="Shawshank.2160p.BluRay.mkv",
                       title="The Shawshank Redemption", year=1994)
    fake_video.resolution = "2160p"
    fake_video.release_group = "REAL-GROUP"
    fake_video.source = "Blu-ray"
    fake_video.hashes = {"opensubtitles": "deadbeef"}

    with patch.object(service, "_lookup_library_metadata",
                       return_value={"title": "The Shawshank Redemption",
                                     "year": "1994",
                                     "path": "/storage/shawshank.mkv",
                                     "sceneName": "shawshank.2160p.bluray"}), \
         patch("os.path.exists", return_value=True), \
         patch("subtitles.utils.get_video",
                return_value=fake_video) as gv:
        v = _build_video("tt0111161", None, None, "movie")

    gv.assert_called_once()
    assert v is fake_video
    assert v.release_group == "REAL-GROUP"
    assert v.imdb_id == "tt0111161"  # compat attached the id post-parse


def test_library_path_missing_file_falls_back_to_virtual():
    """If the library has a path but the file isn't accessible, build the
    virtual Video rather than erroring."""
    from compat import service
    from compat.service import _build_video
    with patch.object(service, "_lookup_library_metadata",
                       return_value={"title": "Shawshank", "year": "1994",
                                     "path": "/nonexistent/file.mkv"}), \
         patch("os.path.exists", return_value=False):
        v = _build_video("tt0111161", None, None, "movie")
    # Virtual Movie built from library title + imdb
    assert v.title == "Shawshank"
    assert v.imdb_id == "tt0111161"
