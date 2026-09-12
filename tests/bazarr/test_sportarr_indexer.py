"""Real media discovery and owned subtitle state on both supported engines."""

import ast
import json
import pickle
import shutil
import subprocess

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from test_sportarr_kind_migration import migration_engine  # noqa: F401


@pytest.fixture
def indexed_library(migration_engine, monkeypatch, tmp_path):  # noqa: F811
    from app import database as db
    from app.config import settings
    from languages import get_languages, custom_lang
    from sportarr import identity
    from sportarr.sync import events

    db.Base.metadata.create_all(migration_engine)
    session = sessionmaker(bind=migration_engine, expire_on_commit=False)()
    for module in (db, identity, events, get_languages, custom_lang):
        monkeypatch.setattr(module, "database", session)
    db.update_profile_id_list.invalidate()
    monkeypatch.setattr(events, "notify", lambda *a: None)
    monkeypatch.setattr(settings.general, "use_embedded_subs", True)
    monkeypatch.setattr(settings.general, "embedded_subtitles_parser", "ffprobe")
    monkeypatch.setattr(settings.general, "subfolder", "current")
    monkeypatch.setattr(settings.general, "single_language", False)
    # Populate the same language records used by the live lookup, in this private DB.
    from languages.get_languages import load_language_in_db

    if migration_engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        monkeypatch.setattr(get_languages, "insert", insert)
        monkeypatch.setattr(custom_lang, "insert", insert)
    monkeypatch.setattr(get_languages, "languages_dict", [], raising=False)
    load_language_in_db()
    session.execute(
        sa.update(db.TableSettingsLanguages)
        .where(
            db.TableSettingsLanguages.code2.in_(["fr", "en", "de", "pb", "zh", "zt"])
        )
        .values(enabled=1)
    )
    subtitle = tmp_path / "embedded.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nBienvenue dans cette compétition sportive.\n"
    )
    assert shutil.which("ffmpeg") and shutil.which("ffprobe"), (
        "Real media tests require ffmpeg and ffprobe"
    )
    source = tmp_path / "fixture.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=5",
            "-i",
            str(subtitle),
            "-t",
            "1",
            "-map",
            "0:v",
            "-map",
            "1:s",
            "-c:v",
            "ffv1",
            "-c:s",
            "srt",
            "-metadata:s:s:0",
            "language=fra",
            str(source),
        ],
        check=True,
    )
    for owner in (1, 2):
        folder = tmp_path / str(owner)
        folder.mkdir()
        shutil.copyfile(source, folder / "event.mkv")
        (
            folder / ("event.en.hi.srt" if owner == 1 else "event.de.forced.srt")
        ).write_text("1\n00:00:00,000 --> 00:00:01,000\nA sporting event subtitle.\n")
        session.execute(
            sa.insert(db.TableArrInstances).values(
                id=owner,
                kind="sportarr",
                name=str(owner),
                stable_key=str(owner),
                port=1867,
                path_mappings=json.dumps([["/sports", str(folder)]]),
            )
        )
        session.execute(
            sa.insert(db.TableSportsLeagues).values(
                id=50 + owner, arr_instance_id=owner, sportarrLeagueId=7, title="League"
            )
        )
        session.execute(
            sa.insert(db.TableSportsEvents).values(
                id=60 + owner,
                arr_instance_id=owner,
                league_id=50 + owner,
                sportarrEventId=8,
                file_id=9,
                path="/sports/event.mkv",
                title="Card",
                file_size=source.stat().st_size,
                audio_language="['French']",
                subtitles="[]",
                missing_subtitles="[]",
                failedAttempts="[]",
            )
        )
    yield session, tmp_path
    db.update_profile_id_list.invalidate()
    session.close()


def sports(monkeypatch, session):
    import subtitles.indexer as package

    assert hasattr(package, "__path__")
    import importlib.util

    assert importlib.util.find_spec("subtitles.indexer.sports") is not None, (
        "Sports indexer is required"
    )
    from subtitles.indexer import sports as module

    monkeypatch.setattr(module, "database", session)
    monkeypatch.setattr(module, "notify", lambda *a: None)
    return module


def row(session, event_id):
    from app.database import TableSportsEvents

    session.expire_all()
    return session.get(TableSportsEvents, event_id)


def test_real_embedded_external_and_colliding_owner_cache(indexed_library, monkeypatch):
    session, _ = indexed_library
    module = sports(monkeypatch, session)
    first = module.store_subtitles_sports(61, 1)
    assert {item[0] for item in first} == {"fr", "en:hi"}
    assert ast.literal_eval(row(session, 62).subtitles) == []
    second = module.store_subtitles_sports(62, 2)
    assert {item[0] for item in second} == {"fr", "de:forced"}
    assert pickle.loads(row(session, 61).ffprobe_cache) != pickle.loads(
        row(session, 62).ffprobe_cache
    )
    assert ast.literal_eval(row(session, 61).missing_subtitles) == []
    assert next(item[1] for item in first if item[1]) == "/sports/event.en.hi.srt"
    for local, owner in ((61, 2), (8, 1), (9, 1)):
        with pytest.raises(ValueError):
            module.store_subtitles_sports(local, owner)


def test_embedded_tracks_write_one_history_row_per_language(indexed_library, monkeypatch):
    """The series and movies indexers record one action=7 (EmbeddedSource) row
    per detected track language. The sports indexer detected the tracks but
    wrote no history at all, so a fully subtitled event looked unrecorded and
    the page had nothing to filter."""
    from app.database import TableHistorySports

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    module.store_subtitles_sports(61, 1)
    rows = session.execute(
        sa.select(TableHistorySports).where(TableHistorySports.action == 7)
    ).scalars().all()
    assert [row.language for row in rows] == ["fr"]
    embedded = rows[0]
    assert embedded.provider == "embedded"
    assert embedded.event_id == 61
    assert embedded.subtitles_path is None
    assert embedded.video_path == "/sports/event.mkv"
    assert embedded.score == embedded.score_out_of
    assert "embedded subtitles detected" in embedded.description
    # Re-indexing the same event already has the row: the dedup must not grow
    # the table, exactly like the series and movies indexers' dedup.
    module.store_subtitles_sports(61, 1)
    rows = session.execute(
        sa.select(TableHistorySports).where(TableHistorySports.action == 7)
    ).scalars().all()
    assert len(rows) == 1


def test_profiles_recompute_missing_and_explicit_none(indexed_library, monkeypatch):
    from app import database as db
    from sportarr import library

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    items = [
        dict(
            id=i,
            language=language,
            hi="False",
            forced="False",
            audio_exclude="False",
            audio_only_include="False",
        )
        for i, language in enumerate(("fr", "en", "de"), 1)
    ]
    session.execute(
        sa.insert(db.TableLanguagesProfiles).values(
            profileId=5, name="Sports", items=json.dumps(items)
        )
    )
    db.update_profile_id_list.invalidate()
    assert library.assign_profile(session, 51, 1, 5)
    module.store_subtitles_sports(61, 1)
    assert ast.literal_eval(row(session, 61).missing_subtitles) == ["de"]
    assert library.assign_profile(session, 51, 1, None)
    library.refresh_league_profiles([51], 1)
    assert ast.literal_eval(row(session, 61).missing_subtitles) == []


def test_failed_index_is_retried_on_unchanged_metadata(indexed_library, monkeypatch):
    from sportarr.sync import events
    from test_sportarr_events import event, file, remote

    session, path = indexed_library
    module = sports(monkeypatch, session)
    remote(
        monkeypatch,
        events,
        [
            event(
                [
                    file(
                        filePath="/sports/event.mkv",
                        size=(path / "1/event.mkv").stat().st_size,
                    )
                ]
            )
        ],
    )
    discover = module.search_external_subtitles
    monkeypatch.setattr(
        module,
        "search_external_subtitles",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("offline")),
    )
    assert events.sync_events(51, 1) == [61]
    assert row(session, 61).ffprobe_cache is None
    monkeypatch.setattr(module, "search_external_subtitles", discover)
    assert events.sync_events(51, 1) == [61]
    assert {x[0] for x in ast.literal_eval(row(session, 61).subtitles)} == {
        "fr",
        "en:hi",
    }


@pytest.mark.parametrize(
    "change", ["mapping", "replacement", "disable", "delete", "file_id"]
)
def test_slow_probe_revalidates_current_owned_file(
    indexed_library, monkeypatch, change
):
    from app.database import TableArrInstances, TableSportsEvents

    session, path = indexed_library
    module = sports(monkeypatch, session)
    probe = module.parse_video_metadata

    def changed_probe(*args, **kwargs):
        data = probe(*args, **kwargs)
        if change == "mapping":
            session.execute(
                sa.update(TableArrInstances)
                .where(TableArrInstances.id == 1)
                .values(path_mappings=json.dumps([["/sports", str(path / "2")]]))
            )
        elif change == "replacement":
            target = path / "1/event.mkv"
            target.write_bytes(target.read_bytes())
        elif change == "disable":
            session.execute(
                sa.update(TableArrInstances)
                .where(TableArrInstances.id == 1)
                .values(enabled=0)
            )
        elif change == "delete":
            session.execute(
                sa.delete(TableArrInstances).where(TableArrInstances.id == 1)
            )
        else:
            session.execute(
                sa.update(TableSportsEvents)
                .where(TableSportsEvents.id == 61)
                .values(file_id=99)
            )
        return data

    monkeypatch.setattr(module, "parse_video_metadata", changed_probe)
    with pytest.raises(ValueError):
        module.store_subtitles_sports(61, 1)
    first = row(session, 61)
    if first:
        assert first.ffprobe_cache is None and first.subtitles == "[]"
    assert row(session, 62).ffprobe_cache is None


def test_mapping_edit_reindexes_unchanged_metadata(indexed_library, monkeypatch):
    from app.database import TableArrInstances
    from sportarr.sync import events
    from test_sportarr_events import event, file, remote

    session, path = indexed_library
    sports(monkeypatch, session)
    remote(
        monkeypatch,
        events,
        [
            event(
                [
                    file(
                        filePath="/sports/event.mkv",
                        size=(path / "1/event.mkv").stat().st_size,
                    )
                ]
            )
        ],
    )
    assert events.sync_events(51, 1) == [61]
    assert "en:hi" in {x[0] for x in ast.literal_eval(row(session, 61).subtitles)}
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(path_mappings=json.dumps([["/sports", str(path / "2")]]))
    )
    assert events.sync_events(51, 1) == [61]
    assert {x[0] for x in ast.literal_eval(row(session, 61).subtitles)} == {
        "fr",
        "de:forced",
    }
    assert row(session, 62).ffprobe_cache is None


def test_explicit_sports_probe_routing_and_cache(indexed_library, monkeypatch):
    from utilities import video_analyzer

    session, path = indexed_library
    module = sports(monkeypatch, session)
    expected = module.store_subtitles_sports(61, 1)
    first = row(session, 61).ffprobe_cache
    assert video_analyzer.parse_video_metadata(
        str(path / "1/event.mkv"), 1, sports_event_id=61, arr_instance_id=1
    )["ffprobe"]
    assert row(session, 61).ffprobe_cache == first
    assert ast.literal_eval(row(session, 61).subtitles) == expected
    with pytest.raises(ValueError):
        video_analyzer.parse_video_metadata(
            str(path / "2/event.mkv"), 1, sports_event_id=61, arr_instance_id=1
        )
    with pytest.raises(ValueError):
        video_analyzer.parse_video_metadata(
            str(path / "1/event.mkv"),
            1,
            movie_file_id=9,
            sports_event_id=61,
            arr_instance_id=1,
        )


@pytest.mark.parametrize("legacy_kind", ["movie", "episode"])
def test_sports_and_existing_media_share_output_ownership(
    indexed_library, monkeypatch, legacy_kind
):
    from app.database import TableArrInstances, TableMovies, TableShows, TableEpisodes
    from app.config import settings
    from subtitles.tools.subsync_engines import (
        SyncOutputOwnerIndex,
        sync_output_owner_is_unique,
    )

    session, path = indexed_library
    shared = path / "subtitles"
    shared.mkdir()
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(shared))
    legacy_folder = path / "legacy"
    legacy_folder.mkdir()
    video = legacy_folder / "event.mkv"
    shutil.copyfile(path / "1/event.mkv", video)
    kind = "radarr" if legacy_kind == "movie" else "sonarr"
    session.execute(
        sa.insert(TableArrInstances).values(
            id=3, kind=kind, name="Legacy", stable_key="legacy", port=1
        )
    )
    if legacy_kind == "movie":
        session.execute(
            sa.insert(TableMovies).values(
                arr_instance_id=3,
                radarrId=1,
                tmdbId="1",
                path=str(video),
                title="Event",
            )
        )
    else:
        session.execute(
            sa.insert(TableShows).values(
                id=1,
                arr_instance_id=3,
                sonarrSeriesId=1,
                title="Show",
                path=str(legacy_folder),
            )
        )
        session.execute(
            sa.insert(TableEpisodes).values(
                arr_instance_id=3,
                series_id=1,
                sonarrSeriesId=1,
                sonarrEpisodeId=1,
                path=str(video),
                title="Event",
                season=1,
                episode=1,
            )
        )
    assert (
        sync_output_owner_is_unique(
            str(video),
            str(shared / "event.en.srt"),
            ownership_index=SyncOutputOwnerIndex(),
        )
        is False
    )


def test_owner_specific_scan_schedules_and_removal(indexed_library, monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.database import TableArrInstances
    from sportarr.scheduler import configure_sports_jobs
    from app.get_args import args

    session, _ = indexed_library
    monkeypatch.setattr(args, "no_signalr", True)
    # configure_sports_jobs is gated on the master toggle, so a job test
    # has to turn it on the way a configured install does.
    from app.config import settings
    monkeypatch.setattr(settings.general, "use_sportarr", True)
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(
            options=json.dumps(
                {
                    "sports_settings": {
                        "full_update": "Weekly",
                        "full_update_day": 2,
                        "full_update_hour": 8,
                    }
                }
            )
        )
    )
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 2)
        .values(options=json.dumps({"sports_settings": {"full_update": "Manually"}}))
    )
    scheduler = BackgroundScheduler()
    configure_sports_jobs(scheduler, session)
    first = scheduler.get_job("sports_full_scan_subtitles_1")
    second = scheduler.get_job("sports_full_scan_subtitles_2")
    assert first is not None and second is not None
    assert "day_of_week='2'" in str(first.trigger) and "hour='8'" in str(first.trigger)
    assert first.kwargs == {"arr_instance_id": 1, "wait_for_completion": True}
    assert "year='21" in str(second.trigger)
    session.execute(
        sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(enabled=0)
    )
    session.execute(sa.delete(TableArrInstances).where(TableArrInstances.id == 2))
    configure_sports_jobs(scheduler, session)
    assert scheduler.get_jobs() == []


def test_authenticated_reindex_updates_exact_local_event(indexed_library, monkeypatch):
    import importlib.util
    from pathlib import Path
    import sys
    from types import ModuleType
    from flask import Flask
    from flask_restx import Api
    from app.config import settings

    session, _ = indexed_library
    sports(monkeypatch, session)
    root = Path(__file__).resolve().parents[2] / "bazarr/api"
    for name in ("_sports_index_api", "_sports_index_api.sports"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, path in [
        ("utils", "utils.py"),
        ("sports.leagues", "sports/leagues.py"),
        ("sports.events", "sports/events.py"),
    ]:
        spec = importlib.util.spec_from_file_location(
            "_sports_index_api." + name, root / path
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    monkeypatch.setattr(module, "database", session)
    app = Flask(__name__)
    Api(app).add_namespace(module.api_ns_sports_events, path="/")
    client = app.test_client()
    headers = {"X-API-KEY": settings.auth.apikey}
    url = "/sports/events/61/subtitles"
    assert client.post(url, json={"arr_instance_id": 1}).status_code == 401
    # An event another owner holds, and an event that does not exist, are both
    # NOT FOUND now, matching the episodes and movies equivalents. They were
    # reported as malformed requests because every sports resolution helper
    # raised a bare ValueError and every handler mapped that onto 400.
    assert (
        client.post(url, json={"arr_instance_id": 2}, headers=headers).status_code
        == 404
    )
    assert (
        client.post(
            "/sports/events/8/subtitles", json={"arr_instance_id": 1}, headers=headers
        ).status_code
        == 404
    )
    response = client.post(url, json={"arr_instance_id": 1}, headers=headers)
    assert response.status_code == 200
    assert {item[0] for item in response.json["subtitles"]} == {"fr", "en:hi"}
    assert response.json["id"] == 61 and response.json["arr_instance_id"] == 1
    detail = client.get("/sports/events/61?arr_instance_id=1", headers=headers)
    assert detail.json["subtitles"] == response.json["subtitles"]
    listing = client.get("/sports/leagues/51/events?arr_instance_id=1", headers=headers)
    assert listing.json["data"][0]["subtitles"] == response.json["subtitles"]
    assert row(session, 62).ffprobe_cache is None


def test_full_scan_executes_only_selected_enabled_owner(indexed_library, monkeypatch):
    from app.jobs_queue import jobs_queue
    from app.database import TableArrInstances

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    monkeypatch.setattr(jobs_queue, "update_job_progress", lambda **kw: None)
    monkeypatch.setattr(jobs_queue, "update_job_name", lambda **kw: None)
    module.sports_full_scan_subtitles(job_id=7, arr_instance_id=1)
    assert row(session, 61).ffprobe_cache is not None
    assert row(session, 62).ffprobe_cache is None
    session.execute(
        sa.update(TableArrInstances).where(TableArrInstances.id == 2).values(enabled=0)
    )
    with pytest.raises(ValueError):
        module.sports_full_scan_subtitles(job_id=7, arr_instance_id=2)


def test_scan_queue_preserves_owner_and_valid_signature(indexed_library, monkeypatch):
    from app.jobs_queue import jobs_queue

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    queued = []
    monkeypatch.setattr(
        jobs_queue, "feed_jobs_pending_queue", lambda **kw: queued.append(kw)
    )
    # Exercise the actual stack/signature capture, without starting its worker.
    monkeypatch.setattr(
        jobs_queue, "wait_for_job", lambda *a, **kw: None, raising=False
    )
    module.sports_full_scan_subtitles(arr_instance_id=2)
    assert len(queued) == 1 and queued[0]["kwargs"]["arr_instance_id"] == 2


@pytest.mark.parametrize("destination", ["relative", "absolute"])
def test_destination_discovery_and_language_variants(
    indexed_library, monkeypatch, destination
):
    from app.config import settings

    session, path = indexed_library
    module = sports(monkeypatch, session)
    folder = path / "1/subs" if destination == "relative" else path / "subtitles"
    folder.mkdir()
    monkeypatch.setattr(settings.general, "subfolder", destination)
    monkeypatch.setattr(
        settings.general,
        "subfolder_custom",
        "subs" if destination == "relative" else str(folder),
    )
    for name in ("event.pt-BR.srt", "event.zh-Hant.srt", "event.en.combined-de.srt"):
        (folder / name).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nThese are subtitles for a sporting event.\n"
        )
    # The untagged sidecar remains beside the video, even with a custom destination.
    (path / "1/event.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        "Dies ist eine deutsche Untertiteldatei für eine Sportveranstaltung. "
        "Die Spieler gehen auf das Spielfeld und beginnen mit dem Spiel.\n"
    )
    actual = module.store_subtitles_sports(61, 1)
    assert {x[0] for x in actual} == {"fr", "en:hi", "pb", "zt", "en:combined-de", "de"}
    assert next(x[1] for x in actual if x[0] == "de") == "/sports/event.srt"


def test_missing_file_after_mapping_change_clears_old_subtitle_state(
    indexed_library, monkeypatch
):
    from app.database import TableArrInstances

    session, path = indexed_library
    module = sports(monkeypatch, session)
    module.store_subtitles_sports(61, 1)
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(path_mappings=json.dumps([["/sports", str(path / "offline")]]))
    )
    module.refresh_sports_files([61], 1)
    assert row(session, 61).subtitles == "[]" and row(session, 61).ffprobe_cache is None


def test_disabled_sports_files_still_prevent_ambiguous_publication(
    indexed_library, monkeypatch
):
    from app.database import TableArrInstances, TableMovies
    from app.config import settings
    from subtitles.tools.subsync_engines import sync_output_owner_is_unique

    session, path = indexed_library
    shared = path / "subtitles"
    shared.mkdir()
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(shared))
    session.execute(sa.update(TableArrInstances).values(enabled=0))
    session.execute(
        sa.insert(TableArrInstances).values(
            id=3, kind="radarr", name="Movies", stable_key="movies", port=1
        )
    )
    video = path / "event.mkv"
    shutil.copyfile(path / "1/event.mkv", video)
    session.execute(
        sa.insert(TableMovies).values(
            arr_instance_id=3, radarrId=1, tmdbId="1", title="Event", path=str(video)
        )
    )
    assert (
        sync_output_owner_is_unique(str(video), str(shared / "event.en.srt")) is False
    )


def test_profile_editor_changes_recompute_sports_missing(indexed_library, monkeypatch):
    import importlib.util
    from pathlib import Path
    import sys
    from types import ModuleType
    from flask import Flask
    from flask_restx import Api
    from app import database as db
    from app.config import settings

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    item = dict(
        id=1,
        language="de",
        hi="False",
        forced="False",
        audio_exclude="False",
        audio_only_include="False",
    )
    session.execute(
        sa.insert(db.TableLanguagesProfiles).values(
            profileId=5, name="Sports", items=json.dumps([item])
        )
    )
    session.execute(
        sa.update(db.TableSportsLeagues)
        .where(db.TableSportsLeagues.id == 51)
        .values(profileId=5)
    )
    db.update_profile_id_list.invalidate()
    module.store_subtitles_sports(61, 1)
    assert row(session, 61).missing_subtitles == "['de']"
    root = Path(__file__).resolve().parents[2] / "bazarr/api"
    for name in ("_sports_profile_api", "_sports_profile_api.system"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, filename in [
        ("utils", "utils.py"),
        ("system.settings", "system/settings.py"),
    ]:
        spec = importlib.util.spec_from_file_location(
            "_sports_profile_api." + name, root / filename
        )
        endpoint = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, endpoint)
        if name == "system.settings":
            # This endpoint's absolute utils import otherwise starts the full API
            # package and its startup jobs. Only the real HTTP boundary is needed.
            package = ModuleType("api")
            package.__path__ = [str(root)]
            monkeypatch.setitem(sys.modules, "api", package)
            monkeypatch.setitem(
                sys.modules, "api.utils", sys.modules["_sports_profile_api.utils"]
            )
            scheduler = ModuleType("app.scheduler")
            scheduler.scheduler = None
            monkeypatch.setitem(sys.modules, "app.scheduler", scheduler)
        spec.loader.exec_module(endpoint)
    monkeypatch.setattr(endpoint, "database", session)
    monkeypatch.setattr(endpoint, "event_stream", lambda *a, **kw: None)
    monkeypatch.setattr(endpoint, "save_settings", lambda *a: None)
    monkeypatch.setattr(endpoint, "forget_deleted_language_profiles", lambda *a: None)
    monkeypatch.setattr(settings.general, "use_sonarr", False)
    monkeypatch.setattr(settings.general, "use_radarr", False)
    # The sports recompute is gated on the master toggle now, like the sonarr
    # and radarr ones. This fixture has an enabled Sportarr instance, which in
    # a real install means the toggle is on.
    monkeypatch.setattr(settings.general, "use_sportarr", True)
    app = Flask(__name__)
    Api(app).add_namespace(endpoint.api_ns_system_settings, path="/")
    response = app.test_client().post(
        "/system/settings",
        headers={"X-API-KEY": settings.auth.apikey},
        data={
            "languages-profiles": json.dumps(
                [
                    dict(
                        profileId=5,
                        name="French",
                        items=[item | {"language": "fr"}],
                        cutoff=None,
                        mustContain=[],
                        mustNotContain=[],
                        originalFormat=False,
                    )
                ]
            )
        },
    )
    assert response.status_code == 204
    assert row(session, 61).missing_subtitles == "[]"


@pytest.mark.parametrize("operation", ["index", "probe", "missing"])
def test_checked_subtitle_writes_lock_owner_until_commit(
    indexed_library, monkeypatch, operation
):
    import threading
    from app.database import TableArrInstances

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    engine = session.get_bind()
    attempted, committed = threading.Event(), threading.Event()
    results, errors, workers = [], [], []
    original = module.require_sportarr
    checked = 0

    def compete():
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql(
                    "PRAGMA busy_timeout=0"
                    if engine.dialect.name == "sqlite"
                    else "SET lock_timeout='100ms'"
                )
                statement = (
                    sa.update(TableArrInstances)
                    .where(TableArrInstances.id == 1)
                    .values(enabled=0)
                )
                try:
                    connection.execute(statement)
                    results.append("committed-before-subtitles")
                except sa.exc.OperationalError as error:
                    assert (
                        error.orig.sqlite_errorcode == 5
                        if engine.dialect.name == "sqlite"
                        else error.orig.sqlstate == "55P03"
                    )
                    connection.rollback()
                    results.append("blocked")
                attempted.set()
                assert committed.wait(5)
                connection.execute(statement)
                results.append("committed-after-subtitles")
        except BaseException as error:
            errors.append(error)
            attempted.set()

    def checked_owner(transaction, owner):
        nonlocal checked
        result = original(transaction, owner)
        if transaction is not session:
            checked += 1
            if checked == (2 if operation == "index" else 1):
                if engine.dialect.name == "sqlite":
                    assert transaction.connection().connection.driver_connection.in_transaction
                worker = threading.Thread(target=compete)
                workers.append(worker)
                worker.start()
                assert attempted.wait(5)
        return result

    monkeypatch.setattr(module, "require_sportarr", checked_owner)
    try:
        if operation == "index":
            module.store_subtitles_sports(61, 1)
        elif operation == "probe":
            module.parse_sports_video_metadata(61, 1)
        else:
            module.list_missing_subtitles_sports(event_id=61, arr_instance_id=1)
    finally:
        committed.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
    assert not errors
    assert results == ["blocked", "committed-after-subtitles"]
    if operation in ("index", "probe"):
        assert row(session, 61).ffprobe_cache is not None
    assert row(session, 62).ffprobe_cache is None


@pytest.mark.parametrize(
    "cutoff,audio_exclude,audio_only,expected",
    [
        (None, "False", "False", ["de", "en:forced"]),
        (1, "False", "False", []),
        (2, "False", "True", ["en:forced"]),
        (2, "True", "False", ["de", "en:forced"]),
    ],
)
def test_profiles_cutoffs_variants_and_audio_rules(
    indexed_library, monkeypatch, cutoff, audio_exclude, audio_only, expected
):
    from app import database as db

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    items = [
        dict(
            id=1,
            language="fr",
            hi="False",
            forced="False",
            audio_exclude="False",
            audio_only_include="False",
        ),
        dict(
            id=2,
            language="de",
            hi="False",
            forced="False",
            audio_exclude=audio_exclude,
            audio_only_include=audio_only,
        ),
        dict(
            id=3,
            language="en",
            hi="False",
            forced="True",
            audio_exclude="False",
            audio_only_include="False",
        ),
    ]
    session.execute(
        sa.insert(db.TableLanguagesProfiles).values(
            profileId=5, name="Profile", items=json.dumps(items), cutoff=cutoff
        )
    )
    session.execute(
        sa.update(db.TableSportsLeagues)
        .where(db.TableSportsLeagues.id == 51)
        .values(profileId=5)
    )
    db.update_profile_id_list.invalidate()
    module.store_subtitles_sports(61, 1)
    assert ast.literal_eval(row(session, 61).missing_subtitles) == expected
    with pytest.raises(ValueError):
        module.list_missing_subtitles_sports(event_id=61, arr_instance_id=2)
    with pytest.raises(ValueError):
        module.list_missing_subtitles_sports(league_id=51, arr_instance_id=2)


def test_global_embedded_setting_refreshes_sports_missing(indexed_library, monkeypatch):
    import sys
    from types import SimpleNamespace
    from app import config, database as db

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    item = dict(
        id=1,
        language="fr",
        hi="False",
        forced="False",
        audio_exclude="False",
        audio_only_include="False",
    )
    session.execute(
        sa.insert(db.TableLanguagesProfiles).values(
            profileId=5, name="French", items=json.dumps([item])
        )
    )
    session.execute(
        sa.update(db.TableSportsLeagues)
        .where(db.TableSportsLeagues.id == 51)
        .values(profileId=5)
    )
    db.update_profile_id_list.invalidate()
    module.store_subtitles_sports(61, 1)
    assert row(session, 61).missing_subtitles == "[]"
    # A write that reached disk: the refactored save refuses a falsy return.
    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setattr(config.settings.general, "use_sonarr", False)
    monkeypatch.setattr(config.settings.general, "use_radarr", False)
    monkeypatch.setitem(sys.modules, "app.scheduler", SimpleNamespace(scheduler=None))
    from app.jobs_queue import jobs_queue

    queued = []
    monkeypatch.setattr(
        jobs_queue, "feed_jobs_pending_queue", lambda **kw: queued.append(kw)
    )
    config.save_settings([("settings-general-use_embedded_subs", ["false"])])
    assert queued[0]["func"] == "sports_full_scan_subtitles"
    assert row(session, 61).missing_subtitles == "['fr']"


def test_shared_embedded_readers_accept_exact_sports_identity(
    indexed_library, monkeypatch
):
    from utilities.video_analyzer import embedded_subs_reader, embedded_audio_reader

    session, path = indexed_library
    sports(monkeypatch, session)
    tracks = embedded_subs_reader(
        str(path / "1/event.mkv"), 1, sports_event_id=61, arr_instance_id=1
    )
    assert tracks[0][0:3] == ["fra", False, False]
    assert (
        embedded_audio_reader(
            str(path / "1/event.mkv"), 1, sports_event_id=61, arr_instance_id=1
        )
        == []
    )
    assert (
        row(session, 61).ffprobe_cache is not None
        and row(session, 62).ffprobe_cache is None
    )


def test_deleted_owner_between_identity_reads_is_rejected(indexed_library, monkeypatch):
    from app.database import TableArrInstances

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    resolve = module.resolve_event_in_session

    def deleted_owner(*args, **kwargs):
        context = resolve(*args, **kwargs)
        session.execute(sa.delete(TableArrInstances).where(TableArrInstances.id == 1))
        return context

    monkeypatch.setattr(module, "resolve_event_in_session", deleted_owner)
    with pytest.raises(ValueError):
        module.store_subtitles_sports(61, 1)
    assert row(session, 62).ffprobe_cache is None


def test_explicit_no_profile_ignores_instance_and_legacy_defaults(
    indexed_library, monkeypatch
):
    from app import database as db
    from app.config import settings

    session, _ = indexed_library
    module = sports(monkeypatch, session)
    item = dict(
        id=1,
        language="de",
        hi="False",
        forced="False",
        audio_exclude="False",
        audio_only_include="False",
    )
    session.execute(
        sa.insert(db.TableLanguagesProfiles).values(
            profileId=5, name="German", items=json.dumps([item])
        )
    )
    session.execute(
        sa.update(db.TableArrInstances)
        .where(db.TableArrInstances.id == 1)
        .values(
            options=json.dumps(
                {"media_defaults": {"default_enabled": True, "default_profile": 5}}
            )
        )
    )
    db.update_profile_id_list.invalidate()
    for noun in ("serie", "movie"):
        monkeypatch.setattr(settings.general, f"{noun}_default_enabled", True)
        monkeypatch.setattr(settings.general, f"{noun}_default_profile", 5)
    module.store_subtitles_sports(61, 1)
    assert row(session, 61).missing_subtitles == "[]"
