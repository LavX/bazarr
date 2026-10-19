# coding=utf-8
"""Extract to file on a sports event goes through the owned sports operation.

The real sports library fixture: a real MKV with a French SRT track under two
Sportarr owners with their own mappings, on SQLite and on PostgreSQL when
BAZARR_PG_TEST_URL is set.
"""
# ruff: noqa: F811
import ast

import pytest
import sqlalchemy as sa

from test_sportarr_indexer import indexed_library, sports  # noqa: F401
from test_sportarr_kind_migration import migration_engine  # noqa: F401

FRENCH = [{"format": "SubRip", "language": "fra", "name": "", "forced": False, "hearing_impaired": False}]


@pytest.fixture
def sports_extract(indexed_library, monkeypatch):
    from app.config import settings
    from sportarr import subtitles as service
    import subtitles.tools.embedded_extract as extract
    import subtitles.tools.translate.batch as batch

    session, folder = indexed_library
    sports(monkeypatch, session).store_subtitles_sports(61, 1)
    monkeypatch.setattr(extract, "database", session)
    monkeypatch.setattr(service, "database", session)
    monkeypatch.setattr(settings.general, "chmod_enabled", False)
    monkeypatch.setattr(settings.general, "default_und_embedded_subtitles_lang", "")
    import utilities.video_analyzer as analyzer

    real = analyzer._handle_alpha3
    # FRENCH names its language by alpha3; the real probe the indexer reads
    # carries a Language object and keeps the real lookup.
    monkeypatch.setattr(analyzer, "_handle_alpha3",
                        lambda track: track["language"] if isinstance(track.get("language"), str) else real(track))
    monkeypatch.setattr(batch, "embedded_subtitle_tracks", lambda path, kind, owner=None: FRENCH)
    monkeypatch.setattr(batch, "extracted_subtitles_dir", lambda: str(folder / "extracted"))
    events, published = [], []
    monkeypatch.setattr(extract, "event_stream", lambda **kw: events.append(kw))
    monkeypatch.setattr("media_servers.events.notify_subtitle_mutation", published.append)
    return session, folder, events, published


def _extracted_rows(session):
    from app.database import TableHistorySports

    session.expire_all()
    return session.execute(
        sa.select(TableHistorySports.event_id, TableHistorySports.arr_instance_id, TableHistorySports.language,
                  TableHistorySports.provider, TableHistorySports.score, TableHistorySports.score_out_of,
                  TableHistorySports.video_path, TableHistorySports.subtitles_path)
        .where(TableHistorySports.action == 8)).all()


def test_a_sports_track_lands_next_to_the_recording(sports_extract):
    from app.database import TableSportsEvents
    from subliminal_patch.score import MAX_SCORES
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, folder, events, published = sports_extract

    written = materialise_embedded_track("sports", 61, 1, "fr")

    assert written == [str(folder / "1" / "event.fr.srt")]
    assert "Bienvenue" in (folder / "1" / "event.fr.srt").read_text()
    assert not (folder / "2" / "event.fr.srt").exists()
    # Score of the track's own action 7 row, which the indexer wrote at the maximum.
    assert _extracted_rows(session) == [
        (61, 1, "fr", "embedded", MAX_SCORES["movie"], MAX_SCORES["movie"], "/sports/event.mkv",
         "/sports/event.fr.srt")]
    session.expire_all()
    listing = ast.literal_eval(session.get(TableSportsEvents, 61).subtitles)
    assert ["fr", "/sports/event.fr.srt", (folder / "1" / "event.fr.srt").stat().st_size] in listing
    assert {"type": "sports", "action": "update", "payload": 61} in events
    assert [(event.media_type, event.operation) for event in published] == [("sports", "extract")]


def test_another_owner_cannot_reach_the_event(sports_extract):
    from subtitles.tools.embedded_extract import TrackNotFound, materialise_embedded_track

    session, folder, _events, published = sports_extract
    with pytest.raises(TrackNotFound):
        materialise_embedded_track("sports", 61, 2, "fr")
    assert _extracted_rows(session) == [] and published == []


def test_a_recording_replaced_mid_extraction_publishes_nothing(sports_extract, monkeypatch):
    import subtitles.tools.translate.batch as batch
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, folder, _events, published = sports_extract
    extract = batch.extract_embedded_subtitle

    def replaced_after_extraction(*args, **kwargs):
        path = extract(*args, **kwargs)
        (folder / "1" / "event.mkv").write_bytes(b"a different recording")
        return path

    monkeypatch.setattr(batch, "extract_embedded_subtitle", replaced_after_extraction)
    with pytest.raises(ValueError, match="Sports file changed"):
        materialise_embedded_track("sports", 61, 1, "fr")
    assert not (folder / "1" / "event.fr.srt").exists()
    assert _extracted_rows(session) == [] and published == []
