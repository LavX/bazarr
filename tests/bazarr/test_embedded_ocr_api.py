# coding=utf-8
"""PATCH /api/subtitles?action=ocr with an empty path: OCR to text for an embedded image track.

The request answers before anything is read: 501 when OCR cannot run here,
404 for a row or a track the row does not list, 415 for anything but PGS and
VobSub (naming what it is), 409 when the SRT is already there or another OCR
job is queued or running, or 202 with the queued job and the file it will
write.
"""
# ruff: noqa: F811
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.database import TableMovies
from test_embedded_extract_to_file import fixture_video, library  # noqa: F401
from test_embedded_ocr_to_file import ocr_library  # noqa: F401
from test_embedded_track_hidden import hidden_db  # noqa: F401


@pytest.fixture
def endpoint(ocr_library, monkeypatch):
    from api.subtitles import subtitles as api_mod
    import subtitles.tools.embedded_ocr as ocr
    from subtitles.tools.ocr import tesseract

    session, root, _events, _published = ocr_library
    monkeypatch.setattr(api_mod, "database", session)
    monkeypatch.setattr(tesseract, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "jobs_queue", SimpleNamespace(list_jobs_from_queue=lambda **kwargs: []))
    queued = []
    monkeypatch.setattr(ocr, "ocr_embedded_track", lambda *args, **kwargs: queued.append((args, kwargs)) or 43)

    def call(**overrides):
        args = {"action": "ocr", "language": "en", "path": "", "type": "movie", "id": 30,
                "arr_instance_id": 1, "forced": "False", "hi": "False", "codec": "pgs"}
        args.update(overrides)
        stub = SimpleNamespace(patch_request_parser=SimpleNamespace(parse_args=lambda: args))
        return api_mod.Subtitles.patch.__wrapped__(stub)

    return call, queued, root, session


def test_ocr_queues_one_job_and_names_the_srt_it_will_write(endpoint):
    call, queued, root, _session = endpoint

    body, status = call()

    assert status == 202
    from app.process_session import PROCESS_SESSION

    assert body == {"job_id": 43, "session": PROCESS_SESSION,
                    "path": str(root / "owner1/movies/Film (2020)/Film.en.srt")}
    assert queued == [(("movie", 30, 1, "en", False, False), {"codec": "pgs", "label": "Film, English"})]
    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists(), "the request reads nothing"


def test_vobsub_and_an_episode(endpoint):
    call, queued, root, _session = endpoint

    body, status = call(type="episode", id=50, language="hu", codec="vobsub")

    assert status == 202 and body["path"] == str(root / "owner1/tv/Show/Season 1/Show S01E01.hu.srt")
    assert queued[0][1]["codec"] == "vobsub"


@pytest.mark.parametrize("stored,codec,message", [
    ([["en", None, None, "pgs"], ["en", None, None, "subrip"]], "subrip",
     "OCR reads PGS and VobSub tracks only, and this English track is SRT, which is text already. "
     "Extract to file writes it as an SRT."),
    ([["en", None, None, "dvbsub"]], "dvbsub",
     "OCR reads PGS and VobSub tracks only, and this English track is DVB. Extract to file copies it out as it is."),
])
def test_dvb_unknown_and_text_tracks_answer_415_naming_the_codec(endpoint, stored, codec, message):
    call, queued, _root, session = endpoint
    session.execute(sa.update(TableMovies).where(TableMovies.id == 1).values(subtitles=str(stored)))

    assert call(codec=codec) == (message, 415)
    assert queued == []


def test_an_existing_srt_answers_409_and_queues_nothing(endpoint, monkeypatch):
    from app.config import settings

    call, queued, root, _session = endpoint
    (root / "owner1/movies/Film (2020)/Film.en.srt").write_text("mine")

    assert call() == ("A subtitle file named Film.en.srt already exists. Delete it first to read this track "
                      "with OCR.", 409)
    # With a subfolder configured, the name next to the video counts too.
    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "Subs")
    assert call()[1] == 409
    assert queued == []


@pytest.mark.parametrize("overrides,status", [
    ({"path": "/movies/Film (2020)/Film.en.srt"}, 400),
    ({"language": "xx"}, 400),
    ({"type": "album"}, 400),
    ({"id": 31}, 404),
    ({"arr_instance_id": 3}, 404),
    # Not listed: never indexed, or hidden by the user.
    ({"language": "fr"}, 404),
    ({"codec": "vobsub"}, 404),
])
def test_refusals(endpoint, overrides, status):
    call, queued, _root, _session = endpoint

    assert call(**overrides)[1] == status
    assert queued == []


def test_ocr_that_cannot_run_here_answers_501(endpoint, monkeypatch):
    from subtitles.tools.ocr import tesseract

    call, queued, _root, _session = endpoint
    monkeypatch.setattr(tesseract, "ocr_available", lambda: False)

    body, status = call()

    assert status == 501 and body.startswith("OCR is not available on this installation")
    assert queued == []


def test_the_second_instance_reads_from_its_own_mount(endpoint):
    call, queued, root, _session = endpoint

    body, status = call(arr_instance_id=2)

    assert status == 202 and body["path"] == str(root / "owner2/movies/Film (2020)/Film.en.srt")
    assert queued[0][0][2] == 2


@pytest.mark.parametrize("job,busy", [
    ({"func": "ocr_embedded_track", "status": "running", "kwargs": {}}, True),
    ({"func": "ocr_embedded_track", "status": "pending", "kwargs": {}}, True),
    ({"func": "mass_batch_operation", "status": "pending", "kwargs": {"action": "ocr"}}, True),
    ({"func": "mass_batch_operation", "status": "running", "kwargs": {"action": "extract"}}, False),
    ({"func": "ocr_embedded_track", "status": "failed", "kwargs": {}}, False),
    # A manual translation that reads an image track with OCR is an OCR job.
    ({"func": "translate_subtitles_file", "status": "pending",
      "kwargs": {"embedded_source": {"language": "en", "codec": "pgs"}}}, True),
    ({"func": "translate_subtitles_file", "status": "running",
      "kwargs": {"embedded_source": {"language": "hu", "codec": "vobsub"}}}, True),
    ({"func": "translate_subtitles_file", "status": "failed",
      "kwargs": {"embedded_source": {"language": "en", "codec": "pgs"}}}, False),
    # A text source, a DVB source, a source without a codec and a file never read with OCR.
    ({"func": "translate_subtitles_file", "status": "running",
      "kwargs": {"embedded_source": {"language": "en", "codec": "subrip"}}}, False),
    ({"func": "translate_subtitles_file", "status": "running",
      "kwargs": {"embedded_source": {"language": "en", "codec": "dvbsub"}}}, False),
    ({"func": "translate_subtitles_file", "status": "running",
      "kwargs": {"embedded_source": {"language": "en"}}}, False),
    ({"func": "translate_subtitles_file", "status": "running", "kwargs": {"embedded_source": None}}, False),
])
def test_a_request_while_an_ocr_job_is_queued_or_running_answers_409(endpoint, monkeypatch, job, busy):
    """One OCR job at a time: a second one would hold a running slot of the jobs queue while it waited."""
    import subtitles.tools.embedded_ocr as ocr

    call, queued, _root, _session = endpoint
    monkeypatch.setattr(ocr, "jobs_queue", SimpleNamespace(list_jobs_from_queue=lambda **kwargs: [
        {"job_id": 3, **job}]))

    body, status = call()

    if busy:
        assert (body, status) == (ocr.BUSY, 409)
        assert queued == []
    else:
        assert status == 202 and len(queued) == 1


def test_the_pack_folder_setting_saves_and_reads_back(monkeypatch):
    """The Subtitles settings page posts settings-ocr-tessdata_dir and reads ocr.tessdata_dir."""
    import sys

    from app import config

    executed = []
    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=executed.append), update=lambda _model: SimpleNamespace(
            values=lambda **_kwargs: None), System=object))
    before = config.settings.ocr.tessdata_dir
    try:
        config.save_settings([("settings-ocr-tessdata_dir", ["/srv/tessdata"])])

        assert config.settings.ocr.tessdata_dir == "/srv/tessdata"
        assert config.get_settings()["ocr"]["tessdata_dir"] == "/srv/tessdata"
    finally:
        config.settings.ocr.tessdata_dir = before

def test_an_old_row_is_probed_and_queues_ocr(endpoint):
    """A row without a stored codec asks the video and queues instead of 415."""
    call, queued, _root, session = endpoint
    session.execute(sa.update(TableMovies).where(TableMovies.id == 1).values(
        subtitles=str([["en", None, None]])))

    body, status = call(codec=None)

    assert status == 202 and body["path"].endswith("Film.en.srt")
    assert queued[0][1]["codec"] == "pgs"


@pytest.fixture
def translate_endpoint(endpoint, monkeypatch):
    """The same movie, asked to translate from an embedded track.

    The translation and the OCR job both put themselves in one fake queue, the
    way the real jobs queue lists them, so a request sees what the other queued.
    """
    from api.subtitles import subtitles as api_mod
    import subtitles.tools.embedded_ocr as ocr

    call_ocr, queued_ocr, root, session = endpoint
    jobs = []
    queued_translations = []
    monkeypatch.setattr(ocr, "jobs_queue", SimpleNamespace(list_jobs_from_queue=lambda **kwargs: list(jobs)))
    monkeypatch.setattr(ocr, "ocr_embedded_track", lambda *args, **kwargs: (
        jobs.append({"job_id": 43, "func": "ocr_embedded_track", "status": "pending", "kwargs": {}}),
        queued_ocr.append((args, kwargs)), 43)[-1])
    monkeypatch.setattr(api_mod, "translation_available", lambda: SimpleNamespace(available=True, reason=""))

    def translate_stub(**kwargs):
        queued_translations.append(kwargs)
        jobs.append({"job_id": 44, "func": "translate_subtitles_file", "status": "pending", "kwargs": kwargs})
        return 44

    monkeypatch.setattr(api_mod, "translate_subtitles_file", translate_stub)

    def translate(**overrides):
        args = {"action": "translate", "language": "hu", "path": "", "type": "movie", "id": 30,
                "arr_instance_id": 1, "forced": "False", "hi": "False", "from_language": "en",
                "from_hi": "False", "from_forced": "False", "codec": "pgs"}
        args.update(overrides)
        stub = SimpleNamespace(patch_request_parser=SimpleNamespace(parse_args=lambda: args))
        return api_mod.Subtitles.patch.__wrapped__(stub)

    return translate, queued_translations, call_ocr, queued_ocr, jobs


def test_translating_from_an_image_track_queues_a_job_that_carries_its_codec(translate_endpoint):
    translate, queued, _call_ocr, _queued_ocr, _jobs = translate_endpoint

    body, status = translate()

    assert (body, status) == ("", 204)
    assert len(queued) == 1
    assert queued[0]["embedded_source"] == {"language": "en", "hi": False, "forced": False, "codec": "pgs"}


def test_an_episode_translated_from_an_image_track_answers_with_its_job_and_session(translate_endpoint):
    from app.process_session import PROCESS_SESSION

    translate, queued, _call_ocr, _queued_ocr, _jobs = translate_endpoint

    body, status = translate(type="episode", id=50)

    assert status == 202
    assert body == {"job_id": 44, "session": PROCESS_SESSION}
    assert len(queued) == 1


def test_translating_from_an_image_track_while_ocr_runs_answers_409(translate_endpoint):
    import subtitles.tools.embedded_ocr as ocr

    translate, queued, call_ocr, _queued_ocr, _jobs = translate_endpoint
    assert call_ocr()[1] == 202  # a row's OCR to text is queued

    assert translate() == (ocr.BUSY, 409)
    assert queued == []


def test_ocr_to_text_while_a_translation_reads_an_image_track_answers_409(translate_endpoint):
    import subtitles.tools.embedded_ocr as ocr

    translate, queued, call_ocr, queued_ocr, _jobs = translate_endpoint
    translate()
    assert len(queued) == 1

    assert call_ocr() == (ocr.BUSY, 409)
    assert queued_ocr == []


def test_a_second_translation_from_an_image_track_answers_409(translate_endpoint):
    import subtitles.tools.embedded_ocr as ocr

    translate, queued, _call_ocr, _queued_ocr, _jobs = translate_endpoint
    translate()

    assert translate(from_language="hu", codec="vobsub") == (ocr.BUSY, 409)
    assert len(queued) == 1


@pytest.mark.parametrize("codec", ["subrip", None, "dvbsub"])
def test_translating_from_a_text_or_unknown_track_is_never_held_up_by_ocr(translate_endpoint, codec):
    translate, queued, call_ocr, _queued_ocr, _jobs = translate_endpoint
    assert call_ocr()[1] == 202

    translate(codec=codec)

    assert len(queued) == 1
    assert "codec" not in queued[0]["embedded_source"] or queued[0]["embedded_source"]["codec"] == codec


def test_translating_from_an_image_track_without_ocr_available_answers_501(translate_endpoint, monkeypatch):
    from subtitles.tools.ocr import tesseract

    translate, queued, _call_ocr, _queued_ocr, _jobs = translate_endpoint
    monkeypatch.setattr(tesseract, "ocr_available", lambda: False)

    body, status = translate()

    assert status == 501 and body.startswith("OCR is not available on this installation")
    assert queued == []


def test_translating_from_an_image_track_whose_srt_is_already_there_answers_409(translate_endpoint, endpoint,
                                                                                monkeypatch):
    """The name OCR would take is checked before queueing, not after the job has failed."""
    from app.config import settings

    translate, queued, _call_ocr, _queued_ocr, _jobs = translate_endpoint
    root = endpoint[2]
    (root / "owner1/movies/Film (2020)/Film.en.srt").write_text("mine")

    assert translate() == ("A subtitle file named Film.en.srt already exists. Delete it first to read this "
                           "track with OCR.", 409)
    # With a subfolder configured, the name next to the video counts too.
    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "Subs")
    assert translate()[1] == 409
    assert queued == []


@pytest.mark.parametrize("overrides,status", [
    # Not listed: never indexed, or hidden by the user.
    ({"from_language": "fr"}, 404),
    ({"codec": "vobsub"}, 404),
    ({"from_hi": "True"}, 404),
])
def test_translating_from_a_hidden_or_unlisted_image_track_answers_404(translate_endpoint, overrides, status):
    translate, queued, _call_ocr, _queued_ocr, _jobs = translate_endpoint

    body, answered = translate(**overrides)

    assert answered == status and "Scan the disk" in body
    assert queued == []


def test_translating_from_an_image_track_of_an_unknown_title_answers_404(translate_endpoint):
    translate, queued, _call_ocr, _queued_ocr, _jobs = translate_endpoint

    assert translate(id=31)[1] == 404
    assert queued == []


def _translation_job(job_id, status="running"):
    return {"job_id": job_id, "func": "translate_subtitles_file", "status": status,
            "kwargs": {"embedded_source": {"language": "en", "codec": "pgs"}}}


@pytest.mark.parametrize("status", ["pending", "running"])
def test_a_translation_blocks_ocr_until_its_ocr_phase_is_over(translate_endpoint, status):
    """While it waits for a lane slot or reads the track it is OCR; once it is translating it is not."""
    import subtitles.tools.embedded_ocr as ocr

    _translate, _queued, call_ocr, queued_ocr, jobs = translate_endpoint
    jobs.append(_translation_job(77, status))
    ocr.ocr_phase_started(77)

    assert call_ocr() == (ocr.BUSY, 409)
    assert queued_ocr == []

    ocr.ocr_phase_over(77)

    assert call_ocr()[1] == 202
    assert len(queued_ocr) == 1


def test_a_second_translation_from_an_image_track_is_allowed_once_the_first_is_translating(translate_endpoint):
    import subtitles.tools.embedded_ocr as ocr

    translate, queued, _call_ocr, _queued_ocr, jobs = translate_endpoint
    jobs.append(_translation_job(77))
    ocr.ocr_phase_started(77)
    assert translate() == (ocr.BUSY, 409)

    ocr.ocr_phase_over(77)

    assert translate()[1] == 204
    assert len(queued) == 1


def test_a_job_that_starts_its_ocr_phase_again_blocks_again(translate_endpoint):
    """A retried job keeps its id: starting the phase clears the mark the first run left."""
    import subtitles.tools.embedded_ocr as ocr

    _translate, _queued, call_ocr, _queued_ocr, jobs = translate_endpoint
    jobs.append(_translation_job(77))
    ocr.ocr_phase_over(77)
    assert call_ocr()[1] == 202
    jobs.pop()

    jobs.append(_translation_job(77))
    ocr.ocr_phase_started(77)

    assert call_ocr() == (ocr.BUSY, 409)


def test_the_phase_marks_of_jobs_that_left_the_queue_are_dropped(translate_endpoint):
    import subtitles.tools.embedded_ocr as ocr

    call_ocr = translate_endpoint[2]
    ocr.ocr_phase_over(1234)
    call_ocr()

    assert 1234 not in ocr._OCR_PHASE_OVER
