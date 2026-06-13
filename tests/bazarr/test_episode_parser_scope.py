# coding=utf-8
"""episodeParser audio_language fallback must be instance-scoped (#156).

Finding 9: when the Sonarr episodeFile carries no language, episodeParser falls
back to ``TableShows.audio_language`` keyed only by ``sonarrSeriesId``. That id
is no longer globally unique, so an unscoped lookup can read a sibling
instance's series. The owning ``arr_instance_id`` must scope it.
"""

from types import SimpleNamespace

from sqlalchemy import insert

from app.database import TableShows


def _episode_payload(series_id):
    """A Sonarr episode dict whose episodeFile has NO language, forcing the
    DB audio_language fallback."""
    return {
        "id": 555,
        "seriesId": series_id,
        "title": "Pilot",
        "seasonNumber": 1,
        "episodeNumber": 1,
        "monitored": True,
        "hasFile": True,
        "episodeFile": {
            "id": 9001,
            "path": "/series/show/s01e01.mkv",
            "size": 10_000_000_000,
            "quality": {"quality": {"name": "WEBDL-1080p"}},
        },
    }


def _patch_parser(monkeypatch, session):
    from sonarr.sync import parser

    monkeypatch.setattr(parser, "database", session)
    monkeypatch.setattr(
        parser, "settings",
        SimpleNamespace(general=SimpleNamespace(
            parse_embedded_audio_track=False, enable_strm_support=False)))
    monkeypatch.setattr(parser.path_mappings, "path_replace", lambda p: p)
    return parser


def test_episode_parser_audio_language_scoped_to_owner(schema_session, monkeypatch):
    parser = _patch_parser(monkeypatch, schema_session)

    # Two shows share sonarrSeriesId=10; owners 1 and 3 with different audio.
    schema_session.execute(insert(TableShows).values(
        id=10, sonarrSeriesId=10, arr_instance_id=1, path="/s1", title="S",
        audio_language="['English']"))
    schema_session.execute(insert(TableShows).values(
        id=11, sonarrSeriesId=10, arr_instance_id=3, path="/s3", title="S",
        audio_language="['Japanese']"))
    schema_session.flush()

    parsed = parser.episodeParser(_episode_payload(10), arr_instance_id=3)

    assert parsed["audio_language"] == "['Japanese']", (
        "audio_language fallback must read the OWNING instance's series; got "
        f"{parsed['audio_language']!r}")
