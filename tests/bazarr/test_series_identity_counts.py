# coding=utf-8

import warnings

from flask import Flask


def test_series_detail_counts_are_scoped_by_local_series_id(schema_session, monkeypatch):
    from api.series import series
    from app.database import TableEpisodes, TableShows

    monkeypatch.setattr(series, "database", schema_session)
    monkeypatch.setattr(series, "get_exclusion_clause", lambda media_type: [])

    schema_session.add_all([
        TableShows(
            id=501,
            sonarrSeriesId=1,
            arr_instance_id=8,
            path="/series/alpha",
            title="Alpha",
            tags="[]",
        ),
        TableShows(
            id=502,
            sonarrSeriesId=1,
            arr_instance_id=9,
            path="/series/beta",
            title="Beta",
            tags="[]",
        ),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=601,
            series_id=501,
            sonarrEpisodeId=11,
            sonarrSeriesId=1,
            arr_instance_id=8,
            path="/series/alpha/s01e01.mkv",
            title="Pilot",
            season=1,
            episode=1,
            missing_subtitles="['en']",
        ),
        TableEpisodes(
            id=602,
            series_id=502,
            sonarrEpisodeId=11,
            sonarrSeriesId=1,
            arr_instance_id=9,
            path="/series/beta/s01e01.mkv",
            title="Pilot",
            season=1,
            episode=1,
            missing_subtitles="['en']",
        ),
    ])

    app = Flask(__name__)
    with app.test_request_context("/api/series?id[]=501"):
        result = series.Series.get.__wrapped__(series.Series())

    assert result["data"][0]["id"] == 501
    assert result["data"][0]["episodeFileCount"] == 1
    assert result["data"][0]["episodeMissingCount"] == 1


def test_series_missing_count_honors_show_exclusion_per_series(schema_session, monkeypatch):
    # A Sonarr exclusion (excluded tag) must test the episode's own show, not
    # every show in the library. Before the join to TableShows was restored in
    # the episodeMissingCount subquery, SQLAlchemy added TableShows to the
    # FROM clause without a join condition: a cartesian product that multiplied
    # the count by the number of eligible shows and counted the episodes of an
    # excluded show as well.
    from api.series import series
    from app.config import settings
    from app.database import TableEpisodes, TableShows

    monkeypatch.setattr(series, "database", schema_session)
    monkeypatch.setattr(settings.sonarr, "excluded_tags", ["anime"])

    schema_session.add_all([
        TableShows(
            id=511,
            sonarrSeriesId=1,
            arr_instance_id=8,
            path="/series/alpha",
            title="Alpha",
            tags="[]",
        ),
        TableShows(
            id=512,
            sonarrSeriesId=2,
            arr_instance_id=8,
            path="/series/beta",
            title="Beta",
            tags="[]",
        ),
        TableShows(
            id=513,
            sonarrSeriesId=3,
            arr_instance_id=8,
            path="/series/excluded",
            title="Excluded",
            tags="['anime']",
        ),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=611,
            series_id=511,
            sonarrEpisodeId=11,
            sonarrSeriesId=1,
            arr_instance_id=8,
            path="/series/alpha/s01e01.mkv",
            title="Pilot",
            season=1,
            episode=1,
            missing_subtitles="['en']",
        ),
        TableEpisodes(
            id=612,
            series_id=512,
            sonarrEpisodeId=12,
            sonarrSeriesId=2,
            arr_instance_id=8,
            path="/series/beta/s01e01.mkv",
            title="Pilot",
            season=1,
            episode=1,
            missing_subtitles="['en']",
        ),
        TableEpisodes(
            id=613,
            series_id=513,
            sonarrEpisodeId=13,
            sonarrSeriesId=3,
            arr_instance_id=8,
            path="/series/excluded/s01e01.mkv",
            title="Pilot",
            season=1,
            episode=1,
            missing_subtitles="['en']",
        ),
    ])

    app = Flask(__name__)
    with app.test_request_context("/api/series"):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = series.Series.get.__wrapped__(series.Series())

    # Checked first: the counts assert below would otherwise mask it, and the
    # warning only fires at compile time, so a cached statement shape in a
    # reused engine could skip it silently.
    assert not [warning for warning in caught if "cartesian product" in str(warning.message)]
    counts = {row["id"]: row["episodeMissingCount"] for row in result["data"]}
    assert counts == {511: 1, 512: 1, 513: 0}
    assert all(row["episodeFileCount"] >= row["episodeMissingCount"] for row in result["data"])
