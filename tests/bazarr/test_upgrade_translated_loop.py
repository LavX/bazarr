# coding=utf-8
"""The translated-subtitle upgrade loop.

A reader reported Bazarr downloading the same OpenSubtitles listing for the same
film every upgrade cycle, the same provider and the same score (74.44%, twelve
hours apart) for as long as Bazarr was left running. Nothing was wrong with the
listing. Three properties of the upgrade path compose into the loop:

  * A translated row (action 6) carries ``translator.default_score`` (50%) or no
    score at all, so it is always an upgrade candidate and the bar it sets is
    trivially beaten by any real subtitle.
  * The upgrade writes its result under the language of the subtitle the
    provider returned, which for a "prefer HI" profile is a different variant
    string (plain ``nl``) than the translated row it replaced (``nl:hi``). No
    other row is ever written in the translated row's group, so it stays that
    group's newest row for good.
  * Removing the translated file is what the upgrade does, and the wanted scan
    re-queues a translation the moment that file is gone, so a fresh translated
    row keeps arriving with the 50% default and the loop has no end.

These tests pin the two places that break it: the upgrade baseline must not come
from a translated row once a real subtitle is on record for that language, and
the wanted scan must not translate a language a real subtitle already covers.
"""

import os
import pathlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.orm import scoped_session, sessionmaker

from app.database import (
    Base,
    TableEpisodes,
    TableHistory,
    TableHistoryMovie,
    TableLanguagesProfiles,
    TableMovies,
    TableShows,
)

# The reported listing: 134 raw points out of the 180 a movie can score, which
# the history API renders as 74.44%.
LISTING_SCORE = 134
MOVIE_MAX_SCORE = 180
# translator.default_score against the 180 point movie scale.
TRANSLATED_SCORE = 90
# The same 74.44% listing on the 360 point episode scale, and the 50% default.
EPISODE_LISTING_SCORE = 268
EPISODE_TRANSLATED_SCORE = 180


# PostgreSQL is first class, and this loop was reported against a real library:
# the fix reads back the history rows it wrote, so both row-timestamp ordering
# and the NULL owner comparison have to hold on the backend that ships. The
# postgres half skips when nothing is reachable, the way the other PG tests
# here do; CI provides a service so it does not skip there.
_PG_URL = os.environ.get(
    "BAZARR_PG_TEST_URL",
    "postgresql+psycopg://postgres:test@127.0.0.1:55432/bazarr")


def _fresh_engine(backend):
    if backend == "sqlite":
        engine = create_engine("sqlite:///:memory:")
    else:
        engine = create_engine(_PG_URL)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"PostgreSQL not reachable at {_PG_URL}: {exc}")
        # Fresh schema per test so repeated runs do not accumulate rows.
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture(params=["sqlite", "postgres"])
def upgrade_db(request, monkeypatch):
    import app.database as database_module
    from subtitles import upgrade

    engine = _fresh_engine(request.param)
    session = scoped_session(sessionmaker(bind=engine))

    settings = SimpleNamespace(
        general=SimpleNamespace(
            days_to_upgrade_subs=365,
            upgrade_manual=False,
            # The reader opted in to replacing translations with real subtitles.
            upgrade_translated=True,
            upgrade_subs=True,
            use_sonarr=True,
            use_radarr=True,
        )
    )
    monkeypatch.setattr(upgrade, "database", session)
    monkeypatch.setattr(upgrade, "settings", settings)
    monkeypatch.setattr(
        database_module,
        "settings",
        SimpleNamespace(
            radarr=SimpleNamespace(excluded_tags=[], only_monitored=False),
            sonarr=SimpleNamespace(
                excluded_series_types=[],
                excluded_tags=[],
                exclude_season_zero=False,
                only_monitored=False,
            ),
        ),
    )

    # Neutralise the collaborators that are not under test, so the assertions
    # below can watch only the search, the notification and the candidate set.
    monkeypatch.setattr(upgrade, "get_providers", lambda: ["opensubtitlescom"])
    monkeypatch.setattr(upgrade, "store_subtitles", lambda *a, **k: None)
    monkeypatch.setattr(upgrade, "store_subtitles_movie", lambda *a, **k: None)
    monkeypatch.setattr(upgrade, "event_stream", lambda *a, **k: None)
    monkeypatch.setattr(upgrade, "get_audio_profile_languages", lambda *a, **k: [])
    monkeypatch.setattr(upgrade, "_language_still_desired", lambda *a, **k: True)
    monkeypatch.setattr(upgrade, "_is_hi_required", lambda *a, **k: True)
    monkeypatch.setattr(upgrade.path_mappings, "path_replace", lambda p: p)
    monkeypatch.setattr(upgrade.path_mappings, "path_replace_movie", lambda p: p)
    monkeypatch.setattr(upgrade, "history_log_movie", lambda *a, **k: None)

    class _JobsQueue:
        def update_job_progress(self, *a, **k):
            pass

        def update_job_name(self, *a, **k):
            pass

    monkeypatch.setattr(upgrade, "jobs_queue", _JobsQueue())

    session.execute(insert(TableLanguagesProfiles).values(
        profileId=1, name="Dutch", items="[]", cutoff=None, originalFormat=None,
    ))

    try:
        yield session
    finally:
        session.remove()
        engine.dispose()


class _FakeProvider:
    """A provider that keeps serving one listing, the way OpenSubtitles did.

    The shipped search only accepts a listing whose raw score reaches the
    minimum the caller set. The upgrade path raises the stored score once before
    passing ``forced_minimum_score`` to subtitles/download.py. The bar the
    upgrade hands down is therefore the whole question: answer with the listing
    whenever it reaches the bar, and a repeat pass that fetches it is real.
    """

    def __init__(self, result, listing_score=LISTING_SCORE):
        self.result = result
        self.listing_score = listing_score
        self.bars = []

    def __call__(self, *args, **kwargs):
        bar = int(kwargs.get("forced_minimum_score") or 0)
        self.bars.append(bar)
        if self.listing_score < bar:
            return []
        return [self.result]


def _listing_result(video_path):
    return SimpleNamespace(
        message="Dutch subtitles upgraded from opensubtitlescom with a score of 74.44%.",
        path=video_path,
        language_code="nl",
        provider="opensubtitlescom",
        score=LISTING_SCORE,
        subs_id="opensubtitlescom-134",
        subs_path=video_path + ".nl.srt",
        matched=None,
        not_matched=None,
    )


def _seed_movie(db, subtitles, missing="['nl:hi']"):
    db.execute(insert(TableMovies).values(
        id=30,
        arr_instance_id=2,
        path="/movies/roofman/roofman.mkv",
        radarrId=30,
        title="Roofman",
        tmdbId="100",
        profileId=1,
        audio_language="[]",
        sceneName="Roofman.2025.1080p",
        missing_subtitles=missing,
        failedAttempts=None,
        subtitles=subtitles,
    ))


def _movie_row(db, **overrides):
    values = dict(
        arr_instance_id=2,
        movie_id=30,
        radarrId=30,
        video_path="/movies/roofman/roofman.mkv",
        score_out_of=MOVIE_MAX_SCORE,
    )
    values.update(overrides)
    db.execute(insert(TableHistoryMovie).values(**values))


def _first_translation(db, now):
    """The translator's row: action 6, the profile's HI variant, 50%."""
    _movie_row(
        db, id=101, action=6, language="nl:hi", provider=None,
        score=TRANSLATED_SCORE, timestamp=now - timedelta(hours=40),
        subtitles_path="/movies/roofman/roofman.mkv.nl.hi.srt",
        description="English subtitles translated to Dutch.",
    )


def _provider_replacement(db, now):
    """The upgrade's own row: the provider listing, plain ``nl``."""
    _movie_row(
        db, id=102, action=3, language="nl", provider="opensubtitlescom",
        score=LISTING_SCORE, timestamp=now - timedelta(hours=28),
        subtitles_path="/movies/roofman/roofman.mkv.nl.srt",
        description="Dutch subtitles upgraded from opensubtitlescom with a score of 74.44%.",
    )


def _second_translation(db, now):
    """The wanted scan's row, written after the upgrade removed the file."""
    _movie_row(
        db, id=103, action=6, language="nl:hi", provider=None,
        score=TRANSLATED_SCORE, timestamp=now - timedelta(hours=16),
        subtitles_path="/movies/roofman/roofman.mkv.nl.hi.srt",
        description="English subtitles translated to Dutch.",
    )


def test_a_replaced_translation_is_not_the_movie_upgrade_baseline(upgrade_db):
    """The action-3 row is the only baseline once a real subtitle has landed.

    The translated row the wanted scan wrote afterwards must not be offered as a
    candidate: its 50% default hands the search a bar of 91 of 180, which the
    74.44% listing clears, and that is the loop.
    """
    from subtitles.upgrade import get_upgradable_movies_subtitles

    now = datetime.now()
    _seed_movie(upgrade_db, '[["nl", "/movies/roofman/roofman.mkv.nl.srt", 4321]]')
    _first_translation(upgrade_db, now)
    _provider_replacement(upgrade_db, now)
    _second_translation(upgrade_db, now)

    assert get_upgradable_movies_subtitles() == {102: None}


def test_the_same_listing_is_not_downloaded_twice_for_a_movie(upgrade_db, monkeypatch):
    """Two upgrade passes against the same listing, one download.

    The first pass replaces the translation, which is the point of
    ``upgrade_translated``. The second must not fetch that listing again, must
    not write history and must not notify.
    """
    from subtitles import upgrade

    now = datetime.now()
    _seed_movie(upgrade_db, (
        '[["nl", "/movies/roofman/roofman.mkv.nl.srt", 4321],'
        ' ["nl:hi", "/movies/roofman/roofman.mkv.nl.hi.srt", 1234]]'))
    _first_translation(upgrade_db, now)

    provider = _FakeProvider(_listing_result("/movies/roofman/roofman.mkv"))
    monkeypatch.setattr(upgrade, "generate_subtitles", provider)
    notifications = []
    monkeypatch.setattr(upgrade, "send_notifications_movie",
                        lambda *a, **k: notifications.append(a))

    upgrade.upgrade_movies_subtitles(job_id="job-1")

    assert provider.bars == [TRANSLATED_SCORE + 1], \
        "the translation is the baseline and the search runs against it"
    assert len(notifications) == 1, "the first replacement is the user visible event"

    # What the two writers leave behind: the upgrade's action-3 row for the
    # provider file, and the wanted scan's action-6 row for the translation it
    # re-queued once the upgrade had removed the translated file.
    _provider_replacement(upgrade_db, now)
    _second_translation(upgrade_db, now)
    notifications.clear()

    upgrade.upgrade_movies_subtitles(job_id="job-1")

    assert notifications == [], "the same listing must not be downloaded again"
    assert provider.bars[1:] == [LISTING_SCORE + 1], \
        "the provider row is the baseline now, and its bar is above the listing"


def test_a_translated_row_no_provider_ever_replaced_is_still_a_candidate(upgrade_db):
    """Replacing a translation with a real subtitle once is the feature.

    With no provider sourced row on record for the language, the translated row
    stays the candidate and the upgrade searches against the 50% default.
    """
    from subtitles.upgrade import get_upgradable_movies_subtitles

    now = datetime.now()
    _seed_movie(upgrade_db, '[["nl:hi", "/movies/roofman/roofman.mkv.nl.hi.srt", 1234]]')
    _first_translation(upgrade_db, now)

    assert get_upgradable_movies_subtitles() == {101: None}


# --------------------------------------------------------------------------
# Series: the same shape, same defect, one row set per episode.
# --------------------------------------------------------------------------

def _seed_episode(db, subtitles='[["nl", "/series/show/s01e01.mkv.nl.srt", 4321]]'):
    db.execute(insert(TableShows).values(
        id=10, arr_instance_id=1, sonarrSeriesId=10, path="/series/show",
        title="Show", profileId=1,
    ))
    db.execute(insert(TableEpisodes).values(
        id=20, series_id=10, arr_instance_id=1, episode=1, monitored="True",
        path="/series/show/s01e01.mkv", season=1, sonarrEpisodeId=20,
        sonarrSeriesId=10, title="Pilot", audio_language="[]",
        sceneName="Show.S01E01.1080p", missing_subtitles="['nl:hi']",
        failedAttempts=None, subtitles=subtitles,
    ))


def _episode_row(db, **overrides):
    values = dict(
        arr_instance_id=1, series_id=10, episode_id=20, sonarrSeriesId=10,
        sonarrEpisodeId=20, video_path="/series/show/s01e01.mkv",
        score_out_of=360,
    )
    values.update(overrides)
    db.execute(insert(TableHistory).values(**values))


def test_a_replaced_translation_is_not_the_episode_upgrade_baseline(upgrade_db):
    from subtitles.upgrade import get_upgradable_episode_subtitles

    now = datetime.now()
    _seed_episode(upgrade_db)
    _episode_row(
        upgrade_db, id=201, action=6, language="nl:hi", provider=None,
        score=EPISODE_TRANSLATED_SCORE, timestamp=now - timedelta(hours=40),
        subtitles_path="/series/show/s01e01.mkv.nl.hi.srt",
        description="English subtitles translated to Dutch.",
    )
    _episode_row(
        upgrade_db, id=202, action=3, language="nl", provider="opensubtitlescom",
        score=268, timestamp=now - timedelta(hours=28),
        subtitles_path="/series/show/s01e01.mkv.nl.srt",
        description="Dutch subtitles upgraded from opensubtitlescom with a score of 74.44%.",
    )
    _episode_row(
        upgrade_db, id=203, action=6, language="nl:hi", provider=None,
        score=EPISODE_TRANSLATED_SCORE, timestamp=now - timedelta(hours=16),
        subtitles_path="/series/show/s01e01.mkv.nl.hi.srt",
        description="English subtitles translated to Dutch.",
    )

    assert get_upgradable_episode_subtitles() == {202: None}


@pytest.mark.parametrize("media", ["episode", "movie"])
@pytest.mark.parametrize(
    "ai_translated,score_offset,expected",
    [
        (True, 1, True),
        (True, 0, True),
        (True, -42, True),
        (False, 1, False),
        (None, 1, False),
    ],
)
def test_ai_provider_row_inside_upgrade_window_is_an_upgrade_candidate(
        upgrade_db, media, ai_translated, score_offset, expected, monkeypatch):
    """With a positive penalty, AI rows remain candidates at and above max."""
    from subtitles import upgrade

    monkeypatch.setattr(upgrade.settings.general, "ai_translated_score_penalty", 1, raising=False)
    from subtitles.upgrade import (
        get_upgradable_episode_subtitles,
        get_upgradable_movies_subtitles,
    )

    now = datetime.now()
    if media == "movie":
        _seed_movie(upgrade_db, '[["nl", "/movies/roofman/roofman.mkv.nl.srt", 4321]]')
        _movie_row(
            upgrade_db, id=301, action=1, language="nl", provider="subdl",
            score=MOVIE_MAX_SCORE - score_offset, timestamp=now,
            subtitles_path="/movies/roofman/roofman.mkv.nl.srt",
            description="Dutch subtitles downloaded from subdl.",
            ai_translated=ai_translated,
        )
        selected = get_upgradable_movies_subtitles()
    else:
        _seed_episode(upgrade_db)
        _episode_row(
            upgrade_db, id=302, action=1, language="nl", provider="subdl",
            score=360 - score_offset, timestamp=now,
            subtitles_path="/series/show/s01e01.mkv.nl.srt",
            description="Dutch subtitles downloaded from subdl.",
            ai_translated=ai_translated,
        )
        selected = get_upgradable_episode_subtitles()

    assert (bool(selected), set(selected)) == ((expected, {301 if media == "movie" else 302})
                                                if expected else (False, set()))


def _seed_provider_upgrade_candidate(db, media, score, ai_translated=True, timestamp=None):
    timestamp = timestamp or datetime.now()
    if media == "movie":
        candidate_id = 303
        _seed_movie(db, '[["nl", "/movies/roofman/roofman.mkv.nl.srt", 4321]]')
        _movie_row(
            db, id=candidate_id, action=1, language="nl", provider="subdl",
            score=score, timestamp=timestamp,
            subtitles_path="/movies/roofman/roofman.mkv.nl.srt",
            description="Dutch subtitles downloaded from subdl.",
            ai_translated=ai_translated,
        )
    else:
        candidate_id = 304
        _seed_episode(db)
        _episode_row(
            db, id=candidate_id, action=1, language="nl", provider="subdl",
            score=score, timestamp=timestamp,
            subtitles_path="/series/show/s01e01.mkv.nl.srt",
            description="Dutch subtitles downloaded from subdl.",
            ai_translated=ai_translated,
        )
    return candidate_id


def _selected_upgrade_candidates(media):
    from subtitles.upgrade import (
        get_upgradable_episode_subtitles,
        get_upgradable_movies_subtitles,
    )

    return (get_upgradable_movies_subtitles() if media == "movie"
            else get_upgradable_episode_subtitles())


@pytest.mark.parametrize("media", ["episode", "movie"])
@pytest.mark.parametrize("score_offset", [1, 0, -42])
def test_high_score_ai_provider_rows_keep_legacy_exclusion_when_penalty_is_zero(
        upgrade_db, monkeypatch, media, score_offset):
    from subtitles import upgrade

    monkeypatch.setattr(upgrade.settings.general, "ai_translated_score_penalty", 0, raising=False)
    score_out_of = MOVIE_MAX_SCORE if media == "movie" else 360
    _seed_provider_upgrade_candidate(
        upgrade_db, media, score=score_out_of - score_offset, ai_translated=True)

    assert _selected_upgrade_candidates(media) == {}


@pytest.mark.parametrize("media", ["episode", "movie"])
@pytest.mark.parametrize("penalty", [None, "10", True, -1, 101, "missing"])
def test_invalid_or_missing_ai_penalty_keeps_legacy_score_filter(
        upgrade_db, monkeypatch, media, penalty):
    from subtitles import upgrade

    if penalty == "missing":
        monkeypatch.delattr(upgrade.settings.general, "ai_translated_score_penalty", raising=False)
    else:
        monkeypatch.setattr(upgrade.settings.general, "ai_translated_score_penalty", penalty, raising=False)
    score_out_of = MOVIE_MAX_SCORE if media == "movie" else 360
    _seed_provider_upgrade_candidate(
        upgrade_db, media, score=score_out_of - 1, ai_translated=True)

    assert _selected_upgrade_candidates(media) == {}


@pytest.mark.parametrize("media", ["episode", "movie"])
def test_ai_candidate_becomes_eligible_when_penalty_changes_from_zero(
        upgrade_db, monkeypatch, media):
    from subtitles import upgrade

    general_settings = upgrade.settings.general
    monkeypatch.setattr(general_settings, "ai_translated_score_penalty", 0, raising=False)
    score_out_of = MOVIE_MAX_SCORE if media == "movie" else 360
    candidate_id = _seed_provider_upgrade_candidate(
        upgrade_db, media, score=score_out_of, ai_translated=True)

    assert _selected_upgrade_candidates(media) == {}

    general_settings.ai_translated_score_penalty = 1
    assert _selected_upgrade_candidates(media) == {candidate_id: None}


@pytest.mark.parametrize("media", ["episode", "movie"])
def test_ai_candidate_remains_inside_the_existing_upgrade_time_window(
        upgrade_db, monkeypatch, media):
    from subtitles import upgrade

    monkeypatch.setattr(upgrade.settings.general, "ai_translated_score_penalty", 1, raising=False)
    score_out_of = MOVIE_MAX_SCORE if media == "movie" else 360
    _seed_provider_upgrade_candidate(
        upgrade_db, media, score=score_out_of + 42, ai_translated=True,
        timestamp=datetime.now() - timedelta(days=366))

    assert _selected_upgrade_candidates(media) == {}


@pytest.mark.parametrize("media", ["episode", "movie"])
def test_human_subtitle_must_beat_hash_inflated_ai_history_score(
        upgrade_db, monkeypatch, media):
    """A selected AI row keeps its stored score as the replacement floor."""
    from subtitles import upgrade

    monkeypatch.setattr(upgrade.settings.general, "ai_translated_score_penalty", 1, raising=False)
    score_out_of = MOVIE_MAX_SCORE if media == "movie" else 360
    ai_score = score_out_of + 42
    _seed_provider_upgrade_candidate(upgrade_db, media, score=ai_score)

    path = ("/movies/roofman/roofman.mkv" if media == "movie"
            else "/series/show/s01e01.mkv")
    listing = _listing_result(path)
    listing.score = ai_score + 1
    provider = _FakeProvider(listing, listing_score=ai_score)
    monkeypatch.setattr(upgrade, "generate_subtitles", provider)
    notifications = []

    if media == "movie":
        run_upgrade = upgrade.upgrade_movies_subtitles
        monkeypatch.setattr(
            upgrade, "send_notifications_movie", lambda *a, **k: notifications.append(a))
    else:
        run_upgrade = upgrade.upgrade_episodes_subtitles
        monkeypatch.setattr(upgrade, "history_log", lambda *a, **k: None)
        monkeypatch.setattr(
            upgrade, "send_notifications", lambda *a, **k: notifications.append(a))

    run_upgrade(job_id="job-1")

    assert notifications == [], "a human subtitle tied with the stored AI score must not replace it"

    # The first raw score above the stored integer score is accepted.
    provider.listing_score = ai_score + 1
    run_upgrade(job_id="job-1")

    assert len(notifications) == 1, "a human subtitle above the stored AI score must replace it"

    provider.listing_score = ai_score + 2
    run_upgrade(job_id="job-1")

    assert len(notifications) == 2, "higher human scores must remain eligible"


# --------------------------------------------------------------------------
# The wanted scan. Its "already translated" guard keys on the translated file
# still existing, and replacing that file is exactly what the upgrade does, so
# the guard fails after every replacement and the translation comes straight
# back. The language is not really missing once a real subtitle covers it.
# --------------------------------------------------------------------------

def _wanted_movie_env(upgrade_db, monkeypatch):
    """_wanted_movie with everything but the translation decision stubbed."""
    from subtitles.wanted import movies as wanted

    monkeypatch.setattr(wanted, "database", upgrade_db)
    monkeypatch.setattr(
        wanted, "get_profiles_list",
        lambda profile_id=None: {
            "items": [{"language": "nl", "hi": "True", "forced": "False",
                       "translate_from": "en"}]
        })
    monkeypatch.setattr(wanted, "get_audio_profile_languages", lambda *a, **k: [])
    monkeypatch.setattr(wanted, "is_search_active", lambda **k: False)
    monkeypatch.setattr(wanted.path_mappings, "path_replace_movie", lambda p: p)
    monkeypatch.setattr(wanted.path_mappings, "path_replace", lambda p: p)
    monkeypatch.setattr(wanted, "generate_subtitles", lambda *a, **k: [])
    monkeypatch.setattr(
        wanted, "settings",
        SimpleNamespace(general=SimpleNamespace(use_whisper_fallback=False),
                       translator=SimpleNamespace(min_source_score=90)))
    monkeypatch.setattr(
        wanted, "jobs_queue",
        SimpleNamespace(_is_an_existing_job=lambda **k: False))

    queued = []
    monkeypatch.setattr("subtitles.tools.translate.main.translate_subtitles_file",
                        lambda **k: queued.append(k))
    return wanted, queued


def _wanted_movie_base(upgrade_db, tmp_path, subtitles):
    """Seeds the movie plus the English source the scan translates from.

    The lookup that finds the translation source checks the filesystem, so the
    English file has to be real. The translated file is deliberately never
    written: the upgrade deleted it, which is what the guard reacts to.
    """
    now = datetime.now()
    english = tmp_path / "roofman.mkv.en.srt"
    english.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    _seed_movie(upgrade_db, subtitles)
    _movie_row(
        upgrade_db, id=100, action=1, language="en", provider="opensubtitlescom",
        score=MOVIE_MAX_SCORE, timestamp=now - timedelta(hours=60),
        subtitles_path=str(english),
        description="English subtitles downloaded from opensubtitlescom with a score of 100%.",
    )
    _first_translation(upgrade_db, now)


def test_wanted_scan_keeps_a_movie_quiet_when_a_provider_file_covers_the_language(
        upgrade_db, monkeypatch, tmp_path):
    """The upgrade deleted the translated file and left a real one behind.

    Re-translating here is what hands the next upgrade a 50% baseline, so the
    provider file on disk has to end the question even though the translated
    path the history points at is gone.
    """
    wanted, queued = _wanted_movie_env(upgrade_db, monkeypatch)
    provider_file = tmp_path / "roofman.mkv.nl.srt"
    provider_file.write_text("1\n00:00:01,000 --> 00:00:02,000\nHallo\n")
    _wanted_movie_base(upgrade_db, tmp_path, (
        f'[["en", "{tmp_path / "roofman.mkv.en.srt"}", 999],'
        f' ["nl", "{provider_file}", 4321]]'))
    _provider_replacement(upgrade_db, datetime.now())

    movie = upgrade_db.execute(
        TableMovies.__table__.select().where(TableMovies.id == 30)).first()
    wanted._wanted_movie(movie, [])

    assert queued == [], "the language is served, so nothing needs translating"


def test_wanted_scan_still_translates_while_no_provider_file_covers_the_language(
        upgrade_db, monkeypatch, tmp_path):
    """The guard must not swallow a language that really is missing.

    Only the translated file was ever there, and the upgrade removed it: the
    language is missing again and the translation has to be re-queued.
    """
    wanted, queued = _wanted_movie_env(upgrade_db, monkeypatch)
    _wanted_movie_base(upgrade_db, tmp_path, (
        f'[["en", "{tmp_path / "roofman.mkv.en.srt"}", 999],'
        f' ["nl:hi", "{tmp_path / "roofman.mkv.nl.hi.srt"}", 1234]]'))

    movie = upgrade_db.execute(
        TableMovies.__table__.select().where(TableMovies.id == 30)).first()
    wanted._wanted_movie(movie, [])

    assert len(queued) == 1, "the translated file is gone, so translate again"


def _wanted_episode_env(upgrade_db, monkeypatch):
    """_wanted_episode with everything but the translation decision stubbed."""
    from subtitles.wanted import series as wanted

    monkeypatch.setattr(wanted, "database", upgrade_db)
    monkeypatch.setattr(
        wanted, "get_profiles_list",
        lambda profile_id=None: {
            "items": [{"language": "nl", "hi": "True", "forced": "False",
                       "translate_from": "en"}]
        })
    monkeypatch.setattr(wanted, "get_audio_profile_languages", lambda *a, **k: [])
    monkeypatch.setattr(wanted, "is_search_active", lambda **k: False)
    monkeypatch.setattr(wanted.path_mappings, "path_replace_movie", lambda p: p)
    monkeypatch.setattr(wanted.path_mappings, "path_replace", lambda p: p)
    monkeypatch.setattr(wanted, "generate_subtitles", lambda *a, **k: [])
    monkeypatch.setattr(
        wanted, "settings",
        SimpleNamespace(general=SimpleNamespace(use_whisper_fallback=False),
                       translator=SimpleNamespace(min_source_score=90)))
    monkeypatch.setattr(
        wanted, "jobs_queue",
        SimpleNamespace(_is_an_existing_job=lambda **k: False))
    monkeypatch.setattr(wanted, "get_providers", lambda: ["opensubtitlescom"])

    queued = []
    monkeypatch.setattr("subtitles.tools.translate.main.translate_subtitles_file",
                        lambda **k: queued.append(k))
    return wanted, queued


def _wanted_episode_base(upgrade_db, tmp_path, subtitles):
    now = datetime.now()
    english = tmp_path / "s01e01.mkv.en.srt"
    english.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    _seed_episode(upgrade_db, subtitles)
    _episode_row(
        upgrade_db, id=200, action=1, language="en", provider="opensubtitlescom",
        score=360, timestamp=now - timedelta(hours=60),
        subtitles_path=str(english),
        description="English subtitles downloaded from opensubtitlescom with a score of 100%.",
    )
    _episode_row(
        upgrade_db, id=201, action=6, language="nl:hi", provider=None,
        score=EPISODE_TRANSLATED_SCORE, timestamp=now - timedelta(hours=40),
        subtitles_path=str(tmp_path / "s01e01.mkv.nl.hi.srt"),
        description="English subtitles translated to Dutch.",
    )


def test_wanted_scan_keeps_an_episode_quiet_when_a_provider_file_covers_the_language(
        upgrade_db, monkeypatch, tmp_path):
    wanted, queued = _wanted_episode_env(upgrade_db, monkeypatch)
    provider_file = tmp_path / "s01e01.mkv.nl.srt"
    provider_file.write_text("1\n00:00:01,000 --> 00:00:02,000\nHallo\n")
    _wanted_episode_base(upgrade_db, tmp_path, (
        f'[["en", "{tmp_path / "s01e01.mkv.en.srt"}", 999],'
        f' ["nl", "{provider_file}", 4321]]'))
    _episode_row(
        upgrade_db, id=202, action=3, language="nl", provider="opensubtitlescom",
        score=268, timestamp=datetime.now() - timedelta(hours=28),
        subtitles_path=str(provider_file),
        description="Dutch subtitles upgraded from opensubtitlescom with a score of 74.44%.",
    )

    wanted.wanted_download_subtitles(20)

    assert queued == [], "the language is served, so nothing needs translating"


def test_wanted_scan_still_translates_an_episode_with_no_provider_file(
        upgrade_db, monkeypatch, tmp_path):
    wanted, queued = _wanted_episode_env(upgrade_db, monkeypatch)
    _wanted_episode_base(upgrade_db, tmp_path, (
        f'[["en", "{tmp_path / "s01e01.mkv.en.srt"}", 999],'
        f' ["nl:hi", "{tmp_path / "s01e01.mkv.nl.hi.srt"}", 1234]]'))

    wanted.wanted_download_subtitles(20)

    assert len(queued) == 1, "the translated file is gone, so translate again"

# --------------------------------------------------------------------------
# Which indexed file counts as covering the language. This decides whether the
# scan stays quiet, so it has to agree with how list_missing_subtitles reads the
# same entries, or the two disagree about whether the language is missing.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "code, exists, is_translated, expected",
    [
        # a plain provider file covers the language
        ("nl", True, False, True),
        # HI counts as good as plain, the way the missing list treats it
        ("nl:hi", True, False, True),
        # forced does not: it covers only the foreign-language inserts
        ("nl:forced", True, False, False),
        # a combined artifact is a bilingual stack, not a Dutch subtitle
        ("nl:combined-en", True, False, False),
        # another language never covers it
        ("fr", True, False, False),
        # an indexed path that is no longer on disk covers nothing
        ("nl", False, False, False),
        # the translated file itself is not a provider file
        ("nl:hi", True, True, False),
    ],
)
def test_provider_file_on_disk_reads_a_variant_the_way_the_indexer_does(
        tmp_path, code, exists, is_translated, expected):
    from subtitles.wanted.utils import _provider_file_on_disk

    translated = str(tmp_path / "roofman.mkv.nl.hi.srt")
    path = translated if is_translated else str(tmp_path / f"roofman.mkv.{code}.srt")
    if exists:
        pathlib.Path(path).write_text("1\n")

    subtitles = f'[["{code}", "{path}", 1234]]'
    translated_path = translated if is_translated else None

    assert _provider_file_on_disk(subtitles, "nl", translated_path) is expected


def test_the_same_listing_is_not_downloaded_twice_for_an_episode(upgrade_db, monkeypatch):
    """The movie acceptance test, on the episode path: two passes, one download."""
    from subtitles import upgrade

    now = datetime.now()
    _seed_episode(upgrade_db, (
        '[["nl", "/series/show/s01e01.mkv.nl.srt", 4321],'
        ' ["nl:hi", "/series/show/s01e01.mkv.nl.hi.srt", 1234]]'))
    _episode_row(
        upgrade_db, id=201, action=6, language="nl:hi", provider=None,
        score=EPISODE_TRANSLATED_SCORE, timestamp=now - timedelta(hours=40),
        subtitles_path="/series/show/s01e01.mkv.nl.hi.srt",
        description="English subtitles translated to Dutch.",
    )

    listing = _listing_result("/series/show/s01e01.mkv")
    listing.score = EPISODE_LISTING_SCORE
    provider = _FakeProvider(listing, listing_score=EPISODE_LISTING_SCORE)
    monkeypatch.setattr(upgrade, "generate_subtitles", provider)
    monkeypatch.setattr(upgrade, "history_log", lambda *a, **k: None)
    notifications = []
    monkeypatch.setattr(upgrade, "send_notifications",
                        lambda *a, **k: notifications.append(a))

    upgrade.upgrade_episodes_subtitles(job_id="job-1")

    assert provider.bars == [EPISODE_TRANSLATED_SCORE + 1], "the translation is the baseline"
    assert len(notifications) == 1

    _episode_row(
        upgrade_db, id=202, action=3, language="nl", provider="opensubtitlescom",
        score=EPISODE_LISTING_SCORE, timestamp=now - timedelta(hours=28),
        subtitles_path="/series/show/s01e01.mkv.nl.srt",
        description="Dutch subtitles upgraded from opensubtitlescom with a score of 74.44%.",
    )
    _episode_row(
        upgrade_db, id=203, action=6, language="nl:hi", provider=None,
        score=EPISODE_TRANSLATED_SCORE, timestamp=now - timedelta(hours=16),
        subtitles_path="/series/show/s01e01.mkv.nl.hi.srt",
        description="English subtitles translated to Dutch.",
    )
    notifications.clear()

    upgrade.upgrade_episodes_subtitles(job_id="job-1")

    assert notifications == [], "the same listing must not be downloaded again"
    assert provider.bars[1:] == [EPISODE_LISTING_SCORE + 1], \
        "the provider row is the baseline now, and its bar is above the listing"
