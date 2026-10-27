# coding=utf-8
"""OCR to text on a sports event goes through the owned sports operation.

The real sports library fixture under two Sportarr owners, with the first
owner's recording replaced by the Extract to file fixture (a PGS English
track), on SQLite and on PostgreSQL when BAZARR_PG_TEST_URL is set.
"""
# ruff: noqa: F811
import shutil
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from test_embedded_extract_sports import sports_extract  # noqa: F401
from test_embedded_extract_to_file import TRACKS, fixture_video  # noqa: F401
from test_embedded_ocr_to_file import ENGLISH, Jobs, ScriptedEngine
from test_sportarr_indexer import indexed_library  # noqa: F401
from test_sportarr_kind_migration import migration_engine  # noqa: F401


@pytest.fixture
def sports_ocr(sports_extract, fixture_video, monkeypatch):
    from app.database import TableSportsEvents
    import subtitles.tools.embedded_ocr as ocr
    import subtitles.tools.translate.batch as batch

    session, folder, events, published = sports_extract
    shutil.copyfile(fixture_video, folder / "1" / "event.mkv")
    monkeypatch.setattr(batch, "embedded_subtitle_tracks", lambda path, kind, owner=None: TRACKS)
    monkeypatch.setattr(ocr, "database", session)
    session.execute(sa.update(TableSportsEvents).values(subtitles=str([["en", None, None, "pgs"]])))
    session.commit()
    engine = ScriptedEngine(ENGLISH)
    monkeypatch.setattr(ocr, "ocr_engine", lambda: engine)
    monkeypatch.setattr(ocr, "jobs_queue", Jobs())
    return session, folder, events, published, engine


def _ocr_rows(session):
    from app.database import TableHistorySports

    session.expire_all()
    return session.execute(
        sa.select(TableHistorySports.event_id, TableHistorySports.arr_instance_id, TableHistorySports.language,
                  TableHistorySports.provider, TableHistorySports.video_path, TableHistorySports.subtitles_path)
        .where(TableHistorySports.action == 8)).all()


def test_a_sports_image_track_is_read_into_an_srt_next_to_the_recording(sports_ocr):
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    session, folder, events, published, _engine = sports_ocr

    written = ocr_embedded_track("sports", 61, 1, "en", codec="pgs", job_id=7)

    assert written == str(folder / "1" / "event.en.srt")
    assert "I don't know." in (folder / "1" / "event.en.srt").read_text()
    assert not (folder / "2" / "event.en.srt").exists()
    assert _ocr_rows(session) == [(61, 1, "en", "ocr-scripted", "/sports/event.mkv", "/sports/event.en.srt")]
    assert {"type": "sports", "action": "update", "payload": 61} in events
    assert [(event.media_type, event.operation) for event in published] == [("sports", "extract")]


def test_another_owner_cannot_reach_the_event(sports_ocr):
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    session, folder, _events, published, engine = sports_ocr

    with pytest.raises(JobFailed, match="Sports event not found"):
        ocr_embedded_track("sports", 61, 2, "en", codec="pgs", job_id=7)
    assert engine.prepared is None
    assert _ocr_rows(session) == [] and published == []


def test_a_recording_replaced_during_ocr_publishes_nothing(sports_ocr, monkeypatch):
    import subtitles.tools.embedded_ocr as ocr

    session, folder, _events, published, _engine = sports_ocr

    class Replacing(ScriptedEngine):
        """Sportarr replaces the recording while the cues are being read."""

        @contextmanager
        def session(self):
            lines = iter(self.answers)

            def read(image):
                (folder / "1" / "event.mkv").write_bytes(b"a different recording")
                return next(lines)

            yield read

    monkeypatch.setattr(ocr, "ocr_engine", lambda: Replacing(ENGLISH))
    with pytest.raises(ValueError, match="Sports file changed"):
        ocr.ocr_embedded_track("sports", 61, 1, "en", codec="pgs", job_id=7)
    assert not (folder / "1" / "event.en.srt").exists()
    assert _ocr_rows(session) == [] and published == []
