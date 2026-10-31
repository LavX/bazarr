# coding=utf-8
"""View, Edit and Download read an embedded track with ``?source=embedded``.

The content and single-file download routes read the track out of the
container into the extraction cache and never write next to the video. The
stored subtitles list decides which tracks exist, a bitmap-only key answers
415 naming its codec, the row's codec picks between two text tracks of one
language key, every lookup stays on the owning instance, and the name
Download and Save give the file is the one a downloaded subtitle would get.

Runs on SQLite, and on PostgreSQL when BAZARR_PG_TEST_URL is set.
"""
# ruff: noqa: F811
import os

import pytest
import sqlalchemy as sa
from flask import Flask
from sqlalchemy.orm import scoped_session, sessionmaker
from test_sportarr_indexer import indexed_library, sports  # noqa: F401
from test_sportarr_kind_migration import migration_engine  # noqa: F401

TRACK_TEXT = "1\n00:00:01,000 --> 00:00:02,000\nFrom the container\n"
FILE_TEXT = "1\n00:00:01,000 --> 00:00:02,000\nFrom the file on disk\n"

LANGUAGES = [
    {"code2": "en", "code3": "eng", "code3b": "eng", "name": "English"},
    {"code2": "fr", "code3": "fra", "code3b": "fre", "name": "French"},
    {"code2": "de", "code3": "deu", "code3b": "ger", "name": "German"},
    {"code2": "hu", "code3": "hun", "code3b": "hun", "name": "Hungarian"},
]

MOVIE_SUBTITLES = repr([
    ["en", "/remote/Film/Film.en.srt", 60],
    ["en", None, None, "subrip"],
    ["en:hi", None, None, "pgs"],
    ["en:hi:forced", None, None, "subrip"],
    ["fr", None, None, "pgs"],
    ["fr", None, None, "subrip"],
    ["de", None, None],
    ["hu:forced", None, None, "subrip"],
    # Two text tracks under one key, listed as two rows: an SRT and an ASS.
    ["it", None, None, "subrip"],
    ["it", None, None, "ass"],
])


class _Mappings:
    """Each instance mounts /remote somewhere of its own."""

    def __init__(self, root):
        self.root = root

    def path_replace_instance(self, path, arr_instance_id, media_type):
        return path.replace("/remote", str(self.root / f"instance{arr_instance_id}"), 1)


@pytest.fixture
def library(migration_engine, monkeypatch, tmp_path):
    from app import database as db
    from app.config import settings
    from api.subtitles import content, download, embedded_source
    from languages import get_languages
    from subtitles.tools import embedded_extract

    db.Base.metadata.create_all(migration_engine)
    session = scoped_session(sessionmaker(bind=migration_engine))
    mappings = _Mappings(tmp_path)
    for module in (content, download, embedded_source, embedded_extract):
        monkeypatch.setattr(module, "database", session)
    for module in (content, download, embedded_extract):
        monkeypatch.setattr(module, "path_mappings", mappings)
        monkeypatch.setattr(module, "get_target_folder", lambda path, create=True: None)
    monkeypatch.setattr(get_languages, "languages_dict", LANGUAGES, raising=False)
    monkeypatch.setenv("SZ_HI_EXTENSION", "hi")
    monkeypatch.setattr(settings.general, "chmod_enabled", False)

    for owner in (1, 2):
        film = tmp_path / f"instance{owner}" / "Film"
        film.mkdir(parents=True)
        (film / "Film.mkv").write_bytes(b"video")
        (film / "Film.en.srt").write_text(FILE_TEXT)
        show = tmp_path / f"instance{owner}" / "Show"
        show.mkdir()
        (show / "Show.S01E01.mkv").write_bytes(b"video")
        session.execute(sa.insert(db.TableMovies).values(
            id=owner, radarrId=30, arr_instance_id=owner, title="Film", tmdbId=str(100 + owner),
            path="/remote/Film/Film.mkv", movie_file_id=owner, file_size=5,
            subtitles=MOVIE_SUBTITLES, missing_subtitles="[]"))
        session.execute(sa.insert(db.TableShows).values(
            id=owner, sonarrSeriesId=10, arr_instance_id=owner, title="Show", path="/remote/Show",
            tags="[]"))
        session.execute(sa.insert(db.TableEpisodes).values(
            id=owner, series_id=owner, sonarrSeriesId=10, sonarrEpisodeId=20, arr_instance_id=owner,
            title="Pilot", season=1, episode=1, monitored="True", path="/remote/Show/Show.S01E01.mkv",
            episode_file_id=owner, file_size=5, subtitles=repr([["en:hi", None, None, "subrip"]]),
            missing_subtitles="[]"))
    session.commit()

    cache = tmp_path / "config" / "extracted_subs"
    cache.mkdir(parents=True)
    calls = []

    def extract(video_path, language_code2, media_type, hi=False, forced=False,
                arr_instance_id=None, exact=False, codec=None):
        calls.append(dict(video_path=video_path, language=language_code2, media_type=media_type,
                          hi=hi, forced=forced, owner=arr_instance_id, exact=exact, codec=codec))
        if language_code2 == "de":
            return None
        name = f"{arr_instance_id}.{language_code2}" + (f".{codec}" if codec else "")
        path = cache / f"{name}.srt"
        path.write_text(TRACK_TEXT)
        return str(path)

    monkeypatch.setattr(embedded_source, "extract_embedded_subtitle", extract)
    yield session, tmp_path, calls
    session.remove()


def _get(route, *args, query):
    from api.subtitles import content, download

    resource = {
        "movie": content.MovieSubtitleContent,
        "episode": content.EpisodeSubtitleContent,
        "movie-download": download.MovieSubtitleFileDownload,
    }[route]
    with Flask(__name__).test_request_context(f"/?{query}"):
        return resource.get.__wrapped__(resource(), *args)


def _json(response):
    assert response.status_code == 200
    return response.get_json()


def test_the_track_is_read_while_a_file_has_the_same_key(library):
    _session, root, calls = library
    film = root / "instance1" / "Film"
    before = sorted(os.listdir(film))

    plain = _json(_get("movie", 30, "en", query="arr_instance_id=1"))
    response = _get("movie", 30, "en", query="source=embedded&arr_instance_id=1")
    embedded = _json(response)

    assert plain["content"] == FILE_TEXT
    assert embedded["content"] == TRACK_TEXT
    assert embedded["exists"] is False
    assert embedded["source"] == "embedded"
    assert embedded["format"] == "srt"
    assert embedded["language"] == "en"
    assert embedded["codec"] == "subrip"
    assert embedded["fileName"] == "Film.en.srt"
    assert embedded["mediaTitle"] == "Film"
    assert embedded["arrInstanceId"] == 1
    assert "ETag" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    assert calls == [dict(video_path=str(film / "Film.mkv"), language="en", media_type="movie",
                          hi=False, forced=False, owner=1, exact=True, codec="subrip")]
    assert sorted(os.listdir(film)) == before


def test_a_bitmap_only_key_answers_415_naming_the_codec(library):
    _session, _root, calls = library

    message, status = _get("movie", 30, "en:hi", query="source=embedded&arr_instance_id=1")

    assert status == 415
    assert "PGS" in message
    assert message == "This embedded track is image-based (PGS). Extract it to a file first."
    assert calls == []


def test_a_key_with_a_text_and_a_bitmap_track_reads_the_text(library):
    _session, _root, calls = library

    body = _json(_get("movie", 30, "fr", query="source=embedded&arr_instance_id=1"))

    assert body["content"] == TRACK_TEXT
    assert body["codec"] == "subrip"
    assert [call["language"] for call in calls] == ["fr"]


def test_a_text_codec_picks_between_text_tracks_of_one_key(library):
    """An English SRT and an English ASS are two rows; each reads its own track."""
    _session, _root, calls = library

    body = _json(_get("movie", 30, "it", query="source=embedded&codec=ass&arr_instance_id=1"))

    assert body["content"] == TRACK_TEXT
    assert body["codec"] == "ass"
    assert [call["codec"] for call in calls] == ["ass"]


def test_without_a_codec_the_first_text_track_is_read(library):
    _session, _root, calls = library

    body = _json(_get("movie", 30, "it", query="source=embedded&arr_instance_id=1"))

    assert body["codec"] == "subrip"
    assert [call["codec"] for call in calls] == ["subrip"]


def test_a_codec_the_list_does_not_hold_is_refused_before_ffmpeg(library):
    _session, _root, calls = library

    _message, status = _get("movie", 30, "it", query="source=embedded&codec=mov_text&arr_instance_id=1")

    assert status == 404
    assert calls == []


def test_a_bitmap_codec_answers_415_even_when_the_key_holds_text(library):
    """The row is what the user clicked: an image-based row has no text."""
    _session, _root, calls = library

    message, status = _get("movie", 30, "fr", query="source=embedded&codec=pgs&arr_instance_id=1")

    assert status == 415
    assert "PGS" in message
    assert calls == []


def test_a_track_indexed_before_the_codec_was_stored_is_tried_as_text(library):
    _session, _root, calls = library

    message, status = _get("movie", 30, "de", query="source=embedded&arr_instance_id=1")

    assert status == 422
    assert "Scan Disk" in message
    assert [call["language"] for call in calls] == ["de"]


@pytest.mark.parametrize("language", ["es", "en:forced"])
def test_a_key_the_list_does_not_hold_is_refused_before_ffmpeg(library, language):
    """Hidden tracks are left out of the stored list, so they land here too."""
    _session, _root, calls = library

    message, status = _get("movie", 30, language, query="source=embedded&arr_instance_id=1")

    assert status == 404
    assert calls == []


@pytest.mark.parametrize("language", ["en:sync-ffsubsync", "en:combined-hu", "../en"])
def test_keys_that_name_a_file_or_nothing_are_refused(library, language):
    _session, _root, calls = library

    _message, status = _get("movie", 30, language, query="source=embedded&arr_instance_id=1")

    assert status == 400
    assert calls == []


def test_a_key_with_modifiers_in_any_case_names_the_stored_track(library):
    """The Global Constraints accept hi and forced in any case, and en:HI is
    the stored en:hi, not a missing key."""
    _session, _root, calls = library

    _message, status = _get("movie", 30, "en:HI", query="source=embedded&arr_instance_id=1")

    assert status == 415
    assert "PGS" in _message
    assert calls == []


def test_an_unknown_source_is_refused(library):
    _message, status = _get("movie", 30, "en", query="source=file&arr_instance_id=1")

    assert status == 400


def test_the_second_instance_reads_its_own_mount(library):
    _session, root, calls = library

    body = _json(_get("movie", 30, "en", query="source=embedded&arr_instance_id=2"))

    assert body["arrInstanceId"] == 2
    assert calls[0]["video_path"] == str(root / "instance2" / "Film" / "Film.mkv")
    assert calls[0]["owner"] == 2


def test_an_upstream_id_two_instances_share_needs_an_owner(library):
    _session, _root, calls = library

    message, status = _get("movie", 30, "en", query="source=embedded")

    assert status == 400
    assert "Ambiguous" in message
    assert calls == []


def test_extract_to_file_refuses_the_same_unscoped_id(library):
    """One resolver for the read and for Extract to file, one ambiguity rule."""
    from subtitles.tools.embedded_extract import ExtractionError, resolve_track

    with pytest.raises(ExtractionError) as refused:
        resolve_track("movie", 30, None, "en")

    assert refused.value.status == 400
    assert resolve_track("movie", 30, 2, "en").video_path.startswith(
        str(library[1] / "instance2"))


def test_an_episode_track_keeps_its_hi_flag(library):
    _session, root, calls = library

    body = _json(_get("episode", 20, "en:hi", query="source=embedded&arr_instance_id=1"))

    assert body["fileName"] == "Show.S01E01.en.hi.srt"
    assert body["episodeTitle"] == "Pilot"
    assert calls == [dict(video_path=str(root / "instance1" / "Show" / "Show.S01E01.mkv"),
                          language="en", media_type="episode", hi=True, forced=False, owner=1,
                          exact=True, codec="subrip")]


def test_hi_is_named_by_the_hi_extension_setting(library, monkeypatch):
    monkeypatch.setenv("SZ_HI_EXTENSION", "sdh")

    body = _json(_get("episode", 20, "en:hi", query="source=embedded&arr_instance_id=1"))

    assert body["fileName"] == "Show.S01E01.en.sdh.srt"


def test_a_forced_track_is_named_like_a_forced_download(library):
    body = _json(_get("movie", 30, "hu:forced", query="source=embedded&arr_instance_id=1"))

    assert body["fileName"] == "Film.hu.forced.srt"


def test_a_track_both_hi_and_forced_is_named_forced_like_a_download(library):
    _session, _root, calls = library

    body = _json(_get("movie", 30, "en:hi:forced", query="source=embedded&arr_instance_id=1"))

    assert body["fileName"] == "Film.en.forced.srt"
    assert (calls[0]["hi"], calls[0]["forced"]) == (True, True)


# ---------------------------------------------------------------- sports, real ffmpeg


@pytest.fixture
def sports_event(indexed_library, monkeypatch):
    from app import get_args
    from api.subtitles import content, embedded_source
    from sportarr import subtitles as service
    from sportarr import workflows
    from subtitles.tools import embedded_extract
    from subtitles.tools.translate import batch

    session, root = indexed_library
    (root / "1" / "event.fr.srt").write_text(FILE_TEXT)
    module = sports(monkeypatch, session)
    module.store_subtitles_sports(61, 1)
    for target in (content, embedded_source, embedded_extract, service, workflows, batch):
        monkeypatch.setattr(target, "database", session)
    config = root / "config"
    config.mkdir()
    monkeypatch.setattr(get_args.args, "config_dir", str(config))
    return session, root, config


def _sports(event_id, language, query):
    from api.subtitles import content

    with Flask(__name__).test_request_context(f"/?{query}"):
        return content.SportsEventSubtitleContent.get.__wrapped__(
            content.SportsEventSubtitleContent(), event_id, language)


def test_a_sports_track_is_read_out_of_the_real_container(sports_event):
    _session, root, config = sports_event
    before = sorted(os.listdir(root / "1"))

    plain = _json(_sports(61, "fr", "arr_instance_id=1"))
    embedded = _json(_sports(61, "fr", "source=embedded&arr_instance_id=1"))

    assert plain["content"] == FILE_TEXT
    assert "Bienvenue dans cette compétition sportive." in embedded["content"]
    assert embedded["exists"] is False
    assert embedded["fileName"] == "event.fr.srt"
    assert embedded["episodeId"] == 61
    assert os.listdir(config / "extracted_subs")
    assert sorted(os.listdir(root / "1")) == before


def test_sports_refuses_while_use_sportarr_is_off(sports_event, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, "use_sportarr", False)

    message, status = _sports(61, "fr", "source=embedded&arr_instance_id=1")

    assert status == 409
    assert "Use Sportarr" in message


def test_another_owner_cannot_read_the_event(sports_event):
    _message, status = _sports(61, "fr", "source=embedded&arr_instance_id=2")

    assert status == 404


def test_the_second_sports_owner_reads_its_own_mount(sports_event, monkeypatch):
    session, root, _config = sports_event
    sports(monkeypatch, session).store_subtitles_sports(62, 2)
    before = sorted(os.listdir(root / "2"))

    body = _json(_sports(62, "fr", "source=embedded&arr_instance_id=2"))

    assert "Bienvenue dans cette compétition sportive." in body["content"]
    assert body["arrInstanceId"] == 2
    assert sorted(os.listdir(root / "2")) == before


# ---------------------------------------------------------------- download route


def test_download_streams_the_track_under_the_name_save_would_use(library):
    _session, root, _calls = library
    before = sorted(os.listdir(root / "instance1" / "Film"))

    response = _get("movie-download", 30, "en", query="source=embedded&arr_instance_id=1")
    response.direct_passthrough = False

    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == "attachment; filename=Film.en.srt"
    assert response.get_data(as_text=True) == TRACK_TEXT
    assert sorted(os.listdir(root / "instance1" / "Film")) == before


def test_download_of_a_bitmap_track_answers_415(library):
    message, status = _get("movie-download", 30, "en:hi", query="source=embedded&arr_instance_id=1")

    assert status == 415
    assert "PGS" in message


def test_download_of_a_row_reads_that_row_s_track(library):
    _session, _root, calls = library

    response = _get("movie-download", 30, "it", query="source=embedded&codec=ass&arr_instance_id=1")
    response.direct_passthrough = False

    assert response.status_code == 200
    assert [call["codec"] for call in calls] == ["ass"]
    assert response.headers["Content-Disposition"] == "attachment; filename=Film.it.srt"


def test_a_plain_download_still_streams_the_file(library):
    response = _get("movie-download", 30, "en", query="arr_instance_id=1")
    response.direct_passthrough = False

    assert response.get_data(as_text=True) == FILE_TEXT


# ---------------------------------------------------------------- create route


@pytest.fixture
def create(library, monkeypatch):
    from api.subtitles import content
    from subtitles import new_subtitle

    reindexed = []
    monkeypatch.setattr(content, "store_subtitles_movie",
                        lambda *args, **kwargs: reindexed.append(kwargs.get("arr_instance_id")))
    monkeypatch.setattr(content, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(new_subtitle, "publication_callback", lambda *args: (lambda path: None))

    def post(body, query="arr_instance_id=1"):
        with Flask(__name__).test_request_context(f"/?{query}", method="POST", json=body):
            return content.MovieSubtitleCreate.post.__wrapped__(content.MovieSubtitleCreate(), 30)

    return post, reindexed


def test_an_unscoped_create_refuses_an_id_two_instances_share(library, create):
    _session, root, _calls = library
    post, reindexed = create

    result = post({"content": TRACK_TEXT, "language": "en", "format": "srt",
                   "overwrite": True}, query="")

    assert result == ("Media id is ambiguous across instances", 400)
    for owner in (1, 2):
        assert (root / f"instance{owner}" / "Film" / "Film.en.srt").read_text() == FILE_TEXT
    assert reindexed == []


def test_an_unscoped_episode_create_refuses_an_id_two_instances_share(library):
    _session, root, _calls = library
    from api.subtitles import content

    with Flask(__name__).test_request_context(
            "/", method="POST",
            json={"content": TRACK_TEXT, "language": "en", "format": "srt",
                  "overwrite": True}):
        result = content.EpisodeSubtitleCreate.post.__wrapped__(
            content.EpisodeSubtitleCreate(), 20)

    assert result == ("Media id is ambiguous across instances", 400)
    for owner in (1, 2):
        assert not (root / f"instance{owner}" / "Show" / "Show.S01E01.en.srt").exists()


def test_create_still_refuses_an_existing_file(library, create):
    _session, root, _calls = library
    post, reindexed = create

    result = post({"content": TRACK_TEXT, "language": "en", "format": "srt"})

    assert result == ("Subtitle file already exists", 409)
    assert (root / "instance1" / "Film" / "Film.en.srt").read_text() == FILE_TEXT
    assert reindexed == []


@pytest.mark.parametrize("flag", ["true", 1, "True"])
def test_only_a_json_true_overwrites(library, create, flag):
    _session, root, _calls = library
    post, _reindexed = create

    result = post({"content": TRACK_TEXT, "language": "en", "format": "srt", "overwrite": flag})

    assert result[1] == 409
    assert (root / "instance1" / "Film" / "Film.en.srt").read_text() == FILE_TEXT


def test_a_confirmed_overwrite_replaces_exactly_the_named_file(library, create):
    _session, root, _calls = library
    post, reindexed = create
    other = root / "instance1" / "Film" / "Film.en.ass"
    other.write_text("[Script Info]\n")

    body, status = post({"content": TRACK_TEXT, "language": "en", "format": "srt",
                         "overwrite": True})

    assert status == 200
    assert body == {"path": str(root / "instance1" / "Film" / "Film.en.srt"), "language": "en"}
    assert (root / "instance1" / "Film" / "Film.en.srt").read_text() == TRACK_TEXT
    assert other.read_text() == "[Script Info]\n"
    assert reindexed == [1]


def test_the_success_code_comes_from_the_state_the_write_saw(library, create, monkeypatch):
    """The 200 or 201 is decided under the write's lock, not by a check taken
    before it: a file that appears or vanishes in between gets the true code."""
    from api.subtitles import content
    from subtitles import new_subtitle

    _session, root, _calls = library
    post, _reindexed = create
    film = root / "instance1" / "Film"
    real = new_subtitle.write_new_subtitle

    def appeared(*args, **kwargs):
        (film / "Film.en.hi.srt").write_text(FILE_TEXT)
        return real(*args, **kwargs)

    monkeypatch.setattr(content, "write_new_subtitle", appeared)
    _body, status = post({"content": TRACK_TEXT, "language": "en", "hi": True, "format": "srt",
                          "overwrite": True})

    assert status == 200
    assert (film / "Film.en.hi.srt").read_text() == TRACK_TEXT

    def vanished(*args, **kwargs):
        (film / "Film.en.srt").unlink()
        return real(*args, **kwargs)

    monkeypatch.setattr(content, "write_new_subtitle", vanished)
    _body, status = post({"content": TRACK_TEXT, "language": "en", "format": "srt",
                          "overwrite": True})

    assert status == 201
    assert (film / "Film.en.srt").read_text() == TRACK_TEXT


def test_a_new_file_still_answers_201(library, create):
    _session, root, _calls = library
    post, _reindexed = create

    body, status = post({"content": TRACK_TEXT, "language": "en", "hi": True, "format": "srt",
                         "overwrite": True})

    assert status == 201
    assert body["language"] == "en:hi"
    assert (root / "instance1" / "Film" / "Film.en.hi.srt").read_text() == TRACK_TEXT


def test_save_names_the_file_the_way_download_does(library, create, monkeypatch):
    """The HI Extension setting names the saved file, as it names the
    download of the same track and a file Extract to file writes."""
    _session, root, _calls = library
    post, _reindexed = create
    monkeypatch.setenv("SZ_HI_EXTENSION", "sdh")

    body, status = post({"content": TRACK_TEXT, "language": "en", "hi": True, "format": "srt"})

    assert status == 201
    assert body == {"path": str(root / "instance1" / "Film" / "Film.en.sdh.srt"), "language": "en:hi"}
    assert not (root / "instance1" / "Film" / "Film.en.hi.srt").exists()


def test_a_code_the_language_table_does_not_know_keeps_its_plain_name(library, create):
    _session, root, _calls = library
    post, _reindexed = create

    body, status = post({"content": TRACK_TEXT, "language": "xx", "forced": True, "format": "srt"})

    assert status == 201
    assert body["path"] == str(root / "instance1" / "Film" / "Film.xx.forced.srt")


def test_a_sports_save_overwrites_only_when_confirmed(sports_event, monkeypatch):
    """The create route's sports branch runs inside the manual operation, and
    a confirmed overwrite publishes through the same owned boundary."""
    from api.subtitles import content
    from subtitles import new_subtitle

    _session, root, _config = sports_event
    monkeypatch.setattr(content, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(new_subtitle, "publication_callback", lambda *args: (lambda path: None))
    target = root / "1" / "event.fr.srt"

    def post(body):
        with Flask(__name__).test_request_context("/?arr_instance_id=1", method="POST", json=body):
            return content.SportsEventSubtitleCreate.post.__wrapped__(
                content.SportsEventSubtitleCreate(), 61)

    refused = post({"content": TRACK_TEXT, "language": "fr", "format": "srt"})
    body, status = post({"content": TRACK_TEXT, "language": "fr", "format": "srt",
                         "overwrite": True})

    assert refused == ("Subtitle file already exists", 409)
    assert status == 200
    assert body["language"] == "fr"
    assert target.read_text() == TRACK_TEXT
