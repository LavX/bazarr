# coding=utf-8
"""Mass extract: every embedded track of the selection written out, one after another.

Collected from the stored listing, like every mass action, so a track the
user hid is never extracted. The database cases run on SQLite and PostgreSQL,
both on an AUTOCOMMIT engine.
"""
# ruff: noqa: F811
from types import SimpleNamespace

import pytest

from app.database import TableEpisodes, TableMovies, TableShows, TableSportsEvents
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_embedded_track_hidden_indexer import _stub_indexer


@pytest.fixture
def mass(hidden_db, monkeypatch):
    from app.database import TableArrInstances
    from subtitles import mass_operations

    monkeypatch.setattr(mass_operations, "database", hidden_db)
    monkeypatch.setattr(mass_operations.path_mappings, "path_replace_instance",
                        lambda path, owner, kind: f"/local{owner}{path}")
    hidden_db.add_all([
        TableArrInstances(id=1, kind="radarr", name="R", stable_key="r", port=1, enabled=1),
        TableArrInstances(id=2, kind="sonarr", name="S", stable_key="s", port=1, enabled=1),
        TableArrInstances(id=3, kind="sportarr", name="P", stable_key="p", port=1, enabled=1),
    ])
    hidden_db.commit()
    return mass_operations, hidden_db


def _items(mass_operations, request):
    items, skipped = mass_operations._collect_subtitle_items(request, "extract", {})
    return [(item["srt_lang"], item["hi"], item["forced"], item["codec"], item["arr_instance_id"],
             item["radarr_id"] or item["sonarr_episode_id"] or item.get("sports_event_id"))
            for item in items], skipped


def test_mass_extract_never_extracts_a_hidden_track(mass, monkeypatch):
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track

    mass_operations, session = mass
    _stub_indexer(mv, session, monkeypatch, [])
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv",
                            tmdbId="1", movie_file_id=700, file_size=111, subtitles="[]"))
    session.commit()
    hide_track(session, "movie", 1, 1, "fr")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)

    items, _skipped = _items(mass_operations, [{"type": "movie", "radarrId": 7, "arr_instance_id": 1}])

    assert [(language, hi) for language, hi, *_rest in items] == [("en", False), ("en", True)]


def test_each_embedded_track_without_a_file_is_one_item(mass):
    mass_operations, session = mass
    session.add_all([
        TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                    subtitles=str([["en", None, None, "subrip"], ["en", None, None, "pgs"], ["fr:forced", None, None],
                                   ["de", "/movies/m.de.srt", 10], ["de", None, None, "subrip"]])),
        TableShows(id=1, arr_instance_id=2, sonarrSeriesId=5, title="S", path="/tv"),
    ])
    session.flush()
    session.add(TableEpisodes(id=1, arr_instance_id=2, series_id=1, sonarrSeriesId=5, sonarrEpisodeId=50,
                              title="E", season=1, episode=1, path="/tv/e.mkv",
                              subtitles=str([["hu:hi", None, None, "vobsub"]])))
    session.commit()

    items, skipped = _items(mass_operations, [{"type": "movie", "radarrId": 7, "arr_instance_id": 1},
                                              {"type": "series", "sonarrSeriesId": 5, "arr_instance_id": 2}])

    assert items == [
        ("hu", True, False, "vobsub", 2, 50),
        ("en", False, False, "subrip", 1, 7),
        ("en", False, False, "pgs", 1, 7),
        # A forced track is extracted too: nothing is converted, only copied.
        ("fr", False, True, None, 1, 7),
    ]
    # German already has a file.
    assert skipped == 1


def test_sports_events_are_collected_under_their_owner(mass):
    from app.database import TableSportsLeagues

    mass_operations, session = mass
    session.add(TableSportsLeagues(id=51, arr_instance_id=3, sportarrLeagueId=7, title="L"))
    session.flush()
    session.add(TableSportsEvents(id=61, arr_instance_id=3, league_id=51, sportarrEventId=8, file_id=9, title="Card",
                                  path="/sports/event.mkv", subtitles=str([["fr", None, None, "subrip"]])))
    session.commit()

    assert _items(mass_operations, [{"type": "sports", "sportsEventId": 61, "arr_instance_id": 3}]) == (
        [("fr", False, False, "subrip", 3, 61)], 0)


def test_the_batch_job_is_named_after_the_action():
    from api.subtitles.batch import ACTION_LABELS

    assert ACTION_LABELS["extract"] == "Extracting Embedded Tracks"


def test_each_item_runs_the_primitive_and_a_taken_name_is_a_skip(mass, monkeypatch):
    from app.jobs_queue import JobFailed
    from subtitles.new_subtitle import SubtitleFileExists
    import subtitles.tools.embedded_extract as extract

    mass_operations, session = mass
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                            subtitles=str([["en", None, None, "subrip"], ["hu", None, None, "pgs"],
                                           ["fr", None, None]])))
    session.commit()
    calls = []

    def materialise(media_type, media_id, owner, language, hi=False, forced=False, codec=None):
        calls.append((media_type, media_id, owner, language, codec))
        if language == "hu":
            raise SubtitleFileExists("/local1/movies/m.hu.sup")
        if language == "fr":
            raise extract.NotExtractable("Could not extract the French track.")
        return ["/local1/movies/m.en.srt"]

    monkeypatch.setattr(extract, "materialise_embedded_track", materialise)
    monkeypatch.setattr(mass_operations, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kw: None, update_job_name=lambda **kw: None))

    with pytest.raises(JobFailed, match="1 of 3 items failed .1 done, 1 skipped.*French track"):
        mass_operations.mass_batch_operation([{"type": "movie", "radarrId": 7, "arr_instance_id": 1}],
                                             "extract", job_id=5)
    assert calls == [("movie", 7, 1, "en", "subrip"), ("movie", 7, 1, "hu", "pgs"), ("movie", 7, 1, "fr", None)]
