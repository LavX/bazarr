# coding=utf-8
"""content.py metadata fallback regression (#156).

Finding 8: ``_get_media_metadata`` episode path looked the show up only by
``TableShows.id == row.series_id``. When ``series_id`` is NULL (transient
pre-INC4 row) that drops ``mediaTitle``/``mediaId``. It must mirror
``resolve_subtitle_path``'s fallback to the upstream ``sonarrSeriesId`` scoped
by the owning instance.
"""

import app.database  # noqa: F401


def test_get_media_metadata_episode_falls_back_to_upstream_id(schema_session, monkeypatch):
    from api.subtitles import content
    from app.database import TableEpisodes, TableShows

    monkeypatch.setattr(content, "database", schema_session)

    schema_session.add(TableShows(
        id=501, sonarrSeriesId=7, arr_instance_id=2, path="/series/alpha",
        title="Alpha Show", tags="[]"))
    schema_session.flush()
    # Episode with a NULL series_id link but a valid upstream sonarrSeriesId.
    schema_session.add(TableEpisodes(
        id=601, series_id=None, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=2, path="/series/alpha/s01e01.mkv", title="Pilot",
        season=1, episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    metadata = content._get_media_metadata("episode", 42, arr_instance_id=2)

    assert metadata is not None
    assert metadata["mediaTitle"] == "Alpha Show", (
        "NULL series_id must fall back to the upstream-id-scoped show lookup")
    assert metadata["mediaId"] == 501


def test_get_media_metadata_episode_fallback_scoped_to_owner(schema_session, monkeypatch):
    """The fallback must not pick a colliding sibling instance's show."""
    from api.subtitles import content
    from app.database import TableEpisodes, TableShows

    monkeypatch.setattr(content, "database", schema_session)

    # Two shows share sonarrSeriesId=7 under instances 2 and 5.
    schema_session.add(TableShows(
        id=501, sonarrSeriesId=7, arr_instance_id=2, path="/s2",
        title="Wrong Show", tags="[]"))
    schema_session.add(TableShows(
        id=502, sonarrSeriesId=7, arr_instance_id=5, path="/s5",
        title="Right Show", tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=602, series_id=None, sonarrEpisodeId=42, sonarrSeriesId=7,
        arr_instance_id=5, path="/s5/e.mkv", title="Pilot", season=1,
        episode=1, monitored="True", subtitles="[]"))
    schema_session.flush()

    metadata = content._get_media_metadata("episode", 42, arr_instance_id=5)

    assert metadata["mediaTitle"] == "Right Show"
    assert metadata["mediaId"] == 502
