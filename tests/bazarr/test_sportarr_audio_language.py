# coding=utf-8
"""Audio track languages for sports events.

Sportarr reports no audio metadata at all (its API returns languages: [] on
every file), so ffprobe is the only source there has ever been. Sonarr and
Radarr both branch on parse_embedded_audio_track and fall back to the arr
payload; sports implemented only the fallback, so the column could never be
populated and the audio_exclude / audio_only_include profile rules were inert
for every sports event.
"""
import pytest
import sqlalchemy as sa
from test_sportarr_events import library as library
from test_sportarr_kind_migration import migration_engine  # noqa: F401


@pytest.fixture
def languages_loaded():
    """Seed the module-level language table the name lookups read.

    create_languages_dict() normally runs at app startup off the database;
    language_from_alpha3 raises NameError without it.
    """
    from languages import get_languages

    get_languages.languages_dict = [
        {"code3": "hun", "code2": "hu", "name": "Hungarian", "code3b": "hun"},
        {"code3": "eng", "code2": "en", "name": "English", "code3b": "eng"},
    ]
    return get_languages.languages_dict


def test_audio_languages_come_out_of_an_already_parsed_blob(languages_loaded):
    """The indexer parses the file for embedded subtitles anyway.

    Deriving the audio languages from that same blob avoids probing a multi-GB
    recording twice.
    """
    from utilities.video_analyzer import audio_languages_from_metadata

    blob = {
        "ffprobe": {
            "audio": [
                {"language": "hun"},
                {"language": "eng"},
            ]
        }
    }
    assert audio_languages_from_metadata(blob, "/media/race.mkv") == [
        "Hungarian",
        "English",
    ]


def test_audio_languages_are_names_not_codes(languages_loaded):
    """get_audio_profile_languages resolves entries by NAME.

    Handing it an ISO code makes it hand back code2 None, so this is the shape
    the rest of the pipeline requires.
    """
    from utilities.video_analyzer import audio_languages_from_metadata

    resolved = audio_languages_from_metadata(
        {"ffprobe": {"audio": [{"language": "hun"}]}}, "/media/race.mkv"
    )
    assert resolved == ["Hungarian"]
    assert "hu" not in resolved


def test_empty_and_missing_metadata_are_survivable():
    from utilities.video_analyzer import audio_languages_from_metadata

    assert audio_languages_from_metadata(None, "/media/race.mkv") == []
    assert audio_languages_from_metadata({}, "/media/race.mkv") == []
    assert audio_languages_from_metadata({"ffprobe": {}}, "/media/race.mkv") == []


@pytest.mark.parametrize('indexer_owns_audio', [True, False])
@pytest.mark.parametrize('changed_file', [False, True])
def test_sync_does_not_clobber_what_the_indexer_derived(library, monkeypatch, indexer_owns_audio, changed_file):
    """The parser can only ever offer '[]', because Sportarr sends nothing.

    Letting the sync write that back erased the ffprobe result, and because
    audio_language took part in the new-file comparison it also made the next
    sync judge the file new and wipe the whole subtitle index with it.
    """
    from app.config import settings
    from app.database import TableSportsEvents
    from test_sportarr_events import event, file, remote

    session, events = library
    monkeypatch.setattr(settings.general, 'parse_embedded_audio_track', indexer_owns_audio)
    remote(monkeypatch, events, [event()])
    local_id = events.sync_events(51, 1)[0]
    indexed = dict(audio_language="['English']", ffprobe_cache=b'ffprobe result',
                   subtitles="[['en', '/sports/part-9.en.srt']]", missing_subtitles="['hu']",
                   failedAttempts="['previous attempt']")
    session.execute(sa.update(TableSportsEvents).where(TableSportsEvents.id == local_id).values(**indexed))
    remote(monkeypatch, events, [event([file(languages=[], size=100001 if changed_file else 100000)])])
    assert events.sync_events(51, 1) == [local_id]
    current = session.get(TableSportsEvents, local_id, populate_existing=True)
    if indexer_owns_audio and not changed_file:
        assert {name: getattr(current, name) for name in indexed} == indexed
    else:
        assert current.audio_language == '[]'
        assert current.ffprobe_cache is None
        assert (current.subtitles, current.missing_subtitles, current.failedAttempts) == ('[]', '[]', '[]')


def test_indexer_writes_audio_language_before_computing_missing():
    """The audio profile rules are evaluated off this column.

    Writing it after _missing would leave audio_exclude / audio_only_include
    reading a stale value for a whole indexing cycle.
    """
    import inspect

    from subtitles.indexer import sports

    source = inspect.getsource(sports.store_subtitles_sports)
    audio_at = source.index("row.audio_language = str(audio_languages_from_metadata")
    missing_at = source.index("row.missing_subtitles = str(_missing(")
    assert audio_at < missing_at


@pytest.mark.parametrize("flag", [True, False])
def test_metadata_is_parsed_when_either_embedded_feature_is_on(flag):
    import inspect

    from subtitles.indexer import sports

    source = inspect.getsource(sports.store_subtitles_sports)
    assert (
        "if settings.general.use_embedded_subs or settings.general.parse_embedded_audio_track:"
        in source
    )


@pytest.fixture
def audio_catalogue(schema_session, monkeypatch, languages_loaded):
    """The library-wide audio-language endpoint bound to a catalogued session.

    The endpoint lives behind an absolute import of api.utils, so it is loaded
    in isolation exactly like the other sports API tests, with the database
    swapped for the fixture session. The sports-only language is served from
    the sports events table; no movie or episode row carries it.
    """
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType

    from app.config import settings
    from app.database import (TableArrInstances, TableMovies,
                              TableSportsEvents, TableSportsLeagues)

    languages_loaded.append(
        {"code3": "eus", "code2": "eu", "name": "Basque", "code3b": "eus"}
    )

    schema_session.add(TableArrInstances(id=42, kind="sportarr", stable_key="sportarr-1",
                                         name="Sportarr", enabled=1, port=1867, api_key="k"))
    schema_session.flush()
    schema_session.add(TableSportsLeagues(id=51, arr_instance_id=42, sportarrLeagueId=7,
                                          title="Formula 1", path="/remote/sports"))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=61, arr_instance_id=42, league_id=51, sportarrEventId=9, file_id=71,
        path="/remote/sports/race.mkv", title="Race", audio_language="['Basque']"))
    schema_session.add(TableArrInstances(id=2, kind="radarr", stable_key="radarr-1",
                                         name="Radarr", enabled=1, port=7878, api_key="r"))
    schema_session.flush()
    schema_session.add(TableMovies(id=1, arr_instance_id=2, radarrId=1, tmdbId="1",
                                   title="Film", path="/m.mkv", audio_language="['English']"))
    schema_session.commit()

    root = Path(__file__).resolve().parents[2] / "bazarr/api"
    for name in ("_audio_catalogue_api", "_audio_catalogue_api.system"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, filename in [
        ("utils", "utils.py"),
        ("system.audio_languages", "system/audio_languages.py"),
    ]:
        spec = importlib.util.spec_from_file_location(
            "_audio_catalogue_api." + name, root / filename
        )
        endpoint = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, endpoint)
        spec.loader.exec_module(endpoint)
    monkeypatch.setattr(endpoint, "database", schema_session)

    from flask import Flask
    from flask_restx import Api

    app = Flask(__name__)
    Api(app).add_namespace(endpoint.api_ns_system_audio_languages, path="/")
    return app.test_client(), {"X-API-KEY": settings.auth.apikey}


def test_sports_audio_languages_feed_the_library_catalogue(audio_catalogue):
    """The catalogue is what the sports wanted page's audio filters read.

    Sportarr itself reports no audio metadata, so the sports events table is
    the only place a sports-only audio language exists; it must contribute to
    the same endpoint movies and episodes feed.
    """
    client, headers = audio_catalogue
    response = client.get("/system/languages/audio", headers=headers)
    assert response.status_code == 200
    body = response.get_json()
    codes = {item["code2"] for item in body}
    assert "eu" in codes, "Basque lives only on the sports event and must come back"
    assert "en" in codes, "the movie's English track must still be present"


def test_sports_rows_that_parse_to_nothing_are_skipped(audio_catalogue, schema_session):
    """Broken or empty sports audio columns must not abort the catalogue."""
    from app.database import TableSportsEvents

    schema_session.get(TableSportsEvents, 61).audio_language = "[]"
    schema_session.commit()
    client, headers = audio_catalogue
    response = client.get("/system/languages/audio", headers=headers)
    assert response.status_code == 200
    codes = {item["code2"] for item in response.get_json()}
    assert "eu" not in codes
    assert "en" in codes
