# coding=utf-8
"""Extract to file: the file that lands next to the video, its name, its history row.

Real ffmpeg on a real MKV, and the real movie and episode indexers, on an
AUTOCOMMIT engine of each kind (SQLite, and PostgreSQL when
BAZARR_PG_TEST_URL is set). Only the probe is fixed: TRACKS is what knowit
reports for the fixture's streams, so the tests do not depend on how a probe
names them.
"""
import ast
import os
import shutil
import subprocess

import pytest
import sqlalchemy as sa
from subliminal_patch.score import MAX_SCORES

from app.database import TableEpisodes, TableHistory, TableHistoryMovie, TableMovies, TableShows
from ocr_fixtures import FIXTURES, mux_mkv
from test_embedded_track_hidden import hidden_db  # noqa: F401

PLAIN = "1\n00:00:01,000 --> 00:00:02,000\nPlain English line\n\n"
HEARING = "1\n00:00:01,000 --> 00:00:02,000\n[DOOR SLAMS] English with sounds\n\n"

LANGUAGES = [
    {"code2": "en", "code3": "eng", "code3b": "eng", "name": "English"},
    {"code2": "hu", "code3": "hun", "code3b": "hun", "name": "Hungarian"},
    {"code2": "fr", "code3": "fra", "code3b": "fre", "name": "French"},
]

# What knowit reports for the fixture's streams, in stream order.
TRACKS = [
    {"format": "PGS", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
    {"format": "VobSub", "language": "hun", "name": "", "forced": False, "hearing_impaired": False},
    {"format": "SubRip", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
    {"format": "SubRip", "language": "eng", "name": "SDH", "forced": False, "hearing_impaired": True},
]


@pytest.fixture(scope="module")
def fixture_video(tmp_path_factory):
    """A PGS and a VobSub stream from the OCR fixtures, then two English SRTs.

    The text streams are added in a second pass: mux_mkv reads every input
    with -fix_sub_duration, which drops a text file's last cue.
    """
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg and shutil.which("ffprobe"), "these tests need ffmpeg and ffprobe on PATH (CI installs them)"
    folder = tmp_path_factory.mktemp("extract-media")
    (folder / "plain.srt").write_text(PLAIN)
    (folder / "hearing.srt").write_text(HEARING)
    sup = os.path.join(FIXTURES, "eng_hd.sup")
    bitmap = mux_mkv(ffmpeg, str(folder / "bitmap.mkv"), [(sup, "copy", "eng"), (sup, "dvdsub", "hun")])
    source = str(folder / "source.mkv")
    subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", bitmap,
                    "-i", str(folder / "plain.srt"), "-i", str(folder / "hearing.srt"),
                    "-map", "0", "-map", "1:s", "-map", "2:s", "-c", "copy", "-c:s:2", "srt", "-c:s:3", "srt",
                    "-metadata:s:s:2", "language=eng", "-metadata:s:s:3", "language=eng", source], check=True)
    return source


@pytest.fixture
def library(hidden_db, fixture_video, monkeypatch, tmp_path):  # noqa: F811
    """A movie and an episode of one owner, a movie of a second owner, all real files."""
    from app.config import settings
    from languages import get_languages
    import radarr.history
    import sonarr.history
    import subtitles.indexer.movies as mv
    import subtitles.indexer.series as se
    import subtitles.tools.embedded_extract as extract
    import subtitles.tools.translate.batch as batch

    session = hidden_db
    monkeypatch.setattr(get_languages, "languages_dict", LANGUAGES, raising=False)
    monkeypatch.setattr("utilities.video_analyzer._handle_alpha3", lambda track: track.get("language"))
    for name, value in (("use_embedded_subs", True), ("subfolder", "current"), ("subfolder_custom", ""),
                        ("chmod_enabled", False), ("default_und_embedded_subtitles_lang", ""),
                        ("single_language", False)):
        monkeypatch.setattr(settings.general, name, value)
    monkeypatch.setenv("SZ_HI_EXTENSION", "hi")
    for module in (extract, radarr.history, sonarr.history, mv, se):
        monkeypatch.setattr(module, "database", session)
    for module in (radarr.history, sonarr.history):
        monkeypatch.setattr(module, "event_stream", lambda **kw: None)
    events = []
    monkeypatch.setattr(extract, "event_stream", lambda **kw: events.append(kw))
    monkeypatch.setattr(batch, "embedded_subtitle_tracks", lambda path, kind, owner=None: TRACKS)
    monkeypatch.setattr(batch, "extracted_subtitles_dir", lambda: str(tmp_path / "config" / "extracted_subs"))
    published = []
    monkeypatch.setattr("media_servers.events.notify_subtitle_mutation", published.append)
    # The real indexers, fed the same tracks the probe would report. Only the
    # embedded half is fixed; the external files are found on disk.
    for module in (mv, se):
        monkeypatch.setattr(module, "embedded_subs_reader", lambda *a, **kw: [
            [t["language"], t["forced"], t["hearing_impaired"], t["format"]] for t in TRACKS])
        monkeypatch.setattr(module, "hidden_languages", lambda *a, **kw: set())
        monkeypatch.setattr(module, "get_language_set", lambda: set())
    monkeypatch.setattr(mv, "list_missing_subtitles_movies", lambda *a, **kw: None)
    monkeypatch.setattr(mv, "_log_embedded_history_movie", lambda *a, **kw: None)
    monkeypatch.setattr(se, "list_missing_subtitles", lambda *a, **kw: None)
    monkeypatch.setattr(se, "_log_embedded_history", lambda *a, **kw: None)

    def mapped(path, owner, kind):
        return str(tmp_path / f"owner{owner}") + path

    def reversed_(path, owner, kind):
        return path.replace(str(tmp_path / f"owner{owner}"), "", 1)

    monkeypatch.setattr(extract.path_mappings, "path_replace_instance", mapped)
    monkeypatch.setattr(extract.path_mappings, "path_replace_reverse_instance", reversed_)
    for owner, video in ((1, "/movies/Film (2020)/Film.mkv"), (1, "/tv/Show/Season 1/Show S01E01.mkv"),
                         (2, "/movies/Film (2020)/Film.mkv")):
        target = tmp_path / f"owner{owner}" / video.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixture_video, target)
    session.add_all([
        TableMovies(id=1, arr_instance_id=1, radarrId=30, title="Film", tmdbId="1",
                    path="/movies/Film (2020)/Film.mkv", subtitles="[]"),
        TableMovies(id=2, arr_instance_id=2, radarrId=30, title="Film", tmdbId="1",
                    path="/movies/Film (2020)/Film.mkv", subtitles="[]"),
        TableShows(id=1, arr_instance_id=1, sonarrSeriesId=5, title="Show", path="/tv/Show"),
    ])
    session.flush()
    session.add(TableEpisodes(id=1, arr_instance_id=1, series_id=1, sonarrSeriesId=5, sonarrEpisodeId=50,
                              title="Pilot", season=1, episode=1, subtitles="[]",
                              path="/tv/Show/Season 1/Show S01E01.mkv"))
    session.commit()
    return session, tmp_path, events, published


def _history(session, table):
    session.expire_all()
    return session.execute(sa.select(table.action, table.language, table.provider, table.score,
                                     table.score_out_of, table.video_path, table.subtitles_path,
                                     table.arr_instance_id).order_by(table.id)).all()


def _listing(session, table, row_id):
    session.expire_all()
    return ast.literal_eval(session.execute(sa.select(table.subtitles).where(table.id == row_id)).scalar_one())


def test_a_text_track_lands_next_to_the_video_and_becomes_an_external_row(library):
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, events, published = library
    film = root / "owner1/movies/Film (2020)"

    written = materialise_embedded_track("movie", 30, 1, "en")

    assert written == [str(film / "Film.en.srt")]
    assert (film / "Film.en.srt").read_text().startswith("1\n00:00:01,000 --> 00:00:02,000\nPlain English line")
    assert _history(session, TableHistoryMovie) == [
        (8, "en", "embedded", MAX_SCORES["movie"], MAX_SCORES["movie"], "/movies/Film (2020)/Film.mkv",
         "/movies/Film (2020)/Film.en.srt", 1)]
    # The real indexer ran: the file is now an external row beside the tracks.
    assert ["en", "/movies/Film (2020)/Film.en.srt", os.path.getsize(written[0])] in _listing(session, TableMovies, 1)
    assert {"type": "movie", "payload": 1} in events
    assert [(event.operation, event.subtitle_path) for event in published] == [
        ("extract", str(film / "Film.en.srt"))]


def test_hi_and_forced_are_named_like_downloads(library, monkeypatch):
    from subtitles.tools.embedded_extract import materialise_embedded_track, output_paths

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    monkeypatch.setenv("SZ_HI_EXTENSION", "sdh")

    written = materialise_embedded_track("movie", 30, 1, "en", hi=True)

    assert written == [str(film / "Film.en.sdh.srt")]
    assert "[DOOR SLAMS]" in (film / "Film.en.sdh.srt").read_text()
    assert _history(session, TableHistoryMovie)[0][1] == "en:hi"
    assert output_paths(str(film / "Film.mkv"), "en", False, True, (".srt",)) == [str(film / "Film.en.forced.srt")]


def test_the_score_is_the_track_s_own_embedded_score(library):
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, _root, _events, _published = library
    session.execute(sa.insert(TableHistoryMovie).values(
        action=7, radarrId=30, movie_id=1, arr_instance_id=1, language="en", provider="embedded",
        score=177, score_out_of=180, video_path="/movies/Film (2020)/Film.mkv", description="x",
        timestamp=sa.func.now()))

    materialise_embedded_track("movie", 30, 1, "en")

    assert [row[3] for row in _history(session, TableHistoryMovie) if row[0] == 8] == [177]


@pytest.mark.parametrize("mode", ["current", "relative", "absolute"])
def test_the_configured_subtitle_folder_is_used(library, monkeypatch, mode):
    from app.config import settings
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    custom = {"current": "", "relative": "Subs", "absolute": str(root / "all-subs")}[mode]
    monkeypatch.setattr(settings.general, "subfolder", mode)
    monkeypatch.setattr(settings.general, "subfolder_custom", custom)
    expected = {"current": film, "relative": film / "Subs", "absolute": root / "all-subs"}[mode]

    written = materialise_embedded_track("movie", 30, 1, "en")

    assert written == [str(expected / "Film.en.srt")]
    assert (expected / "Film.en.srt").is_file()


def test_an_existing_file_is_never_overwritten(library, monkeypatch):
    from app.config import settings
    from subtitles.new_subtitle import SubtitleFileExists
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, _events, published = library
    film = root / "owner1/movies/Film (2020)"
    (film / "Film.en.srt").write_text("mine")

    with pytest.raises(SubtitleFileExists):
        materialise_embedded_track("movie", 30, 1, "en")
    # The same name next to the video counts when a subfolder is configured:
    # the indexer reads both places.
    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "Subs")
    with pytest.raises(SubtitleFileExists):
        materialise_embedded_track("movie", 30, 1, "en")

    assert (film / "Film.en.srt").read_text() == "mine"
    assert not (film / "Subs" / "Film.en.srt").exists()
    assert _history(session, TableHistoryMovie) == []
    assert published == []


def test_image_based_tracks_land_as_they_are(library):
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"

    assert materialise_embedded_track("movie", 30, 1, "en", codec="pgs") == [str(film / "Film.en.sup")]
    assert materialise_embedded_track("movie", 30, 1, "hu", codec="vobsub") == [
        str(film / "Film.hu.idx"), str(film / "Film.hu.sub")]
    # A row indexed before the codec was stored: Hungarian has no text track,
    # so the image track is copied out as it is rather than refused.
    (film / "Film.hu.idx").unlink()
    (film / "Film.hu.sub").unlink()
    assert materialise_embedded_track("movie", 30, 1, "hu") == [str(film / "Film.hu.idx"), str(film / "Film.hu.sub")]
    assert [(row[0], row[1], row[6]) for row in _history(session, TableHistoryMovie)] == [
        (8, "en", "/movies/Film (2020)/Film.en.sup"), (8, "hu", "/movies/Film (2020)/Film.hu.idx"),
        (8, "hu", "/movies/Film (2020)/Film.hu.idx")]
    # The indexer lists neither half of the VobSub pair nor the .sup.
    assert [entry for entry in _listing(session, TableMovies, 1) if entry[1]] == []
    # The image dumps are not kept: their work folders are gone.
    assert not [name for name in os.listdir(root / "config" / "extracted_subs") if name.startswith(".bitmap-")]


def test_tools_refuse_an_image_based_track(library):
    from subtitles.tools.embedded_extract import NotExtractable, materialise_embedded_track

    _session, root, _events, _published = library
    with pytest.raises(NotExtractable):
        materialise_embedded_track("movie", 30, 1, "hu", text_only=True)
    assert not list((root / "owner1/movies/Film (2020)").glob("Film.hu.*"))


@pytest.mark.parametrize("tracks,flags,name", [
    # The forced English track is the PGS one: the full English SRT must not
    # land as Film.en.forced.srt, which players would show as forced.
    ([dict(TRACKS[0], forced=True), *TRACKS[1:]], {"forced": True}, "Film.en.forced.sup"),
    # The plain English track is the PGS one (the plain SRT is a commentary,
    # never listed): the SDH text must not land as Film.en.srt.
    ([*TRACKS[:2], dict(TRACKS[2], name="Commentary"), TRACKS[3]], {}, "Film.en.sup"),
])
def test_a_row_without_a_codec_gets_its_own_track_and_never_another_variant(library, monkeypatch, tracks,
                                                                            flags, name):
    """Rows indexed before the codec was stored carry none, so the text
    extractor is asked first and must not stand another variant in."""
    import sys

    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    monkeypatch.setattr(sys.modules[__name__], "TRACKS", tracks)

    assert materialise_embedded_track("movie", 30, 1, "en", **flags) == [str(film / name)]
    assert {path.name for path in film.iterdir()} == {"Film.mkv", name}


def test_a_taken_image_name_stops_the_copy_before_ffmpeg_reads_the_video(library, monkeypatch):
    """.sup, .idx and .sub files are never listed, so a second mass extract
    collects the same image tracks again: each one stops at its name."""
    import subprocess

    import subtitles.tools.translate.batch as batch
    from subtitles.new_subtitle import SubtitleFileExists
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, _root, _events, published = library
    materialise_embedded_track("movie", 30, 1, "en", codec="pgs")
    materialise_embedded_track("movie", 30, 1, "hu")
    runs = []
    real_run = subprocess.run
    monkeypatch.setattr(batch.subprocess, "run", lambda command, *args, **kwargs: (
        runs.append(command) or real_run(command, *args, **kwargs)))

    with pytest.raises(SubtitleFileExists, match="Film.en.sup"):
        materialise_embedded_track("movie", 30, 1, "en", codec="pgs")
    # A row with no codec: the probe names the VobSub track, and its names
    # are checked before the copy.
    with pytest.raises(SubtitleFileExists, match="Film.hu.idx"):
        materialise_embedded_track("movie", 30, 1, "hu")

    assert runs == []
    assert [row[0] for row in _history(session, TableHistoryMovie)] == [8, 8]
    assert len(published) == 2


def test_the_second_instance_extracts_from_its_own_mount(library):
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, _events, _published = library

    written = materialise_embedded_track("movie", 30, 2, "en")

    assert written == [str(root / "owner2/movies/Film (2020)/Film.en.srt")]
    assert not (root / "owner1/movies/Film (2020)/Film.en.srt").exists()
    assert [row[7] for row in _history(session, TableHistoryMovie)] == [2]
    assert _listing(session, TableMovies, 1) == []


def test_an_episode_track_is_logged_on_the_episode_history(library):
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, events, _published = library
    season = root / "owner1/tv/Show/Season 1"

    written = materialise_embedded_track("episode", 50, 1, "en")

    assert written == [str(season / "Show S01E01.en.srt")]
    assert _history(session, TableHistory) == [
        (8, "en", "embedded", MAX_SCORES["episode"], MAX_SCORES["episode"], "/tv/Show/Season 1/Show S01E01.mkv",
         "/tv/Show/Season 1/Show S01E01.en.srt", 1)]
    assert ["en", "/tv/Show/Season 1/Show S01E01.en.srt", os.path.getsize(written[0])] in _listing(
        session, TableEpisodes, 1)
    assert {"type": "series", "payload": 5} in events and {"type": "episode", "payload": 1} in events


def test_an_unknown_row_is_not_found(library):
    from subtitles.tools.embedded_extract import TrackNotFound, materialise_embedded_track

    with pytest.raises(TrackNotFound):
        materialise_embedded_track("movie", 30, 3, "en")
    with pytest.raises(TrackNotFound):
        materialise_embedded_track("episode", 99, None, "en")


def test_the_row_s_text_codec_picks_its_own_track(library, monkeypatch):
    """Two plain English text tracks with different codecs are two rows: each
    lands with its own text, never the other's."""
    import sys

    from subtitles.new_subtitle import SubtitleFileExists
    from subtitles.tools.embedded_extract import materialise_embedded_track

    _session, root, _events, _published = library
    film = root / "owner1/movies/Film (2020)"
    monkeypatch.setattr(sys.modules[__name__], "TRACKS", [
        *TRACKS[:3], dict(TRACKS[3], format="ASS", name="", hearing_impaired=False)])

    assert materialise_embedded_track("movie", 30, 1, "en", codec="ass") == [str(film / "Film.en.srt")]
    assert "[DOOR SLAMS]" in (film / "Film.en.srt").read_text()
    (film / "Film.en.srt").unlink()
    materialise_embedded_track("movie", 30, 1, "en", codec="subrip")
    assert "Plain English line" in (film / "Film.en.srt").read_text()
    with pytest.raises(SubtitleFileExists):
        materialise_embedded_track("movie", 30, 1, "en", codec="ass")


@pytest.mark.parametrize("name", ["Film.srt", "Film.ass", "Film.vtt", "Film.ssa"])
@pytest.mark.parametrize("mode", ["current", "relative"])
def test_single_language_mode_counts_the_language_less_download_as_taken(library, monkeypatch, mode, name):
    """In single-language mode a download is saved as <video>.<ext>, in its
    own format. A text extraction must not put a second Film.en.srt beside it."""
    from app.config import settings
    from subtitles.new_subtitle import SubtitleFileExists
    from subtitles.tools.embedded_extract import materialise_embedded_track

    session, root, _events, published = library
    film = root / "owner1/movies/Film (2020)"
    monkeypatch.setattr(settings.general, "single_language", True)
    if mode == "relative":
        monkeypatch.setattr(settings.general, "subfolder", "relative")
        monkeypatch.setattr(settings.general, "subfolder_custom", "Subs")
    # The download sits next to the video; with a subfolder its twin counts too.
    (film / name).write_text("MY DOWNLOADED FILE")

    with pytest.raises(SubtitleFileExists, match=name):
        materialise_embedded_track("movie", 30, 1, "en")

    assert (film / name).read_text() == "MY DOWNLOADED FILE"
    assert not list(film.rglob("Film.en.srt"))
    assert _history(session, TableHistoryMovie) == [] and published == []
    # An image track is not a second text subtitle and is still copied out.
    assert [os.path.basename(path) for path in materialise_embedded_track("movie", 30, 1, "en", codec="pgs")] == [
        "Film.en.sup"]
