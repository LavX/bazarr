# coding=utf-8
"""A hidden embedded track is skipped by all three indexers.

Skipping at index time is the whole mechanism: every missing-subtitle
recompute, the Wanted pages, the badges, mass operations and the row menus
read the listing the indexer stored, so a track that is never stored neither
shows nor satisfies its language anywhere. The database cases run on SQLite and
PostgreSQL, both on an AUTOCOMMIT engine.
"""
import ast
import json

import pytest
import sqlalchemy as sa

from app.database import TableEpisodes, TableLanguagesProfiles, TableMovies, TableShows
from test_embedded_track_hidden import hidden_db  # noqa: F401
from test_sportarr_indexer import indexed_library, row, sports  # noqa: F401
from test_sportarr_kind_migration import migration_engine  # noqa: F401

CODES = {"eng": "en", "fra": "fr", "deu": "de"}
TRACKS = [["eng", False, False, "subrip"], ["eng", False, True, "subrip"], ["fra", False, False, "pgs"]]
PROFILE = [{"id": i, "language": code, "hi": "False", "forced": "False",
            "audio_exclude": "False", "audio_only_include": "False"}
           for i, code in enumerate(("en", "fr"), 1)]


def _stub_indexer(module, session, monkeypatch, logged):
    monkeypatch.setattr(module, "database", session)
    monkeypatch.setattr(module.os.path, "exists", lambda p: True)
    monkeypatch.setattr(module.settings.general, "use_embedded_subs", True, raising=False)
    monkeypatch.setattr(module.settings.general, "ignore_pgs_subs", False, raising=False)
    monkeypatch.setattr(module, "embedded_subs_reader", lambda *a, **kw: [list(t) for t in TRACKS])
    monkeypatch.setattr(module, "alpha2_from_alpha3", CODES.get)
    monkeypatch.setattr(module, "get_language_set", lambda: set())
    monkeypatch.setattr(module, "search_external_subtitles", lambda *a, **kw: {})
    monkeypatch.setattr(module, "add_sync_engine_outputs", lambda folder, subs, **kw: subs)
    monkeypatch.setattr(module, "add_combined_outputs", lambda folder, subs, **kw: subs)
    monkeypatch.setattr(module, "guess_external_subtitles", lambda *a, **kw: {})
    monkeypatch.setattr(module, "event_stream", lambda *a, **kw: None)
    monkeypatch.setattr(module, "prune_mismatches_for_media", lambda *a, **kw: None)
    monkeypatch.setattr(module, "get_profiles_list", lambda profile_id=None: {"items": PROFILE})
    monkeypatch.setattr(module, "get_profile_cutoff", lambda profile_id=None: [])
    monkeypatch.setattr(module, "get_audio_profile_languages", lambda *a, **kw: [])
    monkeypatch.setattr(module, "is_search_given_up", lambda *a, **kw: False)
    history = "_log_embedded_history_movie" if module.__name__.endswith("movies") else "_log_embedded_history"
    monkeypatch.setattr(module, history, lambda *args, **kwargs: logged.append(
        [a for a in args if isinstance(a, list)][0]))


def _stored(session, table, row_id):
    session.expire_all()
    found = session.execute(sa.select(table.subtitles, table.missing_subtitles)
                            .where(table.id == row_id)).one()
    return [entry[0] for entry in ast.literal_eval(found.subtitles)], ast.literal_eval(found.missing_subtitles)


def test_movie_hidden_track_is_neither_listed_nor_counted(hidden_db, monkeypatch):  # noqa: F811
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track, unhide_track

    logged = []
    _stub_indexer(mv, hidden_db, monkeypatch, logged)
    hidden_db.add(TableLanguagesProfiles(profileId=1, name="P", items=json.dumps(PROFILE)))
    hidden_db.add_all([
        TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                    movie_file_id=700, file_size=111, profileId=1, subtitles="[]"),
        # The same upstream movie on a second instance keeps its own tracks.
        TableMovies(id=2, arr_instance_id=2, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                    movie_file_id=800, file_size=222, profileId=1, subtitles="[]"),
    ])
    hidden_db.commit()

    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1) == (["en", "en:hi", "fr"], [])

    hide_track(hidden_db, "movie", 1, 1, "en")
    hide_track(hidden_db, "movie", 1, 1, "en:hi")
    logged.clear()
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1) == (["fr"], ["en"])
    assert logged == [["fr"]], "a hidden track must not write a new action 7 history row"

    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=2)
    assert _stored(hidden_db, TableMovies, 2) == (["en", "en:hi", "fr"], [])

    unhide_track(hidden_db, "movie", [1], 1, "en")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1) == (["en", "fr"], [])


def _movie_with_profile(session):
    session.add(TableLanguagesProfiles(profileId=1, name="P", items=json.dumps(PROFILE)))
    session.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv", tmdbId="1",
                            movie_file_id=700, file_size=111, profileId=1, subtitles="[]"))
    session.commit()


def test_hiding_the_plain_track_leaves_english_covered_by_the_hi_track(hidden_db, monkeypatch):  # noqa: F811
    """The wanted computation counts an HI track as good as a normal one, so
    with en and en:hi in the file, hiding en takes the row away but English is
    still satisfied. The API reports this so the page can say it."""
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track

    _stub_indexer(mv, hidden_db, monkeypatch, [])
    _movie_with_profile(hidden_db)
    hide_track(hidden_db, "movie", 1, 1, "en")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1) == (["en:hi", "fr"], [])


def test_one_hide_takes_every_track_with_that_key(hidden_db, monkeypatch):  # noqa: F811
    """An SRT and a PGS English on one remux are both stored as en. They are
    one key to the indexer and the wanted computation, so one hide takes both,
    and only then is English wanted."""
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track

    _stub_indexer(mv, hidden_db, monkeypatch, [])
    monkeypatch.setattr(mv, "embedded_subs_reader", lambda *a, **kw: [
        ["eng", False, False, "subrip"], ["eng", False, False, "pgs"], ["fra", False, False, "pgs"]])
    _movie_with_profile(hidden_db)
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1) == (["en", "en", "fr"], [])

    hide_track(hidden_db, "movie", 1, 1, "en")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1) == (["fr"], ["en"])


def test_global_codec_ignores_still_apply_alongside_hidden(hidden_db, monkeypatch):  # noqa: F811
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track

    _stub_indexer(mv, hidden_db, monkeypatch, [])
    monkeypatch.setattr(mv.settings.general, "ignore_pgs_subs", True, raising=False)
    hidden_db.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv",
                                   tmdbId="1", movie_file_id=700, file_size=111, subtitles="[]"))
    hidden_db.commit()
    hide_track(hidden_db, "movie", 1, 1, "en:hi")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1)[0] == ["en"]


def test_episode_hidden_on_one_row_hides_it_for_the_whole_file(hidden_db, monkeypatch):  # noqa: F811
    import subtitles.indexer.series as se
    from subtitles.embedded_hidden import hide_track

    logged = []
    _stub_indexer(se, hidden_db, monkeypatch, logged)
    hidden_db.add(TableLanguagesProfiles(profileId=1, name="P", items=json.dumps(PROFILE)))
    hidden_db.add(TableShows(id=1, arr_instance_id=1, sonarrSeriesId=1, title="S", path="/tv/s",
                                  profileId=1))
    hidden_db.flush()
    for episode_id in (1, 2):
        hidden_db.add(TableEpisodes(
            id=episode_id, arr_instance_id=1, series_id=1, sonarrSeriesId=1, sonarrEpisodeId=10 + episode_id,
            title="E", path="/tv/s/e01-e02.mkv", season=1, episode=episode_id, episode_file_id=500,
            file_size=111, subtitles="[]"))
    hidden_db.commit()

    hide_track(hidden_db, "episode", 2, 1, "fr")
    se.store_subtitles("/tv/s/e01-e02.mkv", "/local/e01-e02.mkv", arr_instance_id=1)
    for episode_id in (1, 2):
        assert _stored(hidden_db, TableEpisodes, episode_id) == (["en", "en:hi"], ["fr"])
    assert all("fr" not in languages for languages in logged)


def test_embedded_subs_show_desired_filters_what_hidden_left(monkeypatch):
    """Hidden is applied at index time, before postprocess, so show-desired
    only ever sees what is still listed."""
    from api import utils

    monkeypatch.setattr(utils.settings.general, "embedded_subs_show_desired", True)
    monkeypatch.setattr(utils, "get_desired_languages", lambda profile_id: ["fr"])
    monkeypatch.setattr(utils, "language_from_alpha2", lambda code: code)
    monkeypatch.setattr(utils, "alpha3_from_alpha2", lambda code: code)
    item = utils.postprocess({"radarrId": 7, "profileId": 1,
                              "subtitles": str([["en:hi", None, None], ["fr", None, None],
                                                ["en", "/m/m.en.srt", 10]])})
    assert [(s["code2"], s["path"]) for s in item["subtitles"]] == [("en", "/m/m.en.srt"), ("fr", None)]


def test_mass_translate_never_offers_a_hidden_track_as_a_source(hidden_db, monkeypatch):  # noqa: F811
    """Mass operations collect from the stored listing too. A hidden track is
    not in it, so a bulk job can neither translate from it nor, once bulk
    extraction exists, extract it."""
    import subtitles.indexer.movies as mv
    from subtitles import mass_operations
    from subtitles.embedded_hidden import hide_track

    _stub_indexer(mv, hidden_db, monkeypatch, [])
    hidden_db.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv",
                                   tmdbId="1", movie_file_id=700, file_size=111, subtitles="[]"))
    hidden_db.commit()
    hide_track(hidden_db, "movie", 1, 1, "fr")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    stored = hidden_db.execute(sa.select(TableMovies.subtitles)).scalar_one()
    parsed = mass_operations._parse_subtitles_column(stored, include_embedded=True)
    assert [language for language, _path in parsed] == ["en", "en:hi"]


def test_sports_hidden_track_is_skipped_for_that_owner_only(indexed_library, monkeypatch):  # noqa: F811
    from app import database as db
    from app.database import TableHistorySports
    from sportarr import library
    from subtitles.embedded_hidden import hide_track, unhide_track

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    session.execute(sa.insert(db.TableLanguagesProfiles).values(
        profileId=5, name="Sports", items=json.dumps(PROFILE)))
    db.update_profile_id_list.invalidate()
    assert library.assign_profile(session, 51, 1, 5)

    hide_track(session, "sports", 61, 1, "fr", "subrip")
    first = module.store_subtitles_sports(61, 1)
    assert [item[0] for item in first] == ["en:hi"]
    assert ast.literal_eval(row(session, 61).missing_subtitles) == ["fr"]
    assert session.execute(sa.select(TableHistorySports.id).where(
        TableHistorySports.event_id == 61, TableHistorySports.action == 7)).first() is None

    second = module.store_subtitles_sports(62, 2)
    assert {item[0] for item in second} == {"fr", "de:forced"}

    unhide_track(session, "sports", [61], 1, "fr")
    again = module.store_subtitles_sports(61, 1)
    assert {item[0] for item in again} == {"fr", "en:hi"}
    assert ast.literal_eval(row(session, 61).missing_subtitles) == []


@pytest.mark.parametrize("use_embedded", [False])
def test_hidden_records_are_inert_when_embedded_subtitles_are_off(hidden_db, monkeypatch, use_embedded):  # noqa: F811
    import subtitles.indexer.movies as mv
    from subtitles.embedded_hidden import hide_track

    _stub_indexer(mv, hidden_db, monkeypatch, [])
    monkeypatch.setattr(mv.settings.general, "use_embedded_subs", use_embedded, raising=False)
    hidden_db.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv",
                                   tmdbId="1", movie_file_id=700, file_size=111, subtitles="[]"))
    hidden_db.commit()
    hide_track(hidden_db, "movie", 1, 1, "en")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    assert _stored(hidden_db, TableMovies, 1)[0] == []


def test_hiding_a_track_never_hides_a_subtitle_file_of_the_same_language(hidden_db, monkeypatch, tmp_path):  # noqa: F811
    import subtitles.indexer.movies as mv
    from subzero.language import Language
    from subtitles.embedded_hidden import hide_track

    _stub_indexer(mv, hidden_db, monkeypatch, [])
    external = tmp_path / "m.en.srt"
    external.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    found = {"m.en.srt": Language("eng")}
    monkeypatch.setattr(mv, "search_external_subtitles", lambda *a, **kw: dict(found))
    monkeypatch.setattr(mv, "guess_external_subtitles", lambda folder, subs, *a, **kw: subs)
    monkeypatch.setattr(mv, "get_external_subtitles_path", lambda video, subtitle: str(external))
    monkeypatch.setattr(mv.path_mappings, "path_replace_reverse_instance", lambda p, *a: p)
    hidden_db.add(TableMovies(id=1, arr_instance_id=1, radarrId=7, title="M", path="/movies/m.mkv",
                                   tmdbId="1", movie_file_id=700, file_size=111, subtitles="[]"))
    hidden_db.commit()
    hide_track(hidden_db, "movie", 1, 1, "en")
    mv.store_subtitles_movie("/movies/m.mkv", "/local/m.mkv", arr_instance_id=1)
    stored = ast.literal_eval(hidden_db.execute(sa.select(TableMovies.subtitles)).scalar_one())
    assert [entry[:2] for entry in stored] == [["en:hi", None], ["fr", None], ["en", str(external)]]
