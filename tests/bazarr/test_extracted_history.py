# coding=utf-8
"""History action 8 (Extracted) in every consumer of the action codes.

An extracted file is source quality: the upgrade job never takes it as a
candidate, and it ends the candidacy of an older download row of the same
video and language, whose file it now stands in for. Discover counts it as an
arrival but not as a fetch, and the quality statistics leave it out like an
upload.
"""
# ruff: noqa: F811
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from app.database import TableHistory, TableHistoryMovie
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_upgrade_embedded_history import _insert_episode, _insert_movie


@pytest.fixture
def upgrade_db(hidden_db, monkeypatch):
    """The upgrade queries on an AUTOCOMMIT engine of each kind, SQLite and PostgreSQL."""
    from api.subtitles import batch
    import app.database as database_module
    from subtitles import upgrade

    settings = SimpleNamespace(general=SimpleNamespace(
        days_to_upgrade_subs=365, upgrade_manual=False, upgrade_translated=False, upgrade_subs=True,
        use_sportarr=False))
    for module in (upgrade, batch):
        monkeypatch.setattr(module, "database", hidden_db)
        monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(database_module, "settings", SimpleNamespace(
        radarr=SimpleNamespace(excluded_tags=[], only_monitored=False),
        sonarr=SimpleNamespace(excluded_series_types=[], excluded_tags=[], exclude_season_zero=False,
                               only_monitored=False)))
    return hidden_db


def _movie_row(action, minutes_ago, score, history_id, path="/movies/movie.en.srt"):
    return dict(id=history_id, arr_instance_id=2, movie_id=30, radarrId=30, action=action,
                description="x", language="en", provider="opensubtitlescom" if action != 8 else "embedded",
                score=score, score_out_of=180, timestamp=datetime.now() - timedelta(minutes=minutes_ago),
                video_path="/movies/movie.mkv", subtitles_path=path)


def _episode_row(action, minutes_ago, score, history_id):
    return dict(id=history_id, arr_instance_id=1, series_id=10, episode_id=20, sonarrEpisodeId=20,
                sonarrSeriesId=10, action=action, description="x", language="en",
                provider="opensubtitlescom" if action != 8 else "embedded", score=score, score_out_of=360,
                timestamp=datetime.now() - timedelta(minutes=minutes_ago), video_path="/series/show/s01e01.mkv",
                subtitles_path="/series/show/s01e01.en.srt")


def test_an_extracted_file_is_never_an_upgrade_candidate(upgrade_db):
    from subtitles.upgrade import get_upgradable_episode_subtitles, get_upgradable_movies_subtitles

    _insert_movie(upgrade_db)
    _insert_episode(upgrade_db)
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(8, 1, 90, 201)))
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(8, 1, 90, 101)))

    assert get_upgradable_movies_subtitles() == {}
    assert get_upgradable_episode_subtitles() == {}


def test_extracting_ends_an_older_download_s_candidacy(upgrade_db):
    """The download's file was deleted (an extraction refuses to overwrite
    one). Without the newer row the upgrade job would fall back to the file
    for that language, the extracted one, and replace it."""
    from api.subtitles.batch import get_upgradable_media_ids
    from subtitles.upgrade import get_upgradable_episode_subtitles, get_upgradable_movies_subtitles

    _insert_movie(upgrade_db)
    _insert_episode(upgrade_db)
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(1, 60, 90, 201)))
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(1, 60, 90, 101)))
    assert get_upgradable_movies_subtitles() == {201: None}
    assert get_upgradable_episode_subtitles() == {101: None}

    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(8, 1, 180, 202)))
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(8, 1, 360, 102)))

    assert get_upgradable_movies_subtitles() == {}
    assert get_upgradable_episode_subtitles() == {}
    assert get_upgradable_media_ids()["movies"] == [] and get_upgradable_media_ids()["series"] == []


def test_a_download_after_the_extraction_is_a_candidate_again(upgrade_db):
    from subtitles.upgrade import get_upgradable_movies_subtitles

    _insert_movie(upgrade_db)
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(8, 60, 180, 201)))
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(1, 1, 90, 202)))

    assert get_upgradable_movies_subtitles() == {202: None}


def test_discover_counts_an_extraction_as_an_arrival_not_a_fetch():
    from api.history.metrics import ACQUISITION_ACTIONS
    from discover.summary import ARRIVAL_ACTIONS, FETCHED_ACTIONS
    from subtitles.upgrade import get_queries_condition_parameters

    assert 8 in ARRIVAL_ACTIONS
    assert 8 not in FETCHED_ACTIONS
    assert 8 not in ACQUISITION_ACTIONS
    assert 8 not in get_queries_condition_parameters()[1]


@pytest.fixture
def upgrade_run(upgrade_db, monkeypatch):
    """The upgrade jobs up to the download they would start, which is recorded."""
    from subtitles import upgrade

    calls = []
    monkeypatch.setattr(upgrade, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kw: None, update_job_name=lambda **kw: None))
    monkeypatch.setattr(upgrade, "get_providers", lambda: ["opensubtitlescom"])
    monkeypatch.setattr(upgrade, "_language_still_desired", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "_is_hi_required", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "get_audio_profile_languages", lambda audio: [])
    monkeypatch.setattr(upgrade.path_mappings, "path_replace", lambda path: path)
    monkeypatch.setattr(upgrade.path_mappings, "path_replace_movie", lambda path: path)
    monkeypatch.setattr(upgrade, "generate_subtitles",
                        lambda *args, **kwargs: calls.append(kwargs["previous_subtitles_to_delete"]) or [])
    return upgrade, calls


@pytest.mark.parametrize("download_language,extracted_language", [("en:hi", "en"), ("en", "en:hi")])
def test_the_fallback_never_takes_over_an_extracted_file_of_another_variant(upgrade_run, download_language,
                                                                             extracted_language):
    """A download row of one variant whose file is gone, then an extraction of
    the other: the download row is still the latest of its own key, and the
    fallback, which treats HI and plain as one, would adopt the extracted
    file and delete it as the file the upgrade replaces."""
    from app.database import TableEpisodes, TableMovies
    from sqlalchemy import update

    upgrade, calls = upgrade_run
    upgrade_db = upgrade.database
    _insert_movie(upgrade_db)
    _insert_episode(upgrade_db)
    extracted_movie = "/movies/movie.en.hi.srt" if extracted_language == "en:hi" else "/movies/movie.en.srt"
    extracted_episode = extracted_movie.replace("/movies/movie", "/series/show/s01e01")
    upgrade_db.execute(update(TableMovies).values(subtitles=str([["en", extracted_movie, 10]])))
    upgrade_db.execute(update(TableEpisodes).values(subtitles=str([["en", extracted_episode, 10]])))
    gone_movie = "/movies/movie.en.hi.srt" if download_language == "en:hi" else "/movies/movie.en.srt"
    gone_movie = gone_movie.replace(".srt", ".gone.srt")
    upgrade_db.execute(insert(TableHistoryMovie).values(**dict(
        _movie_row(1, 60, 90, 201, path=gone_movie), language=download_language)))
    upgrade_db.execute(insert(TableHistoryMovie).values(**dict(
        _movie_row(8, 1, 180, 202, path=extracted_movie), language=extracted_language)))
    upgrade_db.execute(insert(TableHistory).values(**dict(
        _episode_row(1, 60, 90, 101), language=download_language,
        subtitles_path="/series/show/s01e01.gone.srt")))
    upgrade_db.execute(insert(TableHistory).values(**dict(
        _episode_row(8, 1, 360, 102), language=extracted_language, subtitles_path=extracted_episode)))
    assert 201 in upgrade.get_upgradable_movies_subtitles()
    assert 101 in upgrade.get_upgradable_episode_subtitles()

    upgrade.upgrade_movies_subtitles(job_id=1)
    upgrade.upgrade_episodes_subtitles(job_id=1)

    assert calls == []


def test_the_fallback_still_upgrades_a_download_s_renamed_file(upgrade_run):
    """The fallback's own case, a download whose file was renamed: still an
    upgrade, replacing the file the download left."""
    from app.database import TableMovies
    from sqlalchemy import update

    upgrade, calls = upgrade_run
    upgrade_db = upgrade.database
    _insert_movie(upgrade_db)
    upgrade_db.execute(update(TableMovies).values(subtitles=str([["en", "/movies/movie.en.srt", 10]])))
    upgrade_db.execute(insert(TableHistoryMovie).values(**dict(
        _movie_row(1, 60, 90, 201, path="/movies/movie.en.hi.srt"), language="en:hi")))

    upgrade.upgrade_movies_subtitles(job_id=1)

    assert calls == ["/movies/movie.en.srt"]
