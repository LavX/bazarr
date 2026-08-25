# coding=utf-8
"""Release-type mismatch detection.

A user's downloader picks a release without knowing which release type the
subtitle communities actually cut subtitles for. When the only usable subtitle
is timed for another release type, the search comes back empty even though a
perfectly good subtitle exists. These tests pin the detector that spots that
situation from the candidates the search ALREADY listed, and, at least as
firmly, the situations where it must stay silent.
"""

import pytest


VIDEO_WEB = "Web"

BLURAY_RELEASE = "Show.S01E01.1080p.BluRay.x264-GRP"
WEB_RELEASE = "Show.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb"
HDTV_RELEASE = "Show.S01E01.HDTV.x264-LOL"

# 80% of the episode maximum (360), the shipped default minimum score.
MIN_SCORE = 288


def _candidate(provider_name="goodsubs", release_info=BLURAY_RELEASE, score=270,
               downloaded=False):
    return {
        "provider_name": provider_name,
        "release_info": release_info,
        "score": score,
        "downloaded": downloaded,
    }


def test_source_score_is_the_release_type_weight_of_the_scorer():
    from subtitles.mismatch import source_score

    assert source_score("series") == 25
    assert source_score("movie") == 30


def test_alternative_release_type_that_would_clear_the_threshold_is_reported():
    from subtitles.mismatch import detect_release_type_mismatch

    result = detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[
            _candidate(release_info=BLURAY_RELEASE, score=270),
            _candidate(provider_name="othersubs", release_info=WEB_RELEASE, score=260),
        ],
        min_score=MIN_SCORE,
        media_type="series",
    )

    assert result is not None
    assert result.video_release_type == "web"
    assert result.subtitle_release_type == "blu-ray"
    assert result.provider_name == "goodsubs"
    assert result.score == 270
    assert result.projected_score == 295


def test_the_highest_scoring_alternative_is_the_one_reported():
    from subtitles.mismatch import detect_release_type_mismatch

    result = detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[
            _candidate(provider_name="low", release_info=BLURAY_RELEASE, score=265),
            _candidate(provider_name="high", release_info=HDTV_RELEASE, score=280),
        ],
        min_score=MIN_SCORE,
        media_type="series",
    )

    assert result.provider_name == "high"
    assert result.subtitle_release_type == "hdtv"


def test_multi_line_release_description_agreeing_on_one_type_is_reported():
    from subtitles.mismatch import detect_release_type_mismatch

    result = detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[_candidate(
            release_info=f"{BLURAY_RELEASE}\nShow.S01E01.720p.BluRay.x264-OTHER")],
        min_score=MIN_SCORE,
        media_type="series",
    )

    assert result.subtitle_release_type == "blu-ray"


# --------------------------------------------------------------------------
# Negative cases. A detector that fires on every slightly-off release becomes
# noise the user turns off, at which point it protects nothing. Each of these
# must stay silent.
# --------------------------------------------------------------------------

def test_no_mismatch_when_a_candidate_of_the_video_release_type_cleared_the_threshold():
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[
            _candidate(release_info=BLURAY_RELEASE, score=270),
            _candidate(provider_name="othersubs", release_info=WEB_RELEASE, score=290),
        ],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_no_mismatch_when_a_subtitle_was_downloaded():
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[
            _candidate(release_info=BLURAY_RELEASE, score=270),
            _candidate(provider_name="othersubs", release_info=WEB_RELEASE,
                       score=300, downloaded=True),
        ],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_no_mismatch_when_the_alternative_stays_below_the_threshold():
    from subtitles.mismatch import detect_release_type_mismatch

    # 200 + 25 release-type points is still short of 288: this subtitle is not
    # a release-type victim, it simply does not match the episode.
    assert detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[_candidate(release_info=BLURAY_RELEASE, score=200)],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_unparseable_release_description_never_triggers_a_mismatch():
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[_candidate(release_info="uploaded by someone", score=280),
                    _candidate(release_info="", score=280),
                    _candidate(release_info=None, score=280)],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_synthetic_release_description_of_a_generated_provider_never_triggers():
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[_candidate(provider_name="whisperai",
                               release_info="transcribe English audio -> English SRT",
                               score=280)],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_generated_provider_is_excluded_even_with_a_parseable_release_description():
    from subtitles.mismatch import detect_release_type_mismatch

    # A generated subtitle is not evidence that a Blu-ray subtitle exists, no
    # matter what its release description happens to say.
    assert detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[_candidate(provider_name="whisperai",
                               release_info=BLURAY_RELEASE, score=280)],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_ambiguous_multi_line_release_description_never_triggers():
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[_candidate(
            release_info=f"{BLURAY_RELEASE}\n{HDTV_RELEASE}", score=280)],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_no_mismatch_when_the_video_release_type_is_unknown():
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type=None,
        candidates=[_candidate(release_info=BLURAY_RELEASE, score=280)],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_no_mismatch_when_every_candidate_shares_the_video_release_type():
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type=VIDEO_WEB,
        candidates=[_candidate(release_info=WEB_RELEASE, score=280),
                    _candidate(release_info="Show.S01E01.1080p.WEBRip.x264-GRP",
                               score=285)],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_ultra_hd_blu_ray_is_the_same_release_type_as_blu_ray():
    from subtitles.mismatch import detect_release_type_mismatch

    # The disc timing is the same, so a UHD Blu-ray subtitle against a Blu-ray
    # video is not a release-type problem worth waking the user for.
    assert detect_release_type_mismatch(
        video_release_type="Blu-ray",
        candidates=[_candidate(
            release_info="Show.S01E01.2160p.UHD.BluRay.x265-GRP", score=280)],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_no_candidates_at_all_is_not_a_mismatch():
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type=VIDEO_WEB, candidates=[], min_score=MIN_SCORE,
        media_type="series") is None


@pytest.mark.parametrize("release_info,expected,expected_group", [
    ("Show.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb", "web", "web"),
    ("Show.S01E01.1080p.WEBRip.x264-GRP", "web", "web"),
    ("Show.S01E01.1080p.BluRay.x264-GRP", "blu-ray", "disk-hd"),
    # The name the user is told stays the one guessit found; only the bucket
    # the detector compares on is merged.
    ("Show.S01E01.2160p.UHD.BluRay.x265-GRP", "ultra hd blu-ray", "disk-hd"),
    ("Show.S01E01.HDTV.x264-LOL", "hdtv", "tv"),
    ("Movie.2019.DVDRip.XviD", "dvd", "disk-sd"),
    ("uploaded by someone", None, None),
    ("", None, None),
    (None, None, None),
])
def test_parse_release_type(release_info, expected, expected_group):
    from subtitles.mismatch import parse_release_type, release_type_group

    parsed = parse_release_type(release_info, "series")

    assert parsed == expected
    assert release_type_group(parsed) == expected_group


# --------------------------------------------------------------------------
# Surfacing the rejected candidates. The scoring loop already computes a score
# for every listed subtitle and then breaks out as soon as one falls below the
# minimum, so the candidates the detector needs are exactly the ones the loop
# never visits. They have to be collected from the scored list, not from the
# download loop.
# --------------------------------------------------------------------------

def _episode_video():
    from subliminal.video import Episode

    return Episode("/tv/Show/Show.S01E01.1080p.WEB-DL.x264-NTb.mkv", "Show", 1, 1,
                   source="Web", resolution="1080p")


class _FakeSubtitle:
    hash_verifiable = False
    hearing_impaired_verifiable = False
    hearing_impaired = False
    use_original_format = False
    format = "srt"

    def __init__(self, provider_name, release_info, matches):
        from subzero.language import Language

        self.provider_name = provider_name
        self.release_info = release_info
        self.language = Language("eng")
        self._matches = matches

    def get_matches(self, video):
        return set(self._matches)


def _pool():
    from subliminal_patch.core import SZProviderPool

    pool = SZProviderPool.__new__(SZProviderPool)
    pool.download_subtitle = lambda subtitle: True
    return pool


def test_download_best_subtitles_reports_every_scored_candidate():
    from subzero.language import Language

    accepted = _FakeSubtitle("acceptable", WEB_RELEASE,
                             {"series", "year", "season", "episode"})
    rejected = _FakeSubtitle("rejected", BLURAY_RELEASE,
                             {"series", "season", "episode"})
    sink = []

    downloaded = _pool().download_best_subtitles(
        [accepted, rejected], _episode_video(), {Language("eng")},
        min_score=300, candidate_sink=sink)

    assert [s.provider_name for s in downloaded] == ["acceptable"]
    # 160 series + 90 year + 30 season + 30 episode + 1 hearing impaired = 311,
    # and 311 - 90 = 221 for the candidate that misses the year.
    # matches ride along because the download loop applies more than the score:
    # an episode subtitle that does not match the season and episode is refused
    # however high it scores, and the detector has to apply the same rule.
    assert sink == [
        {"provider_name": "acceptable", "release_info": WEB_RELEASE,
         "score": 311, "downloaded": True,
         "matches": ["episode", "season", "series", "year"]},
        {"provider_name": "rejected", "release_info": BLURAY_RELEASE,
         "score": 221, "downloaded": False,
         "matches": ["episode", "season", "series"]},
    ]


def test_candidates_rejected_below_the_threshold_still_reach_the_sink():
    from subzero.language import Language

    # Nothing clears 300 here, so the download loop breaks on the very first
    # candidate. Both must still be reported.
    first = _FakeSubtitle("first", BLURAY_RELEASE, {"series", "season", "episode"})
    second = _FakeSubtitle("second", HDTV_RELEASE, {"series", "season"})
    sink = []

    downloaded = _pool().download_best_subtitles(
        [first, second], _episode_video(), {Language("eng")},
        min_score=300, candidate_sink=sink)

    assert downloaded == []
    assert [c["provider_name"] for c in sink] == ["first", "second"]
    assert [c["downloaded"] for c in sink] == [False, False]


def test_candidate_sink_is_optional():
    from subzero.language import Language

    subtitle = _FakeSubtitle("acceptable", WEB_RELEASE,
                             {"series", "year", "season", "episode"})

    downloaded = _pool().download_best_subtitles(
        [subtitle], _episode_video(), {Language("eng")}, min_score=300)

    assert [s.provider_name for s in downloaded] == ["acceptable"]


def test_core_persistent_passes_the_sink_through_to_the_pool():
    from unittest.mock import MagicMock

    from subliminal_patch import core_persistent

    from subzero.language import Language

    video = _episode_video()
    pool = MagicMock()
    pool.download_best_subtitles.return_value = []
    sink = []

    core_persistent.download_best_subtitles(
        videos={video}, languages={Language("eng")}, pool_instance=pool,
        min_score=300, candidate_sink=sink)

    assert pool.download_best_subtitles.call_args.kwargs["candidate_sink"] is sink


# --------------------------------------------------------------------------
# Persistence. A detection is recorded so the next scheduled pass over the same
# wanted item stays quiet, and so the wanted view can flag the item.
# --------------------------------------------------------------------------

def _seed_episode(session, episode_local_id, arr_instance_id, sonarr_episode_id=20):
    from sqlalchemy import insert as sa_insert

    from app.database import TableEpisodes, TableShows

    show_id = episode_local_id * 10
    session.execute(sa_insert(TableShows).values(
        id=show_id, sonarrSeriesId=show_id, arr_instance_id=arr_instance_id,
        path=f"/series/{arr_instance_id}/{show_id}", title="Show",
        imdbId=f"tt-{show_id}", tvdbId=show_id))
    session.execute(sa_insert(TableEpisodes).values(
        id=episode_local_id, series_id=show_id, sonarrSeriesId=show_id,
        sonarrEpisodeId=sonarr_episode_id, arr_instance_id=arr_instance_id,
        path=f"/series/{arr_instance_id}/{episode_local_id}.mkv", title="Pilot",
        season=1, episode=1,
        audio_language="English", sceneName="Scene", missing_subtitles="['en']",
        failedAttempts="[]", subtitles="[]"))


def _seed_movie(session, movie_local_id, arr_instance_id, radarr_id=30):
    from sqlalchemy import insert as sa_insert

    from app.database import TableMovies

    session.execute(sa_insert(TableMovies).values(
        id=movie_local_id, radarrId=radarr_id, arr_instance_id=arr_instance_id,
        path=f"/movies/{movie_local_id}.mkv", title="Movie", tmdbId=str(movie_local_id),
        imdbId=f"tt-{movie_local_id}", audio_language="English", sceneName="Scene",
        missing_subtitles="['en']", failedAttempts="[]", subtitles="[]"))


def _mismatch(video_release_type="web", subtitle_release_type="blu-ray"):
    from subtitles.mismatch import ReleaseTypeMismatch

    return ReleaseTypeMismatch(
        video_release_type=video_release_type,
        subtitle_release_type=subtitle_release_type,
        provider_name="goodsubs",
        release_info=BLURAY_RELEASE,
        score=270,
        projected_score=295,
    )


def test_records_the_detection_with_its_owning_instance(schema_session):
    from sqlalchemy import select as sa_select

    from app.database import TableReleaseTypeMismatch
    from subtitles.mismatch import record_mismatch

    assert record_mismatch(schema_session, "series", 101, 2, "en", _mismatch()) is True

    row = schema_session.execute(sa_select(TableReleaseTypeMismatch)).scalars().one()
    assert row.media_type == "series"
    assert row.media_id == 101
    assert row.arr_instance_id == 2
    assert row.language == "en"
    assert row.video_release_type == "web"
    assert row.subtitle_release_type == "blu-ray"
    assert row.provider == "goodsubs"
    assert row.score == 270


def test_a_repeated_pass_does_not_record_again(schema_session):
    from sqlalchemy import func, select as sa_select

    from app.database import TableReleaseTypeMismatch
    from subtitles.mismatch import record_mismatch

    assert record_mismatch(schema_session, "series", 101, 2, "en", _mismatch()) is True
    assert record_mismatch(schema_session, "series", 101, 2, "en", _mismatch()) is False

    count = schema_session.execute(
        sa_select(func.count()).select_from(TableReleaseTypeMismatch)).scalar()
    assert count == 1


def test_colliding_media_ids_across_instances_record_separately(schema_session):
    from subtitles.mismatch import record_mismatch

    assert record_mismatch(schema_session, "series", 101, 1, "en", _mismatch()) is True
    assert record_mismatch(schema_session, "series", 101, 2, "en", _mismatch()) is True


def test_a_replaced_release_records_again(schema_session):
    from subtitles.mismatch import record_mismatch

    # The user re-grabbed the item as a Blu-ray release. That is a different
    # situation from the one already recorded, so it may be reported once.
    assert record_mismatch(schema_session, "series", 101, 2, "en", _mismatch()) is True
    assert record_mismatch(schema_session, "series", 101, 2, "en",
                           _mismatch(video_release_type="blu-ray",
                                     subtitle_release_type="web")) is True


def test_another_language_records_separately(schema_session):
    from subtitles.mismatch import record_mismatch

    assert record_mismatch(schema_session, "series", 101, 2, "en", _mismatch()) is True
    assert record_mismatch(schema_session, "series", 101, 2, "hu", _mismatch()) is True


# --------------------------------------------------------------------------
# The entry point the search calls. Off by default; one notification per
# detection; silent on every later pass.
# --------------------------------------------------------------------------

def _video(source="Web", arr_instance_id=2):
    from types import SimpleNamespace

    return SimpleNamespace(source=source, sonarrSeriesId=10, sonarrEpisodeId=20,
                           radarrId=30, arr_instance_id=arr_instance_id)


def _mismatching_candidates():
    return [_candidate(release_info=BLURAY_RELEASE, score=270),
            _candidate(provider_name="othersubs", release_info=WEB_RELEASE, score=260)]


@pytest.fixture
def detection(schema_session, monkeypatch):
    """The detector wired to an in-memory database and a recording notifier."""
    from types import SimpleNamespace

    from subtitles import mismatch

    _seed_episode(schema_session, 101, 2)
    _seed_movie(schema_session, 301, 2)

    sent = SimpleNamespace(episodes=[], movies=[])
    monkeypatch.setattr(mismatch, "database", schema_session)
    monkeypatch.setattr(mismatch, "send_notifications",
                        lambda *args, **kwargs: sent.episodes.append((args, kwargs)))
    monkeypatch.setattr(mismatch, "send_notifications_movie",
                        lambda *args, **kwargs: sent.movies.append((args, kwargs)))
    monkeypatch.setattr(mismatch.settings.general, "detect_release_type_mismatch", True)
    return SimpleNamespace(module=mismatch, sent=sent, session=schema_session)


def test_detection_is_disabled_by_default():
    from app.config import settings

    assert settings.general.detect_release_type_mismatch is False


def test_no_detection_and_no_notification_while_disabled(detection):
    from sqlalchemy import func, select as sa_select

    from app.database import TableReleaseTypeMismatch

    detection.module.settings.general.detect_release_type_mismatch = False

    assert detection.module.report_release_type_mismatch(
        _video(), "series", "en", _mismatching_candidates(), MIN_SCORE) is None
    assert detection.sent.episodes == []
    assert detection.session.execute(
        sa_select(func.count()).select_from(TableReleaseTypeMismatch)).scalar() == 0


def test_an_episode_mismatch_notifies_exactly_once(detection):
    from sqlalchemy import func, select as sa_select

    from app.database import TableReleaseTypeMismatch

    first = detection.module.report_release_type_mismatch(
        _video(), "series", "en", _mismatching_candidates(), MIN_SCORE)
    second = detection.module.report_release_type_mismatch(
        _video(), "series", "en", _mismatching_candidates(), MIN_SCORE)

    assert first is not None
    assert second is None
    assert len(detection.sent.episodes) == 1
    args, kwargs = detection.sent.episodes[0]
    assert args[0] == 10
    assert args[1] == 20
    assert "blu-ray" in args[2].lower()
    assert kwargs["arr_instance_id"] == 2
    assert detection.session.execute(
        sa_select(func.count()).select_from(TableReleaseTypeMismatch)).scalar() == 1
    row = detection.session.execute(
        sa_select(TableReleaseTypeMismatch)).scalars().one()
    assert row.media_id == 101


def test_a_movie_mismatch_notifies_the_movie_notifier(detection):
    # The movie scale is its own: maximum 180, release type worth 30, and the
    # shipped default minimum of 70% lands on 126.
    candidates = [
        _candidate(release_info="Movie.2019.1080p.BluRay.x264-GRP", score=100),
        _candidate(provider_name="othersubs",
                   release_info="Movie.2019.1080p.WEB-DL.x264-NTb", score=90),
    ]

    result = detection.module.report_release_type_mismatch(
        _video(), "movie", "en", candidates, 126)

    assert result is not None
    assert detection.sent.episodes == []
    assert len(detection.sent.movies) == 1
    args, kwargs = detection.sent.movies[0]
    assert args[0] == 30
    assert kwargs["arr_instance_id"] == 2
    row_media_id = detection.session.execute(
        __import__("sqlalchemy").select(
            __import__("app.database", fromlist=["x"]).TableReleaseTypeMismatch.media_id)
    ).scalar()
    assert row_media_id == 301


def test_a_clean_search_notifies_nothing(detection):
    assert detection.module.report_release_type_mismatch(
        _video(), "series", "en",
        [_candidate(release_info=WEB_RELEASE, score=300, downloaded=True)],
        MIN_SCORE) is None
    assert detection.sent.episodes == []


def test_an_unknown_media_row_is_not_recorded(detection):
    from sqlalchemy import func, select as sa_select

    from app.database import TableReleaseTypeMismatch

    unknown = _video(arr_instance_id=99)

    assert detection.module.report_release_type_mismatch(
        unknown, "series", "en", _mismatching_candidates(), MIN_SCORE) is None
    assert detection.sent.episodes == []
    assert detection.session.execute(
        sa_select(func.count()).select_from(TableReleaseTypeMismatch)).scalar() == 0


# --------------------------------------------------------------------------
# The hook into the search. The detector runs on the results of the search that
# already happened, so enabling it must not add a single provider request.
# --------------------------------------------------------------------------

@pytest.fixture
def search(monkeypatch):
    """generate_subtitles with everything but the search itself stubbed out."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from subtitles import download as download_module
    from subtitles import pool as pool_module
    from subtitles.tools import mods as mods_module

    pool = MagicMock()
    pool.providers = ["goodsubs", "othersubs"]
    pool.discarded_providers = set()
    pool.provider_configs = {}

    video = _episode_video()
    video.sonarrSeriesId = 10
    video.sonarrEpisodeId = 20
    video.arr_instance_id = 2

    calls = SimpleNamespace(searches=[], reports=[])

    def fake_download_best_subtitles(**kwargs):
        calls.searches.append(kwargs)
        sink = kwargs.get("candidate_sink")
        if sink is not None:
            sink.extend(_mismatching_candidates())
        return {}

    from subzero.language import Language

    monkeypatch.setattr(pool_module, "_update_pool", lambda *args, **kwargs: False)
    # languages_dict is only populated once the app has booted against a real
    # database, so hand the search its language object directly.
    monkeypatch.setattr(download_module, "_get_language_obj",
                        lambda languages: {Language("eng")})
    monkeypatch.setattr(download_module, "_get_pool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(download_module, "get_video", lambda *args, **kwargs: video)
    monkeypatch.setattr(download_module, "get_profiles_list",
                        lambda profile_id=None: {"originalFormat": 0})
    monkeypatch.setattr(download_module, "download_best_subtitles",
                        fake_download_best_subtitles)
    monkeypatch.setattr(mods_module, "get_subzero_mods", lambda *args, **kwargs: [])
    # The dogpile cache region is only configured once the app has booted.
    monkeypatch.setattr(download_module.subliminal, "region", MagicMock())

    def run(**kwargs):
        return list(download_module.generate_subtitles(
            "/series/2/s01e01.mkv", [("en", "False", "False")], "English", "None",
            "Show", "series", 1, arr_instance_id=2, **kwargs))

    return SimpleNamespace(run=run, pool=pool, video=video, calls=calls,
                           module=download_module, monkeypatch=monkeypatch)


def test_the_search_hands_its_rejected_candidates_to_the_detector(search):
    reports = []
    search.monkeypatch.setattr(search.module, "report_release_type_mismatch",
                               lambda *args, **kwargs: reports.append((args, kwargs)))

    search.run()

    assert len(reports) == 1
    args, _kwargs = reports[0]
    assert args[0] is search.video
    assert args[1] == "series"
    assert str(args[2]) == "en"
    assert args[3] == _mismatching_candidates()
    assert args[4] == 288  # 80% of the episode maximum, the shipped default


def test_a_downloaded_subtitle_is_never_reported_as_a_mismatch(search):
    reports = []
    search.monkeypatch.setattr(search.module, "report_release_type_mismatch",
                               lambda *args, **kwargs: reports.append(args))
    search.monkeypatch.setattr(search.module, "download_best_subtitles",
                               lambda **kwargs: {search.video: [_FakeSubtitle(
                                   "othersubs", WEB_RELEASE, {"series"})]})
    search.monkeypatch.setattr(search.module, "save_subtitles",
                               lambda *args, **kwargs: [])

    search.run()

    assert reports == []


def test_an_upgrade_search_is_never_reported_as_a_mismatch(search):
    """An upgrade search raises the minimum score above the score of the
    subtitle the user already has. Every candidate is 'rejected' by
    construction, and the user is not missing a subtitle at all, so reporting
    one here would be pure noise."""
    reports = []
    search.monkeypatch.setattr(search.module, "report_release_type_mismatch",
                               lambda *args, **kwargs: reports.append(args))

    search.run(is_upgrade=True, forced_minimum_score=200)

    assert reports == []


def test_enabling_the_detection_adds_no_provider_request(search, detection):
    """The whole point of reusing the rejected candidates: the detector sees a
    search that already happened and asks nobody anything."""
    detection.module.settings.general.detect_release_type_mismatch = False
    search.run()
    disabled_searches = len(search.calls.searches)
    disabled_pool_calls = len(search.pool.mock_calls)

    search.calls.searches.clear()
    search.pool.reset_mock()
    detection.module.settings.general.detect_release_type_mismatch = True
    search.run()

    # It really ran: a notification came out of this pass.
    assert len(detection.sent.episodes) == 1
    assert len(search.calls.searches) == disabled_searches
    assert len(search.pool.mock_calls) == disabled_pool_calls
    assert search.pool.list_subtitles.call_count == 0
    assert search.pool.list_subtitles_prioritized.call_count == 0
    assert search.pool.download_best_subtitles.call_count == 0


# --------------------------------------------------------------------------
# Flagging the affected item in the wanted view.
# --------------------------------------------------------------------------

def _flag(session, media_type, media_id, arr_instance_id=2, language="en"):
    from subtitles.mismatch import record_mismatch

    assert record_mismatch(session, media_type, media_id, arr_instance_id, language,
                           _mismatch()) is True


def test_flagged_media_ids_returns_only_the_recorded_items(schema_session):
    from subtitles.mismatch import flagged_media_ids

    _flag(schema_session, "series", 101)
    _flag(schema_session, "movie", 301)

    assert flagged_media_ids(schema_session, "series", [101, 102]) == {101}
    assert flagged_media_ids(schema_session, "movie", [301, 302]) == {301}
    assert flagged_media_ids(schema_session, "series", []) == set()


def test_flagged_media_ids_does_not_leak_across_media_types(schema_session):
    from subtitles.mismatch import flagged_media_ids

    _flag(schema_session, "movie", 101)

    assert flagged_media_ids(schema_session, "series", [101]) == set()


@pytest.fixture
def language_table(monkeypatch):
    """languages_dict is built at boot from the database; the API postprocessor
    needs it to render language codes."""
    from languages import get_languages

    monkeypatch.setattr(
        get_languages, "languages_dict",
        [{"code3": "eng", "code2": "en", "name": "English", "code3b": "eng"}],
        raising=False)


def _wanted_episodes():
    from flask import Flask

    from api.episodes.wanted import EpisodesWanted

    app = Flask(__name__)
    with app.test_request_context("/api/episodes/wanted"):
        return EpisodesWanted.get.__wrapped__(EpisodesWanted())


def _wanted_movies():
    from flask import Flask

    from api.movies.wanted import MoviesWanted

    app = Flask(__name__)
    with app.test_request_context("/api/movies/wanted"):
        return MoviesWanted.get.__wrapped__(MoviesWanted())


def test_the_wanted_episode_view_flags_a_recorded_mismatch(schema_session, monkeypatch,
                                                          language_table):
    from api.episodes import wanted as wanted_api

    _seed_episode(schema_session, 101, 2, sonarr_episode_id=20)
    _seed_episode(schema_session, 102, 2, sonarr_episode_id=21)
    _flag(schema_session, "series", 101)
    monkeypatch.setattr(wanted_api, "database", schema_session)

    rows = {row["id"]: row for row in _wanted_episodes()["data"]}

    assert rows[101]["release_mismatch"] is True
    assert rows[102]["release_mismatch"] is False


def test_the_wanted_movie_view_flags_a_recorded_mismatch(schema_session, monkeypatch,
                                                        language_table):
    from api.movies import wanted as wanted_api

    _seed_movie(schema_session, 301, 2, radarr_id=30)
    _seed_movie(schema_session, 302, 2, radarr_id=31)
    _flag(schema_session, "movie", 301)
    monkeypatch.setattr(wanted_api, "database", schema_session)

    rows = {row["id"]: row for row in _wanted_movies()["data"]}

    assert rows[301]["release_mismatch"] is True
    assert rows[302]["release_mismatch"] is False


# --------------------------------------------------------------------------
# The detector has to bucket release types exactly the way the scorer does.
# subliminal_patch's guess_matches awards the 'source' match through
# MERGED_FORMATS, so Blu-ray, Ultra HD Blu-ray and HD-DVD are one type to it,
# as are HDTV and SDTV. A detector that split them finer would report items the
# scorer never penalised, and would credit release-type points that were in the
# score already.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("video_release_type,release_info", [
    ("Blu-ray", "Show.S01E01.1080p.HD-DVD.x264-GRP"),
    ("Blu-ray", "Show.S01E01.2160p.UHD.BluRay.x265-GRP"),
    ("HDTV", "Show.S01E01.SDTV.x264-LOL"),
    ("DVD", "Show.S01E01.VHSRip.XviD-GRP"),
])
def test_release_types_the_scorer_considers_equivalent_never_trigger(video_release_type,
                                                                    release_info):
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type=video_release_type,
        candidates=[_candidate(release_info=release_info, score=280)],
        min_score=MIN_SCORE,
        media_type="series",
    ) is None


def test_the_detector_groups_release_types_like_the_scorer():
    from subliminal_patch.subtitle import MERGED_FORMATS
    from subtitles.mismatch import release_type_group

    for merged, sources in MERGED_FORMATS.items():
        groups = {release_type_group(source) for source in sources}
        assert groups == {merged.lower()}


def test_a_regrab_inside_the_same_release_type_group_does_not_record_again(schema_session):
    from subtitles.mismatch import record_mismatch

    assert record_mismatch(schema_session, "series", 101, 2, "en",
                           _mismatch(video_release_type="blu-ray")) is True
    # Same disc source, a different guessit spelling of it. Nothing changed for
    # the user, so nothing is reported.
    assert record_mismatch(schema_session, "series", 101, 2, "en",
                           _mismatch(video_release_type="ultra hd blu-ray")) is False


# --------------------------------------------------------------------------
# Four gaps found in review: the badge outliving the problem, the owning
# instance being rediscovered instead of passed, NULL owners defeating the
# unique index, and an open Wanted page not hearing about a new badge.
# --------------------------------------------------------------------------


def test_a_downloaded_language_clears_its_recorded_mismatch(detection):
    from sqlalchemy import func, select as sa_select

    from app.database import TableReleaseTypeMismatch

    detection.module.report_release_type_mismatch(
        _video(), "series", "en", _mismatching_candidates(), MIN_SCORE)
    assert detection.session.execute(
        sa_select(func.count()).select_from(TableReleaseTypeMismatch)).scalar() == 1

    detection.module.clear_mismatch(detection.session, "series", 101, "en")

    assert detection.session.execute(
        sa_select(func.count()).select_from(TableReleaseTypeMismatch)).scalar() == 0


def test_clearing_one_language_leaves_the_others_flagged(detection):
    detection.module.report_release_type_mismatch(
        _video(), "series", "en", _mismatching_candidates(), MIN_SCORE)
    detection.module.report_release_type_mismatch(
        _video(), "series", "hu", _mismatching_candidates(), MIN_SCORE)

    detection.module.clear_mismatch(detection.session, "series", 101, "en")

    assert detection.module.flagged_media_ids(detection.session, "series", [101]) == {101}


def test_clearing_the_last_language_takes_the_badge_with_it(detection):
    detection.module.report_release_type_mismatch(
        _video(), "series", "en", _mismatching_candidates(), MIN_SCORE)

    detection.module.clear_mismatch(detection.session, "series", 101, "en")

    assert detection.module.flagged_media_ids(detection.session, "series", [101]) == set()


def test_the_caller_s_instance_id_wins_over_the_refiner_s(detection):
    """generate_subtitles knows the owning instance. The video attribute is set
    by the database refiner, which reverses paths through the global mapping and
    can miss the row for a non-default instance, so the known value has to win.
    """
    video = _video(arr_instance_id=None)

    detection.module.report_release_type_mismatch(
        video, "series", "en", _mismatching_candidates(), MIN_SCORE,
        arr_instance_id=2)

    args, kwargs = detection.sent.episodes[0]
    assert kwargs["arr_instance_id"] == 2


def test_an_unowned_row_is_deduplicated_by_the_unique_index(schema_session):
    """NULL never equals NULL, so a nullable owner column cannot dedupe on its
    own and two concurrent searches would both insert and both notify. The
    unowned case is stored as a sentinel so the index does the work.
    """
    from sqlalchemy import select as sa_select

    from app.database import TableReleaseTypeMismatch
    from subtitles.mismatch import UNOWNED, record_mismatch

    assert record_mismatch(schema_session, "series", 111, None, "en", _mismatch()) is True

    stored = schema_session.execute(
        sa_select(TableReleaseTypeMismatch.arr_instance_id)
        .where(TableReleaseTypeMismatch.media_id == 111)).all()
    assert [row.arr_instance_id for row in stored] == [UNOWNED]

    # The pre-check is not what is under test here: bypass it and let the index
    # be the thing that refuses the duplicate.
    assert record_mismatch(schema_session, "series", 111, None, "en", _mismatch()) is False


def test_recording_a_mismatch_tells_an_open_wanted_page(detection, monkeypatch):
    """The Wanted pagination query refreshes only from a socket event, and has
    no polling or focus refresh, so without one the new badge appears on the
    next manual reload and not before."""
    events = []
    monkeypatch.setattr(detection.module, "event_stream",
                        lambda *args, **kwargs: events.append((args, kwargs)))

    detection.module.report_release_type_mismatch(
        _video(), "series", "en", _mismatching_candidates(), MIN_SCORE)

    assert any(kwargs.get("type") == "episode-wanted" or "episode-wanted" in args
               for args, kwargs in events), events


def test_recording_a_movie_mismatch_tells_an_open_wanted_page(detection, monkeypatch):
    events = []
    monkeypatch.setattr(detection.module, "event_stream",
                        lambda *args, **kwargs: events.append((args, kwargs)))

    detection.module.report_release_type_mismatch(
        _video(), "movies", "en", _mismatching_candidates(), MIN_SCORE)

    assert any(kwargs.get("type") == "movie-wanted" or "movie-wanted" in args
               for args, kwargs in events), events


def test_the_search_clears_the_record_when_the_language_finally_lands(search):
    """The end-to-end half of the clearing rule: the download path itself has to
    call it, or the badge only ever clears in a unit test."""
    cleared = []
    search.monkeypatch.setattr(
        search.module, "clear_mismatch_for_video",
        lambda video, media_type, language, arr_instance_id=None:
        cleared.append((media_type, str(language), arr_instance_id)))
    search.monkeypatch.setattr(search.module, "download_best_subtitles",
                               lambda **kwargs: {search.video: [_FakeSubtitle(
                                   "othersubs", WEB_RELEASE, {"series"})]})
    search.monkeypatch.setattr(search.module, "save_subtitles", lambda *args, **kwargs: [])

    search.run()

    assert cleared == [("series", "en", 2)]


def test_the_search_passes_the_owning_instance_to_the_reporter(search):
    """The reporter must not have to rediscover it from the video."""
    reports = []
    search.monkeypatch.setattr(search.module, "report_release_type_mismatch",
                               lambda *args, **kwargs: reports.append(kwargs))

    search.run()

    assert reports == [{"arr_instance_id": 2}]


# --------------------------------------------------------------------------
# Second review pass: eligibility beyond the score, media resolution without a
# refiner-supplied upstream id, clearing only after the file lands, and rows
# outliving the media they describe.
# --------------------------------------------------------------------------


def test_an_episode_candidate_the_download_loop_would_reject_is_not_a_mismatch():
    """Crossing the score threshold is not enough for an episode.

    The download loop separately requires season and episode plus series or
    imdb_id in the original matches, so a wrong-episode candidate sitting just
    under the threshold would still be refused after a release-type regrab.
    Reporting it promises the user a fix that would not work.
    """
    from subtitles.mismatch import detect_release_type_mismatch

    wrong_episode = _candidate(release_info=BLURAY_RELEASE, score=270)
    wrong_episode["matches"] = ["series", "season"]  # no episode match

    assert detect_release_type_mismatch(
        video_release_type="Web", candidates=[wrong_episode],
        min_score=MIN_SCORE, media_type="series") is None


def test_an_episode_candidate_the_loop_would_accept_is_still_a_mismatch():
    from subtitles.mismatch import detect_release_type_mismatch

    right_episode = _candidate(release_info=BLURAY_RELEASE, score=270)
    right_episode["matches"] = ["series", "season", "episode"]

    assert detect_release_type_mismatch(
        video_release_type="Web", candidates=[right_episode],
        min_score=MIN_SCORE, media_type="series") is not None


def test_a_candidate_without_recorded_matches_is_judged_on_score_alone():
    """Older records carry no matches. Treating that as ineligible would turn
    the detector off wherever the sink has not been updated."""
    from subtitles.mismatch import detect_release_type_mismatch

    assert detect_release_type_mismatch(
        video_release_type="Web", candidates=[_candidate()],
        min_score=MIN_SCORE, media_type="series") is not None


def test_movie_candidates_are_not_held_to_the_episode_rule():
    """The season/episode requirement is the download loop's episode branch.
    A movie has no season to match, so nothing extra is asked of it."""
    from subtitles.mismatch import detect_release_type_mismatch

    candidate = _candidate(release_info="Movie.2019.1080p.BluRay.x264-GRP", score=100)
    candidate["matches"] = ["title"]

    assert detect_release_type_mismatch(
        video_release_type="Web", candidates=[candidate],
        min_score=126, media_type="movie") is not None


def test_the_media_row_resolves_from_the_path_when_the_refiner_left_no_id(detection):
    """The database refiner reverses the video path through the GLOBAL mapping,
    so on an instance with a mapping of its own it can fail to find the row and
    leave the video with no sonarrEpisodeId at all. The detection is still about
    a real item, and the path is the thing that identifies it."""
    from types import SimpleNamespace

    from sqlalchemy import update

    from app.database import TableEpisodes

    detection.session.execute(
        update(TableEpisodes).values(path="/tv/show/s01e01.mkv")
        .where(TableEpisodes.id == 101))

    video = SimpleNamespace(source="Web", arr_instance_id=2,
                            original_path="/tv/show/s01e01.mkv")

    assert detection.module.report_release_type_mismatch(
        video, "series", "en", _mismatching_candidates(), MIN_SCORE,
        arr_instance_id=2) is not None


def test_deleting_a_series_takes_its_recorded_mismatches_with_it(schema_session):
    """A local id can be reused by SQLite after a delete, so an orphan row does
    not merely accumulate: it can badge an unrelated new item."""
    from sqlalchemy import func, select as sa_select

    from app.database import TableReleaseTypeMismatch
    from subtitles.mismatch import forget_media, record_mismatch

    _seed_episode(schema_session, 101, 2)
    record_mismatch(schema_session, "series", 101, 2, "en", _mismatch())

    forget_media(schema_session, "series", [101])

    assert schema_session.execute(
        sa_select(func.count()).select_from(TableReleaseTypeMismatch)).scalar() == 0


def test_forgetting_one_item_leaves_the_others_recorded(schema_session):
    from subtitles.mismatch import flagged_media_ids, forget_media, record_mismatch

    _seed_episode(schema_session, 101, 2)
    _seed_episode(schema_session, 102, 2, sonarr_episode_id=21)
    record_mismatch(schema_session, "series", 101, 2, "en", _mismatch())
    record_mismatch(schema_session, "series", 102, 2, "en", _mismatch())

    forget_media(schema_session, "series", [101])

    assert flagged_media_ids(schema_session, "series", [101, 102]) == {102}


def test_a_failed_save_keeps_the_recorded_mismatch(search):
    """The download succeeded but nothing reached disk, so the language is still
    missing. Clearing the record there would drop the badge and the once-only
    guard for a problem the user still has."""
    cleared = []
    search.monkeypatch.setattr(
        search.module, "clear_mismatch_for_video",
        lambda *args, **kwargs: cleared.append(args))
    search.monkeypatch.setattr(search.module, "download_best_subtitles",
                               lambda **kwargs: {search.video: [_FakeSubtitle(
                                   "othersubs", WEB_RELEASE, {"series"})]})

    def exploding_save(*args, **kwargs):
        raise OSError("read-only file system")

    search.monkeypatch.setattr(search.module, "save_subtitles", exploding_save)

    search.run()

    assert cleared == []
