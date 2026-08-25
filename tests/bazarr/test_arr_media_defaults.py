# coding=utf-8
"""Per-instance default language profile stored in ``arr_instances.options``.

The global series/movie default language profile applies the same profile to
every Sonarr/Radarr instance. An instance may override it under
``options["media_defaults"]`` so, for example, an anime Sonarr assigns the anime
profile while the standard one keeps the global default.

The single most important behaviour pinned here is the REVERSE failure, and it
is deliberately the first test in the file: resolving an override where none is
set would silently move profile assignment for every single-instance install on
its next sync. An instance with no ``media_defaults`` block must resolve to the
global value, byte for byte, in every combination of the global settings.
"""
import json

import pytest
from sqlalchemy import insert, select

from app.database import TableLanguagesProfiles, TableMovies, TableShows
from arr_instances.repository import ArrInstanceRepository
from arr_instances.resolution import clear_media_defaults_cache, resolve_default_profile


def _profile(session, profile_id, name, tag=None):
    session.execute(insert(TableLanguagesProfiles).values(
        profileId=profile_id, name=name, items="[]", tag=tag))
    return profile_id


def _instance(session, kind, name, port, blob=None):
    """Create an instance, optionally carrying a media_defaults override."""
    from arr_instances.media_defaults import merge_media_defaults_into_options

    options = merge_media_defaults_into_options(None, blob) if blob else None
    row = ArrInstanceRepository(session).create(kind, name, port=port, options=options)
    session.flush()
    clear_media_defaults_cache()
    return row


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_media_defaults_cache()
    yield
    clear_media_defaults_cache()


# --------------------------------------------------------------------------
# THE reverse-failure case. Everything else in this file is secondary to it.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("global_enabled", "global_profile", "expected"),
    [
        (True, 1, 1),        # the ordinary single-instance install
        (True, "", None),    # "enabled" with nothing picked: legacy sentinel
        (False, 1, None),    # switched off: the stored profile is ignored
        (False, "", None),
    ],
)
def test_instance_without_override_resolves_exactly_the_global_value(
        schema_session, global_enabled, global_profile, expected):
    """A single-instance install that never set an override must see NO change.

    This fails the moment resolution stops being conditional on an override
    actually being present: an unconditional read of media_defaults returns
    None (or a stale value) here instead of the global profile, which is how a
    single-instance user's whole library silently changes profile on the next
    sync.
    """
    _profile(schema_session, 1, "Global")
    row = _instance(schema_session, "sonarr", "Main", 8989)   # no options at all
    assert row.options is None

    assert resolve_default_profile(
        row.id, global_enabled, global_profile, session=schema_session) == expected


def test_no_instance_context_resolves_the_global_value(schema_session):
    """Pre-backfill installs carry no instance id; they keep the global value."""
    _profile(schema_session, 1, "Global")
    assert resolve_default_profile(None, True, 1, session=schema_session) == 1
    assert resolve_default_profile(None, False, 1, session=schema_session) is None


def test_unrelated_options_do_not_look_like_an_override(schema_session):
    """A subtitle_settings-only options blob is not a media_defaults override."""
    _profile(schema_session, 1, "Global")
    options = json.dumps({"subtitle_settings": {"subsync": {"subsync_threshold": 80}}})
    row = ArrInstanceRepository(schema_session).create(
        "sonarr", "Main", port=8989, options=options)
    schema_session.flush()
    clear_media_defaults_cache()

    assert resolve_default_profile(
        row.id, True, 1, session=schema_session) == 1


# --------------------------------------------------------------------------
# Validation of the stored blob
# --------------------------------------------------------------------------

def test_validate_accepts_the_three_states():
    from arr_instances.media_defaults import validate_media_defaults

    assert validate_media_defaults(None) == {}
    assert validate_media_defaults({}) == {}
    assert validate_media_defaults({"default_enabled": False}) == {"default_enabled": False}
    assert validate_media_defaults({"default_enabled": True, "default_profile": 3}) == {
        "default_enabled": True, "default_profile": 3}


def test_validate_drops_a_profile_from_a_disabled_override():
    from arr_instances.media_defaults import validate_media_defaults

    assert validate_media_defaults({"default_enabled": False, "default_profile": 3}) == {
        "default_enabled": False}


@pytest.mark.parametrize(
    "blob",
    [
        {"bogus": 1},                                     # unknown key
        {"default_profile": 3},                           # profile without the flag
        {"default_enabled": True},                        # enabled without a profile
        {"default_enabled": True, "default_profile": None},
        {"default_enabled": "yes"},                       # not a boolean
        {"default_enabled": True, "default_profile": 0},   # not a valid profile id
        {"default_enabled": True, "default_profile": True},  # bool masquerading as 1
        {"default_enabled": True, "default_profile": "3"},   # not an int
    ],
)
def test_validate_rejects_bad_blobs(blob):
    from arr_instances.media_defaults import validate_media_defaults

    with pytest.raises(ValueError):
        validate_media_defaults(blob)


def test_validate_rejects_an_unknown_profile_id():
    from arr_instances.media_defaults import validate_media_defaults

    blob = {"default_enabled": True, "default_profile": 9}
    assert validate_media_defaults(blob, known_profile_ids={9}) == blob
    with pytest.raises(ValueError):
        validate_media_defaults(blob, known_profile_ids={1, 2})


def test_read_and_merge_round_trip():
    from arr_instances.media_defaults import (
        merge_media_defaults_into_options,
        read_media_defaults,
    )

    blob = {"default_enabled": True, "default_profile": 2}
    options = merge_media_defaults_into_options(None, blob)
    assert read_media_defaults(options) == blob
    assert read_media_defaults(None) == {}
    assert read_media_defaults("") == {}
    assert read_media_defaults("not json") == {}


def test_merge_preserves_subtitle_settings_and_clears_cleanly():
    from arr_instances.media_defaults import (
        merge_media_defaults_into_options,
        read_media_defaults,
    )
    from arr_instances.subtitle_settings import read_subtitle_settings

    existing = json.dumps({"subtitle_settings": {"subsync": {"subsync_threshold": 80}}})
    options = merge_media_defaults_into_options(
        existing, {"default_enabled": True, "default_profile": 2})
    # the sibling block is untouched: media defaults are not subtitle settings
    assert read_subtitle_settings(options) == {"subsync": {"subsync_threshold": 80}}
    assert read_media_defaults(options) == {"default_enabled": True, "default_profile": 2}

    cleared = merge_media_defaults_into_options(options, {})
    assert read_media_defaults(cleared) == {}
    assert read_subtitle_settings(cleared) == {"subsync": {"subsync_threshold": 80}}
    # nothing left at all -> no options row content
    assert merge_media_defaults_into_options(
        merge_media_defaults_into_options(None, {"default_enabled": False}), {}) is None


# --------------------------------------------------------------------------
# Resolution with an override present
# --------------------------------------------------------------------------

def test_override_wins_over_the_global_default(schema_session):
    _profile(schema_session, 1, "Global")
    _profile(schema_session, 2, "Anime")
    row = _instance(schema_session, "sonarr", "Anime", 8990,
                    {"default_enabled": True, "default_profile": 2})

    assert resolve_default_profile(row.id, True, 1, session=schema_session) == 2
    # and it applies even when the global default is switched off entirely
    assert resolve_default_profile(row.id, False, "", session=schema_session) == 2


def test_disabled_override_assigns_no_profile(schema_session):
    _profile(schema_session, 1, "Global")
    row = _instance(schema_session, "sonarr", "Bare", 8991, {"default_enabled": False})

    assert resolve_default_profile(row.id, True, 1, session=schema_session) is None


def test_two_instances_resolve_different_profiles(schema_session):
    _profile(schema_session, 1, "Standard")
    _profile(schema_session, 2, "Anime")
    standard = _instance(schema_session, "sonarr", "Standard", 8989,
                         {"default_enabled": True, "default_profile": 1})
    anime = _instance(schema_session, "sonarr", "Anime", 8990,
                      {"default_enabled": True, "default_profile": 2})

    assert resolve_default_profile(standard.id, False, "", session=schema_session) == 1
    assert resolve_default_profile(anime.id, False, "", session=schema_session) == 2


def test_override_naming_a_deleted_profile_falls_back_to_global(schema_session):
    from sqlalchemy import delete

    _profile(schema_session, 1, "Global")
    _profile(schema_session, 2, "Anime")
    row = _instance(schema_session, "sonarr", "Anime", 8990,
                    {"default_enabled": True, "default_profile": 2})
    assert resolve_default_profile(row.id, True, 1, session=schema_session) == 2

    schema_session.execute(
        delete(TableLanguagesProfiles).where(TableLanguagesProfiles.profileId == 2))
    # no dangling profileId is ever written; the global default takes over
    assert resolve_default_profile(row.id, True, 1, session=schema_session) == 1
    assert resolve_default_profile(row.id, False, "", session=schema_session) is None


def test_clearing_the_override_returns_to_the_global_default(schema_session):
    from arr_instances.media_defaults import merge_media_defaults_into_options

    _profile(schema_session, 1, "Global")
    _profile(schema_session, 2, "Anime")
    row = _instance(schema_session, "sonarr", "Anime", 8990,
                    {"default_enabled": True, "default_profile": 2})
    assert resolve_default_profile(row.id, True, 1, session=schema_session) == 2

    ArrInstanceRepository(schema_session).update(
        row.id, options=merge_media_defaults_into_options(row.options, {}))
    clear_media_defaults_cache()
    assert resolve_default_profile(row.id, True, 1, session=schema_session) == 1


def test_cache_is_cleared_per_instance(schema_session):
    from arr_instances.media_defaults import merge_media_defaults_into_options

    _profile(schema_session, 1, "Global")
    _profile(schema_session, 2, "Anime")
    row = _instance(schema_session, "sonarr", "Anime", 8990,
                    {"default_enabled": True, "default_profile": 2})
    assert resolve_default_profile(row.id, True, 1, session=schema_session) == 2

    ArrInstanceRepository(schema_session).update(
        row.id,
        options=merge_media_defaults_into_options(
            None, {"default_enabled": True, "default_profile": 1}))
    # stale until the cache is dropped, then current
    assert resolve_default_profile(row.id, True, 99, session=schema_session) == 2
    clear_media_defaults_cache(row.id)
    assert resolve_default_profile(row.id, True, 1, session=schema_session) == 1


# --------------------------------------------------------------------------
# The sync sites: insert only, per instance
# --------------------------------------------------------------------------

def _noop(*args, **kwargs):
    return None


class _DummyJobs:
    def add_job_from_function(self, *a, **k):
        return None

    def update_job_progress(self, *a, **k):
        return None

    def update_job_name(self, *a, **k):
        return None

    def feed_jobs_pending_queue(self, *a, **k):
        return None


def _series_parser_stub(show, action, tags_dict, language_profiles,
                        serie_default_profile, audio_profiles):
    """Stands in for seriesParser, reproducing its insert-only profile rule."""
    parsed = {"sonarrSeriesId": int(show["id"]), "path": f"/tv/{show['id']}", "title": "S"}
    if action == "insert":
        parsed["profileId"] = serie_default_profile
    return parsed


def _movie_parser_stub(movie, action, tags_dict, language_profiles,
                       movie_default_profile, audio_profiles):
    parsed = {"radarrId": int(movie["id"]), "path": f"/m/{movie['id']}",
              "title": "M", "tmdbId": f"t{movie['id']}", "year": "2020"}
    if action == "insert":
        parsed["profileId"] = movie_default_profile
    return parsed


def _prepare_series_sync(monkeypatch, session):
    import sonarr.sync.series as series_mod

    monkeypatch.setattr(series_mod, "database", session)
    monkeypatch.setattr(series_mod, "event_stream", _noop)
    monkeypatch.setattr(series_mod, "sync_episodes", _noop)
    monkeypatch.setattr(series_mod, "seriesParser", _series_parser_stub)
    return series_mod


def _prepare_movie_sync(monkeypatch, session, movies):
    import radarr.sync.movies as mv_mod

    monkeypatch.setattr(mv_mod, "database", session)
    monkeypatch.setattr(mv_mod, "event_stream", _noop)
    monkeypatch.setattr(mv_mod, "store_subtitles_movie", _noop)
    monkeypatch.setattr(mv_mod, "check_radarr_rootfolder", _noop)
    monkeypatch.setattr(mv_mod, "jobs_queue", _DummyJobs())
    monkeypatch.setattr(mv_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(mv_mod, "get_language_profiles", lambda *a, **k: [])
    def _fetch(*a, **k):
        # The single-movie path asks for one radarr_id and gets one dict back;
        # the bulk path gets the whole list. Mirror both.
        if k.get("radarr_id") is not None:
            return next((m for m in movies if m["id"] == int(k["radarr_id"])), None)
        return movies

    monkeypatch.setattr(mv_mod, "get_movies_from_radarr_api", _fetch)
    monkeypatch.setattr(mv_mod, "movieParser", _movie_parser_stub)
    return mv_mod


def _sync_one_series(series_mod, series_id, arr_instance_id=None):
    series_mod.update_one_series(
        series_id, action="updated", series_data={"id": series_id},
        existing_in_db=False, audio_profiles=[], tags_dict=[], language_profiles=[],
        skip_episode_sync=True, is_signalr=True, arr_instance_id=arr_instance_id)


def _global_series_default(monkeypatch, enabled, profile):
    from app.config import settings

    monkeypatch.setattr(settings.general, "serie_default_enabled", enabled)
    monkeypatch.setattr(settings.general, "serie_default_profile", profile)


def _global_movie_default(monkeypatch, enabled, profile):
    from app.config import settings

    monkeypatch.setattr(settings.general, "movie_default_enabled", enabled)
    monkeypatch.setattr(settings.general, "movie_default_profile", profile)


def _profile_of_series(session, sonarr_series_id):
    return session.execute(
        select(TableShows.profileId)
        .where(TableShows.sonarrSeriesId == sonarr_series_id)).scalar()


def _profile_of_movie(session, radarr_id):
    return session.execute(
        select(TableMovies.profileId).where(TableMovies.radarrId == radarr_id)).scalar()


def test_series_sync_without_override_keeps_the_global_profile(schema_session, monkeypatch):
    """The single-instance install: a sync pass must assign the global profile."""
    _profile(schema_session, 1, "Global")
    inst = _instance(schema_session, "sonarr", "Main", 8989)
    _global_series_default(monkeypatch, True, 1)
    series_mod = _prepare_series_sync(monkeypatch, schema_session)

    _sync_one_series(series_mod, 5)                  # default path, no instance passed
    _sync_one_series(series_mod, 6, inst.id)         # explicitly scoped to that instance

    assert _profile_of_series(schema_session, 5) == 1
    assert _profile_of_series(schema_session, 6) == 1


def test_series_sync_uses_the_instance_override(schema_session, monkeypatch):
    _profile(schema_session, 1, "Standard")
    _profile(schema_session, 2, "Anime")
    anime = _instance(schema_session, "sonarr", "Anime", 8990,
                      {"default_enabled": True, "default_profile": 2})
    _global_series_default(monkeypatch, True, 1)
    series_mod = _prepare_series_sync(monkeypatch, schema_session)

    _sync_one_series(series_mod, 5, anime.id)

    assert _profile_of_series(schema_session, 5) == 2


def test_two_series_instances_assign_different_profiles_in_one_pass(
        schema_session, monkeypatch):
    _profile(schema_session, 1, "Standard")
    _profile(schema_session, 2, "Anime")
    standard = _instance(schema_session, "sonarr", "Standard", 8989,
                         {"default_enabled": True, "default_profile": 1})
    anime = _instance(schema_session, "sonarr", "Anime", 8990,
                      {"default_enabled": True, "default_profile": 2})
    _global_series_default(monkeypatch, False, "")
    series_mod = _prepare_series_sync(monkeypatch, schema_session)

    _sync_one_series(series_mod, 5, standard.id)
    _sync_one_series(series_mod, 6, anime.id)

    assert _profile_of_series(schema_session, 5) == 1
    assert _profile_of_series(schema_session, 6) == 2


def test_series_sync_does_not_move_an_existing_row(schema_session, monkeypatch):
    """An override set later must not reassign media already in the database."""
    _profile(schema_session, 1, "Standard")
    _profile(schema_session, 2, "Anime")
    anime = _instance(schema_session, "sonarr", "Anime", 8990,
                      {"default_enabled": True, "default_profile": 2})
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=5, id=5, arr_instance_id=anime.id, path="/tv/old",
        title="Old", profileId=1))
    _global_series_default(monkeypatch, True, 1)
    series_mod = _prepare_series_sync(monkeypatch, schema_session)

    series_mod.update_one_series(
        5, action="updated", series_data={"id": 5}, existing_in_db=True,
        audio_profiles=[], tags_dict=[], language_profiles=[],
        skip_episode_sync=True, is_signalr=True, arr_instance_id=anime.id)

    assert _profile_of_series(schema_session, 5) == 1   # hand-picked value survives


def test_series_sync_falls_back_when_the_override_profile_is_gone(
        schema_session, monkeypatch):
    _profile(schema_session, 1, "Global")
    anime = _instance(schema_session, "sonarr", "Anime", 8990,
                      {"default_enabled": True, "default_profile": 7})   # never existed
    _global_series_default(monkeypatch, True, 1)
    series_mod = _prepare_series_sync(monkeypatch, schema_session)

    _sync_one_series(series_mod, 5, anime.id)

    assert _profile_of_series(schema_session, 5) == 1


def test_movie_bulk_sync_without_override_keeps_the_global_profile(
        schema_session, monkeypatch):
    _profile(schema_session, 1, "Global")
    _instance(schema_session, "radarr", "Main", 7878)
    _global_movie_default(monkeypatch, True, 1)
    mv_mod = _prepare_movie_sync(monkeypatch, schema_session, [
        {"id": 10, "hasFile": True, "monitored": True, "title": "M",
         "movieFile": {"size": 999999, "path": "/m"}}])

    mv_mod.update_movies(job_id="job-1")

    assert _profile_of_movie(schema_session, 10) == 1


def test_movie_bulk_sync_uses_the_instance_override(schema_session, monkeypatch):
    _profile(schema_session, 1, "Standard")
    _profile(schema_session, 2, "Anime")
    anime = _instance(schema_session, "radarr", "Anime", 7879,
                      {"default_enabled": True, "default_profile": 2})
    _global_movie_default(monkeypatch, True, 1)
    mv_mod = _prepare_movie_sync(monkeypatch, schema_session, [
        {"id": 10, "hasFile": True, "monitored": True, "title": "M",
         "movieFile": {"size": 999999, "path": "/m"}}])

    mv_mod.update_movies(job_id="job-1", arr_instance_id=anime.id, arr_client=object())

    assert _profile_of_movie(schema_session, 10) == 2


def test_single_movie_sync_uses_the_instance_override(schema_session, monkeypatch):
    _profile(schema_session, 1, "Standard")
    _profile(schema_session, 2, "Anime")
    anime = _instance(schema_session, "radarr", "Anime", 7879,
                      {"default_enabled": True, "default_profile": 2})
    _global_movie_default(monkeypatch, True, 1)
    mv_mod = _prepare_movie_sync(monkeypatch, schema_session, [
        {"id": 10, "hasFile": True, "monitored": True, "title": "M",
         "movieFile": {"size": 999999, "path": "/m"}}])

    mv_mod.update_one_movie(10, action="updated", arr_instance_id=anime.id,
                            arr_client=object())

    assert _profile_of_movie(schema_session, 10) == 2


def test_single_movie_sync_without_override_keeps_the_global_profile(
        schema_session, monkeypatch):
    _profile(schema_session, 1, "Global")
    _instance(schema_session, "radarr", "Main", 7878)
    _global_movie_default(monkeypatch, True, 1)
    mv_mod = _prepare_movie_sync(monkeypatch, schema_session, [
        {"id": 10, "hasFile": True, "monitored": True, "title": "M",
         "movieFile": {"size": 999999, "path": "/m"}}])

    mv_mod.update_one_movie(10, action="updated")

    assert _profile_of_movie(schema_session, 10) == 1


def test_a_matching_tag_still_beats_the_instance_default(monkeypatch):
    """Precedence: tag > instance default > global default.

    Driven through the real seriesParser, since that is where the tag override
    is applied, after whichever default the sync site resolved.
    """
    from app.config import settings
    from sonarr.sync.parser import seriesParser

    monkeypatch.setattr(settings.general, "serie_tag_enabled", True)
    monkeypatch.setattr(settings.general, "remove_profile_tags", [])
    show = {
        "id": 5, "title": "S", "path": "/tv/s", "tvdbId": 1, "overview": "",
        "images": [], "alternateTitles": [], "tags": [1], "sortTitle": "s",
        "year": 2020, "seriesType": "anime", "monitored": True, "ended": False,
    }
    language_profiles = [(3, "Tagged", "anime-tag")]

    parsed = seriesParser(show, action="insert", tags_dict=[{"id": 1, "label": "anime-tag"}],
                          language_profiles=language_profiles,
                          serie_default_profile=2, audio_profiles=[])
    assert parsed["profileId"] == 3

    # without a tag match the instance-resolved default stands
    parsed = seriesParser(show, action="insert", tags_dict=[{"id": 1, "label": "other"}],
                          language_profiles=language_profiles,
                          serie_default_profile=2, audio_profiles=[])
    assert parsed["profileId"] == 2


# --------------------------------------------------------------------------
# CRUD: the override round-trips through the existing options column
# --------------------------------------------------------------------------

def test_service_create_and_read_back_the_override(schema_session):
    from arr_instances import service

    _profile(schema_session, 2, "Anime")
    body, status = service.create_instance(schema_session, {
        "kind": "sonarr", "name": "Anime", "ip": "127.0.0.1", "port": 8990,
        "media_defaults": {"default_enabled": True, "default_profile": 2},
    })
    assert status == 201, body
    assert body["media_defaults"] == {"default_enabled": True, "default_profile": 2}

    fetched, _ = service.get_instance(schema_session, body["id"])
    assert fetched["media_defaults"] == {"default_enabled": True, "default_profile": 2}


def test_service_rejects_an_override_naming_an_unknown_profile(schema_session):
    from arr_instances import service

    _profile(schema_session, 2, "Anime")
    body, status = service.create_instance(schema_session, {
        "kind": "sonarr", "name": "Bad", "ip": "127.0.0.1", "port": 8990,
        "media_defaults": {"default_enabled": True, "default_profile": 99},
    })
    assert status == 400
    assert "99" in body["message"]


def test_service_update_round_trips_and_clears_the_override(schema_session):
    from arr_instances import service

    _profile(schema_session, 2, "Anime")
    created, _ = service.create_instance(schema_session, {
        "kind": "sonarr", "name": "Anime", "ip": "127.0.0.1", "port": 8990})
    assert created["media_defaults"] == {}

    updated, status = service.update_instance(schema_session, created["id"], {
        "media_defaults": {"default_enabled": True, "default_profile": 2}})
    assert status == 200, updated
    assert updated["media_defaults"] == {"default_enabled": True, "default_profile": 2}

    cleared, status = service.update_instance(
        schema_session, created["id"], {"media_defaults": {}})
    assert status == 200
    assert cleared["media_defaults"] == {}


def test_service_update_keeps_subtitle_settings_intact(schema_session):
    from arr_instances import service

    _profile(schema_session, 2, "Anime")
    created, _ = service.create_instance(schema_session, {
        "kind": "sonarr", "name": "Anime", "ip": "127.0.0.1", "port": 8990,
        "subtitle_settings": {"subsync": {"subsync_threshold": 80}}})
    updated, _ = service.update_instance(schema_session, created["id"], {
        "media_defaults": {"default_enabled": True, "default_profile": 2}})

    assert updated["subtitle_settings"] == {"subsync": {"subsync_threshold": 80}}
    assert updated["media_defaults"] == {"default_enabled": True, "default_profile": 2}


# --------------------------------------------------------------------------
# The opt-in apply action: unset profiles only, one instance only
# --------------------------------------------------------------------------

def _series(session, local_id, upstream_id, instance_id, profile=None, tags=None):
    session.execute(insert(TableShows).values(
        id=local_id, sonarrSeriesId=upstream_id, arr_instance_id=instance_id,
        path=f"/tv/{local_id}", title=f"S{local_id}", profileId=profile,
        tags=str(tags) if tags is not None else None))


def _movie(session, local_id, upstream_id, instance_id, profile=None, tags=None):
    session.execute(insert(TableMovies).values(
        id=local_id, radarrId=upstream_id, arr_instance_id=instance_id,
        path=f"/m/{local_id}", title=f"M{local_id}", tmdbId=f"t{local_id}",
        profileId=profile, tags=str(tags) if tags is not None else None))


def test_apply_fills_only_unset_profiles_on_that_instance(schema_session):
    from arr_instances import service

    _profile(schema_session, 1, "Standard")
    _profile(schema_session, 2, "Anime")
    standard = _instance(schema_session, "sonarr", "Standard", 8989,
                         {"default_enabled": True, "default_profile": 1})
    anime = _instance(schema_session, "sonarr", "Anime", 8990,
                      {"default_enabled": True, "default_profile": 2})

    _series(schema_session, 1, 10, anime.id, None)   # unset, on the target
    _series(schema_session, 2, 11, anime.id, 1)      # hand-picked, on the target
    _series(schema_session, 3, 12, standard.id, None)  # unset, other instance
    _series(schema_session, 4, 13, None, None)       # unowned

    body, status = service.apply_default_profile(schema_session, anime.id)
    assert status == 200, body
    assert body["updated"] == 1
    assert body["profileId"] == 2
    assert body["kind"] == "sonarr"
    assert body["upstream_ids"] == [10]

    profiles = dict(schema_session.execute(
        select(TableShows.id, TableShows.profileId)).all())
    assert profiles == {1: 2, 2: 1, 3: None, 4: None}


def test_apply_is_scoped_to_movies_of_that_instance(schema_session):
    from arr_instances import service

    _profile(schema_session, 2, "Anime")
    main = _instance(schema_session, "radarr", "Main", 7878)
    anime = _instance(schema_session, "radarr", "Anime", 7879,
                      {"default_enabled": True, "default_profile": 2})
    _movie(schema_session, 1, 10, anime.id, None)
    _movie(schema_session, 2, 11, main.id, None)

    body, status = service.apply_default_profile(schema_session, anime.id)
    assert status == 200, body
    assert body["updated"] == 1

    profiles = dict(schema_session.execute(
        select(TableMovies.id, TableMovies.profileId)).all())
    assert profiles == {1: 2, 2: None}


def test_apply_needs_an_override_to_apply(schema_session):
    from arr_instances import service

    _profile(schema_session, 1, "Global")
    plain = _instance(schema_session, "sonarr", "Main", 8989)
    _series(schema_session, 1, 10, plain.id, None)

    body, status = service.apply_default_profile(schema_session, plain.id)
    assert status == 400
    assert _profile_of_series(schema_session, 10) is None

    # ... and an override that deliberately assigns nothing is not applicable
    off = _instance(schema_session, "sonarr", "Off", 8990, {"default_enabled": False})
    body, status = service.apply_default_profile(schema_session, off.id)
    assert status == 400


def test_apply_rejects_an_override_whose_profile_is_gone(schema_session):
    from sqlalchemy import delete

    from arr_instances import service

    _profile(schema_session, 2, "Anime")
    anime = _instance(schema_session, "sonarr", "Anime", 8990,
                      {"default_enabled": True, "default_profile": 2})
    _series(schema_session, 1, 10, anime.id, None)
    schema_session.execute(
        delete(TableLanguagesProfiles).where(TableLanguagesProfiles.profileId == 2))

    body, status = service.apply_default_profile(schema_session, anime.id)
    assert status == 400
    assert _profile_of_series(schema_session, 10) is None


def test_apply_on_an_unknown_instance_is_404(schema_session):
    from arr_instances import service

    body, status = service.apply_default_profile(schema_session, 4242)
    assert status == 404


def test_refresh_runtime_drops_the_media_defaults_cache(schema_session, monkeypatch):
    """An edited override must take effect on the next sync, not the next restart."""
    from arr_instances import resolution, service

    monkeypatch.setattr(service, "event_stream", _noop)
    resolution._media_defaults_cache[123] = {"default_enabled": True, "default_profile": 9}
    service.refresh_runtime("nonexistent-kind")
    assert resolution._media_defaults_cache == {}


def test_the_api_layer_is_wired_for_media_defaults():
    """Guard against the field or the apply route being written but never routed.

    Read as source rather than imported: pulling api.system.arr_instances drags
    the whole flask_restx chain into this process and would leak it into every
    test that shares it.
    """
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[2].joinpath(
        "bazarr", "api", "system", "arr_instances.py").read_text()
    assert '_create_parser.add_argument("media_defaults"' in source
    assert '_update_parser.add_argument("media_defaults"' in source
    assert "/apply-default-profile" in source
    assert "service.apply_default_profile(" in source


def test_service_update_accepts_both_blobs_in_one_request(schema_session):
    """A PATCH carrying both blobs must not let one overwrite the other."""
    from arr_instances import service

    _profile(schema_session, 2, "Anime")
    created, _ = service.create_instance(schema_session, {
        "kind": "sonarr", "name": "Anime", "ip": "127.0.0.1", "port": 8990})

    updated, status = service.update_instance(schema_session, created["id"], {
        "subtitle_settings": {"subsync": {"subsync_threshold": 80}},
        "media_defaults": {"default_enabled": True, "default_profile": 2},
    })
    assert status == 200, updated
    assert updated["subtitle_settings"] == {"subsync": {"subsync_threshold": 80}}
    assert updated["media_defaults"] == {"default_enabled": True, "default_profile": 2}


# --------------------------------------------------------------------------
# Review findings: the bulk apply undoing a deliberate exclusion, the sync
# re-resolving per item, the health check not seeing instance defaults, and
# the apply endpoint doing a whole library's reindex inside the request.
# --------------------------------------------------------------------------


def test_apply_skips_series_a_tag_deliberately_excluded(schema_session, monkeypatch):
    """A tag in remove_profile_tags makes the sync parser set profileId to None
    on purpose. That is not "unset yet", it is "kept out", and a bulk apply that
    fills it in silently undoes the rule the user configured."""
    from app.config import settings
    from arr_instances import service

    monkeypatch.setattr(settings.general, "serie_tag_enabled", True)
    monkeypatch.setattr(settings.general, "remove_profile_tags", ["nosubs"])

    _profile(schema_session, 2, "Anime")
    anime = _instance(schema_session, "sonarr", "Anime", 8990,
                      {"default_enabled": True, "default_profile": 2})
    _series(schema_session, 1, 10, anime.id, None)
    _series(schema_session, 2, 11, anime.id, None, tags=["nosubs"])

    body, status = service.apply_default_profile(schema_session, anime.id)

    assert status == 200, body
    assert body["updated"] == 1
    assert body["upstream_ids"] == [10]
    profiles = dict(schema_session.execute(
        select(TableShows.id, TableShows.profileId)).all())
    assert profiles == {1: 2, 2: None}


def test_apply_skips_movies_a_tag_deliberately_excluded(schema_session, monkeypatch):
    from app.config import settings
    from arr_instances import service

    monkeypatch.setattr(settings.general, "movie_tag_enabled", True)
    monkeypatch.setattr(settings.general, "remove_profile_tags", ["nosubs"])

    _profile(schema_session, 2, "Anime")
    anime = _instance(schema_session, "radarr", "Anime", 7879,
                      {"default_enabled": True, "default_profile": 2})
    _movie(schema_session, 1, 10, anime.id, None)
    _movie(schema_session, 2, 11, anime.id, None, tags=["nosubs"])

    body, status = service.apply_default_profile(schema_session, anime.id)

    assert body["updated"] == 1
    assert body["upstream_ids"] == [10]


def test_apply_ignores_the_exclusion_when_tag_handling_is_off(schema_session, monkeypatch):
    """The tag list only means anything while tag handling is enabled."""
    from app.config import settings
    from arr_instances import service

    monkeypatch.setattr(settings.general, "serie_tag_enabled", False)
    monkeypatch.setattr(settings.general, "remove_profile_tags", ["nosubs"])

    _profile(schema_session, 2, "Anime")
    anime = _instance(schema_session, "sonarr", "Anime", 8990,
                      {"default_enabled": True, "default_profile": 2})
    _series(schema_session, 1, 10, anime.id, None, tags=["nosubs"])

    body, _status = service.apply_default_profile(schema_session, anime.id)

    assert body["updated"] == 1


def test_the_bulk_series_sync_resolves_the_default_profile_once(schema_session, monkeypatch):
    """Once per sync, not once per series: the resolution issues an indexed
    query whenever an override is set, so a library of a few thousand shows pays
    for a few thousand round trips that all return the same answer. Every other
    invariant in this loop is already hoisted; this one was not."""
    series_mod = _prepare_series_sync(monkeypatch, schema_session)

    calls = []

    def counting(*args, **kwargs):
        calls.append(args)
        return 2

    monkeypatch.setattr(series_mod, "resolve_default_profile", counting)
    monkeypatch.setattr(series_mod, "jobs_queue", _DummyJobs())
    monkeypatch.setattr(series_mod, "check_sonarr_rootfolder", _noop)
    monkeypatch.setattr(series_mod, "get_profile_list", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_tags", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_language_profiles", lambda *a, **k: [])
    monkeypatch.setattr(series_mod, "get_series_from_sonarr_api",
                        lambda *a, **k: [{"id": n, "title": f"S{n}"} for n in range(1, 6)])
    monkeypatch.setattr(series_mod, "get_episodes_from_sonarr_api", lambda *a, **k: [])

    series_mod.update_series(job_id=1)

    assert len(calls) == 1, f"resolved {len(calls)} times for 5 series"


def test_the_health_check_sees_a_per_instance_default_profile(schema_session, monkeypatch):
    """With the global default enabled but no profile chosen, a per-instance
    override is what newly synced media actually gets. Reporting "no default
    profile" then sends the user to fix something that is already configured."""
    from app.config import settings
    from utilities import health

    monkeypatch.setattr(settings.general, "serie_default_enabled", True)
    monkeypatch.setattr(settings.general, "serie_default_profile", "")
    monkeypatch.setattr(settings.general, "movie_default_enabled", False)
    monkeypatch.setattr(health, "database", schema_session)

    _profile(schema_session, 2, "Anime")
    _instance(schema_session, "sonarr", "Anime", 8990,
              {"default_enabled": True, "default_profile": 2})

    assert health.series_default_profile_is_missing() is False


def test_the_health_check_still_reports_a_genuinely_missing_default(schema_session, monkeypatch):
    from app.config import settings
    from utilities import health

    monkeypatch.setattr(settings.general, "serie_default_enabled", True)
    monkeypatch.setattr(settings.general, "serie_default_profile", "")
    monkeypatch.setattr(health, "database", schema_session)

    _instance(schema_session, "sonarr", "Plain", 8989)

    assert health.series_default_profile_is_missing() is True


def test_the_apply_endpoint_queues_the_reindex_instead_of_running_it(monkeypatch):
    """A library of a few thousand unprofiled series means a few thousand
    index passes, each scanning every episode and emitting events. Doing that
    inside the request holds a web worker for minutes and can outlive a proxy
    timeout, while the UI has nothing to show for it."""
    import api.system.arr_instances as endpoint_module

    queued = []
    indexed = []

    class _Queue:
        def feed_jobs_pending_queue(self, job_name, module, func, args=None, kwargs=None,
                                    **options):
            queued.append({"job_name": job_name, "module": module, "func": func,
                           "args": args, "kwargs": kwargs})
            return 1

    monkeypatch.setattr(endpoint_module.service, "apply_default_profile",
                        lambda *a, **k: ({"updated": 3, "profileId": 2, "kind": "sonarr",
                                          "upstream_ids": [10, 11, 12]}, 200))
    monkeypatch.setattr(endpoint_module, "database", _CommitOnly())
    monkeypatch.setattr(endpoint_module, "jobs_queue", _Queue())

    import subtitles.indexer.series as indexer
    monkeypatch.setattr(indexer, "list_missing_subtitles",
                        lambda **kwargs: indexed.append(kwargs))

    resource = endpoint_module.ArrInstanceApplyDefaultProfile()
    body, status = resource.post.__wrapped__(resource, 4)

    assert status == 200
    assert body == {"updated": 3, "profileId": 2}
    assert indexed == [], "the reindex ran inside the request"
    assert len(queued) == 1
    assert queued[0]["kwargs"]["upstream_ids"] == [10, 11, 12]
    assert queued[0]["kwargs"]["arr_instance_id"] == 4


def test_the_queued_reindex_does_the_work_the_request_no_longer_does(monkeypatch):
    from arr_instances import service

    indexed = []
    events = []
    monkeypatch.setattr(service, "event_stream",
                        lambda *args, **kwargs: events.append(kwargs or args))

    import subtitles.indexer.series as indexer
    monkeypatch.setattr(indexer, "list_missing_subtitles",
                        lambda **kwargs: indexed.append(kwargs))

    service.reindex_after_default_profile(
        kind="sonarr", upstream_ids=[10, 11], arr_instance_id=4, job_id=1)

    assert [entry["no"] for entry in indexed] == [10, 11]
    assert all(entry["arr_instance_id"] == 4 for entry in indexed)
    assert events, "the UI was never told the items changed"


class _CommitOnly:
    def commit(self):
        return None
