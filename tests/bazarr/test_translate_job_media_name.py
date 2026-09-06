# coding=utf-8
"""Translation job labels have to name the media, movies included.

``translate_subtitles_file`` and the AI translator both build their label from
``get_title``, and the LLM prompt gets its context from ``get_description``.
Both branch on ``media_type``, and the callers do not agree on how to spell it:
the manual translate endpoint sends "movie" while the batch paths send
"movies". Only the plural matched, so a movie translated from its detail page
fell through to the series branch, found no ``sonarr_series_id`` and produced an
empty title. The job then read "Translating HU to EN" with no film in it, the
Translation Jobs table showed only the language pair, and the prompt lost the
movie's overview.
"""
import pytest

from app.database import TableEpisodes, TableMovies, TableShows
from subtitles.tools.translate.core import translator_utils


@pytest.fixture
def library(schema_session, monkeypatch):
    monkeypatch.setattr(translator_utils, "database", schema_session)
    schema_session.add(
        TableMovies(
            radarrId=7,
            tmdbId="12345",
            title="The Quiet Earth",
            year="1985",
            imdbId="tt0089869",
            overview="The last man alive.",
            path="/movies/quiet.mkv",
            tags="[]",
            monitored="True",
        )
    )
    schema_session.add(
        TableShows(
            sonarrSeriesId=3,
            tvdbId=999,
            title="Dark Matter",
            year="2024",
            imdbId="tt1234567",
            overview="A man wakes in another life.",
            path="/tv/dark-matter",
            tags="[]",
            monitored="True",
            seriesType="standard",
        )
    )
    schema_session.add(
        TableEpisodes(
            sonarrEpisodeId=42,
            sonarrSeriesId=3,
            season=2,
            episode=5,
            title="Superposition",
            path="/tv/dark-matter/s02e05.mkv",
            monitored="True",
        )
    )
    schema_session.commit()
    return schema_session


@pytest.mark.parametrize("media_type", ["movie", "movies"])
def test_movie_title_is_found_however_the_caller_spells_the_media_type(library, media_type):
    assert translator_utils.get_title(media_type, radarr_id=7) == "The Quiet Earth"


@pytest.mark.parametrize("media_type", ["movie", "movies"])
def test_movie_description_is_found_however_the_caller_spells_the_media_type(library, media_type):
    description = translator_utils.get_description(media_type, radarr_id=7, sonarr_series_id=None)
    assert "The Quiet Earth" in description
    assert "The last man alive." in description


def test_unknown_movie_still_gives_an_empty_title(library):
    assert translator_utils.get_title("movie", radarr_id=999) == ""


def test_episode_title_still_carries_the_series_and_the_episode_number(library):
    title = translator_utils.get_title(
        "episode", sonarr_series_id=3, sonarr_episode_id=42
    )
    assert title == "Dark Matter - S02E05 - Superposition"


def test_series_description_is_unchanged(library):
    description = translator_utils.get_description(
        "episode", radarr_id=None, sonarr_series_id=3
    )
    assert "Dark Matter" in description
