# coding=utf-8
"""Hand uploads (history action 4) are final.

An upload replaces the file at its path, so it is never an upgrade candidate,
whatever upgrade_manual says. In both modes it also ends the candidacy of an
older row of the same video and language, the way an extraction does, and
retires an older translation of that language.
"""

# ruff: noqa: F811
import pytest
from sqlalchemy import insert

from app.database import TableHistory, TableHistoryMovie
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_extracted_history import _episode_row, _movie_row, upgrade_db  # noqa: F401
from test_upgrade_embedded_history import _insert_episode, _insert_movie


def _set_upgrade_manual(monkeypatch, enabled):
    from subtitles import upgrade

    monkeypatch.setattr(upgrade.settings.general, "upgrade_manual", enabled)


@pytest.mark.parametrize(
    "upgrade_manual", [True, False], ids=["manual-on", "manual-off"]
)
def test_an_upload_is_never_an_upgrade_candidate(
    upgrade_db, monkeypatch, upgrade_manual
):
    from subtitles.upgrade import (
        get_upgradable_episode_subtitles,
        get_upgradable_movies_subtitles,
    )

    _set_upgrade_manual(monkeypatch, upgrade_manual)
    _insert_movie(upgrade_db)
    _insert_episode(upgrade_db)
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(4, 1, 90, 201)))
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(4, 1, 90, 101)))

    assert get_upgradable_movies_subtitles() == {}
    assert get_upgradable_episode_subtitles() == {}


@pytest.mark.parametrize(
    "upgrade_manual", [True, False], ids=["manual-on", "manual-off"]
)
def test_a_download_replaced_by_a_later_upload_is_not_a_candidate(
    upgrade_db, monkeypatch, upgrade_manual
):
    from api.subtitles.batch import get_upgradable_media_ids
    from subtitles.upgrade import (
        get_upgradable_episode_subtitles,
        get_upgradable_movies_subtitles,
    )

    _set_upgrade_manual(monkeypatch, upgrade_manual)
    _insert_movie(upgrade_db)
    _insert_episode(upgrade_db)
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(1, 60, 90, 201)))
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(4, 1, 90, 202)))
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(1, 60, 90, 101)))
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(4, 1, 90, 102)))

    assert get_upgradable_movies_subtitles() == {}
    assert get_upgradable_episode_subtitles() == {}
    candidates = get_upgradable_media_ids()
    assert candidates["movies"] == [] and candidates["series"] == []


@pytest.mark.parametrize(
    "upgrade_manual", [True, False], ids=["manual-on", "manual-off"]
)
def test_downloads_of_another_language_and_unreplaced_downloads_stay_candidates(
    upgrade_db, monkeypatch, upgrade_manual
):
    from subtitles.upgrade import (
        get_upgradable_episode_subtitles,
        get_upgradable_movies_subtitles,
    )

    _set_upgrade_manual(monkeypatch, upgrade_manual)
    _insert_movie(upgrade_db)
    _insert_episode(upgrade_db)
    upgrade_db.execute(
        insert(TableHistoryMovie).values(
            **dict(
                _movie_row(1, 60, 90, 201, path="/movies/movie.es.srt"), language="es"
            )
        )
    )
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(1, 70, 90, 202)))
    upgrade_db.execute(
        insert(TableHistory).values(
            **dict(
                _episode_row(1, 60, 90, 101),
                language="es",
                subtitles_path="/series/show/s01e01.es.srt",
            )
        )
    )
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(1, 70, 90, 102)))

    assert get_upgradable_movies_subtitles() == {201: None, 202: None}
    assert get_upgradable_episode_subtitles() == {101: None, 102: None}


def _set_upgrade_translated(monkeypatch):
    from subtitles import upgrade

    monkeypatch.setattr(upgrade.settings.general, "upgrade_translated", True)


@pytest.mark.parametrize(
    "upgrade_manual", [True, False], ids=["manual-on", "manual-off"]
)
def test_an_upload_retires_an_older_translation_of_the_same_episode_language(
    upgrade_db, monkeypatch, upgrade_manual
):
    from subtitles.upgrade import get_upgradable_episode_subtitles

    _set_upgrade_manual(monkeypatch, upgrade_manual)
    _set_upgrade_translated(monkeypatch)
    _insert_episode(upgrade_db)
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(4, 60, 90, 101)))
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(6, 1, None, 102)))

    assert 102 not in get_upgradable_episode_subtitles()


@pytest.mark.parametrize(
    "upgrade_manual", [True, False], ids=["manual-on", "manual-off"]
)
def test_an_upload_retires_an_older_translation_of_the_same_movie_language(
    upgrade_db, monkeypatch, upgrade_manual
):
    from subtitles.upgrade import get_upgradable_movies_subtitles

    _set_upgrade_manual(monkeypatch, upgrade_manual)
    _set_upgrade_translated(monkeypatch)
    _insert_movie(upgrade_db)
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(4, 60, 90, 201)))
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(6, 1, None, 202)))

    assert 202 not in get_upgradable_movies_subtitles()


@pytest.mark.parametrize(
    "upgrade_manual", [True, False], ids=["manual-on", "manual-off"]
)
def test_a_translation_stays_a_candidate_when_the_upload_is_another_language(
    upgrade_db, monkeypatch, upgrade_manual
):
    from subtitles.upgrade import (
        get_upgradable_episode_subtitles,
        get_upgradable_movies_subtitles,
    )

    _set_upgrade_manual(monkeypatch, upgrade_manual)
    _set_upgrade_translated(monkeypatch)
    _insert_movie(upgrade_db)
    _insert_episode(upgrade_db)
    upgrade_db.execute(
        insert(TableHistoryMovie).values(
            **dict(
                _movie_row(4, 60, 90, 201, path="/movies/movie.es.srt"), language="es"
            )
        )
    )
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(6, 1, None, 202)))
    upgrade_db.execute(
        insert(TableHistory).values(
            **dict(
                _episode_row(4, 60, 90, 101),
                language="es",
                subtitles_path="/series/show/s01e01.es.srt",
            )
        )
    )
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(6, 1, None, 102)))

    assert set(get_upgradable_movies_subtitles()) == {202}
    assert set(get_upgradable_episode_subtitles()) == {102}


@pytest.mark.parametrize(
    "upgrade_manual", [True, False], ids=["manual-on", "manual-off"]
)
def test_an_upload_on_another_instance_does_not_retire_a_translation(
    upgrade_db, monkeypatch, upgrade_manual
):
    from subtitles.upgrade import (
        get_upgradable_episode_subtitles,
        get_upgradable_movies_subtitles,
    )

    _set_upgrade_manual(monkeypatch, upgrade_manual)
    _set_upgrade_translated(monkeypatch)
    _insert_movie(upgrade_db)
    _insert_episode(upgrade_db)
    upgrade_db.execute(
        insert(TableHistoryMovie).values(
            **dict(_movie_row(4, 60, 90, 201), arr_instance_id=3)
        )
    )
    upgrade_db.execute(insert(TableHistoryMovie).values(**_movie_row(6, 1, None, 202)))
    upgrade_db.execute(
        insert(TableHistory).values(
            **dict(_episode_row(4, 60, 90, 101), arr_instance_id=2)
        )
    )
    upgrade_db.execute(insert(TableHistory).values(**_episode_row(6, 1, None, 102)))

    assert set(get_upgradable_movies_subtitles()) == {202}
    assert set(get_upgradable_episode_subtitles()) == {102}
