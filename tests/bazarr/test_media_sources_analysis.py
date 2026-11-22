"""Track metadata, scoring and profile refresh for standalone source items."""

import ast
import json
import shutil
import subprocess
from datetime import timedelta
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
            {"language": "spa", "format": "ASS", "name": "Spanish SDH", "hearing_impaired": True},
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


def test_probe_and_profile_filter_use_the_same_tracks(source_db, tmp_path, probe):
    from media_sources import indexer, repository
    from app.database import TableLanguagesProfiles, update_profile_id_list

    profile = add_profile(source_db, [
        ("en", "False", "False"), ("fr", "True", "False"), ("es", "False", "False"),
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
    assert ast.literal_eval(row.missing_subtitles) == ["es"]
    assert ast.literal_eval(row.audio_language) == ["English", None]
    assert (row.video_codec, row.audio_codec, row.resolution) == ("H.264", "AAC", "720p")
    assert row.ffprobe_cache
    indexer.index_item(item_id)
    probe.assert_called_once()


def test_replacement_reprobes_and_a_rename_keeps_the_cache(source_db, tmp_path, probe, monkeypatch):
    from discover import metadata as discover_metadata
    from media_sources import repository, scanner

    monkeypatch.setattr(discover_metadata, "search_movies",
                        lambda *_args, **_kwargs: {"data": {"status": "available", "items": []}})
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

    stamped_video(moved, size=before.file_size)
    probe.return_value = metadata(codec="H.265", audio="deu")
    scanner.run_scan(source_id, library_id)
    replaced = repository.get_item(before.id)
    assert replaced.ffprobe_cache != before.ffprobe_cache
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
    from app.config import settings
    from media_sources import indexer, repository
    from subtitles.indexer import missing_refresh
    from test_settings_profile_deletion import _profile_payload, _settings_endpoint

    profile = add_profile(source_db, [("en", "False", "False"), ("fr", "True", "False")])
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=profile)
    path = stamped_video(root / "Local.2001.mkv")
    item_id = add_item(source_db, source_id, library_id, path)
    indexer.index_item(item_id)
    before = repository.get_item(item_id)
    assert ast.literal_eval(before.missing_subtitles) == ["en"]
    monkeypatch.setattr(indexer, "local_stat", lambda *_: pytest.fail("A profile refresh must not read the media"))

    post_settings = _settings_endpoint(source_db, monkeypatch)
    endpoint = sys.modules["api.system.settings"]
    monkeypatch.setattr(endpoint, "queue_missing_subtitles_recalculation",
                        missing_refresh.queue_missing_subtitles_recalculation)
    monkeypatch.setattr(endpoint, "clear_failed_translations", lambda: None)

    def save_options(fields):
        values = dict(fields)
        if "settings-general-use_embedded_subs" in values:
            settings.general.use_embedded_subs = values["settings-general-use_embedded_subs"] == ["true"]

    monkeypatch.setattr(endpoint, "save_settings", save_options)
    assert post_settings({"settings-general-use_embedded_subs": "false"}) == ("", 204)
    assert run_next_refresh(refresh_queue) == {}
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
    assert run_next_refresh(refresh_queue) == {}
    row = repository.get_item(item_id)
    assert ast.literal_eval(row.missing_subtitles) == []
    assert row.subtitles == before.subtitles
    assert row.ffprobe_cache == before.ffprobe_cache and row.indexed_at == before.indexed_at
    probe.assert_called_once()


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
