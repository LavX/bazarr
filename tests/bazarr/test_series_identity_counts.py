# coding=utf-8

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
