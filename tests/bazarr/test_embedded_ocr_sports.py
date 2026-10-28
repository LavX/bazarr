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


def _translate_request(session, monkeypatch, **overrides):
    from types import SimpleNamespace

    from api.subtitles import subtitles as api_mod

    monkeypatch.setattr(api_mod, "database", session)
    monkeypatch.setattr(api_mod, "translation_available", lambda: SimpleNamespace(available=True, reason=""))
    queued = []
    monkeypatch.setattr(api_mod, "translate_subtitles_file", lambda **kwargs: queued.append(kwargs))
    args = {"action": "translate", "language": "hu", "path": "", "type": "sports", "id": 61,
            "arr_instance_id": 1, "forced": "False", "hi": "False", "from_language": "en",
            "from_hi": "False", "from_forced": "False", "codec": "pgs"}
    args.update(overrides)
    stub = SimpleNamespace(patch_request_parser=SimpleNamespace(parse_args=lambda: args))
    return api_mod.Subtitles.patch.__wrapped__(stub), queued


def test_translating_a_sports_event_from_an_image_track_says_to_read_it_first(sports_ocr, monkeypatch):
    """A sports translation binds an existing source file; reading one with OCR is a job, so it is refused."""
    session, folder, _events, published, engine = sports_ocr

    (body, status), queued = _translate_request(session, monkeypatch)

    assert status == 400
    assert body == ("Translating from an image-based track is not available for sports events. "
                    "Read the track with OCR to text first, then translate the subtitle it writes.")
    assert queued == [] and engine.prepared is None and published == []
    assert not (folder / "1" / "event.en.srt").exists()


def test_translating_a_sports_event_from_a_text_track_keeps_extracting_up_front(sports_ocr, monkeypatch):
    """Without an image codec the old path is untouched: it extracts and answers its own failure."""
    from api.subtitles import subtitles as api_mod

    session, _folder, _events, _published, engine = sports_ocr
    monkeypatch.setattr(api_mod, "extract_embedded_subtitle", lambda *args, **kwargs: None)

    (body, status), queued = _translate_request(session, monkeypatch, codec=None)

    assert status == 400 and body.startswith("Could not extract embedded subtitle: codec may be bitmap")
    assert queued == [] and engine.prepared is None

