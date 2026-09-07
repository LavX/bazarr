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

Both lookups also have to be scoped to the owning instance (#156). ``radarrId``
and ``sonarrSeriesId`` are unique only together with ``arr_instance_id``
(``ux_table_movies_instance_upstream_id``), so an unscoped query can hand back a
sibling instance's media and label the job, and prompt the provider, with the
wrong film.
"""
import pytest

from app.database import TableEpisodes, TableMovies, TableShows
from subtitles.tools.translate.core import translator_utils


@pytest.fixture
def library(schema_session, monkeypatch):
    """Two Radarr/Sonarr instances that reuse the same upstream ids."""
    monkeypatch.setattr(translator_utils, "database", schema_session)
    schema_session.add(
        TableMovies(
            arr_instance_id=1,
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
            arr_instance_id=1,
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
            arr_instance_id=1,
            sonarrEpisodeId=42,
            sonarrSeriesId=3,
            season=2,
            episode=5,
            title="Superposition",
            path="/tv/dark-matter/s02e05.mkv",
            monitored="True",
        )
    )
    # The 4K instance reuses the same upstream ids for different media.
    schema_session.add(
        TableMovies(
            arr_instance_id=2,
            radarrId=7,
            tmdbId="67890",
            title="Sorcerer",
            year="1977",
            imdbId="tt0076740",
            overview="Four men drive nitroglycerin through the jungle.",
            path="/movies-4k/sorcerer.mkv",
            tags="[]",
            monitored="True",
        )
    )
    schema_session.add(
        TableShows(
            arr_instance_id=2,
            sonarrSeriesId=3,
            tvdbId=1000,
            title="Severance",
            year="2022",
            imdbId="tt11280740",
            overview="Work you cannot remember.",
            path="/tv-4k/severance",
            tags="[]",
            monitored="True",
            seriesType="standard",
        )
    )
    schema_session.add(
        TableEpisodes(
            arr_instance_id=2,
            sonarrEpisodeId=42,
            sonarrSeriesId=3,
            season=1,
            episode=9,
            title="The We We Are",
            path="/tv-4k/severance/s01e09.mkv",
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


@pytest.mark.parametrize(
    "instance, expected", [(1, "The Quiet Earth"), (2, "Sorcerer")]
)
def test_movie_title_comes_from_the_owning_instance(library, instance, expected):
    # radarrId 7 exists on both instances; only the pair identifies the film.
    assert (
        translator_utils.get_title("movie", radarr_id=7, arr_instance_id=instance)
        == expected
    )


@pytest.mark.parametrize(
    "instance, expected", [(1, "The last man alive."), (2, "nitroglycerin")]
)
def test_movie_description_comes_from_the_owning_instance(library, instance, expected):
    description = translator_utils.get_description(
        "movie", radarr_id=7, sonarr_series_id=None, arr_instance_id=instance
    )
    assert expected in description


@pytest.mark.parametrize(
    "instance, expected",
    [(1, "Dark Matter - S02E05 - Superposition"), (2, "Severance - S01E09 - The We We Are")],
)
def test_episode_title_comes_from_the_owning_instance(library, instance, expected):
    assert (
        translator_utils.get_title(
            "episode", sonarr_series_id=3, sonarr_episode_id=42, arr_instance_id=instance
        )
        == expected
    )


def test_series_description_comes_from_the_owning_instance(library):
    description = translator_utils.get_description(
        "episode", radarr_id=None, sonarr_series_id=3, arr_instance_id=2
    )
    assert "Severance" in description


def test_an_instance_that_owns_nothing_gives_an_empty_title(library):
    assert translator_utils.get_title("movie", radarr_id=7, arr_instance_id=99) == ""


# --- the owner has to survive the trip from the endpoint to the lookup --------


@pytest.mark.parametrize(
    "module_name, service_name, helper",
    [
        ("openrouter_translator", "OpenRouterTranslatorService", "get_title"),
        ("lingarr_translator", "LingarrTranslatorService", "get_title"),
        ("gemini_translator", "GeminiTranslatorService", "get_description"),
    ],
)
def test_translator_services_pass_the_owner_to_the_lookup(
    monkeypatch, module_name, service_name, helper
):
    import importlib

    module = importlib.import_module(
        f"subtitles.tools.translate.services.{module_name}"
    )
    seen = {}

    def _capture(*args, **kwargs):
        seen.update(kwargs)
        seen["positional"] = args
        return ""

    monkeypatch.setattr(module, helper, _capture)

    service = getattr(module, service_name)(
        source_srt_file="in.srt",
        dest_srt_file="out.srt",
        lang_obj=None,
        to_lang="hun",
        from_lang="en",
        media_type="movie",
        video_path="/movies/x.mkv",
        orig_to_lang="hu",
        forced=False,
        hi=False,
        sonarr_series_id=None,
        sonarr_episode_id=None,
        radarr_id=7,
        arr_instance_id=2,
    )

    assert service.arr_instance_id == 2


def test_translate_subtitles_file_labels_the_job_with_the_owning_instance(monkeypatch):
    from subtitles.tools.translate import main as translate_main

    seen = {}

    def _capture(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "The Quiet Earth"

    monkeypatch.setattr(translate_main, "get_title", _capture)
    monkeypatch.setattr(
        translate_main.jobs_queue,
        "add_job_from_function",
        lambda *a, **k: seen.setdefault("label", a[0]),
    )

    translate_main.translate_subtitles_file(
        video_path="/movies/x.mkv",
        source_srt_file="in.srt",
        from_lang="en",
        to_lang="hu",
        forced=False,
        hi=False,
        media_type="movie",
        sonarr_series_id=None,
        sonarr_episode_id=None,
        radarr_id=7,
        metadata={},
        job_id=None,
        arr_instance_id=2,
    )

    assert seen["label"] == "Translating The Quiet Earth (EN to HU)"
    passed = list(seen["args"]) + list(seen["kwargs"].values())
    assert 2 in passed
