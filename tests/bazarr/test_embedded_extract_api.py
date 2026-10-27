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
    # Remove HI reads the owner's keep-lyrics override; this owner has none,
    # and a test run on its own must not need the global database.
    monkeypatch.setattr("arr_instances.resolution._instance_subtitle_settings", lambda *a, **kw: {})
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


@pytest.mark.parametrize("name", ["Film.srt", "Film.ass", "Film.vtt", "Film.ASS"])
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


def test_remove_hi_never_replaces_a_plain_file_that_lands_after_the_job_s_check(library, jobs, monkeypatch):
    """A download can land on Film.en.srt after the job's own check and before
    the mod takes its lock. The mod refuses it under the lock, and keeps the
    extracted file it read."""
    from contextlib import contextmanager

    import subtitles.tools.mods as mods
    from app.jobs_queue import JobFailed
    from subtitles.tools.embedded_extract import run_tool_on_embedded_track

    _session, root, _events, published = library
    film = root / "owner1/movies/Film (2020)"
    real_locks = mods.subtitle_write_locks

    @contextmanager
    def download_lands_first(*args, **kwargs):
        (film / "Film.en.srt").write_text("MY DOWNLOADED FILE")
        with real_locks(*args, **kwargs) as states:
            yield states

    monkeypatch.setattr(mods, "subtitle_write_locks", download_lands_first)

    with pytest.raises(JobFailed, match="Film.en.srt already exists.*Film.en.hi.srt was extracted and kept"):
        run_tool_on_embedded_track("remove_HI", "movie", 30, 1, "en", hi=True, job_id=7)

    assert (film / "Film.en.srt").read_text() == "MY DOWNLOADED FILE"
    assert "[DOOR SLAMS]" in (film / "Film.en.hi.srt").read_text()
    # Only the extraction was published, never a mod output.
    assert [event.operation for event in published] == ["extract"]


def test_the_mod_s_exclusive_write_refuses_a_file_created_after_its_check(library, jobs, tmp_path, monkeypatch):
    """Another program can create the name between the mod's check and its
    publish: the no-replace writer refuses it, and the source stays."""
    import subtitles.new_subtitle as new_subtitle
    import subtitles.tools.mods as mods
    from subtitles.new_subtitle import SubtitleFileExists

    tmp_path = tmp_path / "loose"
    tmp_path.mkdir()
    video = tmp_path / "Film.mkv"
    video.write_bytes(b"video")
    source = tmp_path / "Film.en.hi.srt"
    source.write_text(HEARING)
    target = tmp_path / "Film.en.srt"
    staging = new_subtitle._new_staging_file

    def imported_meanwhile(directory):
        made = staging(directory)
        target.write_text("imported meanwhile")
        return made

    monkeypatch.setattr(new_subtitle, "_new_staging_file", imported_meanwhile)
    with pytest.raises(SubtitleFileExists):
        mods.subtitles_apply_mods("en", str(source), ["remove_HI"], str(video), never_replace=True)

    assert target.read_text() == "imported meanwhile"
    assert "[DOOR SLAMS]" in source.read_text()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["Film.en.hi.srt", "Film.en.srt", "Film.mkv"]


@pytest.mark.parametrize("failure", ["write", "close", "remove-source"])
def test_a_failed_never_replace_mod_leaves_the_source_and_no_destination(library, jobs, tmp_path, monkeypatch,
                                                                       failure):
    """A write or close failure must leave no partial result, and a source
    that cannot be removed must leave the result taken back: either way the
    source is untouched and a retry finds the name free."""
    import errno

    import subtitles.new_subtitle as new_subtitle
    import subtitles.tools.mods as mods

    folder = tmp_path / "failing"
    folder.mkdir()
    video = folder / "Film.mkv"
    video.write_bytes(b"video")
    source = folder / "Film.en.hi.srt"
    source.write_text(HEARING)
    target = folder / "Film.en.srt"

    with monkeypatch.context() as broken:
        if failure == "write":
            def no_space(fd, data):
                raise OSError(errno.ENOSPC, "No space left on device")

            broken.setattr(new_subtitle, "_write_all", no_space)
        elif failure == "close":
            real_close = new_subtitle.os.close
            calls = []

            def failing_close(fd):
                real_close(fd)
                if not calls:
                    calls.append(fd)
                    raise OSError(errno.EIO, "Input/output error")

            broken.setattr(new_subtitle.os, "close", failing_close)
        else:
            real_remove = mods.os.remove

            def locked_source(path):
                if str(path) == str(source):
                    raise PermissionError(errno.EACCES, "Permission denied", str(path))
                return real_remove(path)

            broken.setattr(mods.os, "remove", locked_source)
        with pytest.raises(OSError):
            mods.subtitles_apply_mods("en", str(source), ["remove_HI"], str(video), never_replace=True)

    assert sorted(path.name for path in folder.iterdir()) == ["Film.en.hi.srt", "Film.mkv"]
    assert source.read_text() == HEARING
    # Nothing is left in the way of another try.
    assert mods.subtitles_apply_mods("en", str(source), ["remove_HI"], str(video), never_replace=True) == str(target)
    assert "[DOOR SLAMS]" not in target.read_text() and not source.exists()


def test_remove_hi_carries_the_extraction_to_the_file_it_saves(library, jobs, monkeypatch):
    """Remove HI on an extracted HI track saves Film.en.srt and drops
    Film.en.hi.srt. An older plain-English download recorded at Film.en.srt,
    whose file was deleted before, must not become the owner of the result:
    the upgrade job would replace it with a provider download."""
    from datetime import datetime, timedelta

    import sqlalchemy as sa

    from app.database import TableHistoryMovie
    from subtitles import upgrade
    from subtitles.tools.embedded_extract import run_tool_on_embedded_track

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    session.execute(sa.insert(TableHistoryMovie).values(
        action=1, radarrId=30, movie_id=1, arr_instance_id=1, language="en", provider="opensubtitlescom",
        score=90, score_out_of=180, video_path="/movies/Film (2020)/Film.mkv", description="downloaded",
        subtitles_path="/movies/Film (2020)/Film.en.srt", timestamp=datetime.now() - timedelta(hours=1)))
    session.commit()

    run_tool_on_embedded_track("remove_HI", "movie", 30, 1, "en", hi=True, job_id=7)

    session.expire_all()
    # One extraction, one arrival: its row moved to the file Remove HI saved,
    # with the plain language, and nothing names the removed HI file.
    assert session.execute(sa.select(TableHistoryMovie.language, TableHistoryMovie.subtitles_path,
                                     TableHistoryMovie.description)
                           .where(TableHistoryMovie.action == 8)).all() == [
        ("en", "/movies/Film (2020)/Film.en.srt",
         "English (HI) subtitles extracted from the video file, then Remove HI Tags.")]
    assert (film / "Film.en.srt").is_file()

    calls = []
    monkeypatch.setattr(upgrade, "database", session)
    monkeypatch.setattr(upgrade, "settings", SimpleNamespace(general=SimpleNamespace(
        days_to_upgrade_subs=365, upgrade_manual=False, upgrade_translated=False, upgrade_subs=True,
        use_embedded_subs=True,
        use_sportarr=False)))
    monkeypatch.setattr(upgrade, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kw: None, update_job_name=lambda **kw: None))
    monkeypatch.setattr(upgrade, "get_providers", lambda: ["opensubtitlescom"])
    monkeypatch.setattr(upgrade, "_language_still_desired", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "_is_hi_required", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "get_audio_profile_languages", lambda audio: [])
    monkeypatch.setattr(upgrade.path_mappings, "path_replace_movie", lambda path: str(root / "owner1") + path)
    monkeypatch.setattr(upgrade, "generate_subtitles",
                        lambda *args, **kwargs: calls.append(kwargs["previous_subtitles_to_delete"]) or [])

    assert upgrade.get_upgradable_movies_subtitles() == {}
    upgrade.upgrade_movies_subtitles(job_id=1)

    assert calls == []
    assert (film / "Film.en.srt").is_file()


def test_the_current_path_guard_holds_without_the_carried_row(library, jobs, monkeypatch):
    """The candidate's own path, not only the fallback's, is checked: a
    download row whose recorded path now holds an extracted file is left
    alone even where its own language key still makes it the latest row."""
    from datetime import datetime, timedelta

    import sqlalchemy as sa

    from app.database import TableHistoryMovie, TableMovies
    from subtitles import upgrade

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    (film / "Film.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nExtracted\n\n")
    session.execute(sa.update(TableMovies).where(TableMovies.id == 1).values(
        subtitles=str([["en", "/movies/Film (2020)/Film.en.srt", 10]])))
    for action, language, minutes in ((1, "en:hi", 60), (8, "en", 1)):
        session.execute(sa.insert(TableHistoryMovie).values(
            action=action, radarrId=30, movie_id=1, arr_instance_id=1, language=language, provider="x",
            score=90, score_out_of=180, video_path="/movies/Film (2020)/Film.mkv", description="x",
            subtitles_path="/movies/Film (2020)/Film.en.srt",
            timestamp=datetime.now() - timedelta(minutes=minutes)))
    session.commit()
    calls = []
    monkeypatch.setattr(upgrade, "database", session)
    monkeypatch.setattr(upgrade, "settings", SimpleNamespace(general=SimpleNamespace(
        days_to_upgrade_subs=365, upgrade_manual=False, upgrade_translated=False, upgrade_subs=True,
        use_embedded_subs=True,
        use_sportarr=False)))
    monkeypatch.setattr(upgrade, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kw: None, update_job_name=lambda **kw: None))
    monkeypatch.setattr(upgrade, "get_providers", lambda: ["opensubtitlescom"])
    monkeypatch.setattr(upgrade, "_language_still_desired", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "_is_hi_required", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "get_audio_profile_languages", lambda audio: [])
    monkeypatch.setattr(upgrade.path_mappings, "path_replace_movie", lambda path: str(root / "owner1") + path)
    monkeypatch.setattr(upgrade, "generate_subtitles",
                        lambda *args, **kwargs: calls.append(kwargs["previous_subtitles_to_delete"]) or [])
    assert list(upgrade.get_upgradable_movies_subtitles()) != []

    upgrade.upgrade_movies_subtitles(job_id=1)

    assert calls == []


def test_single_language_mode_lets_a_vobsub_pair_be(endpoint, monkeypatch):
    from app.config import settings

    call, queued, root = endpoint
    monkeypatch.setattr(settings.general, "single_language", True)
    (root / "owner1/movies/Film (2020)/Film.idx").write_text("# VobSub index file, v7")
    (root / "owner1/movies/Film (2020)/Film.sub").write_bytes(b"\x00\x00\x01\xba")

    body, status = call()

    assert status == 202 and os.path.basename(body["path"]) == "Film.en.srt"


def test_an_in_place_mod_on_an_extracted_file_keeps_its_lineage(library, jobs, monkeypatch):
    """A tool on the extracted file's own row rewrites it in place and logs
    no history. A newer download of another variant whose file is gone must
    still not adopt it through the fallback."""
    from datetime import datetime, timedelta

    import sqlalchemy as sa

    import subtitles.tools.mods as mods
    from app.database import TableHistoryMovie
    from subtitles import upgrade
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    materialise_embedded_track("movie", 30, 1, "en")
    before = (film / "Film.en.srt").read_bytes()
    mods.subtitles_apply_mods("en", str(film / "Film.en.srt"), ["fix_uppercase", "common"], str(film / "Film.mkv"))
    (film / "Film.en.srt").write_text((film / "Film.en.srt").read_text() + "\n")
    assert (film / "Film.en.srt").read_bytes() != before
    session.execute(sa.insert(TableHistoryMovie).values(
        action=1, radarrId=30, movie_id=1, arr_instance_id=1, language="en:hi", provider="opensubtitlescom",
        score=90, score_out_of=180, video_path="/movies/Film (2020)/Film.mkv", description="downloaded",
        subtitles_path="/movies/Film (2020)/Film.en.hi.srt", timestamp=datetime.now() + timedelta(minutes=1)))
    session.commit()
    calls = []
    monkeypatch.setattr(upgrade, "database", session)
    monkeypatch.setattr(upgrade, "settings", SimpleNamespace(general=SimpleNamespace(
        days_to_upgrade_subs=365, upgrade_manual=False, upgrade_translated=False, upgrade_subs=True,
        use_embedded_subs=True,
        use_sportarr=False)))
    monkeypatch.setattr(upgrade, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kw: None, update_job_name=lambda **kw: None))
    monkeypatch.setattr(upgrade, "get_providers", lambda: ["opensubtitlescom"])
    monkeypatch.setattr(upgrade, "_language_still_desired", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "_is_hi_required", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "get_audio_profile_languages", lambda audio: [])
    monkeypatch.setattr(upgrade.path_mappings, "path_replace_movie", lambda path: str(root / "owner1") + path)
    monkeypatch.setattr(upgrade, "generate_subtitles",
                        lambda *args, **kwargs: calls.append(kwargs["previous_subtitles_to_delete"]) or [])
    assert list(upgrade.get_upgradable_movies_subtitles()) != []

    upgrade.upgrade_movies_subtitles(job_id=1)

    assert calls == []


def test_a_failed_mod_never_takes_back_a_file_another_program_put_there(library, jobs, tmp_path, monkeypatch):
    """The result is published, then removing the source fails. If another
    program replaced the result in between, the rollback leaves its file."""
    import errno

    import subtitles.tools.mods as mods

    folder = tmp_path / "replaced"
    folder.mkdir()
    video = folder / "Film.mkv"
    video.write_bytes(b"video")
    source = folder / "Film.en.hi.srt"
    source.write_text(HEARING)
    target = folder / "Film.en.srt"
    real_remove = mods.os.remove

    def replaced_then_locked(path):
        if str(path) == str(source):
            foreign = folder / "foreign.tmp"
            foreign.write_text("imported by another program")
            os.replace(foreign, target)
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_remove(path)

    monkeypatch.setattr(mods.os, "remove", replaced_then_locked)
    with pytest.raises(PermissionError):
        mods.subtitles_apply_mods("en", str(source), ["remove_HI"], str(video), never_replace=True)

    assert target.read_text() == "imported by another program"
    assert source.read_text() == HEARING


@pytest.mark.parametrize("failures", [1, 2], ids=["fails-once", "keeps-failing"])
def test_a_failed_history_move_never_fails_a_successful_remove_hi(library, jobs, monkeypatch, failures):
    """The file is written, published and indexed before the extraction's row
    moves to it. A database error there must not fail the job; a passing one
    is retried, so the result keeps its lineage."""
    import sqlalchemy as sa

    import subtitles.tools.embedded_extract as extract
    from app.database import TableHistoryMovie

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    real_move = extract.carry_extraction_lineage
    attempts = []

    def flaky_move(*args, **kwargs):
        attempts.append(1)
        if len(attempts) <= failures:
            raise sa.exc.OperationalError("UPDATE table_history_movie", {}, Exception("database is locked"))
        return real_move(*args, **kwargs)

    monkeypatch.setattr(extract, "carry_extraction_lineage", flaky_move)

    output = extract.run_tool_on_embedded_track("remove_HI", "movie", 30, 1, "en", hi=True, job_id=7)

    assert output == str(film / "Film.en.srt")
    assert "English with sounds" in (film / "Film.en.srt").read_text()
    assert not (film / "Film.en.hi.srt").exists()
    assert jobs.names[-1] == "Remove HI Tags: Film.en.hi.srt"
    session.expire_all()
    rows = session.execute(sa.select(TableHistoryMovie.language, TableHistoryMovie.subtitles_path)
                           .where(TableHistoryMovie.action == 8)).all()
    if failures == 1:
        assert rows == [("en", "/movies/Film (2020)/Film.en.srt")]
    else:
        # Logged and left: the job succeeded, the row still names the HI file.
        assert rows == [("en:hi", "/movies/Film (2020)/Film.en.hi.srt")]
    assert len(attempts) == 2


def _upgrade_calls(session, root, monkeypatch):
    """Run the movie upgrade job up to the download it would start."""
    from subtitles import upgrade

    calls = []
    monkeypatch.setattr(upgrade, "database", session)
    monkeypatch.setattr(upgrade, "settings", SimpleNamespace(general=SimpleNamespace(
        days_to_upgrade_subs=365, upgrade_manual=False, upgrade_translated=False, upgrade_subs=True,
        use_embedded_subs=True,
        use_sportarr=False)))
    monkeypatch.setattr(upgrade, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kw: None, update_job_name=lambda **kw: None))
    monkeypatch.setattr(upgrade, "get_providers", lambda: ["opensubtitlescom"])
    monkeypatch.setattr(upgrade, "_language_still_desired", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "_is_hi_required", lambda language, profile: True)
    monkeypatch.setattr(upgrade, "get_audio_profile_languages", lambda audio: [])
    monkeypatch.setattr(upgrade.path_mappings, "path_replace_movie", lambda path: str(root / "owner1") + path)
    monkeypatch.setattr(upgrade, "generate_subtitles",
                        lambda *args, **kwargs: calls.append(kwargs["previous_subtitles_to_delete"]) or [])
    upgrade.upgrade_movies_subtitles(job_id=1)
    return calls


def _plain_download_at_the_result_name(session, minutes):
    from datetime import datetime, timedelta

    import sqlalchemy as sa

    from app.database import TableHistoryMovie

    session.execute(sa.insert(TableHistoryMovie).values(
        action=1, radarrId=30, movie_id=1, arr_instance_id=1, language="en", provider="opensubtitlescom",
        score=90, score_out_of=180, video_path="/movies/Film (2020)/Film.mkv", description="downloaded",
        subtitles_path="/movies/Film (2020)/Film.en.srt", timestamp=datetime.now() + timedelta(minutes=minutes)))
    session.commit()


def _extraction_rows(session):
    import sqlalchemy as sa

    from app.database import TableHistoryMovie

    session.expire_all()
    return session.execute(sa.select(TableHistoryMovie.language, TableHistoryMovie.subtitles_path)
                           .where(TableHistoryMovie.action == 8)).all()


@pytest.mark.parametrize("download", ["before-the-extraction", "after-the-extraction"])
def test_remove_hi_from_the_file_s_own_row_carries_the_lineage(library, jobs, monkeypatch, download):
    """The extracted HI file is an ordinary external row: Remove HI from its
    own menu renames it to the plain name. A plain download recorded at that
    name, whose file is gone, must not adopt the result, even when it was
    recorded after the extraction: the moved row is the newest at the name."""
    import subtitles.tools.mods as mods
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    if download == "before-the-extraction":
        _plain_download_at_the_result_name(session, -60)
    materialise_embedded_track("movie", 30, 1, "en", hi=True)
    if download == "after-the-extraction":
        _plain_download_at_the_result_name(session, 0)

    assert mods.apply_subtitle_mods(language="en", subtitle_path=str(film / "Film.en.hi.srt"), mods=["remove_HI"],
                                    video_path=str(film / "Film.mkv"), media_type="movie", media_id=30, job_id=9,
                                    arr_instance_id=1) == str(film / "Film.en.srt")

    assert _extraction_rows(session) == [("en", "/movies/Film (2020)/Film.en.srt")]
    assert _upgrade_calls(session, root, monkeypatch) == []


def test_remove_hi_as_a_bulk_action_carries_the_lineage(library, jobs, monkeypatch):
    from subtitles import mass_operations
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    _plain_download_at_the_result_name(session, -60)
    materialise_embedded_track("movie", 30, 1, "en", hi=True)
    monkeypatch.setattr(mass_operations, "database", session)
    monkeypatch.setattr(mass_operations, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kw: None, update_job_name=lambda **kw: None))

    result = mass_operations.mass_batch_operation([{"type": "movie", "radarrId": 30, "arr_instance_id": 1}],
                                                  "remove_HI", job_id=5)

    assert result["queued"] == 1 and result["errors"] == []
    assert (film / "Film.en.srt").is_file() and not (film / "Film.en.hi.srt").exists()
    assert _extraction_rows(session) == [("en", "/movies/Film (2020)/Film.en.srt")]
    assert _upgrade_calls(session, root, monkeypatch) == []


@pytest.mark.parametrize("when", ["before-it-is-sampled", "after-publication"])
def test_the_mod_rollback_knows_its_own_file_from_the_writer(library, jobs, tmp_path, monkeypatch, when):
    """The mod's own result is known by the file the writer wrote, not by
    reading the name back, so a replacement that lands right after the
    publish is never taken for the mod's file."""
    import errno

    import subtitles.new_subtitle as new_subtitle
    import subtitles.tools.mods as mods

    folder = tmp_path / "early"
    folder.mkdir()
    video = folder / "Film.mkv"
    video.write_bytes(b"video")
    source = folder / "Film.en.hi.srt"
    source.write_text(HEARING)
    target = folder / "Film.en.srt"

    def foreign_replacement():
        foreign = folder / "foreign.tmp"
        foreign.write_text("imported by another program")
        os.replace(foreign, target)

    real_write = new_subtitle.write_bytes_atomically

    def write_then_replaced(path, data, **kwargs):
        identity = real_write(path, data, **kwargs)
        if when == "before-it-is-sampled":
            foreign_replacement()
        return identity

    real_remove = mods.os.remove

    def locked_source(path):
        if str(path) == str(source):
            if when == "after-publication":
                foreign_replacement()
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_remove(path)

    monkeypatch.setattr(new_subtitle, "write_bytes_atomically", write_then_replaced)
    monkeypatch.setattr(mods.os, "remove", locked_source)
    with pytest.raises(PermissionError):
        mods.subtitles_apply_mods("en", str(source), ["remove_HI"], str(video), never_replace=True)

    assert target.read_text() == "imported by another program"
    assert source.read_text() == HEARING


def test_a_source_already_gone_counts_as_removed(library, jobs, tmp_path, monkeypatch):
    """Another program removing the source between the write and its removal
    must not cost the result too."""
    import subtitles.tools.mods as mods

    folder = tmp_path / "vanished"
    folder.mkdir()
    video = folder / "Film.mkv"
    video.write_bytes(b"video")
    source = folder / "Film.en.hi.srt"
    source.write_text(HEARING)
    real_remove = mods.os.remove

    def already_gone(path):
        if str(path) == str(source):
            real_remove(path)
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_remove(path)

    monkeypatch.setattr(mods.os, "remove", already_gone)

    assert mods.subtitles_apply_mods("en", str(source), ["remove_HI"], str(video),
                                     never_replace=True) == str(folder / "Film.en.srt")
    assert "English with sounds" in (folder / "Film.en.srt").read_text() and not source.exists()
