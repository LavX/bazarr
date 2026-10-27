# coding=utf-8
"""Mass OCR: the image tracks of a selection read into SRTs, one after another.

Collected from the stored listing like every mass action, so a hidden track is
never read. Only PGS and VobSub tracks of a subtitle the title's profile wants
(its language, HI and forced, and the item's audio rules), and only when that
subtitle has no file and no embedded text track. The database cases run on
SQLite and PostgreSQL, both AUTOCOMMIT.
"""
# ruff: noqa: F811
import json
import os
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.database import (TableEpisodes, TableLanguagesProfiles, TableMovies, TableShows, TableSportsEvents,
                          TableSportsLeagues)
from test_embedded_extract_to_file import fixture_video, library  # noqa: F401
from test_embedded_ocr_to_file import ocr_library, ocr_run  # noqa: F401
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_embedded_track_hidden_indexer import _stub_indexer
from test_mass_extract import mass  # noqa: F401


@pytest.fixture(autouse=True)
def _languages(languages_loaded):
    """The module-level language table the audio rule's name lookups read.

    The application loads it at startup; these cases build their own database.
    """
    return languages_loaded


def _profile(session, profile_id, *wanted):
    """A profile wanting each of ``wanted``: "en", "en:hi" or "en:forced"."""
    session.add(TableLanguagesProfiles(
        profileId=profile_id, name=f"P{profile_id}", cutoff=None, originalFormat=0,
        items=json.dumps([{"id": index, "language": key.split(":")[0], "hi": str(key.endswith(":hi")),
                           "forced": str(key.endswith(":forced")), "audio_exclude": "False"}
                          for index, key in enumerate(wanted, 1)])))
    session.flush()


def _items(mass_operations, request):
    items, skipped = mass_operations._collect_subtitle_items(request, "ocr", {})
    return [(item["srt_lang"], item["hi"], item["forced"], item["codec"], item["arr_instance_id"],
             item["radarr_id"] or item["sonarr_episode_id"] or item.get("sports_event_id"))
            for item in items], skipped


def test_only_wanted_image_tracks_without_a_file_or_a_text_track_are_read(mass):
    mass_operations, session = mass
    _profile(session, 1, "en", "hu", "de", "fr", "es")
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                            profileId=1, subtitles=str([
                                ["en", None, None, "pgs"],
                                # Hungarian: a plain VobSub track and an HI PGS one. The
                                # profile wants plain Hungarian: the VobSub is read.
                                ["hu", None, None, "vobsub"], ["hu:hi", None, None, "pgs"],
                                # German has a file already.
                                ["de", "/movies/m.de.srt", 10], ["de", None, None, "pgs"],
                                # French has an embedded text track: Extract gives it.
                                ["fr", None, None, "subrip"], ["fr", None, None, "pgs"],
                                # Spanish is DVB, and Italian is not wanted.
                                ["es", None, None, "dvbsub"], ["it", None, None, "pgs"],
                                # Two English image tracks under one key: one read.
                                ["en", None, None, "pgs"]])))
    session.commit()

    items, skipped = _items(mass_operations, [{"type": "movie", "radarrId": 7, "arr_instance_id": 1}])

    assert items == [("en", False, False, "pgs", 1, 7), ("hu", False, False, "vobsub", 1, 7)]
    # German and French were wanted and left alone.
    assert skipped == 2


@pytest.mark.parametrize("wanted,listing,read,skipped", [
    # A profile wanting plain English reads the plain track, not its HI and forced siblings.
    ({("en", False, False)}, ["en", "en:hi", "en:forced"], ["en"], 0),
    # HI subtitles fill a want for plain ones, as they do for wanted subtitles.
    ({("en", False, False)}, ["en:hi", "en:forced"], ["en:hi"], 0),
    ({("en", True, False)}, ["en", "en:hi"], ["en:hi"], 0),
    ({("en", True, False)}, ["en"], [], 0),
    ({("en", False, True)}, ["en", "en:hi", "en:forced"], ["en:forced"], 0),
    ({("en", False, False), ("en", False, True)}, ["en:forced", "en"], ["en:forced", "en"], 0),
])
def test_the_profile_s_hi_and_forced_choose_the_variant_read(wanted, listing, read, skipped):
    from subtitles.mass_operations import _ocr_sources

    sources = _ocr_sources(str([[key, None, None, "pgs"] for key in listing]), wanted)

    assert sources == ([(key, "pgs") for key in read], skipped)


def test_a_file_or_text_track_of_the_hi_variant_covers_a_plain_want():
    from subtitles.mass_operations import _ocr_sources

    assert _ocr_sources(str([["en", None, None, "pgs"], ["en:hi", "/m/m.en.hi.srt", 10]]),
                        {("en", False, False)}) == ([], 1)
    assert _ocr_sources(str([["en", None, None, "pgs"], ["en:hi", None, None, "subrip"]]),
                        {("en", False, False)}) == ([], 1)
    # A plain file does not cover an HI want.
    assert _ocr_sources(str([["en:hi", None, None, "pgs"], ["en", "/m/m.en.srt", 10]]),
                        {("en", True, False)}) == ([("en:hi", "pgs")], 0)


def test_a_combined_or_sync_artifact_does_not_cover_the_image_track():
    from subtitles.mass_operations import _ocr_sources

    pgs = ["en", None, None, "pgs"]
    want = {("en", False, False)}
    # A combined subtitle and a generated sync output are not the English
    # subtitle the profile wants: the track is still read.
    assert _ocr_sources(str([pgs, ["en:combined-hu", "/m/m.en.combined-hu.srt", 10]]),
                        want) == ([("en", "pgs")], 0)
    assert _ocr_sources(str([pgs, ["en", "/m/m.en.alass.srt", 10]]), want) == ([("en", "pgs")], 0)
    assert _ocr_sources(str([pgs, ["en", "/m/m.en.ffsubsync.srt", 10]]), want) == ([("en", "pgs")], 0)
    # The same holds for the HI sibling that fills a plain want, and for an HI want.
    assert _ocr_sources(str([pgs, ["en:hi:combined-hu", "/m/m.en.hi.combined-hu.srt", 10]]),
                        want) == ([("en", "pgs")], 0)
    assert _ocr_sources(str([["en:hi", None, None, "pgs"], ["en:hi:combined-hu", "/m/m.en.hi.combined-hu.srt", 10]]),
                        {("en", True, False)}) == ([("en:hi", "pgs")], 0)
    # A real file still covers it, whatever generated files sit beside it, and a
    # forced file still covers a forced want while a plain want is unaffected.
    assert _ocr_sources(str([pgs, ["en:combined-hu", "/m/m.en.combined-hu.srt", 10],
                             ["en", "/m/m.en.srt", 10]]), want) == ([], 1)
    assert _ocr_sources(str([["en:forced", None, None, "pgs"], ["en:forced", "/m/m.en.forced.srt", 10]]),
                        {("en", False, True)}) == ([], 1)
    assert _ocr_sources(str([pgs, ["en:forced", "/m/m.en.forced.srt", 10]]), want) == ([("en", "pgs")], 0)


def test_mass_ocr_queues_the_track_a_combined_artifact_sits_beside(mass):
    mass_operations, session = mass
    _profile(session, 1, "en")
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                            profileId=1, subtitles=str([
                                ["en", None, None, "pgs"],
                                ["en:combined-hu", "/movies/m.en.combined-hu.srt", 10]])))
    session.commit()

    items, skipped = _items(mass_operations, [{"type": "movie", "radarrId": 7, "arr_instance_id": 1}])

    assert items == [("en", False, False, "pgs", 1, 7)]
    assert skipped == 0


@pytest.mark.parametrize("language", ["en", "en:hi", "en:forced"])
@pytest.mark.parametrize("combined", [True, False], ids=["combined", "standalone"])
def test_episode_ocr_coverage_ignores_combined_but_keeps_real_subtitles(mass, language, combined):
    mass_operations, session = mass
    _profile(session, 1, language)
    session.add(TableShows(id=1, arr_instance_id=2, sonarrSeriesId=5, title="S", path="/tv", profileId=1))
    session.flush()
    external_language = f"{language}:combined-hu" if combined else language
    session.add(TableEpisodes(
        id=1, arr_instance_id=2, series_id=1, sonarrSeriesId=5, sonarrEpisodeId=50, title="E",
        season=1, episode=1, path="/tv/e.mkv", subtitles=str([
            [language, None, None, "pgs"], [external_language, "/tv/e.en.srt", 10]])))
    session.commit()

    items, skipped = _items(mass_operations, [{"type": "episode", "sonarrEpisodeId": 50, "arr_instance_id": 2}])

    expected = [("en", language == "en:hi", language == "en:forced", "pgs", 2, 50)] if combined else []
    assert items == expected
    assert skipped == (0 if combined else 1)


@pytest.mark.parametrize("rules,audio,read", [
    # "English, exclude if the audio is English": no English track is read.
    ({"audio_exclude": "True"}, ["English"], []),
    ({"audio_exclude": "True"}, ["French"], ["en"]),
    # "English, only if the audio is English": a French-audio title reads none.
    ({"audio_only_include": "True"}, ["French"], []),
    ({"audio_only_include": "True"}, ["English"], ["en"]),
])
def test_the_profile_s_audio_rules_drop_wants(mass, languages_loaded, rules, audio, read):
    mass_operations, session = mass
    session.add(TableLanguagesProfiles(
        profileId=1, name="P1", cutoff=None, originalFormat=0,
        items=json.dumps([{"id": 1, "language": "en", "hi": "False", "forced": "False", **rules}])))
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                            profileId=1, audio_language=str(audio),
                            subtitles=str([["en", None, None, "pgs"]])))
    session.commit()

    items, _skipped = _items(mass_operations, [{"type": "movie", "radarrId": 7, "arr_instance_id": 1}])

    assert [item[0] for item in items] == read


def test_a_title_without_a_profile_reads_nothing(mass):
    mass_operations, session = mass
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                            subtitles=str([["en", None, None, "pgs"]])))
    session.commit()

    assert _items(mass_operations, [{"type": "movie", "radarrId": 7, "arr_instance_id": 1}]) == ([], 0)


def test_mass_ocr_never_reads_a_hidden_track(mass, monkeypatch):
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track

    mass_operations, session = mass
    _profile(session, 1, "en", "fr")
    _stub_indexer(mv, session, monkeypatch, [])
    monkeypatch.setattr(mv, "embedded_subs_reader", lambda *a, **kw: [
        ["eng", False, False, "PGS"], ["fra", False, False, "PGS"]])
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv",
                            tmdbId="1", movie_file_id=700, file_size=111, profileId=1, subtitles="[]"))
    session.commit()
    hide_track(session, "movie", 1, 1, "fr")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)

    items, _skipped = _items(mass_operations, [{"type": "movie", "radarrId": 7, "arr_instance_id": 1}])

    assert [language for language, *_rest in items] == ["en"]


def test_episodes_follow_their_series_profile_and_sports_their_league_s(mass):
    mass_operations, session = mass
    _profile(session, 1, "hu")
    _profile(session, 2, "fr:forced")
    session.add_all([
        TableShows(id=1, arr_instance_id=2, sonarrSeriesId=5, title="S", path="/tv", profileId=1),
        TableSportsLeagues(id=51, arr_instance_id=3, sportarrLeagueId=7, title="L", profileId=2),
    ])
    session.flush()
    session.add_all([
        TableEpisodes(id=1, arr_instance_id=2, series_id=1, sonarrSeriesId=5, sonarrEpisodeId=50, title="E",
                      season=1, episode=1, path="/tv/e.mkv",
                      subtitles=str([["hu", None, None, "vobsub"], ["en", None, None, "pgs"]])),
        TableSportsEvents(id=61, arr_instance_id=3, league_id=51, sportarrEventId=8, file_id=9, title="Card",
                          path="/sports/event.mkv",
                          subtitles=str([["fr:forced", None, None, "pgs"], ["hu", None, None, "pgs"]])),
    ])
    session.commit()

    assert _items(mass_operations, [{"type": "series", "sonarrSeriesId": 5, "arr_instance_id": 2}]) == (
        [("hu", False, False, "vobsub", 2, 50)], 0)
    assert _items(mass_operations, [{"type": "sports", "sportsEventId": 61, "arr_instance_id": 3}]) == (
        [("fr", False, True, "pgs", 3, 61)], 0)


def test_the_batch_job_is_named_after_the_action():
    from api.subtitles.batch import ACTION_LABELS
    from subtitles.mass_operations import VALID_ACTIONS

    assert "ocr" in VALID_ACTIONS
    assert ACTION_LABELS["ocr"] == "Reading Image Subtitles with OCR"


def test_each_item_is_read_in_turn_and_a_taken_name_is_a_skip(mass, monkeypatch):
    from app.jobs_queue import JobFailed
    from subtitles.new_subtitle import SubtitleFileExists
    from subtitles.tools.ocr import tesseract
    from subtitles.tools.ocr.errors import TooLittleText
    import subtitles.tools.embedded_ocr as ocr

    mass_operations, session = mass
    _profile(session, 1, "en", "hu", "fr")
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                            profileId=1, subtitles=str([["en", None, None, "pgs"], ["hu", None, None, "vobsub"],
                                                        ["fr", None, None, "pgs"]])))
    session.commit()
    calls, messages = [], []

    def read(media_type, media_id, owner, language, hi=False, forced=False, codec=None, *, progress=None):
        calls.append((media_type, media_id, owner, language, codec))
        progress("recognise", 2, 9)
        if language == "hu":
            raise SubtitleFileExists("/local1/movies/m.hu.srt")
        if language == "fr":
            raise TooLittleText("OCR produced too little text, wrong language pack or unreadable track")
        return "/local1/movies/m.en.srt", None

    monkeypatch.setattr(tesseract, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_embedded", read)
    monkeypatch.setattr(mass_operations, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kw: messages.append(kw.get("progress_message")),
        update_job_name=lambda **kw: None))

    with pytest.raises(JobFailed, match="1 of 3 items failed .1 done, 1 skipped.*too little text"):
        mass_operations.mass_batch_operation([{"type": "movie", "radarrId": 7, "arr_instance_id": 1}],
                                             "ocr", job_id=5)
    assert calls == [("movie", 7, 1, "en", "pgs"), ("movie", 7, 1, "hu", "vobsub"), ("movie", 7, 1, "fr", "pgs")]
    assert "ocr: m.mkv (en track): Reading cue 2 of 9" in messages


def test_a_stopped_batch_stops_the_item_being_read(mass, monkeypatch):
    from app.jobs_queue import JobCancelled
    from subtitles.tools.ocr import tesseract
    import subtitles.tools.embedded_ocr as ocr

    mass_operations, session = mass
    _profile(session, 1, "en", "hu")
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                            profileId=1, subtitles=str([["en", None, None, "pgs"], ["hu", None, None, "pgs"]])))
    session.commit()
    calls = []

    def read(media_type, media_id, owner, language, hi=False, forced=False, codec=None, *, progress=None):
        calls.append(language)
        progress("recognise", 1, 9)
        return "/local1/movies/m.en.srt", None

    def update_job_progress(**kwargs):
        if "Reading cue" in (kwargs.get("progress_message") or ""):
            raise JobCancelled("stopped")

    monkeypatch.setattr(tesseract, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_embedded", read)
    monkeypatch.setattr(mass_operations, "jobs_queue", SimpleNamespace(
        update_job_progress=update_job_progress, update_job_name=lambda **kw: None))

    with pytest.raises(JobCancelled):
        mass_operations.mass_batch_operation([{"type": "movie", "radarrId": 7, "arr_instance_id": 1}],
                                             "ocr", job_id=5)
    assert calls == ["en"]


def test_mass_ocr_that_cannot_run_here_fails_before_reading_anything(monkeypatch):
    from app.jobs_queue import JobFailed
    from subtitles import mass_operations
    from subtitles.tools.ocr import tesseract

    monkeypatch.setattr(tesseract, "ocr_available", lambda: False)
    monkeypatch.setattr(mass_operations, "_collect_subtitle_items",
                        lambda *a, **kw: pytest.fail("nothing is collected"))

    with pytest.raises(JobFailed, match="OCR is not available on this installation") as failed:
        mass_operations.mass_batch_operation([{"type": "movie", "radarrId": 7}], "ocr", job_id=5)
    assert failed.value.retryable is False


def test_mass_ocr_while_another_ocr_job_is_queued_fails_before_reading_anything(monkeypatch):
    """One OCR job at a time: the batch does not start while a row's OCR to text is queued or running."""
    from app.jobs_queue import JobFailed
    from subtitles import mass_operations
    import subtitles.tools.embedded_ocr as ocr
    from subtitles.tools.ocr import tesseract

    monkeypatch.setattr(tesseract, "ocr_available", lambda: True)
    monkeypatch.setattr(mass_operations, "_collect_subtitle_items",
                        lambda *a, **kw: pytest.fail("nothing is collected"))
    monkeypatch.setattr(ocr, "jobs_queue", SimpleNamespace(list_jobs_from_queue=lambda **kwargs: [
        {"job_id": 5, "func": "mass_batch_operation", "status": "running", "kwargs": {"action": "ocr"}},
        {"job_id": 4, "func": "ocr_embedded_track", "status": "pending", "kwargs": {}}]))

    with pytest.raises(JobFailed, match="^Another OCR run is queued or running"):
        mass_operations.mass_batch_operation([{"type": "movie", "radarrId": 7}], "ocr", job_id=5)


def _batch_ocr_call(monkeypatch, queued):
    from flask import Flask
    from api.subtitles import batch as batch_mod
    import subtitles.tools.embedded_ocr as ocr

    fed = []
    monkeypatch.setattr(batch_mod, "jobs_queue", SimpleNamespace(
        feed_jobs_pending_queue=lambda **kwargs: fed.append(kwargs)))
    monkeypatch.setattr(ocr, "jobs_queue", SimpleNamespace(list_jobs_from_queue=lambda **kwargs: queued))
    body = {"action": "ocr", "items": [{"type": "movie", "radarrId": 7}], "options": {}}
    app = Flask(__name__)
    with app.test_request_context("/api/subtitles/batch", method="POST", json=body):
        answer = batch_mod.BatchOperation.post.__wrapped__(batch_mod.BatchOperation())
    return answer, fed


def test_the_batch_route_refuses_a_second_ocr_before_queueing_anything(monkeypatch):
    from subtitles.tools.embedded_ocr import BUSY

    answer, fed = _batch_ocr_call(monkeypatch, [
        {"job_id": 4, "func": "ocr_embedded_track", "status": "running", "kwargs": {}}])

    assert answer == ({"error": BUSY}, 409)
    assert fed == []


def test_the_batch_route_queues_a_mass_ocr_when_no_ocr_job_is_queued(monkeypatch):
    answer, fed = _batch_ocr_call(monkeypatch, [])

    assert answer[1] == 200
    assert [entry["kwargs"]["action"] for entry in fed] == ["ocr"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a folder whatever its mode")
def test_a_folder_bazarr_cannot_write_fails_the_item_at_once(ocr_run, monkeypatch):
    """The real OCR path: an item whose folder is read-only fails before anything is read, naming why."""
    from app.jobs_queue import JobFailed
    from subtitles import mass_operations
    from subtitles.tools.ocr import tesseract

    session, root, _events, published, engine, _jobs = ocr_run
    film = root / "owner1/movies/Film (2020)"
    monkeypatch.setattr(mass_operations, "database", session)
    monkeypatch.setattr(tesseract, "ocr_available", lambda: True)
    monkeypatch.setattr(mass_operations, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kw: None, update_job_name=lambda **kw: None))
    _profile(session, 1, "en", "hu")
    session.execute(sa.update(TableMovies).values(profileId=1))
    session.commit()
    film.chmod(0o555)
    try:
        with pytest.raises(JobFailed) as failed:
            mass_operations.mass_batch_operation([{"type": "movie", "radarrId": 30, "arr_instance_id": 1}],
                                                 "ocr", job_id=5)
    finally:
        film.chmod(0o755)

    # English has embedded text tracks (a skip); the Hungarian VobSub fails at once.
    assert str(failed.value).startswith("Mass ocr: 1 of 1 items failed (0 done, 1 skipped).")
    assert f"Film.mkv (hu track) (Bazarr+ cannot write subtitles into {film} (Permission denied)" in str(failed.value)
    assert engine.prepared is None and published == []

@pytest.mark.parametrize("rule,read", [
    ("audio_exclude", []),
    ("audio_only_include", ["en"]),
])
def test_sports_mass_ocr_applies_the_profile_s_audio_rules(mass, rule, read):
    """Gate item 1: a sports event's own audio decides its wants like a movie's."""
    mass_operations, session = mass
    session.add(TableLanguagesProfiles(
        profileId=9, name="P9", cutoff=None, originalFormat=0,
        items=json.dumps([{"id": 1, "language": "en", "hi": "False", "forced": "False",
                           "audio_exclude": str(rule == "audio_exclude"),
                           "audio_only_include": str(rule == "audio_only_include")}])))
    session.flush()
    session.add(TableSportsLeagues(id=51, arr_instance_id=3, sportarrLeagueId=7, title="L", profileId=9))
    session.flush()
    session.add(TableSportsEvents(id=61, arr_instance_id=3, league_id=51, sportarrEventId=8, file_id=9,
                                  title="Card", path="/sports/event.mkv", audio_language=str(["English"]),
                                  subtitles=str([["en", None, None, "pgs"]])))
    session.commit()

    items, _skipped = mass_operations._collect_subtitle_items(
        [{"type": "sports", "sportsEventId": 61, "arr_instance_id": 3}], "ocr", {})
    assert [item["srt_lang"] for item in items] == read

