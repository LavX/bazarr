"""Track metadata, scoring and profile refresh for standalone source items."""

import ast
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Event
from unittest.mock import Mock

import pytest
import sqlalchemy as sa

from media_source_helpers import add_group, add_item, add_profile, add_source, stamped_video


def metadata(*, codec="H.264", audio="eng"):
    return {"ffprobe": {
        "video": [{"codec": codec, "resolution": "720p", "frame_rate": 25.0}],
        "audio": [{"codec": "AAC", "language": audio}, {}],
        "duration": timedelta(seconds=43),
        "subtitle": [
            {"language": "fra", "format": "SRT", "name": "French Forced"},
            {"language": "hun", "format": "ASS", "name": "Hungarian SDH", "hearing_impaired": True},
            {"language": "hun", "format": "SRT", "name": "Commentary"},
        ],
    }, "mediainfo": {}}


@pytest.fixture
def probe(monkeypatch, languages_loaded):
    from app.config import settings
    from utilities import video_analyzer

    monkeypatch.setattr(settings.general, "embedded_subtitles_parser", "ffprobe")
    monkeypatch.setattr(settings.general, "use_embedded_subs", True)
    monkeypatch.setattr(settings.general, "ignore_ass_subs", True)
    monkeypatch.setattr(settings.general, "default_und_audio_lang", "de")
    probe = Mock(return_value=metadata())
    monkeypatch.setattr(video_analyzer, "parse_video_metadata", probe)
    return probe


def test_probe_and_profile_filter_use_the_same_tracks(source_db, tmp_path, probe, monkeypatch):
    from app.config import settings
    from media_sources import indexer, repository
    from app.database import TableLanguagesProfiles, update_profile_id_list

    profile = add_profile(source_db, [
        ("en", "False", "False"), ("fr", "True", "False"), ("hu", "False", "False"),
        ("de", "False", "False"), ("ja", "False", "False")])
    items = json.loads(source_db.execute(sa.select(TableLanguagesProfiles.items)).scalar_one())
    items[0]["audio_exclude"] = "True"
    items[3]["audio_exclude"] = "True"
    items[4]["audio_only_include"] = "True"
    source_db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(items)))
    update_profile_id_list.invalidate()
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=profile)
    path = stamped_video(root / "Film.2001.mkv")
    item_id = add_item(source_db, source_id, library_id, path, title="Film", year="2001")

    assert indexer.index_item(item_id) == [["fr:forced", None, None, "srt", "French Forced"]]
    row = repository.get_item(item_id)
    assert ast.literal_eval(row.missing_subtitles) == ["hu"]
    assert ast.literal_eval(row.audio_language) == ["English", None]
    assert (row.video_codec, row.audio_codec, row.resolution) == ("H.264", "AAC", "720p")
    assert row.ffprobe_cache
    monkeypatch.setattr(settings.general, "ignore_ass_subs", False)
    assert indexer.index_item(item_id) == [
        ["fr:forced", None, None, "srt", "French Forced"],
        ["hu:hi", None, None, "ass", "Hungarian SDH"],
    ]
    assert ast.literal_eval(repository.get_item(item_id).missing_subtitles) == []
    probe.assert_called_once()


@pytest.fixture
def offline_movies(monkeypatch):
    from discover import metadata as discover_metadata

    monkeypatch.setattr(discover_metadata, "search_movies",
                        lambda *_args, **_kwargs: {"data": {"status": "available", "items": []}})


def replace_preserving_size_and_mtime(path):
    old = path.stat()
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(b"1" * old.st_size)
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
    os.replace(replacement, path)
    assert path.stat().st_ino != old.st_ino


def test_replacement_reprobes_and_a_rename_keeps_the_cache(source_db, tmp_path, probe, offline_movies):
    from media_sources import repository, scanner

    source_id, library_id, root = add_source(source_db, tmp_path)
    path = stamped_video(root / "Film.2001.mkv")
    scanner.run_scan(source_id, library_id)
    (before,) = repository.list_items(library_id=library_id)
    assert before.ffprobe_cache and before.video_codec == "H.264"

    moved = root / "Film.2001.Remux.mkv"
    path.rename(moved)
    scanner.run_scan(source_id, library_id)
    (renamed,) = repository.list_items(library_id=library_id)
    assert renamed.id == before.id and renamed.path == str(moved)
    assert renamed.ffprobe_cache == before.ffprobe_cache
    probe.assert_called_once()

    replace_preserving_size_and_mtime(moved)
    probe.return_value = metadata(codec="H.265", audio="deu")
    scanner.run_scan(source_id, library_id)
    replaced = repository.get_item(before.id)
    assert replaced.ffprobe_cache != before.ffprobe_cache
    assert replaced.file_mtime_ns == before.file_mtime_ns and replaced.file_size == before.file_size
    assert replaced.video_codec == "H.265"
    assert ast.literal_eval(replaced.audio_language) == ["German", None]
    assert probe.call_count == 2

    stamped_video(moved, size=before.file_size)
    probe.return_value = None
    scanner.run_scan(source_id, library_id)
    failed = repository.get_item(before.id)
    assert failed.ffprobe_cache is None and failed.audio_language is None
    assert failed.video_codec is None and failed.audio_codec is None and failed.resolution is None
    assert ast.literal_eval(failed.subtitles) == []

    probe.return_value = metadata(codec="AV1", audio="eng")
    scanner.run_scan(source_id, library_id)
    assert repository.get_item(before.id).video_codec == "AV1"


def test_a_remount_reuses_only_cache_tied_to_the_admitted_file(
    source_db, tmp_path, probe, offline_movies, monkeypatch,
):
    from media_sources import analysis, identity, repository, scanner, service

    profile = add_profile(source_db, [("fr", "True", "False")])
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=profile)
    path = stamped_video(root / "Film.2001.mkv")
    scanner.run_scan(source_id, library_id)
    (item,) = repository.list_items(library_id=library_id)
    before = identity.stored_content_stamp(item)
    previous_device = str(int(before[0]) + 1)
    previous_inode = str(int(before[1]) + 1)
    previous = (previous_device, previous_inode, *before[2:])
    cached = analysis.read_metadata(item.ffprobe_cache)
    repository.update_item(item.id, source_db, file_dev=previous_device, file_ino=previous_inode,
                           ffprobe_cache=analysis.stamp_index(cached, previous))
    (library_root,) = repository.get_roots(library_id)
    repository.record_root_device(library_root.id, previous_device, source_db)
    probe.return_value = None

    (result,) = scanner.run_scan(source_id, library_id)
    assert result.unchanged == 1 and result.inserted == 0
    after = repository.get_item(item.id)
    assert identity.stored_content_stamp(after) == before
    assert analysis.cached_metadata(after, path.stat()) == cached
    probe.assert_called_once()

    # An old blob can stay through admission, but a search needs fresh tracks.
    repository.update_item(item.id, source_db, ffprobe_cache=b"legacy probe")
    probe.return_value = metadata()
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **_: None)
    service.search_source_item(item.id, job_id=1)
    assert probe.call_count == 2
    assert analysis.cached_metadata(repository.get_item(item.id), path.stat()) == cached


def test_index_drops_a_probe_if_the_file_changes_during_analysis(source_db, tmp_path, probe):
    from media_sources import indexer, repository

    source_id, library_id, root = add_source(source_db, tmp_path)
    path = stamped_video(root / "Film.2001.mkv")
    item_id = add_item(source_db, source_id, library_id, path)

    def replace_during_probe(*args, **kwargs):
        stamped_video(path, size=path.stat().st_size)
        return metadata()

    probe.side_effect = replace_during_probe
    assert indexer.index_item(item_id) is None
    row = repository.get_item(item_id)
    assert row.ffprobe_cache is None and row.subtitles is None and row.missing_subtitles is None


def test_an_arr_movie_and_a_source_movie_have_the_same_scored_fields(source_db, tmp_path, probe, monkeypatch):
    from app.config import settings
    from app.database import TableMovies
    from media_sources import indexer
    from media_sources.context import resolve_item
    from provider_hub.protocol import language_from_payload, video_to_payload
    from subtitles import utils
    from subtitles.refiners import database as db_refiner, ffprobe

    monkeypatch.setattr(settings.general, "skip_hashing", True)
    monkeypatch.setattr(db_refiner, "database", source_db)
    monkeypatch.setattr(ffprobe, "database", source_db)
    monkeypatch.setattr(ffprobe, "parse_video_metadata", probe)
    monkeypatch.setattr(utils, "registered_refiners", {
        "database": db_refiner.refine_from_db, "ffprobe": ffprobe.refine_from_ffprobe,
    })
    source_id, library_id, root = add_source(source_db, tmp_path)
    path = stamped_video(root / "Film.2001.BluRay-GRP.mkv")
    item_id = add_item(source_db, source_id, library_id, path, title="Film", year="2001",
                       imdb_id="tt0000021", tmdb_id="21", scene_name=path.stem)
    source_db.execute(sa.insert(TableMovies).values(
        radarrId=1, path=str(path), title="Film", sortTitle="film", year="2001", imdbId="tt0000021",
        tmdbId="21", format="BluRay", resolution="720p", video_codec="h264", audio_codec="aac",
        alternativeTitles="[]", file_size=path.stat().st_size, movie_file_id=33, audio_language="[]",
        monitored="True"))
    indexer.index_item(item_id)
    args = (str(path), "Film", path.stem)
    arr = utils.get_video(*args)
    source = utils.get_video(*args, source=resolve_item(item_id))
    assert arr is not None and source is not None
    fields = ("title", "year", "imdb_id", "source", "release_group", "resolution", "video_codec",
              "audio_codec", "fps", "audio_languages", "streaming_service", "edition")
    assert {field: getattr(source, field) for field in fields} == {field: getattr(arr, field) for field in fields}
    assert source.tmdb_id == int(arr.tmdb_id) == 21
    assert source.audio_languages == {"eng"}
    arr_payload = json.loads(json.dumps(video_to_payload(arr)))
    source_payload = json.loads(json.dumps(video_to_payload(source)))
    assert {key: source_payload[key] for key in fields} == {key: arr_payload[key] for key in fields}
    assert [language_from_payload(language).alpha3 for language in source_payload["audio_languages"]] == ["eng"]


def test_sports_runs_no_registered_refiner_and_episodes_get_group_ids(source_db, tmp_path, probe, monkeypatch):
    from app.config import settings
    from media_sources import indexer
    from media_sources.context import resolve_item
    from subtitles import utils

    monkeypatch.setattr(settings.general, "skip_hashing", True)
    refs = {name: Mock() for name in ("database", "ffprobe", "arr_history", "anidb", "anilist")}
    monkeypatch.setattr(utils, "registered_refiners", refs)
    source_id, library_id, root = add_source(source_db, tmp_path, content_kind="sports")
    league = add_group(source_db, source_id, library_id, kind="league", title="Formula 1")
    path = stamped_video(root / "Monaco.Grand.Prix.2024.mkv")
    sport_id = add_item(source_db, source_id, library_id, path, kind="sports_event", group_id=league,
                        title="Monaco Grand Prix", part_name="Race", event_date="2024-05-26")
    indexer.index_item(sport_id)
    video = utils.get_video(str(path), "Monaco Grand Prix", "None", source=resolve_item(sport_id))
    assert (video.title, video.year, video.duration) == ("Monaco Grand Prix Race", 2024, 43.0)
    assert (video.video_codec, video.audio_codec, video.resolution) == ("H.264", "AAC", "720p")
    assert all(ref.call_count == 0 for ref in refs.values())

    tv_owner, tv_library, tv_root = add_source(source_db, tmp_path, name="TV", folder="TV", content_kind="series")
    show = add_group(source_db, tv_owner, tv_library, imdb_id="tt0000021", tvdb_id=22)
    path = stamped_video(tv_root / "Show" / "Show.S01E02.mkv")
    episode_id = add_item(source_db, tv_owner, tv_library, path, kind="episode", group_id=show, title="Episode",
                          season=1, episode=2, imdb_id="tt0000031", tvdb_id=32, tmdb_id="33")
    indexer.index_item(episode_id)
    video = utils.get_video(str(path), "Show", "None", media_type="series", source=resolve_item(episode_id))
    assert (video.series_imdb_id, video.series_tvdb_id) == ("tt0000021", 22)
    assert (video.imdb_id, video.tvdb_id, video.tmdb_id) == ("tt0000031", 32, 33)
    assert refs["anidb"].call_count == refs["anilist"].call_count == 1
    assert refs["database"].call_count == refs["ffprobe"].call_count == refs["arr_history"].call_count == 0


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="The local media binaries are required")
def test_real_probe_caches_streams_and_embedded_track_title(source_db, tmp_path, monkeypatch, languages_loaded):
    from app.config import settings
    from media_sources import analysis, indexer, repository
    from media_sources.context import resolve_item
    from provider_hub.protocol import video_to_payload
    from provider_hub.worker import _json_default
    from subtitles.utils import get_video

    monkeypatch.setattr(settings.general, "embedded_subtitles_parser", "ffprobe")
    monkeypatch.setattr(settings.general, "use_embedded_subs", True)
    source_id, library_id, root = add_source(source_db, tmp_path)
    path = root / "Local.2001.mkv"
    subtitle = tmp_path / "embedded.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,500\nBonjour\n")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-threads", "1", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000", "-i", str(subtitle), "-t", "1",
         "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "mpeg4", "-c:a", "flac", "-c:s", "srt",
         "-metadata:s:a:0", "language=eng", "-metadata:s:s:0", "language=fra", "-metadata:s:s:0",
         "title=French Forced", str(path)], check=True, timeout=30)
    item_id = add_item(source_db, source_id, library_id, path)
    listing = indexer.index_item(item_id)
    row = repository.get_item(item_id)
    assert listing == [["fr:forced", None, None, "subrip", "French Forced"]]
    assert ast.literal_eval(row.audio_language) == ["English"]
    assert row.audio_codec and row.video_codec and row.resolution
    assert analysis.read_metadata(row.ffprobe_cache)["ffprobe"]["duration"] == timedelta(seconds=1)
    video = get_video(str(path), "Local", "None", source=resolve_item(item_id))
    payload = json.loads(json.dumps(video_to_payload(video), default=_json_default))
    assert payload["duration"] == 1.0 and payload["fps"] == 25.0
    assert [language["alpha3"] for language in payload["audio_languages"]] == ["eng"]


@pytest.fixture
def refresh_queue(monkeypatch):
    from app import jobs_queue, config
    from subtitles.indexer import missing_refresh

    for setting in ("use_sonarr", "use_radarr", "use_sportarr"):
        monkeypatch.setattr(config.settings.general, setting, False)
    queue = jobs_queue.JobsQueue()
    monkeypatch.setattr(jobs_queue, "event_stream", lambda **_: None)
    monkeypatch.setattr(missing_refresh, "jobs_queue", queue)
    return queue


def run_next_refresh(queue):
    job = queue._reserve_next_job()
    assert job is not None
    queue._run_job(job)
    assert queue.list_jobs_from_queue(job_id=job.job_id)[0]["status"] == "completed"
    return job.kwargs.get("source_scope")


def test_settings_and_profile_saves_refresh_the_stored_listing_without_a_scan(
    source_db, tmp_path, probe, refresh_queue, monkeypatch,
):
    import sys
    from app import config
    from media_sources import indexer, repository
    from subtitles.indexer import missing_refresh
    from test_settings_profile_deletion import _profile_payload, _settings_endpoint

    profile = add_profile(source_db, [("en", "False", "False"), ("fr", "True", "False")])
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=profile)
    path = stamped_video(root / "Local.2001.mkv")
    item_id = add_item(source_db, source_id, library_id, path)
    monkeypatch.setattr(config.settings.general, "default_und_embedded_subtitles_lang", "")
    probe.return_value["ffprobe"]["subtitle"].append({"format": "SRT", "name": "Untitled"})
    indexer.index_item(item_id)
    before = repository.get_item(item_id)
    assert ast.literal_eval(before.missing_subtitles) == ["en"]
    monkeypatch.setattr(indexer, "local_stat", lambda *_: pytest.fail("A profile refresh must not read the media"))

    post_settings = _settings_endpoint(source_db, monkeypatch)
    endpoint = sys.modules["api.system.settings"]
    monkeypatch.setattr(endpoint, "queue_missing_subtitles_recalculation",
                        missing_refresh.queue_missing_subtitles_recalculation)
    monkeypatch.setattr(endpoint, "clear_failed_translations", lambda: None)

    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    assert post_settings({"settings-general-use_embedded_subs": "false"}) == ("", 204)
    assert run_next_refresh(refresh_queue) is None
    assert not refresh_queue.jobs_pending_queue
    row = repository.get_item(item_id)
    assert ast.literal_eval(row.missing_subtitles) == ["en", "fr:forced"]
    assert ast.literal_eval(row.subtitles) == []

    payload = _profile_payload(profile, "Revised")
    payload["items"] = [{"id": 2, "language": "fr", "forced": "True", "hi": "False"}]
    assert post_settings({"languages-profiles": json.dumps([payload])}) == ("", 204)
    assert run_next_refresh(refresh_queue) is None
    row = repository.get_item(item_id)
    assert ast.literal_eval(row.missing_subtitles) == ["fr:forced"]

    assert post_settings({"settings-general-use_embedded_subs": "true"}) == ("", 204)
    assert run_next_refresh(refresh_queue) is None
    assert not refresh_queue.jobs_pending_queue
    row = repository.get_item(item_id)
    assert ast.literal_eval(row.missing_subtitles) == []
    assert row.subtitles == before.subtitles
    assert row.ffprobe_cache == before.ffprobe_cache and row.indexed_at == before.indexed_at
    probe.assert_called_once()

    notification = json.dumps({"name": "Fixture", "enabled": True, "url": "disabled://fixture"})
    assert post_settings({"notifications-providers": notification}) == ("", 204)
    assert not refresh_queue.jobs_pending_queue

    assert post_settings({"settings-general-default_und_embedded_subtitles_lang": "hu"}) == ("", 204)
    assert run_next_refresh(refresh_queue) == {}
    assert ["hu", None, None, "srt", "Untitled"] in ast.literal_eval(repository.get_item(item_id).subtitles)
    assert not refresh_queue.jobs_pending_queue

    def refuse(*_args, **_kwargs):
        raise RuntimeError("Queue unavailable")

    monkeypatch.setattr(refresh_queue, "feed_jobs_pending_queue", refuse)
    body, status = post_settings({"settings-general-default_und_embedded_subtitles_lang": "fr"})
    assert status == 503 and body["code"] == "settings_refresh_failed"
    assert config.settings.general.default_und_embedded_subtitles_lang == "fr"
    body, status = post_settings({"settings-general-use_embedded_subs": "false"})
    assert status == 503 and body["code"] == "settings_refresh_failed"
    assert config.settings.general.use_embedded_subs is False


def test_library_and_group_profile_changes_scope_the_same_refresh(
    source_db, tmp_path, probe, refresh_queue, monkeypatch,
):
    from flask import Flask

    from api import api_bp
    from app.config import settings
    from media_sources import indexer, repository

    old = add_profile(source_db, [("en", "False", "False")])
    new = add_profile(source_db, [("de", "False", "False")], profile_id=2)
    source_id, library_id, root = add_source(source_db, tmp_path, content_kind="series", profile_id=old)
    first = add_group(source_db, source_id, library_id, title="First")
    second = add_group(source_db, source_id, library_id, title="Second")
    episode_ids = [
        add_item(source_db, source_id, library_id, stamped_video(root / str(group_id) / "Show.S01E01.mkv"),
                 kind="episode", group_id=group_id)
        for group_id in (first, second)
    ]
    other_owner, other_library, other_root = add_source(source_db, tmp_path, name="Other", folder="Other",
                                                       profile_id=old)
    other_item = add_item(source_db, other_owner, other_library, stamped_video(other_root / "Movie.2001.mkv"))
    for item_id in [*episode_ids, other_item]:
        indexer.index_item(item_id)
    monkeypatch.setattr(indexer, "local_stat", lambda *_: pytest.fail("A profile refresh must not read the media"))

    def wanted(item_ids):
        return [ast.literal_eval(repository.get_item(item_id).missing_subtitles) for item_id in item_ids]

    headers = {"X-API-KEY": "synthetic-media-analysis-key"}
    monkeypatch.setitem(settings.auth, "apikey", headers["X-API-KEY"])
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    client = app.test_client()

    group_url = f"/api/sources/groups/{first}"
    assert client.patch(group_url, json={"profile_id": new}).status_code == 401
    assert client.patch(group_url, json={"profile_id": True}, headers=headers).status_code == 400
    assert client.patch(group_url, json={"profile_id": new, "title": "Ignored"},
                        headers=headers).status_code == 400
    response = client.patch(group_url, json={"profile_id": new}, headers=headers)
    assert response.status_code == 200 and response.json["profile_id"] == new
    assert run_next_refresh(refresh_queue) == {"group_id": first}
    assert wanted(episode_ids) == [["de"], ["en"]]

    library_url = f"/api/system/media-sources/{source_id}/libraries/{library_id}"
    response = client.patch(library_url, json={"profile_id": None}, headers=headers)
    assert response.status_code == 200 and response.json["profile_id"] is None
    assert run_next_refresh(refresh_queue) == {"library_id": library_id}
    assert wanted(episode_ids) == [["de"], []]
    assert wanted([other_item]) == [["en"]]
    assert probe.call_count == 3


def test_no_source_means_no_source_recalculation(source_db, refresh_queue, monkeypatch):
    from media_sources import indexer
    from subtitles.indexer import missing_refresh

    recalc = Mock()
    monkeypatch.setattr(indexer, "recalculate_missing_subtitles", recalc)
    assert missing_refresh._library_steps() == []
    missing_refresh.queue_missing_subtitles_recalculation()
    run_next_refresh(refresh_queue)
    recalc.assert_not_called()


@pytest.mark.parametrize("extension", ["mkv", "sTrM"])
def test_search_of_a_legacy_external_only_index(source_db, tmp_path, probe, monkeypatch, extension):
    from app.config import settings
    from media_sources import identity, indexer, repository, service
    from subtitles import download
    from subzero.language import Language

    monkeypatch.setattr(settings.general, "enable_strm_support", True)
    monkeypatch.setattr(indexer, "get_language_set", lambda: {Language("hun")})
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **_: None)
    monkeypatch.setattr(service, "get_providers", lambda: ["fixture"])
    requested = Mock(return_value=iter(()))
    monkeypatch.setattr(download, "generate_subtitles", requested)
    profile = add_profile(source_db, [("fr", "True", "False")])
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=profile)
    path = stamped_video(root / f"Film.2001.{extension}")
    if extension == "sTrM":
        path.write_text("https://example.invalid/fixture.mkv\n")
    sidecar = path.with_suffix(".hu.srt")
    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nFixture\n")
    dev, ino, size, mtime = identity.stat_file(path)
    item_id = add_item(source_db, source_id, library_id, path, file_dev=dev, file_ino=ino, file_size=size,
                       file_mtime_ns=mtime)
    external = [["hu", str(sidecar), sidecar.stat().st_size]]
    # Development wrote the external listing and missing languages without a probe cache.
    repository.write_item_index(item_id, str(external), "['fr:forced']", source_db)
    assert repository.get_item(item_id).ffprobe_cache is None

    enumerate_sidecars = Mock(wraps=indexer.list_external_subtitles)
    monkeypatch.setattr(indexer, "list_external_subtitles", enumerate_sidecars)
    probe.return_value = None
    service.search_source_item(item_id, job_id=1)
    requested.assert_called_once()
    assert requested.call_args.args[1:3] == ([("fr", "False", "True")], "None")
    enumerate_sidecars.assert_not_called()
    assert ast.literal_eval(repository.get_item(item_id).subtitles) == external

    probe.return_value = metadata()
    service.search_source_item(item_id, job_id=2)
    after = repository.get_item(item_id)
    if extension == "sTrM":
        probe.assert_not_called()
        enumerate_sidecars.assert_not_called()
        assert ast.literal_eval(after.subtitles) == external and requested.call_count == 2

        path.write_text("https://example.invalid/replacement.mkv\n")
        service.search_source_item(item_id, job_id=3)
        service.search_source_item(item_id, job_id=4)
        probe.assert_not_called()
        enumerate_sidecars.assert_called_once()
        assert ast.literal_eval(repository.get_item(item_id).subtitles) == external
    else:
        assert probe.call_count == 2
        requested.assert_called_once()
        enumerate_sidecars.assert_called_once()
        assert after.video_codec == "H.264" and after.missing_subtitles == "[]"
        assert ast.literal_eval(after.subtitles) == [["fr:forced", None, None, "srt", "French Forced"], *external]


@pytest.mark.parametrize("scenario", ["reuse", "restore", "recover"])
def test_search_refreshes_replacement_tracks_before_a_scan(
    source_db, tmp_path, probe, offline_movies, monkeypatch, scenario,
):
    from media_sources import analysis, repository, scanner, service
    from subtitles import download

    profile = add_profile(source_db, [("fr", "True", "False")])
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=profile)
    path = stamped_video(root / "Film.2001.mkv")
    scanner.run_scan(source_id, library_id)
    (before,) = repository.list_items(library_id=library_id)
    assert ast.literal_eval(before.missing_subtitles) == []

    original = root / ".original"
    os.link(path, original)
    replace_preserving_size_and_mtime(path)
    fresh = metadata(audio="deu")
    fresh["ffprobe"]["subtitle"] = []
    probe_succeeds = scenario == "reuse"
    probe.return_value = fresh if probe_succeeds else None
    requested = Mock(return_value=iter(()))
    monkeypatch.setattr(service, "get_providers", lambda: ["fixture"])
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **_: None)
    monkeypatch.setattr(download, "generate_subtitles", requested)
    service.search_source_item(before.id, job_id=1)

    requested.assert_called_once()
    assert requested.call_args.args[1] == [("fr", "False", "True")]
    assert requested.call_args.args[2] == ("German" if probe_succeeds else "None")
    after = repository.get_item(before.id)
    assert after.file_ino == before.file_ino
    assert ast.literal_eval(after.subtitles) == []
    assert bool(analysis.streams(analysis.read_metadata(after.ffprobe_cache))) is probe_succeeds

    if probe_succeeds:
        # The scan admits the replacement without discarding the search's good probe.
        probe.return_value = None
        scanner.run_scan(source_id, library_id)
        admitted = repository.get_item(before.id)
        assert admitted.file_ino != before.file_ino
        assert admitted.ffprobe_cache == after.ffprobe_cache
        assert ast.literal_eval(admitted.audio_language) == ["German", None]
        assert probe.call_count == 2
    else:
        # A stamp-only listing can be stale after restoration, and a recovered
        # probe may discover previously unknown tracks on the replacement.
        if scenario == "restore":
            os.replace(original, path)
        probe.return_value = metadata()
        service.search_source_item(before.id, job_id=2)
        restored = repository.get_item(before.id)
        assert ast.literal_eval(restored.missing_subtitles) == []
        assert analysis.cached_metadata(restored, path.stat())["ffprobe"] == metadata()["ffprobe"]
        assert probe.call_count == 3
        requested.assert_called_once()


def test_an_index_in_flight_cannot_replace_the_cache_admitted_after_a_remount(
    source_db, tmp_path, probe, offline_movies, monkeypatch,
):
    from media_sources import analysis, identity, indexer, repository, scanner

    owner, library, root = add_source(source_db, tmp_path)
    stamped_video(root / "Movie.2001.mkv")
    scanner.run_scan(owner, library)
    (before,) = repository.list_items(library_id=library)
    stamp = identity.stored_content_stamp(before)
    new_device = str(int(stamp[0]) + 1)
    cache = analysis.stamp_index(analysis.read_metadata(before.ffprobe_cache), (new_device, *stamp[1:]))
    transaction = indexer.source_transaction

    @contextmanager
    def admit_remount_before_lock(target):
        repository.update_item(before.id, target, file_dev=new_device, ffprobe_cache=cache)
        with transaction(target) as connection:
            yield connection

    monkeypatch.setattr(indexer, "source_transaction", admit_remount_before_lock)
    assert indexer.index_item(before.id) is None
    after = repository.get_item(before.id)
    assert after.file_dev == new_device and after.ffprobe_cache == cache
    assert (after.indexed_at, after.subtitles, after.audio_language) == (
        before.indexed_at, before.subtitles, before.audio_language)


def test_handover_of_a_replaced_file_discards_its_previous_tracks(source_db, tmp_path, probe, offline_movies):
    from media_sources import indexer, repository, scanner

    profile = add_profile(source_db, [("fr", "True", "False")])
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=profile)
    path = stamped_video(root / "Film.2001.mkv")
    scanner.run_scan(source_id, library_id)
    (before,) = repository.list_items(library_id=library_id)
    assert ast.literal_eval(before.missing_subtitles) == []
    assert before.ffprobe_cache
    repository.update_library(library_id, source_db, enabled=0)
    receiving_source = repository.create_source(kind="folder", name="Receiving", target=source_db)
    receiving_library = repository.create_library(
        source_id=receiving_source, content_kind="movie", locator=str(root), profile_id=profile,
        target=source_db)

    replace_preserving_size_and_mtime(path)
    probe.return_value = None
    (scan,) = scanner.run_scan(receiving_source, receiving_library)
    assert scan.handed_over == 1
    after = repository.get_item(before.id)
    assert after.library_id == receiving_library
    assert after.ffprobe_cache is None and after.audio_language is None
    assert ast.literal_eval(after.missing_subtitles) == ["fr:forced"]

    indexer.recalculate_missing_subtitles(library_id=receiving_library)
    after_refresh = repository.get_item(before.id)
    assert ast.literal_eval(after_refresh.subtitles) == []
    assert ast.literal_eval(after_refresh.missing_subtitles) == ["fr:forced"]


def test_new_subtitle_index_survives_an_overlapping_scanner_index(source_db, tmp_path, probe, monkeypatch):
    from media_sources import indexer, repository
    from subtitles.tools.subsync_engines import subtitle_write_lock, subtitle_write_locks
    from subzero.language import Language

    source_id, library_id, root = add_source(source_db, tmp_path)
    path = stamped_video(root / "Film.2001.mkv")
    item_id = add_item(source_db, source_id, library_id, path)
    sidecar = root / "Film.2001.en.srt"
    monkeypatch.setattr(indexer, "get_language_set", lambda: {Language("eng")})
    scanner_probing, output_probing = Event(), Event()
    release_scan, release_output = Event(), Event()
    scanner_publishing, allow_publish = Event(), Event()
    write = repository.write_item_index

    def wait_for_writes(*args, **kwargs):
        if not scanner_probing.is_set():
            scanner_probing.set()
            assert release_scan.wait(5)
        else:
            output_probing.set()
            assert release_output.wait(5)
        return metadata()

    probe.side_effect = wait_for_writes

    def observe_publish(*args, **kwargs):
        if not scanner_publishing.is_set():
            scanner_publishing.set()
            assert allow_publish.wait(5)
        return write(*args, **kwargs)

    monkeypatch.setattr(repository, "write_item_index", observe_publish)
    sidecar_lock = subtitle_write_lock(str(path), str(root))
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            scan = executor.submit(indexer.index_item, item_id)
            assert scanner_probing.wait(5)
            with subtitle_write_locks(str(path), str(sidecar)):
                sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish\n")
            output = executor.submit(indexer.index_item, item_id)
            assert output_probing.wait(5)
            release_scan.set()
            assert scanner_publishing.wait(5)
            acquired = sidecar_lock.lock.acquire(blocking=False)
            if acquired:
                sidecar_lock.lock.release()
            assert not acquired, "The scanner must hold the subtitle lock through publication"
            allow_publish.set()
            assert scan.result(timeout=5) is not None
            release_output.set()
            assert output.result(timeout=5) is not None
        finally:
            release_scan.set()
            release_output.set()
            allow_publish.set()
    assert [entry[1] for entry in ast.literal_eval(repository.get_item(item_id).subtitles) if entry[1]] == [
        str(sidecar)]


def test_overlapping_library_edits_recalculate_the_final_profile(
    source_db, tmp_path, probe, refresh_queue, monkeypatch,
):
    from media_sources import indexer, repository, service

    original = add_profile(source_db, [("en", "False", "False")])
    intervening = add_profile(source_db, [("de", "False", "False")], profile_id=2)
    owner, library_id, root = add_source(source_db, tmp_path, profile_id=original)
    item_id = add_item(source_db, owner, library_id, stamped_video(root / "Movie.2001.mkv"))
    indexer.index_item(item_id)
    update = repository.update_library

    def concurrent_write(lib_id, target=None, **values):
        update(lib_id, target, profile_id=intervening)
        indexer.recalculate_missing_subtitles(library_id=lib_id)
        assert ast.literal_eval(repository.get_item(item_id).missing_subtitles) == ["de"]
        return update(lib_id, target, **values)

    monkeypatch.setattr(repository, "update_library", concurrent_write)
    service.update_library(owner, library_id, {"profile_id": original})
    assert run_next_refresh(refresh_queue) == {"library_id": library_id}
    assert ast.literal_eval(repository.get_item(item_id).missing_subtitles) == ["en"]
