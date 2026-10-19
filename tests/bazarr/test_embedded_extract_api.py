# coding=utf-8
"""PATCH /api/subtitles with an empty path: Extract to file, and tools that extract first.

The request answers before any ffmpeg runs: 404, 409 (the file is already
there), 415 (a tool on an image-based track) or 202 with the job. The jobs
themselves run here too, against real files: the tool must change the file
next to the video, never the extraction cache, and translate keeps its
cache-only path.
"""
# ruff: noqa: F811
import os
from types import SimpleNamespace

import pytest

from test_embedded_extract_to_file import HEARING, fixture_video, library  # noqa: F401
from test_embedded_track_hidden import hidden_db  # noqa: F401


class Jobs:
    """Stands in for the queue: records names, runs nothing on its own."""

    def __init__(self):
        self.names, self.messages = [], []

    def add_job_from_function(self, name, is_progress=False, **kwargs):
        self.names.append(name)
        return 41

    def update_job_name(self, job_id, new_job_name):
        self.names.append(new_job_name)

    def update_job_progress(self, job_id, progress_message=None, **kwargs):
        if progress_message:
            self.messages.append(progress_message)

    def get_job_name(self, job_id):
        return self.names[-1] if self.names else ""


@pytest.fixture
def endpoint(library, monkeypatch):
    from api.subtitles import subtitles as api_mod
    import subtitles.tools.embedded_extract as extract

    session, root, _events, _published = library
    monkeypatch.setattr(api_mod, "database", session)
    queued = []
    monkeypatch.setattr(extract, "extract_embedded_track",
                        lambda *args, **kwargs: queued.append(("extract", args, kwargs)) or 41)
    monkeypatch.setattr(extract, "run_tool_on_embedded_track",
                        lambda *args, **kwargs: queued.append(("tool", args, kwargs)) or 42)

    def call(**overrides):
        args = {"action": "extract", "language": "en", "path": "", "type": "movie", "id": 30,
                "arr_instance_id": 1, "forced": "False", "hi": "False", "codec": None}
        args.update(overrides)
        stub = SimpleNamespace(patch_request_parser=SimpleNamespace(parse_args=lambda: args))
        return api_mod.Subtitles.patch.__wrapped__(stub)

    return call, queued, root


def test_extract_queues_one_job_and_names_the_file_it_will_write(endpoint):
    call, queued, root = endpoint

    body, status = call()

    assert status == 202
    assert body == {"job_id": 41, "path": str(root / "owner1/movies/Film (2020)/Film.en.srt")}
    assert queued == [("extract", ("movie", 30, 1, "en", False, False),
                       {"codec": None, "label": "Film, English"})]
    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists(), "the request runs no ffmpeg"


@pytest.mark.parametrize("codec,name", [("pgs", "Film.en.sup"), ("vobsub", "Film.en.idx"),
                                        ("subrip", "Film.en.srt"), (None, "Film.en.srt")])
def test_the_row_s_codec_names_the_file(endpoint, codec, name):
    call, queued, root = endpoint

    body, status = call(codec=codec)

    assert status == 202 and os.path.basename(body["path"]) == name
    assert queued[0][2]["codec"] == codec


def test_an_existing_file_answers_409_and_queues_nothing(endpoint, monkeypatch):
    from app.config import settings

    call, queued, root = endpoint
    (root / "owner1/movies/Film (2020)/Film.en.srt").write_text("mine")

    assert call() == ("A subtitle file named Film.en.srt already exists. Delete it first to extract "
                      "this track again.", 409)
    assert call(action="remove_HI") == ("A subtitle file named Film.en.srt already exists. Use the tool "
                                        "on that file's row instead.", 409)
    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "Subs")
    body, status = call()

    assert status == 409 and "Film.en.srt" in body
    assert queued == []


@pytest.mark.parametrize("flags", [{"hi": "True"}, {"forced": "True"}])
def test_remove_hi_never_replaces_the_plain_file_of_the_language(endpoint, flags):
    """Remove HI saves its result under the plain name, without the HI or
    forced tag, as it does for any file. A file there (a download, say) is
    not the one the user picked and must not be replaced."""
    call, queued, root = endpoint
    (root / "owner1/movies/Film (2020)/Film.en.srt").write_text("MY DOWNLOADED FILE")

    assert call(action="remove_HI", **flags) == ("A subtitle file named Film.en.srt already exists. Use the "
                                                 "tool on that file's row instead.", 409)
    # A tool that keeps the file's name still runs.
    assert call(action="OCR_fixes", **flags)[1] == 202
    assert [kind for kind, _args, _kwargs in queued] == ["tool"]


def test_a_tool_queues_extraction_and_the_tool_as_one_job(endpoint):
    call, queued, _root = endpoint

    body, status = call(action="remove_HI", hi="True")

    assert (status, body["job_id"]) == (202, 42)
    assert queued == [("tool", ("remove_HI", "movie", 30, 1, "en", True, False),
                       {"options": {"reference": None, "max_offset_seconds": None, "no_fix_framerate": False,
                                    "gss": False, "output_mode": None, "enabled_engines": None},
                        "label": "Film, English (HI)", "codec": None})]


def test_sync_options_travel_with_the_job(endpoint):
    call, queued, _root = endpoint

    call(action="sync", reference="a:0", max_offset_seconds="120", gss="True", no_fix_framerate="True",
         output_mode="keep_all", enabled_engines="ffsubsync")

    assert queued[0][2]["options"] == {"reference": "a:0", "max_offset_seconds": "120", "no_fix_framerate": True,
                                       "gss": True, "output_mode": "keep_all", "enabled_engines": "ffsubsync"}


@pytest.mark.parametrize("action", ["shift_offset(h=0,m=0,s=1,ms=0)", "change_FPS(from=23.976,to=25)",
                                    "color(name=white)", "OCR_fixes", "emoji", "common"])
def test_every_subtitle_tool_composes(endpoint, action):
    call, queued, _root = endpoint

    assert call(action=action)[1] == 202
    assert queued[0][1][0] == action


def test_a_tool_on_an_image_based_track_answers_415(endpoint):
    call, queued, _root = endpoint

    body, status = call(action="sync", codec="pgs")

    assert status == 415 and "image-based" in body
    assert queued == []


@pytest.mark.parametrize("overrides,status", [
    ({"action": "delete_everything"}, 400),
    ({"action": "extract", "path": "/movies/Film (2020)/Film.en.srt"}, 400),
    ({"language": "xx"}, 400),
    ({"id": 31}, 404),
    ({"arr_instance_id": 3}, 404),
])
def test_refusals(endpoint, overrides, status):
    call, queued, _root = endpoint

    assert call(**overrides)[1] == status
    assert queued == []


def test_the_second_instance_extracts_from_its_own_mount(endpoint):
    call, queued, root = endpoint

    body, status = call(arr_instance_id=2)

    assert status == 202 and body["path"] == str(root / "owner2/movies/Film (2020)/Film.en.srt")
    assert queued[0][1][2] == 2


def test_translate_keeps_its_cache_only_path(endpoint, monkeypatch):
    """A translation from an embedded track writes only the translated file."""
    from api.subtitles import subtitles as api_mod

    call, queued, _root = endpoint
    translated = {}
    monkeypatch.setattr(api_mod, "translate_subtitles_file", lambda **kw: translated.update(kw))

    assert call(action="translate", language="hu", from_language="en") == ("", 204)

    assert queued == []
    assert translated["embedded_source"] == {"language": "en", "hi": False, "forced": False}


# The queued jobs, run the way the queue runs them.

@pytest.fixture
def jobs(monkeypatch):
    import subtitles.tools.embedded_extract as extract
    import subtitles.tools.mods as mods

    queue = Jobs()
    for module in (extract, mods):
        monkeypatch.setattr(module, "jobs_queue", queue)
    # The mod's own post-processing announces the change; no socket here.
    monkeypatch.setattr("app.event_handler.event_stream", lambda **kw: None)
    return queue


def test_remove_hi_changes_the_file_next_to_the_video_not_the_cache(library, jobs):
    from subtitles.tools.embedded_extract import run_tool_on_embedded_track

    _session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"

    run_tool_on_embedded_track("remove_HI", "movie", 30, 1, "en", hi=True, job_id=7)

    # Remove HI writes the plain file and drops the HI one, next to the video.
    assert "[DOOR SLAMS]" not in (film / "Film.en.srt").read_text()
    assert "English with sounds" in (film / "Film.en.srt").read_text()
    assert not (film / "Film.en.hi.srt").exists()
    cache = root / "config" / "extracted_subs"
    cached = [path for path in os.listdir(cache) if path.endswith(".hi.srt")]
    assert len(cached) == 1 and "[DOOR SLAMS]" in (cache / cached[0]).read_text()
    assert jobs.messages[0] == "Extracting the embedded subtitle track"
    assert jobs.names[-1] == "Remove HI Tags: Film.en.hi.srt"


def test_the_tool_job_never_replaces_a_plain_file_that_appeared_since(library, jobs):
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_extract import run_tool_on_embedded_track

    _session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    (film / "Film.en.srt").write_text("MY DOWNLOADED FILE")

    with pytest.raises(JobFailed, match="Film.en.srt already exists.*Film.en.hi.srt was extracted and kept"):
        run_tool_on_embedded_track("remove_HI", "movie", 30, 1, "en", hi=True, job_id=7)

    assert (film / "Film.en.srt").read_text() == "MY DOWNLOADED FILE"
    assert "[DOOR SLAMS]" in (film / "Film.en.hi.srt").read_text()


def test_a_tool_never_runs_on_another_variant_s_text(library, jobs, monkeypatch):
    """A row indexed before the codec was stored, whose forced English track
    is the PGS one: the full English SRT must not be extracted in its place."""
    import test_embedded_extract_to_file as extract_tests
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_extract import run_tool_on_embedded_track

    _session, root, _events, _published = library
    monkeypatch.setattr(extract_tests, "TRACKS", [dict(extract_tests.TRACKS[0], forced=True),
                                                  *extract_tests.TRACKS[1:]])

    with pytest.raises(JobFailed, match="could not be read as text"):
        run_tool_on_embedded_track("remove_HI", "movie", 30, 1, "en", forced=True, job_id=7)
    assert not list((root / "owner1/movies/Film (2020)").glob("Film.en.*"))


def test_sync_runs_on_the_extracted_file(library, jobs, monkeypatch):
    from subtitles.tools.embedded_extract import run_tool_on_embedded_track

    _session, root, _events, _published = library
    synced = {}
    monkeypatch.setattr("subtitles.sync.sync_subtitles", lambda **kw: synced.update(kw) or True)

    run_tool_on_embedded_track("sync", "movie", 30, 1, "en", options={"reference": None, "gss": True}, job_id=7)

    video = root / "owner1/movies/Film (2020)/Film.mkv"
    assert synced["srt_path"] == str(video.with_suffix(".en.srt"))
    assert (synced["video_path"], synced["reference"], synced["radarr_id"], synced["job_id"]) == (
        str(video), str(video), 30, 7)
    assert synced["gss"] is True and synced["force_sync"] is True


def test_a_tool_on_a_track_that_is_not_text_fails_the_job(library, jobs):
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_extract import run_tool_on_embedded_track

    _session, root, _events, _published = library
    with pytest.raises(JobFailed, match="could not be read as text"):
        run_tool_on_embedded_track("remove_HI", "movie", 30, 1, "hu", job_id=7)
    assert not list((root / "owner1/movies/Film (2020)").glob("Film.hu.*"))


def test_the_extract_job_names_what_it_wrote_and_fails_with_the_reason(library, jobs):
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_extract import extract_embedded_track

    _session, root, _events, _published = library

    assert extract_embedded_track("movie", 30, 1, "en", job_id=7) == [
        str(root / "owner1/movies/Film (2020)/Film.en.srt")]
    assert jobs.names[-1] == "Extracted Film.en.srt"
    with pytest.raises(JobFailed, match="Film.en.srt already exists"):
        extract_embedded_track("movie", 30, 1, "en", job_id=8)


@pytest.mark.parametrize("name", ["Film.srt", "Film.ass", "Film.vtt"])
def test_single_language_mode_answers_409_for_the_language_less_download(endpoint, monkeypatch, name):
    """A download in single-language mode is <video>.<ext>, in its own
    format: a text extraction or a tool must not write a second Film.en.srt
    beside it."""
    from app.config import settings

    call, queued, root = endpoint
    monkeypatch.setattr(settings.general, "single_language", True)
    (root / "owner1/movies/Film (2020)" / name).write_text("MY DOWNLOADED FILE")

    assert call() == (f"A subtitle file named {name} already exists. Delete it first to extract this track "
                      "again.", 409)
    assert call(action="OCR_fixes")[1] == 409
    # An image copy is not a second text subtitle.
    body, status = call(codec="pgs")
    assert status == 202 and os.path.basename(body["path"]) == "Film.en.sup"
    assert [kind for kind, _args, _kwargs in queued] == ["extract"]


def test_a_tool_carries_the_row_s_text_codec(endpoint):
    call, queued, _root = endpoint

    assert call(action="OCR_fixes", codec="ASS")[1] == 202
    assert queued[0][2]["codec"] == "ass"


def test_a_tool_runs_on_the_text_track_of_the_row_s_codec(library, jobs, monkeypatch):
    """Two plain English text tracks with different codecs are two rows: the
    tool runs on the one the user picked."""
    import test_embedded_extract_to_file as extract_tests
    from subtitles.tools.embedded_extract import run_tool_on_embedded_track

    _session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    monkeypatch.setattr(extract_tests, "TRACKS", [
        *extract_tests.TRACKS[:3], dict(extract_tests.TRACKS[3], format="ASS", name="", hearing_impaired=False)])

    run_tool_on_embedded_track("OCR_fixes", "movie", 30, 1, "en", codec="ass", job_id=7)

    assert "English with sounds" in (film / "Film.en.srt").read_text()
