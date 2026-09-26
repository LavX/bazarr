"""Tests for the compat _build_video enrichment path.

The compat endpoint has no real video file - it builds a virtual Video from
whatever the client and local library can provide. Providers score heavily on
fields like source, release_group, resolution, so enriching the Video makes
the difference between 0 and dozens of useful results.
"""
from unittest.mock import patch
import pytest


@pytest.mark.parametrize("media_type,season,episode", [("movie", None, None), ("episode", 0, 3)])
def test_title_only_never_adopts_library_or_filename_context(monkeypatch, media_type, season, episode):
    from compat import service

    def forbidden(*args, **kwargs):
        pytest.fail("title-only search touched library metadata, media or filename parsing")

    monkeypatch.setattr(service, "_lookup_library_metadata", forbidden)
    monkeypatch.setattr(service, "_parse_video_from_library", forbidden)
    monkeypatch.setattr(service, "_guessit_filename", forbidden)
    video = service._build_video(
        "tt0133093", season, episode, media_type,
        query="1917", title_only=True, year=2019,
        moviehash="1234567890abcdef", moviebytesize=500,
    )
    assert video.name == ""
    assert (video.title if media_type == "movie" else video.series) == "1917"
    assert video.year == 2019
    assert video.hashes == {}
    assert video.source is None and video.release_group is None
    assert video.size is None
    if media_type == "episode":
        assert (video.season, video.episode, video.series_imdb_id) == (0, 3, "tt0133093")


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
    """Client-supplied hashes enable exact-hash matching on OS-hash providers."""
    from compat.service import _build_video
    v = _build_video("tt0111161", None, None, "movie",
                     moviehash="8e245d9679d31e12")
    assert v.hashes.get("bsplayer") == "8e245d9679d31e12"
    assert v.hashes.get("opensubtitles") == "8e245d9679d31e12"
    assert v.hashes.get("opensubtitlescom") == "8e245d9679d31e12"
    # NapiProjekt uses a different hash algorithm, so it must NOT receive the
    # OpenSubtitles moviehash (its get_subhash would raise on the 16-char value).
    assert "napiprojekt" not in v.hashes


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


@pytest.mark.parametrize("query,kind,title,year,season,episode", [
    ("Example.Movie.2024.1080p.WEB-DL", "movie", "Example Movie", 2024, None, None),
    ("Example.Show.S02E03.1080p", "episode", "Example Show", None, 2, 3),
    ("Example.Show.0x3.1080p", "episode", "Example Show", None, 0, 3),
    ("/tmp/existing.mkv", "movie", "existing", None, None, None),
])
def test_release_query_policy_uses_only_inferred_filename_hints(monkeypatch, query, kind, title, year, season, episode):
    from compat import service
    from subliminal.video import Episode

    def forbidden(*args, **kwargs):
        pytest.fail("Raw construction used library, media or refiners")

    for name in ("_lookup_library_metadata", "_parse_video_from_library", "_refine_from_imdb"):
        monkeypatch.setattr(service, name, forbidden)
    video = service._build_video(None, None, None, "movie", query=query, release_query=True)
    assert video.name == query
    assert isinstance(video, Episode) is (kind == "episode")
    assert (video.series if kind == "episode" else video.title) == title
    assert video.imdb_id is None and video.year == year
    assert video.size is None and video.hashes == {}
    assert video.source is None and video.resolution is None
    if kind == "episode":
        assert (video.season, video.episode, video.series_imdb_id) == (season, episode, None)


@pytest.mark.parametrize("query", [
    "Example.Show.S02E03E04", "Example.Show.S01-S02E03", "Example.Show.E03",
    "Example.Show.S02", "Example.Show.103", "Example.Show.2024.03.12", "1080p.WEB-DL",
])
def test_release_query_policy_rejects_ambiguous_or_incomplete_hints(query):
    from compat import service
    with pytest.raises(ValueError, match="release name|episode"):
        service._build_video(None, None, None, "movie", query=query, release_query=True)


@pytest.mark.parametrize("hints", [
    {}, {"type": "episode", "title": "Show", "episode": 3},
    {"type": "episode", "title": "Show", "season": 2, "episode": [3, 4]},
    {"type": "episode", "title": "Show", "season": [1, 2], "episode": 3},
    {"type": "episode", "title": "Show", "season": 1, "episode": 3},
    {"type": "movie", "title": ["One", "Two"]},
    {"type": "movie", "title": "Film", "year": [2023, 2024]},
    {"type": "movie", "title": "Film", "episode": 3},
    {"type": ["movie", "episode"], "title": "Film"},
])
def test_release_query_policy_never_collapses_conflicting_parser_values(monkeypatch, hints):
    from compat import service
    monkeypatch.setattr(service, "_guessit_filename", lambda query: hints)
    with pytest.raises(ValueError):
        service._build_video(None, None, None, "movie", query="Example.Show.S02E03", release_query=True)


def test_release_query_does_not_adopt_an_existing_file(monkeypatch, tmp_path):
    from compat import service
    media = tmp_path / "Example.Movie.2024.mkv"
    media.write_bytes(b"synthetic media fixture")

    def forbidden(*args, **kwargs):
        pytest.fail("Raw query attempted file or library adoption")

    for name in ("_lookup_library_metadata", "_parse_video_from_library", "_refine_from_imdb"):
        monkeypatch.setattr(service, name, forbidden)
    video = service._build_video(None, None, None, "movie", query=str(media), release_query=True)
    assert video.name == str(media) and video.title == "Example Movie"
    assert video.imdb_id is None and video.size is None and video.hashes == {}
    assert media.read_bytes() == b"synthetic media fixture"


@pytest.mark.parametrize("options", [
    {"moviehash": "hash"}, {"moviebytesize": 2}, {"title_only": True},
    {"year": 2024}, {"series_anidb_id": 7}, {"series_anidb_episode_id": 8},
])
def test_release_query_policy_cannot_be_combined_with_file_or_identified_properties(options):
    from compat import service
    with pytest.raises(ValueError):
        service._build_video(None, None, None, "movie", query="Example.Movie.2024", release_query=True, **options)


def test_title_only_preserves_separate_episode_identity_in_worker_payload():
    from compat.service import _build_video
    from provider_hub.protocol import video_to_payload
    identity = {"id": 401, "show_id": 100, "imdb_id": "tt7654321", "tvdb_id": 501,
                "show_tvdb_id": 300, "title": "Home", "air_date": "2026-09-01"}
    video = _build_video("tt1234567", 2, 1, "episode", query="Northern Light", year=2020,
                         title_only=True, episode_identity=identity)
    assert video.series == "Northern Light" and video.title == "Home" and video.year == 2020
    assert video.series_imdb_id == "tt1234567" and video.imdb_id == "tt7654321"
    assert video.series_tvdb_id == 300 and video.tvdb_id == 501
    assert video.series_tmdb_id == 100 and video.tmdb_id == 401
    assert video.absolute_episode is None
    worker = video_to_payload(video)
    assert worker["series_imdb_id"] == "tt1234567" and worker["imdb_id"] == "tt7654321"
    assert worker["tmdb_id"] == 401 and worker["title"] == "Home"
