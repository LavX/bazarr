from guessit import guessit
from subliminal import Episode, Movie
from subliminal_patch import subtitle
from subliminal_patch.video import Video


def test_video_fromguess_episode():
    # subliminal's Episode.fromguess refuses a guess without a title or an
    # episode number, so both keys are part of the minimum a caller must hand
    # it. Without them this never reached an assertion, it raised GuessingError.
    video = Video.fromguess(
        "Breaking.Bad.S01E01.Bluray.mkv",
        {
            "type": "episode",
            "title": "Breaking Bad",
            "season": 1,
            "episode": 1,
            "streaming_service": "foo",
            "random_key": "bar",
        },
    )
    assert video.streaming_service == "foo"
    assert video.other is None
    assert isinstance(video, Episode)


def test_video_fromguess_episode_keeps_other():
    video = Video.fromguess(
        "Breaking.Bad.S01E01.Proper.Bluray.mkv",
        {
            "type": "episode",
            "title": "Breaking Bad",
            "season": 1,
            "episode": 1,
            "other": "Proper",
        },
    )
    assert video.other == "Proper"


def test_video_fromguess_movie():
    # Movie.fromguess needs a title for the same reason.
    video = Video.fromguess(
        "Taxi.Driver.1976.Bluray.mkv",
        {
            "type": "movie",
            "title": "Taxi Driver",
            "edition": "foo",
            "random_key": "bar",
            "other": "Proper",
        },
    )
    assert video.edition == "foo"
    assert video.other == "Proper"
    assert isinstance(video, Movie)


def test_video_fromguess_movie_without_optional_keys():
    video = Video.fromguess(
        "Taxi.Driver.1976.Bluray.mkv",
        {"type": "movie", "title": "Taxi Driver"},
    )
    assert video.edition is None
    assert video.other is None


def test_video_fromname_episode():
    video = Video.fromname("Breaking.Bad.S01E01.NF.WEB-DL.1080p.x264-FOO.mkv")

    assert video.series == "Breaking Bad"
    assert video.title is None
    assert video.season == 1
    assert video.episode == 1
    assert video.source == "Web"
    assert video.streaming_service == "Netflix"
    assert video.resolution == "1080p"
    assert video.video_codec == "H.264"
    assert video.release_group == "FOO"


def test_video_fromname_movie():
    video = Video.fromname("Some.Flick.2022.UHD.Bluray.Proper.2160p.FLAC.HEVC-FOO.mkv")

    assert video.source == "Ultra HD Blu-ray"
    assert video.title == "Some Flick"
    assert video.year == 2022
    assert video.other == "Proper"
    assert video.resolution == "2160p"
    assert video.video_codec == "H.265"
    assert video.audio_codec == "FLAC"


def test_video_fromname_movie_edition():
    video = Video.fromname(
        "Some.Flick.2022.Directors.Cut.1080p.BluRay.x264-FOO.mkv"
    )

    assert video.edition == "Director's Cut"


# Edition is worth 30 points on a movie, so dropping it did not only lose an
# attribute: guess_matches treats "neither side names an edition" as a match,
# and with the video side always empty that fired for every edition-less
# subtitle. These pin the scoring input rather than the attribute alone.
def test_edition_less_subtitle_does_not_match_edition_tagged_movie():
    video = Video.fromname(
        "Some.Flick.2022.Directors.Cut.1080p.BluRay.x264-FOO.mkv"
    )
    matches = subtitle.guess_matches(
        video, guessit("Some.Flick.2022.1080p.BluRay.x264-FOO.mkv")
    )

    assert "edition" not in matches


def test_same_edition_subtitle_still_matches_edition_tagged_movie():
    video = Video.fromname(
        "Some.Flick.2022.Directors.Cut.1080p.BluRay.x264-FOO.mkv"
    )
    matches = subtitle.guess_matches(
        video, guessit("Some.Flick.2022.Directors.Cut.1080p.BluRay.x264-BAR.mkv")
    )

    assert "edition" in matches


def test_edition_less_movie_is_unaffected():
    video = Video.fromname("Some.Flick.2022.1080p.BluRay.x264-FOO.mkv")
    matches = subtitle.guess_matches(
        video, guessit("Some.Flick.2022.1080p.BluRay.x264-BAR.mkv")
    )

    assert video.edition is None
    assert "edition" in matches
