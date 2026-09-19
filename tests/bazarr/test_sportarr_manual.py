"""Owned manual provider execution and real subtitle publication."""

# ruff: noqa: F811
import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from test_sportarr_kind_migration import migration_engine  # noqa: F401
from test_sportarr_indexer import indexed_library, sports  # noqa: F401
from test_provider_hub import _manifest


def test_sports_manual_service_exists():
    assert importlib.util.find_spec("sportarr.subtitles"), (
        "Sports manual service is required"
    )


@pytest.fixture
def manual_library(indexed_library, monkeypatch, tmp_path):
    session, folder = indexed_library
    assert importlib.util.find_spec("sportarr.subtitles"), (
        "Sports manual service is required"
    )
    from app.config import settings
    from arr_instances.resolution import clear_subtitle_settings_cache
    from sportarr import notify, profile_hooks, subtitles as service
    from subtitles import pool
    from subliminal import Movie, Episode
    from subzero.language import Language
    from subliminal_patch.core import SZProviderPool
    from subliminal_patch.extensions import provider_registry
    from provider_hub.registry import HubProxyProvider

    sports(monkeypatch, session)
    monkeypatch.setattr(service, "database", session)
    monkeypatch.setattr(profile_hooks, "database", session)
    # This provider fixture publishes real local files, but has no Sportarr
    # HTTP service. Notification tests install their own transport recorder.
    monkeypatch.setattr(notify, "_rescan_request", lambda owner, **kwargs: None)
    monkeypatch.setattr(settings.general, "use_embedded_subs", False)
    monkeypatch.setattr(settings.general, "use_postprocessing", False)
    monkeypatch.setattr(settings.subsync, "use_subsync", False)
    monkeypatch.setattr(settings.general, "subzero_mods", "")
    monkeypatch.setattr(settings.general, "skip_hashing", False)
    clear_subtitle_settings_cache()
    import subliminal
    from dogpile.cache import make_region
    from dogpile.cache.region import register_backend

    register_backend(
        "subzero.cache.file", "subzero.cache_backends.file", "SZFileBackend"
    )
    region = make_region().configure(
        "subzero.cache.file",
        arguments={"appname": "manual-test", "app_cache_dir": str(folder)},
    )
    monkeypatch.setattr(subliminal, "region", region)
    bundle = tmp_path / "provider"
    bundle.mkdir()
    source = """import json
from pathlib import Path
class ExampleProvider:
    def search(self, video, languages, config):
        Path(config['receipt']).write_text(json.dumps(video))
        return [{'id': 'same-release', 'language': language, 'release_info': str(video['title']),
                 'matches': ['title', 'year'] if video['kind'] == 'movie' else ['series', 'season', 'episode'],
                 'provider_payload': {'path': video['original_path']}} for language in languages]
    def download(self, provider_payload, language, config):
        return {'content': '1\\n00:00:00,000 --> 00:00:01,000\\n<i>Sporting event</i>\\n\\n2\\n00:00:01,100 --> 00:00:02,000\\nSecond cue\\n'}
"""
    # Worker contract returns text or content_b64, so retain ordinary text output.
    source = source.replace("return {'content': ", "return ").replace(
        "Second cue\\n'}", "Second cue\\n'"
    )
    (bundle / "provider.py").write_text(source)
    manifest = _manifest(
        provider_content=source.encode(),
        dependencies={"requirements": []},
        secret_fields=[],
        config_schema={"type": "object", "properties": {"receipt": {"type": "string"}}},
    )

    class FixtureProvider(HubProxyProvider):
        provider_name = "examplehub"
        languages = {Language("eng")}
        video_types = (Movie, Episode)
        bundle_path = str(bundle)
        python_path = sys.executable
        manifest_json = json.dumps(manifest)

    monkeypatch.setitem(provider_registry.providers, "examplehub", FixtureProvider)
    monkeypatch.setattr(pool, "_pools", {})
    monkeypatch.setattr(pool, "provider_pool", lambda: SZProviderPool)
    monkeypatch.setattr(pool, "get_providers_sorted", lambda: ["examplehub"])
    monkeypatch.setattr(
        pool,
        "get_providers_auth",
        lambda: {"examplehub": {"receipt": str(folder / "video.json")}},
    )
    monkeypatch.setattr(pool, "provider_is_usable", lambda *a, **k: True)
    monkeypatch.setattr(service, "get_providers", lambda: ["examplehub"])
    yield service, session, folder
    for value in pool._pools.values():
        value.terminate()
    clear_subtitle_settings_cache()


def test_provider_movie_contract_and_real_owned_save(manual_library):
    from app.database import TableSportsEvents, TableHistorySports

    service, session, folder = manual_library
    results = service.manual_search_sports(61, "en", arr_instance_id=1)
    assert len(results) == 1
    assert results[0]["orig_score"] > 0
    payload = json.loads((folder / "video.json").read_text())
    assert payload["kind"] == "movie"
    assert payload["original_path"] == str(folder / "1/event.mkv")
    assert payload["size"] == (folder / "1/event.mkv").stat().st_size
    assert (
        payload["imdb_id"] is None
        and payload["season"] is None
        and payload["episode"] is None
    )
    saved = service.manual_download_sports(61, results[0], arr_instance_id=1)
    assert saved.result.provider == "examplehub"
    assert (folder / "1/event.en.srt").is_file()
    assert not (folder / "2/event.en.srt").exists()
    session.expire_all()
    assert any(
        item[1] == "/sports/event.en.srt"
        for item in ast.literal_eval(session.get(TableSportsEvents, 61).subtitles)
    )
    assert session.get(TableSportsEvents, 62).subtitles == "[]"
    history = session.execute(sa.select(TableHistorySports)).scalar_one()
    assert (history.event_id, history.arr_instance_id, history.score_out_of) == (
        61,
        1,
        180,
    )


@pytest.mark.parametrize("target,owner", [(62, 2), (61, 2), (8, 1)])
def test_candidate_cannot_be_replayed_for_other_event_or_owner(
    manual_library, target, owner
):
    service, _, folder = manual_library
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    with pytest.raises(ValueError):
        service.manual_download_sports(target, candidate, arr_instance_id=owner)
    assert not list(folder.glob("*/event.en.srt"))


@pytest.mark.parametrize("change", ["file", "mapping", "disabled", "file_id"])
def test_stale_candidate_rejected_before_save(manual_library, change):
    from app.database import TableArrInstances, TableSportsEvents

    service, session, folder = manual_library
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    if change == "file":
        with (folder / "1/event.mkv").open("ab") as stream:
            stream.write(b"changed")
    elif change == "mapping":
        session.execute(
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 1)
            .values(path_mappings=json.dumps([["/sports", str(folder / "2")]]))
        )
    elif change == "disabled":
        session.execute(
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 1)
            .values(enabled=0)
        )
    else:
        session.execute(
            sa.update(TableSportsEvents)
            .where(TableSportsEvents.id == 61)
            .values(file_id=10)
        )
    with pytest.raises(ValueError):
        service.manual_download_sports(61, candidate, arr_instance_id=1)
    assert not list(folder.glob("*/event.en.srt"))


def test_blacklist_refresh_is_scoped_to_owner(manual_library):
    from app.database import TableBlacklistSports

    service, session, _ = manual_library
    assert service.manual_search_sports(61, "en", arr_instance_id=1)
    session.execute(
        sa.insert(TableBlacklistSports).values(
            event_id=61,
            league_id=51,
            arr_instance_id=1,
            provider="examplehub",
            subs_id="examplehub:same-release",
        )
    )
    assert service.manual_search_sports(61, "en", arr_instance_id=1) == []
    assert service.manual_search_sports(62, "en", arr_instance_id=2)


@pytest.mark.parametrize("index_failure", [False, True, "owner"])
def test_manual_api_requires_owner_and_returns_refreshed_event(
    manual_library, monkeypatch, index_failure
):
    from flask import Flask
    from flask_restx import Api
    from types import ModuleType

    root = Path(__file__).resolve().parents[2] / "bazarr/api"
    for name in ("_sports_manual_api", "_sports_manual_api.sports"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, path in [
        ("utils", "utils.py"),
        ("sports.leagues", "sports/leagues.py"),
        ("sports.subtitles", "sports/subtitles.py"),
    ]:
        spec = importlib.util.spec_from_file_location(
            "_sports_manual_api." + name, root / path
        )
        routes = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, routes)
        spec.loader.exec_module(routes)
    service, session, _ = manual_library
    monkeypatch.setattr(routes, "database", session)
    app = Flask(__name__)
    Api(app).add_namespace(routes.api_ns_sports_subtitles, path="/api")
    from app.config import settings

    client = app.test_client()
    headers = {"X-API-KEY": settings.auth.apikey}
    assert (
        client.post(
            "/api/sports/events/61/search",
            json={"language": "en", "arr_instance_id": 1},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/sports/events/61/search", json={"language": "en"}, headers=headers
        ).status_code
        == 400
    )
    response = client.post(
        "/api/sports/events/61/search",
        json={"language": "en", "arr_instance_id": 1},
        headers=headers,
    )
    assert response.status_code == 200 and response.json["data"]
    if index_failure:
        from subtitles.indexer import sports as indexer

        def failed_refresh(*args, **kwargs):
            if index_failure == "owner":
                from app.database import TableArrInstances

                session.execute(
                    sa.update(TableArrInstances)
                    .where(TableArrInstances.id == 1)
                    .values(enabled=0)
                )
            raise OSError("injected index failure")

        monkeypatch.setattr(indexer, "store_subtitles_sports", failed_refresh)
    response = client.post(
        "/api/sports/events/61/download",
        json={"candidate": response.json["data"][0], "arr_instance_id": 1},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json["publication"]["published"] is True
    assert response.json["publication"]["history"] == "committed"
    assert response.json["publication"]["index"] == (
        "owner_changed"
        if index_failure == "owner"
        else "failed"
        if index_failure
        else "completed"
    )
    event = response.json["event"]
    if index_failure == "owner":
        assert event is None
        return
    assert event["id"] == 61 and event["arr_instance_id"] == 1
    if not index_failure:
        assert ["en", "/sports/event.en.srt"] in [
            item[:2] for item in event["subtitles"]
        ]


def test_owner_mods_postprocessing_and_no_native_notifications(
    manual_library, monkeypatch
):
    import shlex
    from app.database import TableArrInstances
    from subtitles import processing

    service, session, folder = manual_library
    script = folder / "postprocess.py"
    script.write_text(
        'import sys\nfrom pathlib import Path\np=Path(sys.argv[1])\ns=p.read_text()\nassert "<i>" not in s\nassert sys.argv[2:] == ["", ""]\np.write_text(s.replace("Sporting event", "Owner A processed"))\n'
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {{{{subtitles}}}} {{{{series_id}}}} {{{{episode_id}}}}"
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(
            options=json.dumps(
                {
                    "subtitle_settings": {
                        "general": {
                            "subzero_mods": ["remove_tags"],
                            "use_postprocessing": True,
                            "postprocessing_cmd": command,
                            "use_postprocessing_threshold_movie": False,
                        }
                    }
                }
            )
        )
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Sports reached a native-only processing hook")

    for name in ("notify_radarr", "notify_sonarr"):
        monkeypatch.setattr(processing, name, forbidden)
    for event, owner in ((61, 1), (62, 2)):
        result = service.manual_search_sports(event, "en", arr_instance_id=owner)[0]
        service.manual_download_sports(event, result, arr_instance_id=owner)
    assert "Owner A processed" in (folder / "1/event.en.srt").read_text()
    assert "Sporting event" in (folder / "2/event.en.srt").read_text()
    assert "Owner A processed" not in (folder / "2/event.en.srt").read_text()


@pytest.mark.parametrize("phase", ["worker", "writer"])
def test_changed_file_during_download_or_publication_is_rejected(
    manual_library, monkeypatch, phase
):
    from subtitles import manual

    service, _, folder = manual_library
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    target = "download_subtitles" if phase == "worker" else "save_subtitles"
    original = getattr(manual, target)

    def changed(*args, **kwargs):
        with (folder / "1/event.mkv").open("ab") as stream:
            stream.write(b"changed during work")
        return original(*args, **kwargs)

    monkeypatch.setattr(manual, target, changed)
    with pytest.raises(ValueError):
        service.manual_download_sports(61, candidate, arr_instance_id=1)
    assert not (folder / "1/event.en.srt").exists()


def test_sports_candidate_rejected_by_legacy_download(manual_library):
    from subtitles.manual import manual_download_subtitle

    service, _, folder = manual_library
    result = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    with pytest.raises(ValueError):
        manual_download_subtitle(
            str(folder / "1/event.mkv"),
            "None",
            "False",
            "False",
            result["subtitle"],
            result["provider"],
            "None",
            "Event",
            "movie",
            False,
            None,
        )


@pytest.mark.parametrize("media_type", ["movie", "series"])
def test_shared_legacy_search_uses_real_provider_and_native_video(
    manual_library, monkeypatch, media_type
):
    from subtitles import manual, utils
    from subzero.language import Language

    service, _, folder = manual_library
    monkeypatch.setattr(utils, "registered_refiners", {})
    # Parsing and provider/scoring still run; unrelated DB/remote refiners are isolated.
    filename = folder / (
        "Film.2026.mkv" if media_type == "movie" else "Show.S01E02.mkv"
    )
    filename.write_bytes((folder / "1/event.mkv").read_bytes())
    results = manual.manual_search(
        str(filename),
        None,
        ["examplehub"],
        "None",
        "Film",
        media_type,
        language_set={Language("eng")},
    )
    assert len(results) == 1 and results[0]["orig_score"] > 0
    video = json.loads((folder / "video.json").read_text())
    assert video["kind"] == ("movie" if media_type == "movie" else "episode")
    if media_type == "series":
        assert video["season"] == 1 and video["episode"] == 2


def test_owner_sync_runs_real_output_runner_without_native_ids(
    manual_library, monkeypatch
):
    from app.database import TableArrInstances
    from app.config import settings
    from subtitles.tools.subsyncer import SubSyncer

    service, session, folder = manual_library
    monkeypatch.setattr(settings.subsync, "output_mode", "keep_all")
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(
            options=json.dumps(
                {
                    "subtitle_settings": {
                        "subsync": {
                            "use_subsync": True,
                            "use_subsync_movie_threshold": False,
                            "enabled_engines": ["ffsubsync"],
                            "max_offset_seconds": 60,
                        }
                    }
                }
            )
        )
    )
    calls = []

    def engine(self, output_path, **kwargs):
        assert (
            kwargs["sonarr_series_id"] is None
            and kwargs["sonarr_episode_id"] is None
            and kwargs["radarr_id"] is None
        )
        assert kwargs["arr_instance_id"] == 1 and kwargs["reference"] == "a:0"
        Path(output_path).write_text(
            Path(self.srtin).read_text().replace("Sporting event", "Synchronized event")
        )
        calls.append(output_path)
        return {"offset_seconds": 0.1, "framerate_scale_factor": 1.0}

    monkeypatch.setattr(SubSyncer, "_run_ffsubsync_engine", engine)
    for event, owner in ((61, 1), (62, 2)):
        result = service.manual_search_sports(event, "en", arr_instance_id=owner)[0]
        service.manual_download_sports(event, result, arr_instance_id=owner)
    assert len(calls) == 1
    outputs = list((folder / "1").glob("*ffsubsync*"))
    assert len(outputs) == 1 and "Synchronized event" in outputs[0].read_text()
    from app.database import TableSportsEvents

    session.expire_all()
    assert any(
        item[0] == "en:sync-ffsubsync"
        for item in ast.literal_eval(session.get(TableSportsEvents, 61).subtitles)
    )
    assert not list((folder / "2").glob("*ffsubsync*"))


@pytest.mark.parametrize("stage", ["sync", "postprocess"])
def test_owner_disable_during_processing_does_not_block_or_publish_stale_output(
    manual_library, monkeypatch, stage
):
    import time
    from sqlalchemy.orm import Session
    from app.database import TableArrInstances, TableHistorySports
    from app.config import settings
    from subtitles.tools.subsyncer import SubSyncer
    import subtitles.post_processing as pp

    service, session, folder = manual_library
    other = Session(bind=session.get_bind())
    elapsed = []

    def disable_owner():
        start = time.monotonic()
        # Independent connection: would time out if processing held BEGIN IMMEDIATE.
        if other.bind.dialect.name == "sqlite":
            other.execute(sa.text("PRAGMA busy_timeout=300"))
        else:
            other.execute(sa.text("SET lock_timeout = '300ms'"))
        other.execute(
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 1)
            .values(enabled=0)
        )
        elapsed.append(time.monotonic() - start)

    if stage == "sync":
        monkeypatch.setattr(settings.subsync, "output_mode", "keep_all")
        options = {
            "subsync": {
                "use_subsync": True,
                "use_subsync_movie_threshold": False,
                "enabled_engines": ["ffsubsync"],
            }
        }

        def engine(self, output_path, **kwargs):
            disable_owner()
            Path(output_path).write_text(
                Path(self.srtin)
                .read_text()
                .replace("Sporting event", "Stale synced result")
            )
            return {"offset_seconds": 0, "framerate_scale_factor": 1}

        monkeypatch.setattr(SubSyncer, "_run_ffsubsync_engine", engine)
    else:
        options = {
            "general": {
                "use_postprocessing": True,
                "postprocessing_cmd": "{{subtitles}}",
                "use_postprocessing_threshold_movie": False,
            }
        }

        def postprocess(command, path):
            import shlex

            disable_owner()
            target = Path(shlex.split(command)[0])
            target.write_text(
                target.read_text().replace("Sporting event", "Stale processed result")
            )

        monkeypatch.setattr(pp, "_postprocessing_locked", postprocess)
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(options=json.dumps({"subtitle_settings": options}))
    )
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    try:
        saved = service.manual_download_sports(61, candidate, arr_instance_id=1)
        assert saved.publication["published"] is True
        assert saved.publication["status"] == "published_with_warnings"
        assert saved.publication["index"] == "owner_changed"
        assert len(elapsed) == 1 and elapsed[0] < 0.3
        assert "Stale" not in (folder / "1/event.en.srt").read_text()
        assert not list((folder / "1").glob("*ffsubsync*"))
        assert not list((folder / "1").glob(".bazarr-write-*"))
        assert session.execute(sa.select(TableHistorySports)).first() is None
    finally:
        other.close()


def test_sports_video_uses_real_file_hash_and_probe_metadata(manual_library):
    from subtitles.utils import get_video
    from sportarr.identity import resolve_event
    from subliminal_patch.core import hash_opensubtitles

    service, _, folder = manual_library
    path = folder / "1/event.mkv"
    import subprocess

    # Use a codec, resolution and frame rate accepted by the shared knowit normalizer.
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=25",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(path),
        ],
        check=True,
    )
    with path.open("ab") as stream:
        stream.write(b"\0" * (11 * 1024 * 1024))
    context = resolve_event(61, 1)
    video = get_video(
        str(path),
        "Event",
        "None",
        providers={"opensubtitlescom"},
        media_type="sports",
        context=context,
    )
    assert video.hashes["opensubtitlescom"] == hash_opensubtitles(str(path))
    assert video.fps == 25 and video.video_codec
    assert video.size == path.stat().st_size


def test_cancelled_download_cannot_publish(manual_library, monkeypatch):
    import threading
    from subtitles import manual

    service, _, folder = manual_library
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    cancel = threading.Event()
    original = manual.download_subtitles

    def download(*args, **kwargs):
        result = original(*args, **kwargs)
        cancel.set()
        return result

    monkeypatch.setattr(manual, "download_subtitles", download)
    with pytest.raises(ValueError, match="stopped"):
        service.manual_download_sports(61, candidate, arr_instance_id=1, cancel=cancel)
    assert not (folder / "1/event.en.srt").exists()


def test_direct_sports_sync_requires_a_publication_guard(manual_library):
    from subtitles.sync import sync_subtitles
    from sportarr.identity import resolve_event

    _, _, folder = manual_library
    with pytest.raises(ValueError, match="publication"):
        sync_subtitles(
            str(folder / "1/event.mkv"),
            str(folder / "1/event.en.srt"),
            "en",
            False,
            False,
            60,
            arr_instance_id=1,
            context=resolve_event(61, 1),
            track_job_progress=False,
        )


def test_direct_sports_sync_captures_and_executes_real_queue_arguments(
    manual_library, monkeypatch
):
    from subtitles import sync
    from subtitles.cache import subtitle_cache
    from subtitles.tools.subsyncer import SubSyncer

    service, _, folder = manual_library
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    service.manual_download_sports(61, candidate, arr_instance_id=1)
    bound = subtitle_cache.get(candidate["subtitle"])
    queued = []

    def feed(**kwargs):
        queued.append(kwargs)
        return 101

    def engine(self, output_path, **kwargs):
        assert kwargs["radarr_id"] is None and kwargs["sonarr_episode_id"] is None
        Path(output_path).write_text(
            Path(self.srtin).read_text().replace("Sporting event", "Queued sync")
        )
        return {"offset_seconds": 0, "framerate_scale_factor": 1}

    def validate():
        return service.validate_candidate(bound, bound.context)

    def guard():
        return service.sports_publication(bound)

    monkeypatch.setattr(sync.jobs_queue, "feed_jobs_pending_queue", feed)
    monkeypatch.setattr(SubSyncer, "_run_ffsubsync_engine", engine)
    assert (
        sync.sync_subtitles(
            str(folder / "1/event.mkv"),
            str(folder / "1/event.en.srt"),
            "en",
            False,
            False,
            60,
            arr_instance_id=1,
            context=bound.context,
            validate=validate,
            publication_guard=guard,
            force_sync=True,
            enabled_engines=["ffsubsync"],
            output_mode="overwrite",
        )
        is False
    )
    assert len(queued) == 1
    arguments = queued[0]["kwargs"]
    assert arguments["context"] == bound.context
    assert arguments["validate"] is validate and arguments["publication_guard"] is guard
    assert sync.sync_subtitles(**dict(arguments, job_id=101)) is True
    assert "Queued sync" in (folder / "1/event.en.srt").read_text()


@pytest.mark.parametrize("existing", [False, True])
def test_absolute_destination_rejects_ambiguous_sports_creation(
    manual_library, monkeypatch, existing
):
    from app.config import settings
    from app.database import TableHistorySports, TableSportsEvents

    service, session, folder = manual_library
    target = folder / "shared-subtitles"
    target.mkdir()
    destination = target / "event.en.srt"
    expected = b"Owner A edited subtitle"
    if existing:
        destination.write_bytes(expected)
        session.execute(
            sa.update(TableSportsEvents)
            .where(TableSportsEvents.id == 61)
            .values(subtitles=str([["en", str(destination), len(expected)]]))
        )
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(target))
    candidate = service.manual_search_sports(62, "en", arr_instance_id=2)[0]
    with pytest.raises((ValueError, OSError), match="owner|ambiguous|destination"):
        service.manual_download_sports(62, candidate, arr_instance_id=2)
    assert (
        destination.read_bytes() == expected if existing else not destination.exists()
    )
    assert session.execute(sa.select(TableHistorySports)).first() is None


@pytest.mark.parametrize("media_type", ["movie", "episode"])
def test_sports_destination_preserves_native_recorded_owner(
    manual_library, monkeypatch, media_type
):
    from app.config import settings
    from app.database import (
        TableArrInstances,
        TableMovies,
        TableEpisodes,
        TableShows,
        TableSportsEvents,
    )

    service, session, folder = manual_library
    target = folder / "shared-subtitles"
    target.mkdir()
    destination = target / "event.en.srt"
    expected = b"Native owner edited subtitle"
    destination.write_bytes(expected)
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(target))
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id == 62)
        .values(path="/sports/other.mkv")
    )
    session.execute(
        sa.insert(TableArrInstances).values(
            id=3,
            kind="radarr" if media_type == "movie" else "sonarr",
            name="Native",
            stable_key="native",
            port=7878,
        )
    )
    values = dict(
        id=71,
        arr_instance_id=3,
        path=str(folder / "native/unrelated-name.mkv"),
        subtitles=str([["en", str(destination), len(expected)]]),
    )
    if media_type == "movie":
        session.execute(
            sa.insert(TableMovies).values(
                **values, radarrId=8, title="Native", tmdbId="native-fixture"
            )
        )
    else:
        session.execute(
            sa.insert(TableShows).values(
                id=71,
                arr_instance_id=3,
                sonarrSeriesId=8,
                title="Native",
                tvdbId=8,
                path=str(folder / "native"),
            )
        )
        session.execute(
            sa.insert(TableEpisodes).values(
                **values,
                sonarrSeriesId=8,
                sonarrEpisodeId=8,
                season=1,
                episode=1,
                title="Native",
            )
        )
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    with pytest.raises((ValueError, OSError), match="owner|ambiguous|destination"):
        service.manual_download_sports(61, candidate, arr_instance_id=1)
    assert destination.read_bytes() == expected


def test_overwrite_cleanup_releases_owner_transaction_before_library_lookup(
    manual_library, monkeypatch
):
    import time
    from sqlalchemy.orm import Session
    from app.config import settings
    from app.database import TableArrInstances, TableHistorySports
    from subtitles.tools import subsync_engines
    from subtitles.tools.subsyncer import SubSyncer

    service, session, folder = manual_library
    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "subs")
    monkeypatch.setattr(settings.subsync, "use_subsync", True)
    monkeypatch.setattr(settings.subsync, "use_subsync_movie_threshold", False)
    monkeypatch.setattr(settings.subsync, "enabled_engines", ["ffsubsync"])
    monkeypatch.setattr(settings.subsync, "output_mode", "overwrite")
    engine_done = []
    attempts = []
    blocked = []
    original = subsync_engines.SyncOutputOwnerIndex._load_owners

    def engine(self, output_path, **kwargs):
        Path(output_path).write_text(
            Path(self.srtin).read_text().replace("Sporting event", "Published sync")
        )
        engine_done.append(True)
        return {"offset_seconds": 0, "framerate_scale_factor": 1}

    def load_owners():
        if engine_done and not attempts:
            other = Session(bind=session.get_bind())
            started = time.monotonic()
            try:
                other.execute(
                    sa.text(
                        "PRAGMA busy_timeout=300"
                        if other.bind.dialect.name == "sqlite"
                        else "SET lock_timeout = '300ms'"
                    )
                )
                other.execute(
                    sa.update(TableArrInstances)
                    .where(TableArrInstances.id == 1)
                    .values(enabled=0)
                )
            except sa.exc.OperationalError as exc:
                blocked.append(type(exc.orig).__name__)
            finally:
                attempts.append(time.monotonic() - started)
                other.close()
        return original()

    monkeypatch.setattr(SubSyncer, "_run_ffsubsync_engine", engine)
    monkeypatch.setattr(
        subsync_engines.SyncOutputOwnerIndex, "_load_owners", staticmethod(load_owners)
    )
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    saved = service.manual_download_sports(61, candidate, arr_instance_id=1)
    assert saved.publication["published"] is True
    assert saved.publication["index"] == "owner_changed"
    assert attempts and not blocked
    assert "Published sync" in (folder / "1/subs/event.en.srt").read_text()
    assert session.execute(sa.select(TableHistorySports)).first() is None


@pytest.mark.parametrize("lock_kind", ["import", "owner_row", "event_row"])
def test_busy_import_rejects_publication_without_changing_file_or_history(
    manual_library,
    lock_kind,
):
    import time
    from sqlalchemy.orm import Session
    from app.database import (
        TableMovies,
        TableHistorySports,
        TableArrInstances,
        TableSportsEvents,
    )
    from subtitles.cache import subtitle_cache
    from subtitles.tools.subsync_engines import write_subtitle_file

    service, session, folder = manual_library
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    service.manual_download_sports(61, candidate, arr_instance_id=1)
    destination = folder / "1/event.en.srt"
    expected = destination.read_bytes().replace(
        b"Sporting event", b"User edited subtitle"
    )
    destination.write_bytes(expected)
    engine = session.get_bind()
    bound = subtitle_cache.get(candidate["subtitle"])

    def short_timeout(connection, *_):
        if engine.dialect.name == "sqlite":
            connection.execute("PRAGMA busy_timeout=300")

    sa.event.listen(engine, "checkout", short_timeout)
    with engine.connect().execution_options(
        isolation_level="SERIALIZABLE"
    ) as connection:
        with connection.begin() as transaction:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            with Session(bind=connection) as other:
                if lock_kind == "import":
                    other.execute(
                        sa.insert(TableMovies).values(
                            id=81,
                            radarrId=81,
                            title="Import in progress",
                            tmdbId="import-fixture",
                            path=str(folder / "import.mkv"),
                        )
                    )
                else:
                    table, local_id = (
                        (TableArrInstances, 1)
                        if lock_kind == "owner_row"
                        else (TableSportsEvents, 61)
                    )
                    other.execute(
                        sa.select(table.id)
                        .where(table.id == local_id)
                        .with_for_update()
                    )
                started = time.monotonic()
                with pytest.raises(ValueError, match="busy.*retry"):
                    write_subtitle_file(
                        str(folder / "1/event.mkv"),
                        str(destination),
                        b"Should not publish",
                        publication_guard=lambda: service.sports_publication(bound),
                    )
                assert time.monotonic() - started < 1
                if connection.dialect.name == "postgresql":
                    other.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
                # A failed publisher must release its partial table/row lock set.
                other.execute(
                    sa.update(TableArrInstances)
                    .where(TableArrInstances.id == 2)
                    .values(name="After busy")
                )
                transaction.rollback()
    sa.event.remove(engine, "checkout", short_timeout)
    assert destination.read_bytes() == expected
    assert (
        session.execute(
            sa.select(sa.func.count()).select_from(TableHistorySports)
        ).scalar_one()
        == 1
    )


@pytest.mark.parametrize("change", ["import", "mapping"])
def test_destination_owner_change_between_preparation_and_transaction_is_rejected(
    manual_library, monkeypatch, change
):
    from sqlalchemy.orm import Session
    from app.config import settings
    from app.database import TableArrInstances, TableMovies, TableSportsEvents

    service, session, folder = manual_library
    destination = folder / "shared/event.en.srt"
    destination.parent.mkdir()
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(destination.parent))
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id == 62)
        .values(path="/sports/other.mkv")
    )
    prepare = service.SportsOutputNamespace
    prepared = []

    def prepare_then_change(*args, **kwargs):
        namespace = prepare(*args, **kwargs)
        if not prepared:
            with Session(bind=session.get_bind()) as other:
                if change == "import":
                    other.execute(
                        sa.insert(TableMovies).values(
                            id=81,
                            radarrId=81,
                            title="Imported event",
                            tmdbId="import-fixture",
                            path=str(folder / "native/event.mkv"),
                        )
                    )
                else:
                    other.execute(
                        sa.update(TableArrInstances)
                        .where(TableArrInstances.id == 2)
                        .values(
                            path_mappings=json.dumps(
                                [["/sports/other.mkv", str(folder / "2/event.mkv")]]
                            )
                        )
                    )
            prepared.append(True)
        return namespace

    monkeypatch.setattr(service, "SportsOutputNamespace", prepare_then_change)
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    with pytest.raises(ValueError, match="destination"):
        service.manual_download_sports(61, candidate, arr_instance_id=1)
    assert prepared and not destination.exists()


@pytest.mark.parametrize(
    "mapping_kind",
    ["native_filename", "native_recorded", "native_first_match", "sports_filename"],
)
def test_destination_ownership_uses_actual_mapping_semantics(
    manual_library, monkeypatch, mapping_kind
):
    from app.config import settings
    from app.database import TableArrInstances, TableMovies, TableSportsEvents

    service, session, folder = manual_library
    target = folder / "shared"
    target.mkdir()
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(target))
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id == 62)
        .values(path="/sports/other.mkv")
    )
    if mapping_kind == "sports_filename":
        session.execute(
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 2)
            .values(
                path_mappings=json.dumps(
                    [["/sports/other.mkv", str(folder / "2/event.mkv")]]
                )
            )
        )
    else:
        mapping = {
            "native_filename": [["/remote/film.mkv", str(folder / "native/event.mkv")]],
            "native_recorded": [["/remote/private.srt", str(target / "event.en.srt")]],
            "native_first_match": [
                ["/remote", str(folder / "native")],
                ["film", "event"],
            ],
        }[mapping_kind]
        session.execute(
            sa.insert(TableArrInstances).values(
                id=3,
                kind="radarr",
                name="Native",
                stable_key="native",
                port=7878,
                path_mappings=json.dumps(mapping),
            )
        )
        session.execute(
            sa.insert(TableMovies).values(
                id=81,
                arr_instance_id=3,
                radarrId=81,
                title="Native",
                tmdbId="native-fixture",
                path="/remote/film.mkv",
                subtitles=str([["en", "/remote/private.srt", 1]]),
            )
        )
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    if mapping_kind == "native_first_match":
        service.manual_download_sports(61, candidate, arr_instance_id=1)
        assert "Sporting event" in (target / "event.en.srt").read_text()
    else:
        with pytest.raises(ValueError, match="destination"):
            service.manual_download_sports(61, candidate, arr_instance_id=1)
        assert not (target / "event.en.srt").exists()


def test_direct_sports_sync_rejects_foreign_destination_before_enqueue(manual_library):
    from subtitles.sync import sync_subtitles
    from subtitles.cache import subtitle_cache

    service, _, folder = manual_library
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    bound = subtitle_cache.get(candidate["subtitle"])
    foreign = folder / "2/event.en.srt"
    foreign.write_text("Foreign recording")
    with pytest.raises(ValueError, match="destination"):
        sync_subtitles(
            str(folder / "1/event.mkv"),
            str(foreign),
            "en",
            False,
            False,
            60,
            arr_instance_id=1,
            context=bound.context,
            force_sync=True,
            validate=lambda: service.validate_candidate(bound, bound.context),
            publication_guard=lambda: service.sports_publication(bound),
        )
    assert foreign.read_text() == "Foreign recording"


@pytest.mark.parametrize("stage", ["postprocess", "sync"])
def test_new_recorded_owner_during_processing_preserves_published_bytes(
    manual_library, monkeypatch, stage
):
    from sqlalchemy.orm import Session
    from app.config import settings
    from app.database import TableMovies, TableHistorySports
    import subtitles.post_processing as pp
    from subtitles.tools.subsyncer import SubSyncer

    service, session, folder = manual_library
    destination = folder / "1/event.en.srt"
    published = []

    def claim():
        published.append(destination.read_bytes())
        with Session(bind=session.get_bind()) as other:
            other.execute(
                sa.insert(TableMovies).values(
                    id=81,
                    radarrId=81,
                    title="Native claim",
                    tmdbId="native-fixture",
                    path=str(folder / "native/other.mkv"),
                    subtitles=str([["en", str(destination), len(published[-1])]]),
                )
            )

    if stage == "postprocess":
        monkeypatch.setattr(settings.general, "use_postprocessing", True)
        monkeypatch.setattr(
            settings.general, "use_postprocessing_threshold_movie", False
        )
        monkeypatch.setattr(settings.general, "postprocessing_cmd", "{{subtitles}}")

        def postprocess(command, _):
            import shlex

            claim()
            Path(shlex.split(command)[0]).write_text("Stale postprocessing output")

        monkeypatch.setattr(pp, "_postprocessing_locked", postprocess)
    else:
        monkeypatch.setattr(settings.subsync, "use_subsync", True)
        monkeypatch.setattr(settings.subsync, "use_subsync_movie_threshold", False)
        monkeypatch.setattr(settings.subsync, "enabled_engines", ["ffsubsync"])
        monkeypatch.setattr(settings.subsync, "output_mode", "overwrite")

        def engine(self, output_path, **kwargs):
            claim()
            Path(output_path).write_text("Stale sync output")
            return {"offset_seconds": 0, "framerate_scale_factor": 1}

        monkeypatch.setattr(SubSyncer, "_run_ffsubsync_engine", engine)
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    saved = service.manual_download_sports(61, candidate, arr_instance_id=1)
    assert saved.publication["published"] is True
    assert saved.publication["status"] == "published_with_warnings"
    assert saved.publication["index"] == "owner_changed"
    assert len(published) == 1 and destination.read_bytes() == published[0]
    assert session.execute(sa.select(TableHistorySports)).first() is None
    assert not list(destination.parent.glob(".bazarr-write-*"))


def test_two_sports_publishers_retry_contention_without_cross_owner_history(
    manual_library, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace
    from sqlalchemy.orm import scoped_session, sessionmaker
    from app.config import settings
    from app.database import TableSportsEvents, TableHistorySports
    from subtitles.cache import subtitle_cache
    from subtitles.tools.subsync_engines import (
        subtitle_write_locks,
        write_subtitle_file,
    )

    service, session, folder = manual_library
    target = folder / "shared"
    target.mkdir()
    (folder / "2/event.mkv").rename(folder / "2/other.mkv")
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id == 62)
        .values(path="/sports/other.mkv")
    )
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(target))
    candidates = [
        subtitle_cache.get(
            service.manual_search_sports(60 + owner, "en", arr_instance_id=owner)[0][
                "subtitle"
            ]
        )
        for owner in (1, 2)
    ]
    sessions = scoped_session(sessionmaker(bind=session.get_bind()))
    monkeypatch.setattr(service, "database", sessions)
    ready = Barrier(2)

    def publish(candidate, concurrent=True):
        context = candidate.context
        destination = target / (
            "event.en.srt" if context.arr_instance_id == 1 else "other.en.srt"
        )
        content = f"Owner {context.arr_instance_id} subtitle".encode()
        if concurrent:
            ready.wait(timeout=5)
        try:
            # Distinct stems retain independent real output coordinators.
            with subtitle_write_locks(context.mapped_path, str(destination)):
                write_subtitle_file(
                    context.mapped_path,
                    str(destination),
                    content,
                    publication_guard=lambda: service.sports_publication(candidate),
                )
                with service.sports_publication(candidate) as (transaction, _):
                    service.sports_history(
                        transaction,
                        context,
                        SimpleNamespace(
                            message="Saved",
                            path=context.original_path,
                            language_code="en",
                            provider="examplehub",
                            score=100,
                            subs_id="same-release",
                            subs_path=str(destination),
                            matched=[],
                            not_matched=[],
                        ),
                    )
            return destination, content
        except ValueError as exc:
            assert "busy. Please retry" in str(exc)
            return None, candidate
        finally:
            sessions.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, candidates))
    results = [
        publish(candidate, concurrent=False) if path is None else (path, candidate)
        for path, candidate in results
    ]
    assert all(path.read_bytes() == content for path, content in results)
    histories = (
        session.execute(
            sa.select(TableHistorySports).order_by(TableHistorySports.arr_instance_id)
        )
        .scalars()
        .all()
    )
    assert [(row.event_id, row.arr_instance_id) for row in histories] == [
        (61, 1),
        (62, 2),
    ]
    assert len({row.subtitles_path for row in histories}) == 2


@pytest.mark.parametrize("change", ["setting", "symlink"])
def test_candidate_rejects_changed_destination_policy(
    manual_library, monkeypatch, change
):
    from app.config import settings

    service, _, folder = manual_library
    first, second, link = folder / "first", folder / "second", folder / "output"
    first.mkdir()
    second.mkdir()
    link.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(link))
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    if change == "setting":
        monkeypatch.setattr(settings.general, "subfolder_custom", str(second))
    else:
        link.unlink()
        link.symlink_to(second, target_is_directory=True)
    with pytest.raises(ValueError, match="changed"):
        service.manual_download_sports(61, candidate, arr_instance_id=1)
    assert not list(first.iterdir()) and not list(second.iterdir())


@pytest.mark.parametrize(
    "failure",
    [
        "publication",
        "processing",
        "artifact",
        "history",
        "index",
        "index_once",
        "cancel",
        "owner",
    ],
)
def test_published_download_reports_phase_and_owner_safe_refresh(
    manual_library, monkeypatch, failure, caplog
):
    from threading import Event
    from app.database import TableHistorySports, TableArrInstances
    from sportarr import artifacts
    from subtitles import processing
    from subtitles.indexer import sports as indexer

    service, session, folder = manual_library
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    cancel = Event()
    calls = []
    original_index = indexer.store_subtitles_sports
    original_process = processing.process_subtitle

    def index(*args, **kwargs):
        calls.append(args)
        if failure == "index" or (failure == "index_once" and len(calls) == 1):
            raise OSError("injected index failure")
        return original_index(*args, **kwargs)

    def fail(*args, **kwargs):
        raise OSError("injected failure after file publication")

    def process(*args, **kwargs):
        if failure == "cancel":
            cancel.set()
        elif failure == "owner":
            session.execute(
                sa.update(TableArrInstances)
                .where(TableArrInstances.id == 1)
                .values(enabled=0)
            )
        if failure in ("cancel", "owner"):
            raise ValueError("operation stopped after publication")
        return original_process(*args, **kwargs)

    monkeypatch.setattr(indexer, "store_subtitles_sports", index)
    monkeypatch.setattr(
        processing, "process_subtitle", fail if failure == "processing" else process
    )
    if failure == "artifact":
        monkeypatch.setattr(artifacts, "capture_artifact", fail)
    if failure == "history":
        monkeypatch.setattr(service, "sports_history", fail)
    if failure == "publication":
        from contextlib import contextmanager

        original_publication = service.sports_publication

        @contextmanager
        def after_replace(*args, **kwargs):
            with original_publication(*args, **kwargs) as publication:
                yield publication
            raise OSError("injected failure after publication acknowledgement")

        monkeypatch.setattr(service, "sports_publication", after_replace)
    saved = service.manual_download_sports(
        61, candidate, arr_instance_id=1, cancel=cancel
    )
    state = saved.publication
    assert state["published"] is True
    assert state["status"] == "published_with_warnings"
    assert (
        "published" in state["message"].lower()
        and "retry" not in state["message"].lower()
    )
    assert (folder / "1/event.en.srt").read_bytes()
    rows = session.execute(sa.select(TableHistorySports)).all()
    assert len(rows) == int(failure in ("index", "index_once"))
    assert state["history"] == (
        "committed" if rows else "failed" if failure == "history" else "not_started"
    )
    expected = (
        "cancelled"
        if failure == "cancel"
        else "owner_changed"
        if failure == "owner"
        else "failed"
        if failure == "index"
        else "completed"
    )
    assert state["index"] == expected
    assert len(calls) == (
        0
        if failure in ("cancel", "owner")
        else 2
        if failure in ("index", "index_once")
        else 1
    )
    assert state["refresh_attempts"] == len(calls)
    assert state["refresh_queued"] is False
    assert (
        "published" in caplog.text.lower() and "injected" in caplog.text.lower()
        if failure not in ("cancel", "owner")
        else "operation stopped after publication" in caplog.text
    )


def test_history_commit_acknowledgement_loss_keeps_committed_bytes_and_history(
    manual_library, monkeypatch, caplog
):
    from contextlib import contextmanager
    from app.database import TableHistorySports

    service, session, folder = manual_library
    original = service.sports_publication

    @contextmanager
    def uncertain_history(candidate, cancel=None, *, outcome=None):
        with original(candidate, cancel, outcome=outcome) as publication:
            yield publication
        if outcome is not None:
            raise OSError("injected lost history commit acknowledgement")

    monkeypatch.setattr(service, "sports_publication", uncertain_history)
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    saved = service.manual_download_sports(61, candidate, arr_instance_id=1)
    assert saved.publication["history"] == "uncertain"
    assert saved.publication["published"] is True
    assert saved.publication["index"] == "completed"
    assert session.execute(sa.select(TableHistorySports)).scalar_one().event_id == 61
    assert (folder / "1/event.en.srt").read_bytes()
    assert "lost history commit acknowledgement" in caplog.text


def test_second_publication_refreshes_cached_destination_ownership(manual_library):
    from app.database import TableMovies
    service, session, folder = manual_library
    selected = service.manual_search_sports(61, 'en', arr_instance_id=1)[0]
    service.manual_download_sports(61, selected, arr_instance_id=1)
    destination = folder / '1/event.en.srt'
    published = destination.read_bytes()
    session.execute(sa.insert(TableMovies).values(id=81, radarrId=81, tmdbId='81',
        title='Other owner', path=str(folder / '1/event.mkv')))
    selected = service.manual_search_sports(61, 'en', arr_instance_id=1)[0]
    with pytest.raises(ValueError, match='ambiguous|recorded'):
        service.manual_download_sports(61, selected, arr_instance_id=1)
    assert destination.read_bytes() == published
