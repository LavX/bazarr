# coding=utf-8
"""OCR to text: which track is read, how it is read, and the SRT that lands next to the video.

Real ffmpeg on the Extract to file fixture (a PGS English, a VobSub Hungarian
and two English text tracks), the real indexers, on an AUTOCOMMIT engine of
each kind (SQLite, and PostgreSQL when BAZARR_PG_TEST_URL is set). Tesseract
is a scripted stand-in unless a test says it runs the real engine.
"""
# ruff: noqa: F811
import ast
import os
import shutil
import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock

import pysubs2
import pytest
import sqlalchemy as sa
from subliminal_patch.score import MAX_SCORES

from app.database import TableEpisodes, TableHistory, TableHistoryMovie, TableMovies
from ocr_fixtures import FIXTURES
from test_embedded_extract_to_file import fixture_video, library  # noqa: F401
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_ocr_pipeline import packs  # noqa: F401

# What the indexer stores for the fixture's four streams, in stream order.
STORED = [["en", None, None, "pgs"], ["hu", None, None, "vobsub"], ["en", None, None, "subrip"],
          ["en:hi", None, None, "subrip"]]


@pytest.fixture(scope="module")
def late_video(tmp_path_factory):
    """A PGS English track whose first cue comes 31 s in, past what ffprobe reads to size it."""
    ffmpeg = shutil.which("ffmpeg")
    folder = tmp_path_factory.mktemp("ocr-late")
    video = str(folder / "late.mkv")
    subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-copyts",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5:d=45",
                    "-itsoffset", "30", "-fix_sub_duration", "-i", os.path.join(FIXTURES, "eng_hd.sup"),
                    "-map", "0:v", "-map", "1:s", "-c:v", "mpeg4", "-c:s", "copy",
                    "-metadata:s:s:0", "language=eng", video], check=True)
    return video


@pytest.fixture
def ocr_library(library, monkeypatch):
    """The Extract to file library, with the tracks listed the way the indexer lists them."""
    import subtitles.tools.embedded_ocr as ocr

    session, root, events, published = library
    monkeypatch.setattr(ocr, "database", session)
    session.execute(sa.update(TableMovies).values(subtitles=str(STORED)))
    session.execute(sa.update(TableEpisodes).values(subtitles=str(STORED)))
    return session, root, events, published


def _track(media_type="movie", media_id=30, owner=1, language="en", hi=False, forced=False):
    from subtitles.tools.embedded_extract import resolve_track

    return resolve_track(media_type, media_id, owner, language, hi, forced)


def test_the_stored_codec_decides_which_track_is_read(ocr_library):
    from subtitles.tools.embedded_ocr import ocr_codec, stored_codecs

    assert stored_codecs(_track()) == ["pgs", "subrip"]
    assert ocr_codec(_track(), "pgs") == "pgs"
    assert ocr_codec(_track(language="hu"), "VobSub") == "vobsub"
    # No codec in the request: the image track of the key.
    assert ocr_codec(_track(), None) == "pgs"
    assert ocr_codec(_track("episode", 50), "pgs") == "pgs"


@pytest.mark.parametrize("stored,requested,message", [
    ([["en", None, None, "pgs"], ["en", None, None, "subrip"]], "subrip",
     "OCR reads PGS and VobSub tracks only, and this English track is SRT, which is text already. "
     "Extract to file writes it as an SRT."),
    ([["en", None, None, "dvbsub"]], "dvbsub",
     "OCR reads PGS and VobSub tracks only, and this English track is DVB. Extract to file copies it out as it is."),
    ([["en", None, None, "dvbsub"]], None,
     "OCR reads PGS and VobSub tracks only, and this English track is DVB. Extract to file copies it out as it is."),
])
def test_anything_but_pgs_and_vobsub_is_refused_naming_what_it_is(ocr_library, stored, requested, message):
    from subtitles.tools.embedded_extract import NotExtractable
    from subtitles.tools.embedded_ocr import ocr_codec

    session = ocr_library[0]
    session.execute(sa.update(TableMovies).where(TableMovies.id == 1).values(subtitles=str(stored)))

    with pytest.raises(NotExtractable) as refused:
        ocr_codec(_track(), requested)
    assert str(refused.value) == message
    assert refused.value.status == 415


def test_a_track_the_row_does_not_list_is_never_read(ocr_library):
    """A hidden track is not in the listing, so it is refused like one that does not exist."""
    from subtitles.tools.embedded_extract import TrackNotFound
    from subtitles.tools.embedded_ocr import ocr_codec

    with pytest.raises(TrackNotFound, match="No embedded French PGS track is listed"):
        ocr_codec(_track(language="fr"), "pgs")
    with pytest.raises(TrackNotFound, match="No embedded English VobSub track is listed"):
        ocr_codec(_track(), "vobsub")
    with pytest.raises(TrackNotFound):
        ocr_codec(_track(forced=True), "pgs")


def test_ocr_reads_the_row_s_own_image_stream(ocr_library):
    from subtitles.tools.embedded_extract import NotExtractable
    from subtitles.tools.embedded_ocr import ocr_stream

    assert ocr_stream(_track(), "pgs") == 0
    assert ocr_stream(_track(language="hu"), "vobsub") == 1
    # Exactly the row's variant: there is no forced English image track, and
    # the plain one is never read in its place.
    with pytest.raises(NotExtractable, match="The video has no English .forced. PGS track to read"):
        ocr_stream(_track(forced=True), "pgs")


def test_the_canvas_is_given_only_when_ffprobe_cannot_size_the_track(fixture_video, late_video):
    from subtitles.tools.embedded_ocr import subtitle_canvas

    ffprobe = shutil.which("ffprobe")
    assert subtitle_canvas(fixture_video, 0, "pgs", ffprobe=ffprobe) is None
    assert subtitle_canvas(fixture_video, 1, "vobsub", ffprobe=ffprobe) is None
    assert subtitle_canvas(late_video, 0, "pgs", ffprobe=ffprobe) == (1920, 1080)
    assert subtitle_canvas(late_video, 0, "pgs", ffprobe="/nonexistent/ffprobe") == (1920, 1080)
    assert subtitle_canvas(late_video, 0, "vobsub", ffprobe="/nonexistent/ffprobe") == (720, 576)


@pytest.mark.parametrize("step,value,maximum,message", [
    ("pack", 1_250_000, 5_296_273, "Downloading the OCR language pack hun.traineddata (1.2 of 5.3 MB)"),
    ("pack", 1, 1, "Rendering the image subtitles"),
    ("render", 42, 0, "Rendering the image subtitles"),
    ("recognise", 7, 812, "Reading cue 7 of 812"),
    ("write", 1, 1, "Writing the SRT next to the video"),
])
def test_each_step_says_what_it_does(step, value, maximum, message):
    from subtitles.tools.embedded_ocr import progress_message

    assert progress_message(step, value, maximum, pack="hun") == message


# The job, run the way the queue runs it.

ENGLISH = ["Hello there, how are you?", "We need to leave\nbefore the storm.", "l don't know."]


class ScriptedEngine:
    """Reads the fixture's cues as the lines it is given; prepare() reports a pack download."""

    name = "scripted"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prepared = None

    def prepare(self, language, progress=None):
        self.prepared = language
        if progress:
            progress("pack", 2_000_000, 4_113_088)
            progress("pack", 4_113_088, 4_113_088)

    @contextmanager
    def session(self):
        answers = iter(self.answers)
        yield lambda image: next(answers)


class Jobs:
    """Stands in for the queue: records names and progress, stops when ``cancel_at`` matches.

    ``queued`` is what it lists as queued, running and finished jobs.
    """

    def __init__(self, cancel_at=None):
        self.names, self.progress, self.queued = [], [], []
        self.cancel_at = cancel_at

    def list_jobs_from_queue(self, job_id=None, status=None):
        return list(self.queued)

    def add_job_from_function(self, name, is_progress=False, **kwargs):
        self.names.append(name)
        return 41

    def update_job_name(self, job_id, new_job_name):
        self.names.append(new_job_name)

    def update_job_progress(self, job_id, progress_value=None, progress_max=None, progress_message=""):
        from app.jobs_queue import JobCancelled

        self.progress.append((progress_value, progress_max, progress_message))
        if self.cancel_at and self.cancel_at in progress_message:
            raise JobCancelled("cancelled")


@pytest.fixture
def ocr_run(ocr_library, monkeypatch):
    import subtitles.tools.embedded_ocr as ocr

    engine = ScriptedEngine(ENGLISH)
    jobs = Jobs()
    monkeypatch.setattr(ocr, "ocr_engine", lambda: engine)
    monkeypatch.setattr(ocr, "jobs_queue", jobs)
    return (*ocr_library, engine, jobs)


def _history(session, table=TableHistoryMovie):
    session.expire_all()
    return session.execute(sa.select(table.action, table.language, table.provider, table.score,
                                     table.video_path, table.subtitles_path, table.arr_instance_id,
                                     table.description).order_by(table.id)).all()


def _listing(session, table, row_id):
    session.expire_all()
    return ast.literal_eval(session.execute(sa.select(table.subtitles).where(table.id == row_id)).scalar_one())


def _texts(path):
    return [event.plaintext for event in pysubs2.load(str(path))]


def test_the_srt_lands_next_to_the_video_and_becomes_an_external_row(ocr_run):
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    session, root, events, published, engine, jobs = ocr_run
    film = root / "owner1/movies/Film (2020)"

    written = ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    assert written == str(film / "Film.en.srt")
    # The OCR Fixes mod ran in memory: "l don't" is "I don't".
    assert _texts(film / "Film.en.srt") == ["Hello there, how are you?", "We need to leave\nbefore the storm.",
                                            "I don't know."]
    assert engine.prepared == "eng"
    assert _history(session) == [
        (8, "en", "ocr-scripted", MAX_SCORES["movie"], "/movies/Film (2020)/Film.mkv",
         "/movies/Film (2020)/Film.en.srt", 1,
         "English subtitles read from the PGS track with OCR (3 cues, 0 empty).")]
    assert ["en", "/movies/Film (2020)/Film.en.srt", os.path.getsize(written)] in _listing(session, TableMovies, 1)
    assert {"type": "movie", "payload": 1} in events
    assert [(event.operation, event.subtitle_path) for event in published] == [("extract", written)]
    assert jobs.names == ["OCR to text: wrote Film.en.srt (3 cues)"]


def test_the_job_says_what_each_step_does(ocr_run):
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    _session, _root, _events, _published, _engine, jobs = ocr_run

    ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    messages = [message for _value, _maximum, message in jobs.progress]
    assert messages[0] == "Downloading the OCR language pack eng.traineddata (2.0 of 4.1 MB)"
    assert jobs.progress[0][:2] == (2_000_000, 4_113_088)
    assert jobs.progress[2] == (0, None, "Rendering the image subtitles")
    assert [entry for entry in jobs.progress if entry[2].startswith("Reading cue")] == [
        (1, 3, "Reading cue 1 of 3"), (2, 3, "Reading cue 2 of 3"), (3, 3, "Reading cue 3 of 3")]
    assert jobs.progress[-1] == ("max", None, "Writing the SRT next to the video")


@pytest.mark.parametrize("variant,stored,name", [
    ({"forced": True}, "en:forced", "Film.en.forced.srt"),
    ({"hearing_impaired": True}, "en:hi", "Film.en.sdh.srt"),
])
def test_hi_and_forced_are_named_like_downloads(ocr_run, monkeypatch, variant, stored, name):
    import test_embedded_extract_to_file as extract_tests
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    session, root, _events, _published, _engine, _jobs = ocr_run
    monkeypatch.setenv("SZ_HI_EXTENSION", "sdh")
    monkeypatch.setattr(extract_tests, "TRACKS", [dict(extract_tests.TRACKS[0], **variant),
                                                  *extract_tests.TRACKS[1:]])
    session.execute(sa.update(TableMovies).where(TableMovies.id == 1).values(
        subtitles=str([[stored, None, None, "pgs"]])))
    hi, forced = variant.get("hearing_impaired", False), variant.get("forced", False)

    written = ocr_embedded_track("movie", 30, 1, "en", hi=hi, forced=forced, codec="pgs", job_id=7)

    assert os.path.basename(written) == name
    assert _history(session)[0][1] == stored


@pytest.mark.parametrize("mode", ["current", "relative", "absolute"])
def test_the_configured_subtitle_folder_is_used(ocr_run, monkeypatch, mode):
    from app.config import settings
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    _session, root, _events, _published, _engine, _jobs = ocr_run
    film = root / "owner1/movies/Film (2020)"
    custom = {"current": "", "relative": "Subs", "absolute": str(root / "all-subs")}[mode]
    monkeypatch.setattr(settings.general, "subfolder", mode)
    monkeypatch.setattr(settings.general, "subfolder_custom", custom)
    expected = {"current": film, "relative": film / "Subs", "absolute": root / "all-subs"}[mode]

    assert ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7) == str(expected / "Film.en.srt")


def test_an_existing_srt_stops_the_job_before_the_video_is_read(ocr_run):
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    session, root, _events, published, engine, _jobs = ocr_run
    film = root / "owner1/movies/Film (2020)"
    (film / "Film.en.srt").write_text("mine")

    with pytest.raises(JobFailed, match="Film.en.srt already exists. Delete it first to read this track with OCR"):
        ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    assert engine.prepared is None
    assert (film / "Film.en.srt").read_text() == "mine"
    assert _history(session) == [] and published == []


def test_a_file_that_appears_during_ocr_is_never_replaced(ocr_run, monkeypatch):
    from app.jobs_queue import JobFailed
    import subtitles.tools.embedded_ocr as ocr

    session, root, _events, published, _engine, _jobs = ocr_run
    film = root / "owner1/movies/Film (2020)"

    class Downloading(ScriptedEngine):
        """A download of the language lands while the cues are read."""

        @contextmanager
        def session(self):
            lines = iter(self.answers)

            def read(image):
                (film / "Film.en.srt").write_text("a download")
                return next(lines)

            yield read

    monkeypatch.setattr(ocr, "ocr_engine", lambda: Downloading(ENGLISH))
    with pytest.raises(JobFailed, match="Film.en.srt already exists"):
        ocr.ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    assert (film / "Film.en.srt").read_text() == "a download"
    assert _history(session) == [] and published == []


def test_a_language_without_a_pack_fails_before_anything_is_read(ocr_run, monkeypatch):
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_ocr import ocr_embedded_track
    from subtitles.tools.ocr import tessdata

    session, _root, _events, _published, engine, _jobs = ocr_run
    monkeypatch.delitem(tessdata.TESSDATA_CHECKSUMS, "eng")

    with pytest.raises(JobFailed, match="No OCR language pack for eng"):
        ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)
    assert engine.prepared is None and _history(session) == []


def test_an_offline_box_is_told_the_url_and_the_folder(ocr_library, monkeypatch, tmp_path):
    """No pack yet and GitHub unreachable: the failure says where to get the file and where it goes."""
    import requests

    from app.config import settings
    from app.jobs_queue import JobFailed
    import subtitles.tools.embedded_ocr as ocr
    from subtitles.tools.ocr import tessdata, tesseract

    session, root, _events, published = ocr_library
    folder = tmp_path / "tessdata"

    def offline(*args, **kwargs):
        raise requests.ConnectionError("Network is unreachable")

    monkeypatch.setattr(settings.ocr, "tessdata_dir", str(folder))
    monkeypatch.setattr(tesseract, "ocr_available", lambda: True)
    monkeypatch.setattr(tessdata.requests, "get", offline)
    monkeypatch.setattr(ocr, "jobs_queue", Jobs())

    with pytest.raises(JobFailed) as failed:
        ocr.ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    assert "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/eng.traineddata" in str(failed.value)
    assert f"place it in {folder}" in str(failed.value)
    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists()
    assert _history(session) == [] and published == []


def test_one_ocr_run_at_a_time(ocr_run, monkeypatch):
    """A second run never waits for the first inside its job: it ends at once, having read nothing."""
    from app.jobs_queue import JobFailed
    from subtitles.tools.ocr.decode import require_ffmpeg
    from subtitles.tools.ocr.errors import OcrUnavailable
    import subtitles.tools.embedded_ocr as ocr

    session, root, _events, published, engine, jobs = ocr_run
    probed = []
    select_stream = ocr.ocr_stream

    def probe(*args, **kwargs):
        index = select_stream(*args, **kwargs)
        probed.append(index)
        return index

    monkeypatch.setattr(ocr, "ocr_stream", probe)

    with ocr._RUN_SLOT:
        with pytest.raises(JobFailed, match="^Another OCR run is queued or running. Try again when it finishes"):
            ocr.ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)
    assert probed == [], "the busy run probed the video before taking the slot"
    assert engine.prepared is None and jobs.progress == []
    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists()
    assert _history(session) == [] and published == []

    try:
        require_ffmpeg()
    except OcrUnavailable as error:
        pytest.skip(str(error))
    assert ocr.ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=8).endswith("Film.en.srt")
    assert probed == [0]


def quick_job(job_id=None):
    """A job that is not OCR, for the queue case below."""
    return "done"


def test_a_second_ocr_job_never_holds_a_queue_slot(ocr_run, monkeypatch):
    """Two running slots, one OCR run going: a second OCR job ends at once, and the next job starts.

    A job that waited for the run would sit in a running slot for all of it,
    and with both slots taken every other job (syncs, searches, backups)
    would queue behind the two OCR jobs.
    """
    import threading

    from app import jobs_queue as queue_module
    from app.config import settings
    import subtitles.tools.embedded_ocr as ocr

    session, _root, _events, published, engine, _jobs = ocr_run
    monkeypatch.setattr(queue_module, "event_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(settings.general, "concurrent_jobs", 2)
    queue = queue_module.JobsQueue()
    monkeypatch.setattr(ocr, "jobs_queue", queue)
    row = {"media_type": "movie", "media_id": 30, "arr_instance_id": 1}
    first = queue.feed_jobs_pending_queue("OCR to text: Film, Hungarian", "subtitles.tools.embedded_ocr",
                                          "ocr_embedded_track", kwargs={**row, "language": "hu", "codec": "vobsub"})
    second = queue.feed_jobs_pending_queue("OCR to text: Film, English", "subtitles.tools.embedded_ocr",
                                           "ocr_embedded_track", kwargs={**row, "language": "en", "codec": "pgs"})
    other = queue.feed_jobs_pending_queue("Backup", __name__, "quick_job")

    # The first OCR job is running: it holds a slot and the OCR slot.
    assert queue._reserve_next_job().job_id == first
    with ocr._RUN_SLOT:
        job = queue._reserve_next_job()
        assert job.job_id == second
        worker = threading.Thread(target=queue._run_job, args=(job,), daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "the second OCR job kept its running slot"
        assert job.status == "failed" and job.error["message"].startswith("Another OCR run is queued or running")
        nxt = queue._reserve_next_job()
        assert nxt is not None and nxt.job_id == other
        queue._run_job(nxt)
        assert nxt.status == "completed"
    assert engine.prepared is None
    assert _history(session) == [] and published == []


def test_a_track_hidden_after_queueing_is_not_read(ocr_run):
    """The listing is re-read when the job runs: a track hidden meanwhile is not read."""
    from subtitles.tools.embedded_extract import TrackNotFound
    import subtitles.tools.embedded_ocr as ocr

    session, root, _events, published, engine, jobs = ocr_run
    session.execute(sa.update(TableMovies).values(subtitles=str([["en", None, None, "subrip"]])))
    session.commit()

    with pytest.raises(TrackNotFound, match="No embedded English PGS track is listed"):
        ocr.ocr_embedded("movie", 30, 1, "en", codec="pgs")

    assert engine.prepared is None and jobs.progress == []
    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists()
    assert _history(session) == [] and published == []


def test_an_inaccessible_configured_folder_fails_before_anything_is_read(ocr_run, monkeypatch):
    """A configured subtitle folder that cannot be created is named, not silently replaced."""
    from app.config import settings
    from app.jobs_queue import JobFailed
    import subtitles.tools.embedded_ocr as ocr

    session, root, _events, published, engine, jobs = ocr_run
    blocker = root / "blocked"
    blocker.write_text("not a directory")
    wanted = blocker / "subs"
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(wanted))

    with pytest.raises(JobFailed) as failed:
        ocr.ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    assert str(failed.value) == (f"Bazarr+ cannot write subtitles into {wanted} "
                                 f"(Not a directory). Nothing was read.")
    assert engine.prepared is None and jobs.progress == []
    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists()
    assert _history(session) == [] and published == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a folder whatever its mode")
def test_a_folder_bazarr_cannot_write_fails_the_job_before_anything_is_read(ocr_run):
    """A read-only library, or a user without write access: seconds, not a whole run, and the reason."""
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    session, root, _events, published, engine, jobs = ocr_run
    film = root / "owner1/movies/Film (2020)"
    film.chmod(0o555)
    try:
        with pytest.raises(JobFailed) as failed:
            ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)
    finally:
        film.chmod(0o755)

    assert str(failed.value) == f"Bazarr+ cannot write subtitles into {film} (Permission denied). Nothing was read."
    assert engine.prepared is None and jobs.progress == []
    assert os.listdir(film) == ["Film.mkv"]
    assert _history(session) == [] and published == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a folder whatever its mode")
def test_a_write_that_fails_after_the_run_names_the_file(ocr_run, monkeypatch):
    """The folder's access changes while the cues are read: the job names the file and why."""
    from app.jobs_queue import JobFailed
    import subtitles.tools.embedded_ocr as ocr

    session, root, _events, published, _engine, _jobs = ocr_run
    film = root / "owner1/movies/Film (2020)"

    class Locking(ScriptedEngine):
        @contextmanager
        def session(self):
            lines = iter(self.answers)

            def read(image):
                film.chmod(0o555)
                return next(lines)

            yield read

    monkeypatch.setattr(ocr, "ocr_engine", lambda: Locking(ENGLISH))
    try:
        with pytest.raises(JobFailed) as failed:
            ocr.ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)
    finally:
        film.chmod(0o755)

    assert str(failed.value) == f"Bazarr+ could not write {film / 'Film.en.srt'} (Permission denied)."
    assert os.listdir(film) == ["Film.mkv"]
    assert _history(session) == [] and published == []


def test_too_little_text_fails_the_job_and_writes_nothing(ocr_run):
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    session, root, _events, published, engine, _jobs = ocr_run
    engine.answers = ["", "", "Only line"]

    with pytest.raises(JobFailed, match="OCR produced too little text, wrong language pack or unreadable track"):
        ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists()
    assert _history(session) == [] and published == []


def test_the_vobsub_track_of_an_episode_and_the_second_instance(ocr_run):
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    session, root, events, _published, engine, _jobs = ocr_run
    engine.answers = ["Első sor", "Második sor", "Harmadik sor"]

    episode = ocr_embedded_track("episode", 50, 1, "hu", codec="vobsub", job_id=7)
    engine.answers = ENGLISH
    second = ocr_embedded_track("movie", 30, 2, "en", codec="pgs", job_id=8)

    assert episode == str(root / "owner1/tv/Show/Season 1/Show S01E01.hu.srt")
    assert _texts(episode) == ["Első sor", "Második sor", "Harmadik sor"]
    assert engine.prepared == "eng"
    assert [(row[0], row[1], row[2], row[5]) for row in _history(session, TableHistory)] == [
        (8, "hu", "ocr-scripted", "/tv/Show/Season 1/Show S01E01.hu.srt")]
    assert {"type": "episode", "payload": 1} in events
    assert second == str(root / "owner2/movies/Film (2020)/Film.en.srt")
    assert [(row[6], row[5]) for row in _history(session)] == [(2, "/movies/Film (2020)/Film.en.srt")]
    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists()


def test_the_upgrade_job_never_replaces_an_ocr_file(ocr_run, monkeypatch):
    """An older download of the language whose file was deleted must not be
    upgraded through the OCR'd file that now carries its name."""
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from subtitles import upgrade
    from subtitles.tools.embedded_ocr import ocr_embedded_track

    session = ocr_run[0]
    monkeypatch.setattr(upgrade, "database", session)
    monkeypatch.setattr(upgrade, "settings", SimpleNamespace(general=SimpleNamespace(
        days_to_upgrade_subs=365, upgrade_manual=False, upgrade_translated=False, upgrade_subs=True,
        use_embedded_subs=False)))
    session.execute(sa.insert(TableHistoryMovie).values(
        id=201, arr_instance_id=1, movie_id=1, radarrId=30, action=1, description="x", language="en",
        provider="opensubtitlescom", score=90, score_out_of=180,
        timestamp=datetime.now() - timedelta(minutes=60), video_path="/movies/Film (2020)/Film.mkv",
        subtitles_path="/movies/Film (2020)/Film.en.srt"))
    assert upgrade.get_upgradable_movies_subtitles() == {201: None}

    ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    assert upgrade.get_upgradable_movies_subtitles() == {}


def test_stopping_the_job_mid_read_ends_the_worker_and_writes_nothing(ocr_library, packs, monkeypatch):
    """The real engine: a stopped job leaves no Tesseract worker, no frames and no file behind."""
    from app.config import settings
    from app.jobs_queue import JobCancelled
    import subtitles.tools.embedded_ocr as ocr
    from subtitles.tools.ocr import pipeline, tesseract

    session, root, _events, published = ocr_library
    work = root / "ocr-work"
    monkeypatch.setattr(settings.ocr, "tessdata_dir", packs)
    monkeypatch.setattr(pipeline, "work_parent", lambda: str(work))
    jobs = Jobs(cancel_at="Reading cue 2")
    monkeypatch.setattr(ocr, "jobs_queue", jobs)
    workers = []
    real_worker = tesseract.TesseractWorker

    def recording_worker():
        workers.append(real_worker())
        return workers[-1]

    monkeypatch.setattr(tesseract, "TesseractWorker", recording_worker)

    with pytest.raises(JobCancelled):
        ocr.ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    assert workers and all(worker.process.returncode is not None for worker in workers)
    assert os.listdir(work) == []
    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists()
    assert _history(session) == [] and published == []


def test_stopping_a_translation_from_an_image_track_ends_it_cancelled_and_leaves_nothing(
        ocr_library, packs, monkeypatch):
    """The real engine inside the translation job: Stop ends the job Cancelled, with no worker, no frames,
    no half SRT next to the video and no failed-translation hold."""
    from app.config import settings
    from app.jobs_queue import JobCancelled
    import subtitles.tools.embedded_ocr as ocr
    from subtitles.tools.ocr import pipeline, tesseract
    from subtitles.tools.translate import failure_record, main

    session, root, _events, published = ocr_library
    work = root / "ocr-work"
    monkeypatch.setattr(settings.ocr, "tessdata_dir", packs)
    monkeypatch.setattr(pipeline, "work_parent", lambda: str(work))
    jobs = Jobs(cancel_at="Reading cue 2")
    monkeypatch.setattr(ocr, "jobs_queue", jobs)
    monkeypatch.setattr(main, "jobs_queue", jobs)
    monkeypatch.setattr(main, "activity", MagicMock())
    failure_record.clear_failed_translations()
    workers = []
    real_worker = tesseract.TesseractWorker

    def recording_worker():
        workers.append(real_worker())
        return workers[-1]

    monkeypatch.setattr(tesseract, "TesseractWorker", recording_worker)

    with pytest.raises(JobCancelled):
        main.translate_subtitles_file(
            "/movies/Film (2020)/Film.mkv", None, "en", "hu", False, False, "movie", None, None, 30, None,
            job_id=7, arr_instance_id=1,
            embedded_source={"language": "en", "hi": False, "forced": False, "codec": "pgs"})

    film = root / "owner1/movies/Film (2020)"
    assert workers and all(worker.process.returncode is not None for worker in workers)
    assert os.listdir(work) == []
    assert sorted(path.name for path in film.iterdir()) == ["Film.mkv"], "no half SRT, no translation"
    assert _history(session) == [] and published == []
    assert not failure_record.translation_recently_failed(1, "movie", 30, "en", "hu", False, False)
    # Stopped, the job is past its OCR phase: it must not keep OCR requests out.
    assert 7 in ocr._OCR_PHASE_OVER
    ocr._OCR_PHASE_OVER.discard(7)


def test_the_real_engine_reads_the_pgs_track(ocr_library, packs, monkeypatch):
    from app.config import settings
    import subtitles.tools.embedded_ocr as ocr

    session, root, _events, _published = ocr_library
    monkeypatch.setattr(settings.ocr, "tessdata_dir", packs)
    monkeypatch.setattr(ocr, "jobs_queue", Jobs())

    written = ocr.ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    assert _texts(written) == ["Hello there, how are you?", "We need to leave\nbefore the storm.", "I don't know."]
    assert [row[2] for row in _history(session)] == ["ocr-tesseract"]


def test_a_track_ffprobe_cannot_size_is_read_on_a_full_hd_canvas(ocr_library, packs, late_video, monkeypatch):
    """ffmpeg would otherwise draw the cues on the 64x64 video frame, too small to read."""
    from app.config import settings
    import subtitles.tools.embedded_ocr as ocr
    import subtitles.tools.translate.batch as batch

    _session, root, _events, _published = ocr_library
    film = root / "owner1/movies/Film (2020)"
    shutil.copyfile(late_video, film / "Film.mkv")
    monkeypatch.setattr(batch, "embedded_subtitle_tracks", lambda path, kind, owner=None: [
        {"format": "PGS", "language": "eng", "name": "", "forced": False, "hearing_impaired": False}])
    monkeypatch.setattr(settings.ocr, "tessdata_dir", packs)
    monkeypatch.setattr(ocr, "jobs_queue", Jobs())

    written = ocr.ocr_embedded_track("movie", 30, 1, "en", codec="pgs", job_id=7)

    events = pysubs2.load(written).events
    assert [event.plaintext for event in events] == ["Hello there, how are you?",
                                                    "We need to leave\nbefore the storm.", "I don't know."]
    assert [(event.start, event.end) for event in events] == [(31000, 33500), (35000, 37250), (39000, 41000)]

def test_render_progress_names_the_total():
    from subtitles.tools.embedded_ocr import progress_message

    assert progress_message("render", 5, 10) == "Rendering the image subtitles (50%)"
    assert progress_message("render", 5, 0) == "Rendering the image subtitles"

def test_an_old_row_is_probed_for_its_codec(ocr_library, monkeypatch):
    """A row stored before codecs were saved can still be read without a manual scan."""
    import subtitles.tools.translate.batch as batch
    from subtitles.tools.embedded_ocr import ocr_codec

    session = ocr_library[0]
    session.execute(sa.update(TableMovies).values(subtitles=str([["hu", None, None]])))
    session.commit()
    monkeypatch.setattr(batch, "embedded_subtitle_tracks", lambda *a, **k: [
        {"format": "PGS", "language": "hun", "forced": False, "hearing_impaired": False}])

    assert ocr_codec(_track(language="hu"), None) == "pgs"



@pytest.fixture
def probe_languages(monkeypatch):
    """The language table boot builds, with the custom Brazilian Portuguese code."""
    from languages import get_languages

    monkeypatch.setattr(get_languages, "languages_dict", [
        {"code2": "en", "code3": "eng", "name": "English", "code3b": "eng"},
        {"code2": "hu", "code3": "hun", "name": "Hungarian", "code3b": "hun"},
        {"code2": "pt", "code3": "por", "name": "Portuguese", "code3b": "por"},
        {"code2": "pb", "code3": "pob", "name": "Brazilian Portuguese", "code3b": None},
    ], raising=False)


def _probe_track(language="en", hi=False, forced=False):
    from types import SimpleNamespace

    return SimpleNamespace(video_path="/media/film.mkv", media_type="movie", arr_instance_id=1,
                           language=language, hi=hi, forced=forced)


def _probe(monkeypatch, tracks):
    import subtitles.tools.translate.batch as batch

    monkeypatch.setattr(batch, "embedded_subtitle_tracks", lambda *a, **k: tracks)


VARIANTS = [
    {"format": "PGS", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
    {"format": "SubRip", "language": "eng", "name": "SDH", "forced": False, "hearing_impaired": True},
    {"format": "VobSub", "language": "eng", "name": "", "forced": True, "hearing_impaired": False},
    {"format": "DVBSUB", "language": "eng", "name": "Forced", "forced": False, "hearing_impaired": False},
    {"format": "PGS", "language": "hun", "name": "", "forced": False, "hearing_impaired": False},
]


@pytest.mark.parametrize("hi,forced,found", [
    (False, False, ["pgs"]),
    (True, False, ["subrip"]),
    # The flag and a title that says "forced" are the same variant, as the indexer files them.
    (False, True, ["vobsub", "dvbsub"]),
])
def test_a_probe_matches_only_the_rows_own_variant(probe_languages, monkeypatch, hi, forced, found):
    from subtitles.tools.embedded_ocr import probed_codecs

    _probe(monkeypatch, VARIANTS)

    assert probed_codecs(_probe_track(hi=hi, forced=forced)) == found


def test_a_probe_skips_commentary_and_takes_the_undefined_default_like_the_indexer(probe_languages, monkeypatch):
    from app.config import settings
    from subtitles.tools.embedded_ocr import probed_codecs

    _probe(monkeypatch, [
        {"format": "PGS", "language": "eng", "name": "Director's Commentary", "forced": False,
         "hearing_impaired": False},
        {"format": "VobSub", "name": "", "forced": False, "hearing_impaired": False},  # no language tag
        {"format": "SubRip", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
    ])
    monkeypatch.setattr(settings.general, "default_und_embedded_subtitles_lang", "en")
    assert probed_codecs(_probe_track()) == ["vobsub", "subrip"]

    monkeypatch.setattr(settings.general, "default_und_embedded_subtitles_lang", "")
    assert probed_codecs(_probe_track()) == ["subrip"]


def test_a_probe_reads_a_bare_three_letter_tag_and_the_custom_brazilian_code(probe_languages, monkeypatch):
    from subtitles.tools.embedded_ocr import probed_codecs

    _probe(monkeypatch, [
        {"format": "PGS", "language": "por", "name": "Brazilian Portuguese", "forced": False,
         "hearing_impaired": False},
        {"format": "VobSub", "language": "por", "name": "Portuguese", "forced": False,
         "hearing_impaired": False},
        {"format": "SubRip", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
    ])

    assert probed_codecs(_probe_track(language="pb")) == ["pgs"]
    assert probed_codecs(_probe_track(language="pt")) == ["vobsub"]
    assert probed_codecs(_probe_track(language="en")) == ["subrip"]


def test_a_probe_that_finds_nothing_leaves_the_row_unknown_and_ocr_says_so(ocr_library, monkeypatch):
    """The fallback: the probe has no such track, so the row's own None stays and the refusal names it."""
    from subtitles.tools.embedded_extract import NotExtractable
    from subtitles.tools.embedded_ocr import ocr_codec

    session = ocr_library[0]
    session.execute(sa.update(TableMovies).values(subtitles=str([["hu", None, None]])))
    session.commit()
    _probe(monkeypatch, [{"format": "PGS", "language": "eng", "name": "", "forced": False,
                          "hearing_impaired": False}])

    with pytest.raises(NotExtractable, match="does not know the format of this Hungarian track yet"):
        ocr_codec(_track(language="hu"), None)


def test_an_old_row_of_the_hi_variant_is_probed_for_that_variant(ocr_library, monkeypatch):
    from subtitles.tools.embedded_ocr import ocr_codec

    session = ocr_library[0]
    session.execute(sa.update(TableMovies).values(subtitles=str([["en:hi", None, None]])))
    session.commit()
    _probe(monkeypatch, [
        {"format": "VobSub", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
        {"format": "PGS", "language": "eng", "name": "SDH", "forced": False, "hearing_impaired": True},
    ])

    assert ocr_codec(_track(language="en", hi=True), None) == "pgs"
