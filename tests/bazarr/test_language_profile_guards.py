# coding=utf-8
"""Media whose language profile is missing, unresolvable or partially shaped.

Two crashes, both of which end a user action behind an HTTP 204 with nothing in
the UI to explain it:

* ``get_profiles_list`` answers a NULL profile id with the whole profile LIST
  and an id that resolves to nothing with ``None``, and the manual-upload path
  subscripted the answer straight away. A movie or episode with no usable
  profile crashed the upload instead of saving the file the user picked.
* A language-profile item may reach the database without the optional
  ``audio_exclude`` / ``audio_only_include`` keys: the migration that fills them
  in runs at startup only, and the settings endpoint stored whatever it was
  handed. Every later indexing pass then died on a KeyError.
"""
import io
import json

import pytest
from sqlalchemy import insert, select as sa_select, text, update as sa_update


SRT = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n\n"

# What the profile editor sends before the optional keys were added to it, and
# what any client can still POST today.
PARTIAL_ITEM = {"id": 1, "language": "en", "forced": "False", "hi": "False"}


def _seed_profile(session, profile_id=1, original_format=1):
    from app.database import TableLanguagesProfiles

    session.execute(insert(TableLanguagesProfiles).values(
        profileId=profile_id, name="Test", items=json.dumps([PARTIAL_ITEM]),
        cutoff=None, originalFormat=original_format))


def _seed_show(session, profile_id=None):
    from app.database import TableEpisodes, TableShows

    session.execute(insert(TableShows).values(
        id=10, sonarrSeriesId=10, arr_instance_id=None, path="/series/10",
        title="Show", imdbId="tt-10", tvdbId=10, profileId=profile_id))
    session.execute(insert(TableEpisodes).values(
        id=100, series_id=10, sonarrSeriesId=10, sonarrEpisodeId=20,
        arr_instance_id=None, path="/series/10/100.mkv", title="Pilot",
        season=1, episode=1, audio_language="[]", sceneName="Scene",
        missing_subtitles="[]", failedAttempts="[]", subtitles="[]"))


def _seed_movie(session, profile_id=None):
    from app.database import TableMovies

    session.execute(insert(TableMovies).values(
        id=100, radarrId=30, arr_instance_id=None, path="/movies/100.mkv",
        title="Movie", tmdbId="100", imdbId="tt-100", audio_language="[]",
        sceneName="Scene", missing_subtitles="[]", failedAttempts="[]",
        subtitles="[]", profileId=profile_id))


def _dangle(session, table, column, value, profile_id=99):
    """Point a media row at a profile id that does not exist.

    Foreign keys are off for the statement on purpose. SQLite enforces them only
    when the connection asks it to, so a database repaired, restored or edited
    outside Bazarr keeps whatever reference it had, and the lookup then resolves
    to nothing exactly the way it does here.
    """
    session.commit()  # SQLite ignores the pragma inside an open transaction
    session.execute(text("PRAGMA foreign_keys=OFF"))
    session.execute(sa_update(table).where(column == value)
                    .values(profileId=profile_id))
    session.commit()
    session.execute(text("PRAGMA foreign_keys=ON"))


@pytest.fixture
def profile_db(schema_session, monkeypatch):
    """Point the profile lookup and its cache at the test database."""
    import app.database as database_module
    from languages import get_languages

    monkeypatch.setattr(database_module, "database", schema_session)
    database_module.update_profile_id_list.invalidate()
    # Built at startup from the languages table, which no in-memory schema has.
    monkeypatch.setattr(get_languages, "languages_dict", [
        {"code3": "eng", "code2": "en", "name": "English", "code3b": None}],
        raising=False)
    yield schema_session
    database_module.update_profile_id_list.invalidate()


# ---------------------------------------------------------------------------
# Manual upload against media with no usable language profile
# ---------------------------------------------------------------------------

@pytest.fixture
def upload(profile_db, monkeypatch):
    """Drive the real manual-upload path up to the point it writes to disk.

    ``save_subtitles`` is the first thing past the profile lookup that touches
    the filesystem, so it reports what the upload decided and then returns
    nothing, which makes the function log and return the way it does for any
    target it cannot write.
    """
    from subtitles import processing as processing_module
    from subtitles import upload as upload_module
    from subtitles.tools import mods as mods_module

    monkeypatch.setattr(upload_module, "database", profile_db)
    monkeypatch.setattr(processing_module, "_postprocessing_config",
                        lambda media_type, arr_instance_id: (False, None, None, None))
    monkeypatch.setattr(mods_module, "get_subzero_mods", lambda arr_instance_id: None)

    decided = {}

    def _save_subtitles(path, subtitles, **kwargs):
        decided["use_original_format"] = subtitles[0].use_original_format
        decided["formats"] = kwargs.get("formats")
        return []

    monkeypatch.setattr(upload_module, "save_subtitles", _save_subtitles)

    def _call(media_type, **ids):
        upload_module.manual_upload_subtitle(
            path="/movies/100.mkv" if media_type == "movie" else "/series/10/100.mkv",
            language="en", forced=False, hi=False, media_type=media_type,
            subtitle=io.BytesIO(SRT), filename="upload.srt", audio_language="[]",
            job_id=1, **ids)
        return decided

    return _call


@pytest.mark.parametrize("dangling", [False, True], ids=["no-profile", "dangling-profile"])
def test_uploading_to_a_movie_without_a_usable_profile(profile_db, upload, dangling):
    from app.database import TableMovies

    _seed_profile(profile_db)
    _seed_movie(profile_db, profile_id=None)
    if dangling:
        _dangle(profile_db, TableMovies, TableMovies.radarrId, 30)
    profile_db.commit()

    decided = upload("movie", radarrId=30)

    assert decided["use_original_format"] is False, (
        "a movie with no usable profile must still take the upload, on the "
        "converted-to-srt default every profile starts with")
    assert decided["formats"] == ("srt",)


@pytest.mark.parametrize("dangling", [False, True], ids=["no-profile", "dangling-profile"])
def test_uploading_to_an_episode_without_a_usable_profile(profile_db, upload, dangling):
    from app.database import TableShows

    _seed_profile(profile_db)
    _seed_show(profile_db, profile_id=None)
    if dangling:
        _dangle(profile_db, TableShows, TableShows.sonarrSeriesId, 10)
    profile_db.commit()

    decided = upload("series", sonarrSeriesId=10, sonarrEpisodeId=20)

    assert decided["use_original_format"] is False, (
        "an episode with no usable profile must still take the upload, on the "
        "converted-to-srt default every profile starts with")
    assert decided["formats"] == ("srt",)


@pytest.mark.parametrize("original_format, expected", [(1, True), (0, False)])
def test_a_real_profile_still_decides_the_upload_format(profile_db, upload,
                                                        original_format, expected):
    """The guard must not flatten the setting it is guarding."""
    _seed_profile(profile_db, original_format=original_format)
    _seed_movie(profile_db, profile_id=1)
    profile_db.commit()

    decided = upload("movie", radarrId=30)

    assert decided["use_original_format"] is expected


# ---------------------------------------------------------------------------
# Profile items that arrive without the optional audio keys
# ---------------------------------------------------------------------------

def _indexer_stubs(monkeypatch, module, session, profile_items, cutoff=None):
    monkeypatch.setattr(module, "database", session)
    monkeypatch.setattr(module, "get_profiles_list",
                        lambda profile_id=None: {"items": profile_items})
    monkeypatch.setattr(module, "get_profile_cutoff", lambda profile_id=None: cutoff)
    monkeypatch.setattr(module, "is_search_given_up", lambda *a, **kw: False)
    monkeypatch.setattr(module, "event_stream", lambda *a, **kw: None)
    monkeypatch.setattr(module, "prune_mismatches_for_media", lambda *a, **kw: None)


def test_indexing_a_movie_whose_profile_item_omits_the_audio_keys(profile_db,
                                                                  monkeypatch):
    from app.database import TableMovies
    from subtitles.indexer import movies as movies_indexer

    _seed_profile(profile_db)
    _seed_movie(profile_db, profile_id=1)
    profile_db.commit()
    _indexer_stubs(monkeypatch, movies_indexer, profile_db, [dict(PARTIAL_ITEM)])

    movies_indexer.list_missing_subtitles_movies(no=30)

    missing = profile_db.execute(sa_select(TableMovies.missing_subtitles)).scalar()
    assert missing == "['en']", (
        "an item with no audio keys is not audio-restricted, so the language "
        "stays desired rather than ending the whole indexing pass")


def test_indexing_an_episode_whose_profile_item_omits_the_audio_keys(profile_db,
                                                                     monkeypatch):
    from app.database import TableEpisodes
    from subtitles.indexer import series as series_indexer

    _seed_profile(profile_db)
    _seed_show(profile_db, profile_id=1)
    profile_db.commit()
    _indexer_stubs(monkeypatch, series_indexer, profile_db, [dict(PARTIAL_ITEM)])

    series_indexer.list_missing_subtitles(epno=20)

    missing = profile_db.execute(sa_select(TableEpisodes.missing_subtitles)).scalar()
    assert missing == "['en']"


def test_a_cutoff_item_that_omits_the_audio_keys_does_not_end_the_pass(profile_db,
                                                                       monkeypatch):
    """get_profile_cutoff hands back items from the same list, same exposure."""
    from app.database import TableMovies
    from subtitles.indexer import movies as movies_indexer

    _seed_profile(profile_db)
    _seed_movie(profile_db, profile_id=1)
    profile_db.commit()
    _indexer_stubs(monkeypatch, movies_indexer, profile_db, [dict(PARTIAL_ITEM)],
                   cutoff=[dict(PARTIAL_ITEM)])

    movies_indexer.list_missing_subtitles_movies(no=30)

    missing = profile_db.execute(sa_select(TableMovies.missing_subtitles)).scalar()
    assert missing == "['en']", (
        "the cutoff language has no subtitle on disk, so the cutoff is not met "
        "and the language is still missing")


@pytest.mark.parametrize("flag", ["audio_only_include", "audio_exclude"])
def test_the_audio_flags_still_do_their_job_when_they_are_present(profile_db,
                                                                  monkeypatch, flag):
    """The default must not swallow a flag the user actually set.

    The movie has no English audio track, so include-only drops the language and
    exclude-on-match leaves it alone.
    """
    from app.database import TableMovies
    from subtitles.indexer import movies as movies_indexer

    _seed_profile(profile_db)
    _seed_movie(profile_db, profile_id=1)
    profile_db.commit()
    item = dict(PARTIAL_ITEM, audio_exclude="False", audio_only_include="False")
    item[flag] = "True"
    _indexer_stubs(monkeypatch, movies_indexer, profile_db, [item])

    movies_indexer.list_missing_subtitles_movies(no=30)

    missing = profile_db.execute(sa_select(TableMovies.missing_subtitles)).scalar()
    assert missing == ("[]" if flag == "audio_only_include" else "['en']")


# ---------------------------------------------------------------------------
# The settings endpoint must not be able to store the shape at all
# ---------------------------------------------------------------------------

@pytest.fixture
def post_settings(profile_db, monkeypatch):
    """Drive the real settings endpoint with a form, as the frontend submits one."""
    import api.system.settings as endpoint

    monkeypatch.setattr(endpoint, "database", profile_db)
    monkeypatch.setattr(endpoint, "event_stream", lambda *a, **kw: None)
    monkeypatch.setattr(endpoint, "list_missing_subtitles", lambda *a, **kw: None)
    monkeypatch.setattr(endpoint, "list_missing_subtitles_movies", lambda *a, **kw: None)

    from arr_instances import resolution
    monkeypatch.setattr(resolution, "database", profile_db, raising=False)

    def _call(form):
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context("/", method="POST", data=form):
            return endpoint.SystemSettings.post.__wrapped__(object())

    return _call


@pytest.mark.parametrize("profile_id", [1, 2], ids=["update", "insert"])
def test_saving_a_profile_item_without_the_audio_keys_fills_them_in(
        profile_db, post_settings, profile_id):
    """Normalise rather than reject.

    The startup migration already answers a missing key with "False", so giving
    the same answer at save time keeps a payload that was legal a moment ago
    legal, and stops the indexer from being handed a shape it has to survive for
    the rest of the run.
    """
    from app.database import TableLanguagesProfiles

    _seed_profile(profile_db)
    profile_db.commit()

    post_settings({"languages-profiles": json.dumps([{
        "profileId": profile_id, "name": "Partial", "cutoff": None,
        "items": [dict(PARTIAL_ITEM)], "mustContain": [], "mustNotContain": [],
        "originalFormat": False, "tag": None}])})

    stored = json.loads(profile_db.execute(
        sa_select(TableLanguagesProfiles.items)
        .where(TableLanguagesProfiles.profileId == profile_id)).scalar())
    assert stored[0]["audio_exclude"] == "False"
    assert stored[0]["audio_only_include"] == "False"
    assert stored[0]["translate_from"] is None


def test_the_startup_migration_still_fills_the_optional_keys(profile_db):
    """The endpoint and the migration answer a missing key the same way."""
    import app.database as database_module
    from app.database import TableLanguagesProfiles

    profile_db.execute(insert(TableLanguagesProfiles).values(
        profileId=1, name="Legacy", cutoff=None, originalFormat=0,
        items=json.dumps([{"id": 1, "language": "en", "forced": "False",
                           "hi": "only"}])))
    profile_db.commit()

    database_module.upgrade_languages_profile_values()

    stored = json.loads(profile_db.execute(
        sa_select(TableLanguagesProfiles.items)).scalar())[0]
    assert stored["hi"] == "True", "the legacy hi conversion has to survive"
    assert stored["audio_exclude"] == "False"
    assert stored["audio_only_include"] == "False"
    assert stored["translate_from"] is None
