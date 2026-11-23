"""A source item through the shared subtitle pipeline: the seam, the pool, the blacklist, sync and history."""
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from subliminal_patch.subtitle import Subtitle
from subzero.language import Language

from media_source_helpers import (ARR_AND_SPORTS_TABLES, add_group, add_item, add_profile, add_source, table_counts,
                                  video)

CONTENT = b"1\n00:00:01,000 --> 00:00:02,000\nDownloaded\n"


class FakeSubtitle(Subtitle):
    provider_name = "fake"
    id = "sub-1"

    def __init__(self, language=None, score=100):
        super().__init__(language or Language("eng"))
        self.content = CONTENT
        self.format = "srt"
        self.score = score
        self.matches = set()


@pytest.fixture
def world(source_db, tmp_path, monkeypatch, languages_loaded):
    """One folder source with a profile that wants English, and the pipeline's outer edges faked: providers,
    the media server publication and the webhook."""
    from app import event_handler, notifier
    from media_servers import dispatcher
    from media_sources import indexer, service
    from subtitles import download, pool, processing, sync

    profile = add_profile(source_db, [("en", "False", "False")])
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=profile)
    published = []
    events = []
    monkeypatch.setattr(event_handler.socketio, "emit", lambda event, payload: events.append((event, payload)))
    monkeypatch.setattr(notifier, "database", source_db)
    original_update_pool = pool._update_pool
    monkeypatch.setattr(dispatcher, "notify_subtitle_mutation", published.append)
    monkeypatch.setattr(indexer, "get_language_set", lambda: {Language("eng"), Language("hun")})
    monkeypatch.setattr(pool, "_update_pool", lambda *args, **kwargs: False)
    monkeypatch.setattr(download, "_get_pool", lambda *args, **kwargs: SimpleNamespace(
        providers=["fake"], discarded_providers=[]))
    monkeypatch.setattr(download.subliminal, "region", Mock())
    monkeypatch.setattr(processing, "_defaul_sync_checker", lambda subtitle: False)
    monkeypatch.setattr(processing, "call_external_webhook", Mock())
    monkeypatch.setattr(service, "get_providers", lambda: ["fake"])
    monkeypatch.setattr(sync, "_resolve_subsync_overrides", lambda *args, **kwargs: (True, False, 0, None, "60"))
    return SimpleNamespace(db=source_db, source_id=source_id, library_id=library_id, root=root, profile=profile,
                           published=published, events=events, tmp_path=tmp_path,
                           original_update_pool=original_update_pool)


def _movie(world, name="Film.2001.1080p.BluRay.x264-GRP.mkv"):
    from media_sources.context import resolve_item
    from media_sources.indexer import index_item

    path = video(world.root / name)
    item_id = add_item(world.db, world.source_id, world.library_id, path, title="Film", year="2001",
                       scene_name=path.stem, file_size=1000)
    index_item(item_id)
    return resolve_item(item_id)


def _episode(world):
    from media_sources.context import resolve_item
    from media_sources.indexer import index_item

    group = add_group(world.db, world.source_id, world.library_id, kind="show", title="Bluey", tvdb_id=387380,
                      imdb_id="tt7678620")
    path = video(world.root / "Bluey" / "Bluey.S01E02.1080p.WEB.h264-GRP.mkv")
    item_id = add_item(world.db, world.source_id, world.library_id, path, kind="episode", group_id=group,
                       title="Mum School", season=1, episode=2, scene_name=path.stem)
    index_item(item_id)
    return resolve_item(item_id)


def _sports(world):
    from media_sources.context import resolve_item
    from media_sources.indexer import index_item

    group = add_group(world.db, world.source_id, world.library_id, kind="league", title="Formula 1")
    path = video(world.root / "Formula 1" / "Formula.1.2024-05-26.Monaco.Grand.Prix.Race.1080p.mkv")
    item_id = add_item(world.db, world.source_id, world.library_id, path, kind="sports_event", group_id=group,
                       title="Monaco Grand Prix", part_name="Race", event_date="2024-05-26", year="2024",
                       scene_name=path.stem)
    index_item(item_id)
    return resolve_item(item_id)


def _download(monkeypatch, subtitle=None, minimum=None):
    """Make the pool hand back one subtitle for the video it was asked about, and remember the minimum score."""
    from subtitles import download

    asked = {}

    def best(videos, languages, min_score, **kwargs):
        asked["min_score"] = min_score
        (found,) = videos
        asked["video"] = found
        result = subtitle or FakeSubtitle()
        return {found: [result]} if result.language in languages else {}

    monkeypatch.setattr(download, "download_best_subtitles", best)
    return asked


def _generate(context, languages=(("en", "False", "False"),), **kwargs):
    from subtitles.download import generate_subtitles

    title = context.group_title if context.kind == "episode" else context.title
    return list(generate_subtitles(context.path, list(languages), "None", str(context.scene_name), title,
                                   context.media_type, context.profile_id, source=context, **kwargs))


# The whole flow

def test_a_search_saves_a_subtitle_next_to_the_file_and_writes_history_and_the_index(world, monkeypatch):
    from media_sources import repository, service
    from media_sources.context import SourceItemContext

    context = _movie(world)
    asked = _download(monkeypatch)
    jobs = []
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: jobs.append(kwargs))
    assert repository.get_item(context.item_id).missing_subtitles == "['en']"

    service.search_source_item(context.item_id, job_id=3)

    saved = context.path.replace(".mkv", ".en.srt")
    assert open(saved, "rb").read() == CONTENT
    item = repository.get_item(context.item_id)
    assert "Film.2001.1080p.BluRay.x264-GRP.en.srt" in item.subtitles and item.missing_subtitles == "[]"
    (row,) = world.db.execute(sa.select(repository.History)).all()
    assert (row.item_id, row.source_id, row.item_kind, row.action) == (context.item_id, world.source_id, "movie", 1)
    assert (row.language, row.provider, row.score, row.subs_id) == ("en", "fake", 100, "sub-1")
    assert (row.item_title, row.source_name, row.video_path, row.subtitles_path) == ("Film", "Local", context.path,
                                                                                      saved)
    assert jobs[-1]["progress_message"] == "1 subtitle(s) downloaded"
    # The provider saw a movie parsed from the path and filled from the item, with the item as its context.
    found = asked["video"]
    assert isinstance(found.source_context, SourceItemContext) and found.source_context.item_id == context.item_id
    assert (found.title, found.year) == ("Film", 2001)
    # The media servers heard one download, for a movie, with no owner.
    assert [(event.media_type, event.operation, event.arr_instance_id) for event in world.published] == [
        ("movie", "download", None)]
    assert world.published[0].video_path == context.path


def test_a_search_with_nothing_missing_or_no_profile_does_not_ask_a_provider(world, monkeypatch):
    from media_sources import repository, service

    context = _movie(world)
    asked = _download(monkeypatch)
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: None)
    repository.update_item(context.item_id, world.db, missing_subtitles="[]")
    service.search_source_item(context.item_id, job_id=3)
    other = _movie(world, "Other.2002.mkv")
    world.db.execute(sa.update(repository.Libraries).values(profile_id=None))
    service.search_source_item(other.item_id, job_id=4)
    assert asked == {}
    assert world.db.execute(sa.select(sa.func.count()).select_from(repository.History)).scalar_one() == 0


def test_the_search_job_queues_itself_and_fails_on_a_missing_item_or_file(world, monkeypatch):
    from app.jobs_queue import JobFailed
    from media_sources import service

    context = _movie(world)
    queued = []
    monkeypatch.setattr(service.jobs_queue, "add_job_from_function",
                        lambda name, is_progress: queued.append(name) or 12)
    assert service.search_source_item(context.item_id) == 12 and queued == ["Searching subtitles for Film"]
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: None)
    with pytest.raises(JobFailed):
        service.search_source_item(9999, job_id=1)
    import os
    os.unlink(context.path)
    with pytest.raises(JobFailed):
        service.search_source_item(context.item_id, job_id=1)


# Two keywords, never both

def test_a_call_with_both_a_sports_context_and_a_source_raises(world):
    from subtitles import pool
    from subtitles.processing import process_subtitle
    from subtitles.utils import get_video

    context = _movie(world)
    with pytest.raises(ValueError):
        get_video(context.path, "Film", "None", media_type="movie", context=object(), source=context)
    with pytest.raises(ValueError):
        pool._pool_key("movie", 1, object(), context)
    with pytest.raises(ValueError):
        process_subtitle(FakeSubtitle(), "sports", "None", context.path, 100, context=object(), source=context,
                         validate=lambda: None, publication_guard=object())
    with pytest.raises(ValueError):
        process_subtitle(FakeSubtitle(), "movie", "None", context.path, 100, context=object(), source=context)


def test_no_sportarr_function_receives_a_source_item(world, monkeypatch):
    """A source sports event is a source item, never a sports context. The Sportarr entry points that take a
    context are spied on through a whole search of a movie, an episode and a sports event."""
    import sportarr
    from sportarr import automatic, history, subtitles as sports_subtitles, video as sports_video, workflows
    from sportarr.sync import leagues

    seen = []

    def spy(owner, name):
        original = getattr(owner, name)

        def wrapper(*args, **kwargs):
            seen.append((owner.__name__, name))
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, name, wrapper)

    for owner, names in ((sports_subtitles, ("validate_context", "candidate_signature", "get_blacklist_sports",
                                             "sports_history")),
                         (sports_video, ("get_sports_video",)), (history, ("blacklist_log_sports",)),
                         (leagues, ("require_sportarr",)), (workflows, ("require_sports_enabled",))):
        for name in names:
            spy(owner, name)
    for name in ("generate_sports_subtitles", "search_sports_event"):
        if hasattr(automatic, name):
            spy(automatic, name)

    for context in (_movie(world), _episode(world), _sports(world)):
        _download(monkeypatch)
        result = _generate(context)
        assert len(result) == 1
    assert seen == []
    assert sportarr  # imported so the spies above point at the real modules


# The pool

def test_the_source_pool_has_its_own_key_per_kind_and_profile(world):
    from subtitles import pool

    movie, episode, event = _movie(world), _episode(world), _sports(world)
    keys = {pool._pool_key(c.media_type, c.profile_id, None, c) for c in (movie, episode, event)}
    assert keys == {f"source_movie_{world.profile}", f"source_episode_{world.profile}",
                    f"source_sports_event_{world.profile}"}
    # The arr keys are what they were.
    assert pool._pool_key("movie", 1, None) == "movie_1" and pool._pool_key("series", None, None) == "series_"


def test_the_source_pool_loads_only_its_own_blacklist(world, monkeypatch):
    from media_sources import history
    from subtitles import pool

    movie, episode = _movie(world), _episode(world)
    history.blacklist_log_source(movie, "alpha", "movie-release", "en")
    history.blacklist_log_source(episode, "beta", "episode-release", "en")
    spies = []
    for owner, name in ((pool, "get_blacklist"), (pool, "get_blacklist_movie")):
        monkeypatch.setattr(owner, name, lambda *a, **k: spies.append(name) or [("arr", "row")])

    assert pool._blacklist("movie", None, movie) == [("alpha", "movie-release")]
    assert pool._blacklist("series", None, episode) == [("beta", "episode-release")]
    assert spies == []
    # And the arr blacklists never see a source row.
    assert pool._blacklist("movie") == [("arr", "row")] and spies == ["get_blacklist_movie"]


def test_the_source_pool_is_refreshed_by_update_pools(world, monkeypatch):
    from media_sources import history
    from subtitles import pool

    movie = _movie(world)
    history.blacklist_log_source(movie, "alpha", "release-1", "en")
    updates = []

    class RecordingPool:
        def update(self, providers, auth, blacklist, ban_list, language_equals):
            updates.append(blacklist)
            return True

    monkeypatch.setattr(pool, "get_providers_sorted", lambda: ["fake"])
    monkeypatch.setattr(pool, "get_providers_auth", lambda: {})
    monkeypatch.setattr(pool, "get_ban_list", lambda profile_id: {})
    monkeypatch.setattr(pool, "get_language_equals", lambda: {})
    monkeypatch.setattr(pool, "_init_pool", lambda *args, **kwargs: RecordingPool())
    monkeypatch.setattr(pool, "_pools", {})
    monkeypatch.setattr(pool, "_update_pool", world.original_update_pool)

    @pool.update_pools
    def search(path, media_type, profile_id, source=None):
        return "ran"

    assert search(movie.path, "movie", world.profile, source=movie) == "ran"

    assert list(pool._pools) == [f"source_movie_{world.profile}"]
    assert updates == [[("alpha", "release-1")]]


# A provider asking for a release to be blacklisted

def _blacklist_rows(world):
    from media_sources import repository
    return world.db.execute(sa.select(repository.Blacklist)).all()


@pytest.mark.parametrize("operation", ["search", "download"])
def test_an_mgb_on_a_source_search_or_download_writes_the_source_blacklist_and_no_arr_row(
        world, monkeypatch, operation):
    from app import get_providers
    from subliminal_patch import core
    from subtitles import pool  # noqa: F401  (the app's pool wiring)

    movie = _movie(world)
    language = Language("eng")
    error = core.MustGetBlacklisted("release-9", "movie")

    class Provider:
        languages = {language}

        def __init__(self, **kwargs):
            pass

        @classmethod
        def check(cls, video):
            return True

        def initialize(self):
            pass

        def terminate(self):
            pass

        def list_subtitles(self, video, languages):
            raise error

        def download_subtitle(self, subtitle):
            raise error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    arr_calls = []
    monkeypatch.setattr(get_providers, "blacklist_log_movie", lambda *a, **k: arr_calls.append(("movie", a)))
    monkeypatch.setattr(get_providers, "blacklist_log", lambda *a, **k: arr_calls.append(("series", a)))
    before = table_counts(world.db, ARR_AND_SPORTS_TABLES)
    sports_pool = core.SZProviderPool(["alpha"], {}, throttle_callback=get_providers.provider_throttle)
    carrier = SimpleNamespace(radarrId=None, sonarrSeriesId=None, sonarrEpisodeId=None, arr_instance_id=None,
                              sports_context=None, source_context=movie, fps=None)
    if operation == "search":
        sports_pool.list_subtitles_provider("alpha", carrier, {language}, detailed=True)
    else:
        subtitle = SimpleNamespace(provider_name="alpha", language=language, radarrId=None, sonarrSeriesId=None,
                                   sonarrEpisodeId=None, arr_instance_id=None, sports_context=None,
                                   source_context=movie)
        assert sports_pool.download_subtitle(subtitle) is False

    (row,) = _blacklist_rows(world)
    assert (row.provider, row.subs_id, row.language, row.item_kind) == ("alpha", "release-9", "en", "movie")
    assert (row.item_id, row.source_id, row.video_path, row.item_title, row.source_name) == (
        movie.item_id, world.source_id, movie.path, "Film", "Local")
    assert arr_calls == []
    assert table_counts(world.db, ARR_AND_SPORTS_TABLES) == before


def test_an_unattributed_arr_mgb_still_goes_to_the_arr_branch(world, monkeypatch):
    """The source arm is taken only for a source context: the arr and sports branches are unchanged."""
    from app import get_providers

    calls = []
    monkeypatch.setattr(get_providers, "blacklist_log_movie", lambda *a, **k: calls.append(a))
    get_providers._handle_mgb("alpha", SimpleNamespace(id="r", media_type="movie"),
                              {"radarrId": 7, "sonarrSeriesId": None, "sonarrEpisodeId": None}, Language("eng"))
    assert calls and _blacklist_rows(world) == []


def test_a_throttle_callback_that_predates_sources_is_not_given_the_keyword(monkeypatch):
    from subliminal_patch import core

    class Provider:
        languages = {Language("eng")}

        def __init__(self, **kwargs):
            pass

        @classmethod
        def check(cls, video):
            return True

        def initialize(self):
            pass

        def terminate(self):
            pass

        def list_subtitles(self, video, languages):
            raise OSError("down")

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    calls = []

    def legacy(name, exc, ids=None, language=None, sports_context=None):
        calls.append((name, sports_context))

    def kwargs_callback(name, exc, **context):
        calls.append(sorted(context))

    for callback in (legacy, kwargs_callback):
        calls.clear()
        pool = core.SZProviderPool(["alpha"], {}, throttle_callback=callback)
        carrier = SimpleNamespace(sports_context=None, source_context="item", fps=None)
        pool.list_subtitles_provider("alpha", carrier, {Language("eng")}, detailed=True)
        assert len(calls) == 1
        if callback is kwargs_callback:
            assert "source_context" in calls[0]
        else:
            assert calls == [("alpha", None)]
        calls.clear()
        plain = SimpleNamespace(sports_context=None, fps=None)
        pool = core.SZProviderPool(["alpha"], {}, throttle_callback=callback)
        pool.list_subtitles_provider("alpha", plain, {Language("eng")}, detailed=True)
        if callback is kwargs_callback:
            assert "source_context" not in calls[0]


# The video

def test_the_source_video_skips_the_arr_refiners_and_takes_its_fields_from_the_item(world, monkeypatch):
    from subtitles import utils

    movie, episode, event = _movie(world), _episode(world), _sports(world)
    ran = []
    monkeypatch.setattr(utils, "registered_refiners", {
        key: (lambda path, video, key=key: ran.append(key)) for key in
        ("database", "ffprobe", "arr_history", "anidb", "anilist")})

    found = utils.get_video(movie.path, movie.title, "None", media_type="movie", source=movie)
    assert (found.title, found.year, found.source_context) == ("Film", 2001, movie)
    assert ran == []

    found = utils.get_video(episode.path, episode.group_title, "None", media_type="series", source=episode)
    assert (found.series, found.season, found.episode, found.title) == ("Bluey", 1, 2, "Mum School")
    assert (found.series_tvdb_id, found.series_imdb_id, found.source_context) == (387380, "tt7678620", episode)
    # AniDB and AniList still run for episodes, in that order, and nothing else does.
    assert ran == ["anidb", "anilist"]

    ran.clear()
    found = utils.get_video(event.path, event.title, "None", media_type="movie", source=event)
    assert (found.title, found.year, found.imdb_id, found.source_context) == ("Monaco Grand Prix Race", 2024, None,
                                                                              event)
    assert found.size == 1000 and ran == []


def test_the_source_video_survives_a_scene_name_it_cannot_parse(world):
    from subtitles import utils

    movie = _movie(world)
    found = utils.get_video(movie.path, movie.title, "%%%not a release%%%", media_type="movie", source=movie)
    assert found is not None and found.title == "Film"


# Scoring and publication

def test_a_sports_event_is_scored_as_a_movie_and_held_to_the_sports_minimum(world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, "minimum_score_movie", 70)
    monkeypatch.setattr(settings.general, "minimum_score_sports", 55)
    movie, event = _movie(world), _sports(world)
    asked = _download(monkeypatch)
    _generate(movie)
    movie_minimum = asked["min_score"]
    _generate(event)
    assert movie_minimum != asked["min_score"]
    from subliminal_patch.score import MAX_SCORES
    assert asked["min_score"] == int(MAX_SCORES["movie"] * 55 / 100)
    assert movie_minimum == int(MAX_SCORES["movie"] * 70 / 100)


@pytest.mark.parametrize("make, publication", [(_movie, "movie"), (_episode, "episode"), (_sports, "sports")])
def test_each_kind_of_source_item_publishes_with_its_own_type(world, monkeypatch, make, publication):
    context = make(world)
    _download(monkeypatch)
    assert len(_generate(context)) == 1
    assert [(event.media_type, event.operation, event.arr_instance_id) for event in world.published] == [
        (publication, "download", None)]


def test_a_source_search_never_reports_or_clears_a_release_type_mismatch(world, monkeypatch):
    from subtitles import download

    context = _movie(world)
    report, clear = Mock(), Mock()
    monkeypatch.setattr(download, "report_release_type_mismatch", report)
    monkeypatch.setattr(download, "clear_mismatch_for_video", clear)
    _download(monkeypatch)
    _generate(context)
    monkeypatch.setattr(download, "download_best_subtitles", lambda videos, **kwargs: {next(iter(videos)): []})
    _generate(context)
    report.assert_not_called()
    clear.assert_not_called()


def test_check_if_still_required_reads_the_items_missing_languages(world, monkeypatch):
    from media_sources import repository

    context = _movie(world)
    asked = _download(monkeypatch)
    repository.update_item(context.item_id, world.db, missing_subtitles="[]")
    # The language the caller asks for was satisfied meanwhile, so the provider is not asked.
    assert _generate(context, check_if_still_required=True) == []
    assert "video" not in asked
    repository.update_item(context.item_id, world.db, missing_subtitles="['en']")
    assert len(_generate(context, check_if_still_required=True)) == 1


# Processing

def test_an_item_deleted_between_the_search_and_the_finalize_logs_one_line_and_keeps_the_file(
        world, monkeypatch, caplog):
    from media_sources import repository
    from subtitles.processing import process_subtitle

    context = _movie(world)
    subtitle = FakeSubtitle()
    subtitle.storage_path = context.path.replace(".mkv", ".en.srt")
    open(subtitle.storage_path, "wb").write(CONTENT)
    repository.delete_items([context.item_id], world.db)

    with caplog.at_level(logging.INFO):
        result = process_subtitle(subtitle, "movie", "None", context.path, 100, source=context)

    assert result is None
    lines = [record for record in caplog.records if record.levelno == logging.INFO
             and "was deleted while its subtitles were being fetched" in record.getMessage()]
    assert len(lines) == 1 and str(context.item_id) in lines[0].getMessage()
    assert open(subtitle.storage_path, "rb").read() == CONTENT


def test_processing_a_source_subtitle_returns_the_result_without_mapping_or_arr_calls(world, monkeypatch):
    from subtitles import processing

    context = _movie(world)
    subtitle = FakeSubtitle()
    subtitle.storage_path = context.path.replace(".mkv", ".en.srt")
    open(subtitle.storage_path, "wb").write(CONTENT)
    for name in ("notify_radarr", "notify_sonarr", "_trigger_auto_translation", "_trigger_combine"):
        monkeypatch.setattr(processing, name, Mock())
    mapped = Mock()
    monkeypatch.setattr(processing.path_mappings, "path_replace_reverse_instance", mapped)
    monkeypatch.setattr(processing.path_mappings, "path_replace_reverse_movie", mapped)
    monkeypatch.setattr(processing.path_mappings, "path_replace_reverse", mapped)

    result = processing.process_subtitle(subtitle, "movie", "None", context.path, 100, source=context)

    assert (result.path, result.subs_path, result.language_code, result.provider) == (
        context.path, subtitle.storage_path, "en", "fake")
    mapped.assert_not_called()
    for name in ("notify_radarr", "notify_sonarr"):
        getattr(processing, name).assert_not_called()
    for name in ("_trigger_auto_translation", "_trigger_combine"):
        assert getattr(processing, name).call_args.kwargs["source"] == context


# Sync

def _fake_sync(monkeypatch, output):
    from subtitles import sync
    from subtitles.tools.subsync_engines import RESULT_SUCCESS, SyncEngineResult

    sync_result = Mock(success=True, output_mode="overwrite")
    sync_result.successful_results = [SyncEngineResult(engine="ffsubsync", status=RESULT_SUCCESS,
                                                       output_path=output, raw_result={"offset_seconds": 2.0})]
    sync_result.results = sync_result.successful_results
    sync_result.skipped_results = []
    sync_result.failed_results = []
    calls = []

    class FakeSubSyncer:
        def sync(self, **kwargs):
            calls.append(kwargs)
            return sync_result

    monkeypatch.setattr(sync, "SubSyncer", FakeSubSyncer)
    return calls


@pytest.mark.parametrize("make, series, publication", [(_movie, False, "movie"), (_episode, True, "episode"),
                                                       (_sports, False, "sports")])
def test_a_source_sync_takes_its_thresholds_and_publication_from_the_items_kind(
        world, monkeypatch, make, series, publication):
    from subtitles import sync

    context = make(world)
    seen = []
    monkeypatch.setattr(sync, "_resolve_subsync_overrides",
                        lambda owner, is_series, engines, offset: seen.append((owner, is_series)) or (
                            True, False, 0, None, "60"))
    published = []
    monkeypatch.setattr(sync, "publication_callback",
                        lambda *args, **kwargs: published.append((args, kwargs)) or (lambda path: None))
    calls = _fake_sync(monkeypatch, context.path.replace(".mkv", ".en.synced.srt"))

    assert sync.sync_subtitles(video_path=context.path, srt_path=context.path.replace(".mkv", ".en.srt"),
                               srt_lang="en", forced=False, hi=False, percent_score=100, force_sync=True,
                               track_job_progress=False, source=context) is True

    assert seen and all(entry == (None, series) for entry in seen)
    assert published == [((publication, context.path, "sync", None), {"source": context})]
    assert calls[0]["source"] is context and calls[0]["sonarr_episode_id"] is None


def test_a_source_sync_writes_its_history_to_the_source_table(world, monkeypatch):
    from media_sources import repository
    from subtitles.tools import subsyncer

    context = _movie(world)
    original = Path(context.path).with_suffix(".en.srt")
    original.write_bytes(CONTENT)
    output = original.with_suffix(".alass.srt")
    arr_calls = []

    def align(self, engine, output_path, video_path, no_fix_framerate):
        output_path.write_bytes(CONTENT)
        return {"offset_seconds": 1.5, "framerate_scale_factor": 1.0, "quality_of_fit": 0.9}

    monkeypatch.setattr(subsyncer.SubSyncer, "_run_external_engine", align)
    monkeypatch.setattr(subsyncer, "history_log_movie", lambda *a, **k: arr_calls.append(a))
    monkeypatch.setattr(subsyncer, "history_log", lambda *a, **k: arr_calls.append(a))
    before = table_counts(world.db, ARR_AND_SPORTS_TABLES)

    result = subsyncer.SubSyncer().sync(
        video_path=context.path, srt_path=str(original), srt_lang="en", hi=False, forced=False,
        max_offset_seconds="60", no_fix_framerate=True, gss=True, force_sync=True,
        output_mode="keep_all", enabled_engines=["alass"], source=context)

    assert result.success and output.read_bytes() == CONTENT
    (row,) = world.db.execute(sa.select(repository.History)).all()
    assert (row.action, row.item_id, row.source_id, row.item_kind, row.language) == (5, context.item_id,
                                                                                    world.source_id, "movie", "en")
    assert (row.video_path, row.subtitles_path, row.source_name) == (context.path, str(output), "Local")
    assert "offset of 1.5 seconds" in row.description
    assert arr_calls == [] and table_counts(world.db, ARR_AND_SPORTS_TABLES) == before


def test_a_source_sync_that_keeps_every_output_indexes_the_item(world, monkeypatch):
    from media_sources import indexer
    from subtitles import sync

    context = _movie(world)
    indexed = []
    monkeypatch.setattr(indexer, "index_item", lambda item_id, *a: indexed.append(item_id))
    sync._index_keep_all_outputs(context.path, source=context)
    assert indexed == [context.item_id]


# Nothing of the arr is read or written

def test_the_source_path_never_calls_get_profile_id_and_never_writes_an_arr_or_sports_table(world, monkeypatch):
    from app import database as db
    from media_sources import service

    movie, episode, match = _movie(world), _episode(world), _sports(world)
    monkeypatch.setattr(db, "get_profile_id", Mock(side_effect=AssertionError("get_profile_id was called")))
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: None)
    statements = []
    before = table_counts(world.db, ARR_AND_SPORTS_TABLES)

    @event.listens_for(world.db.get_bind(), "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        for item in (movie, episode, match):
            _download(monkeypatch)
            service.search_source_item(item.item_id, job_id=5)
    finally:
        event.remove(world.db.get_bind(), "before_cursor_execute", record)

    assert table_counts(world.db, ARR_AND_SPORTS_TABLES) == before
    writes = [statement for statement in statements if re.match(r"\s*(INSERT|UPDATE|DELETE)", statement, re.I)]
    assert writes
    arr = "|".join(ARR_AND_SPORTS_TABLES)
    assert not [statement for statement in writes if re.search(rf'\b("?)({arr})\1(\s|\()', statement)]
    assert not [statement for statement in statements if re.search(r"table_(shows|episodes|movies)\b", statement)]


# The missing languages of an item

def _profile(items, cutoff=None):
    rows = [{"language": language, "forced": forced, "hi": hi} for language, forced, hi in items]
    return {"items": rows}


@pytest.mark.parametrize("items, cutoff, listing, expected", [
    # Everything wanted, nothing there.
    ([("en", "False", "False"), ("hu", "False", "False")], None, [], ["en", "hu"]),
    # An English subtitle on disk satisfies English.
    ([("en", "False", "False"), ("hu", "False", "False")], None, [["en", "/a.en.srt", 5]], ["hu"]),
    # A forced file does not satisfy a normal one, a normal one does not satisfy a forced one.
    ([("en", "False", "False")], None, [["en:forced", "/a.en.forced.srt", 5]], ["en"]),
    ([("en", "True", "False")], None, [["en", "/a.en.srt", 5]], ["en:forced"]),
    # A hearing-impaired subtitle counts for the normal language.
    ([("en", "False", "False")], None, [["en:hi", "/a.en.hi.srt", 5]], []),
    ([("en", "False", "True")], None, [["en", "/a.en.srt", 5]], ["en:hi"]),
    # Once the cutoff language is there, nothing more is wanted.
    ([("en", "False", "False"), ("hu", "False", "False")], {"language": "en", "forced": "False", "hi": "False"},
     [["en", "/a.en.srt", 5]], []),
    ([("en", "False", "False"), ("hu", "False", "False")], {"language": "en", "forced": "False", "hi": "False"},
     [["en:hi", "/a.en.hi.srt", 5]], []),
    ([("en", "False", "False"), ("hu", "False", "False")], {"language": "hu", "forced": "False", "hi": "False"},
     [["en", "/a.en.srt", 5]], ["hu"]),
    # A combined artifact is not a subtitle of its base language.
    ([("en", "False", "False")], None, [["en:combined-hu", "/a.combined.srt", 5]], ["en"]),
    # An embedded track (no path) counts only when embedded subtitles are used.
    ([("en", "False", "False")], None, [["en", None, None]], []),
])
def test_missing_subtitles_follow_the_profile_cutoff_forced_and_hi(items, cutoff, listing, expected):
    from media_sources.indexer import compute_missing_subtitles

    result = compute_missing_subtitles(_profile(items), [cutoff] if cutoff else [], listing, use_embedded_subs=True)
    assert result == expected


def test_embedded_tracks_do_not_count_when_embedded_subtitles_are_off():
    from media_sources.indexer import compute_missing_subtitles

    profile = _profile([("en", "False", "False")])
    assert compute_missing_subtitles(profile, [], [["en", None, None]], use_embedded_subs=False) == ["en"]


def test_audio_flags_are_applied_the_way_an_arr_movie_without_audio_information_gets_them():
    from media_sources.indexer import compute_missing_subtitles

    profile = {"items": [
        {"language": "en", "forced": "False", "hi": "False", "audio_exclude": "True"},
        {"language": "hu", "forced": "False", "hi": "False", "audio_only_include": "True"},
        {"language": "de", "forced": "False", "hi": "False"}]}
    assert compute_missing_subtitles(profile, [], [], use_embedded_subs=True) == ["en", "de"]
    assert compute_missing_subtitles(profile, [], [], matches_audio=lambda language: True,
                                     use_embedded_subs=True) == ["hu", "de"]


def test_a_language_the_search_has_given_up_on_is_not_listed_as_missing(monkeypatch):
    from media_sources import indexer

    monkeypatch.setattr(indexer, "is_search_given_up", lambda language, attempts: language == "hu")
    profile = _profile([("en", "False", "False"), ("hu", "False", "False")])
    assert indexer.compute_missing_subtitles(profile, [], [], failed_attempts="x", use_embedded_subs=True) == ["en"]


def test_indexing_an_item_whose_row_is_gone_logs_a_warning_and_a_missing_file_changes_nothing(
        world, caplog, tmp_path):
    from media_sources import indexer, repository

    context = _movie(world)
    with caplog.at_level(logging.WARNING):
        assert repository.write_item_index(99999, "[]", "[]") is False
    assert "changed 0 rows instead of one" in caplog.text
    assert indexer.index_item(99999) is None
    import os
    os.unlink(context.path)
    before = repository.get_item(context.item_id)
    assert indexer.index_item(context.item_id) is None
    assert repository.get_item(context.item_id).indexed_at == before.indexed_at


# A custom subtitle folder

def test_indexing_with_a_custom_subtitle_folder_reads_no_arr_table_and_keeps_a_sync_output(
        world, monkeypatch, tmp_path):
    from app.config import settings
    from media_sources import indexer, repository

    subtitles = tmp_path / "subtitles"
    subtitles.mkdir()
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(subtitles))
    monkeypatch.setattr(indexer, "get_target_folder", lambda path, create=False: str(subtitles))
    context = _movie(world)
    base = context.path.rsplit("/", 1)[1].replace(".mkv", "")
    (subtitles / f"{base}.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    (subtitles / f"{base}.en.ffsubsync.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nSynced\n")
    statements = []

    @event.listens_for(world.db.get_bind(), "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        indexer.index_item(context.item_id)
    finally:
        event.remove(world.db.get_bind(), "before_cursor_execute", record)

    assert not [s for s in statements if re.search(
        r"table_(shows|episodes|movies|sports_events)\b|arr_instances", s)]
    listing = repository.get_item(context.item_id).subtitles
    assert f"{base}.en.srt" in listing
    # A generated output of this item's own file is counted, which the arr ownership lookup would not have known.
    assert f"{base}.en.ffsubsync.srt" in listing


def test_the_source_ownership_index_only_claims_items_of_the_source_tables(world, tmp_path):
    from media_sources.indexer import SourceOutputOwnerIndex

    first, second = _movie(world), _movie(world, "Other.2002.mkv")
    owners = SourceOutputOwnerIndex._load_owners()
    claimed = {path for paths in owners.values() for path in paths}
    assert claimed == {first.path, second.path}


# Both keywords on the sync entry point, and the webhook's media type

def test_a_sync_with_both_a_sports_context_and_a_source_raises(world):
    from subtitles import sync

    context = _movie(world)
    with pytest.raises(ValueError):
        sync.sync_subtitles(video_path=context.path, srt_path=context.path.replace(".mkv", ".en.srt"),
                            srt_lang="en", forced=False, hi=False, percent_score=100, context=object(),
                            validate=lambda: None, publication_guard=object(), source=context,
                            track_job_progress=False)


@pytest.mark.parametrize("make, expected", [(_movie, "movie"), (_episode, "series"), (_sports, "sports")])
def test_the_webhook_hears_the_publication_type_of_a_source_item(world, monkeypatch, make, expected):
    from subtitles import processing

    context = make(world)
    subtitle = FakeSubtitle()
    subtitle.storage_path = context.path.replace(".mkv", ".en.srt")
    open(subtitle.storage_path, "wb").write(CONTENT)
    webhook = Mock()
    monkeypatch.setattr(processing, "call_external_webhook", webhook)
    processing.process_subtitle(subtitle, context.media_type, "None", context.path, 100, source=context)
    assert webhook.call_args.kwargs["media_type"] == {"movie": "movie", "series": "episode",
                                                      "sports": "sports"}[expected]


# The sync path asks the source tables, not the arr tables, who owns a generated output

def _custom_folder(world, monkeypatch, tmp_path):
    from app.config import settings
    from media_sources import indexer

    subtitles = tmp_path / "subtitles"
    subtitles.mkdir()
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(subtitles))
    monkeypatch.setattr(indexer, "get_target_folder", lambda path, create=False: str(subtitles))
    return subtitles


def _statements(world):
    statements = []

    @event.listens_for(world.db.get_bind(), "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    return statements, lambda: event.remove(world.db.get_bind(), "before_cursor_execute", record)


ARR_READ = re.compile(r"table_(shows|episodes|movies|sports_events)\b|arr_instances")


def test_a_source_publication_proves_ownership_from_the_source_tables_only(world, monkeypatch, tmp_path):
    from media_sources.indexer import SourceOutputOwnerIndex
    from subtitles.tools.subsync_engines import SubtitlePublication, subtitle_source_version

    subtitles = _custom_folder(world, monkeypatch, tmp_path)
    context = _movie(world)
    source = subtitles / (context.path.rsplit("/", 1)[1].replace(".mkv", ".en.srt"))
    source.write_text(CONTENT.decode())
    statements, stop = _statements(world)
    try:
        owned = SubtitlePublication(context.path, str(source), subtitle_source_version(str(source)),
                                    ownership_index=SourceOutputOwnerIndex())
        # Without the source index the arr tables are asked, and a source item owns nothing there.
        arr_way = SubtitlePublication(context.path, str(source), subtitle_source_version(str(source)))
    finally:
        stop()
    assert owned.owns_destinations is True and arr_way.owns_destinations is False
    assert any(ARR_READ.search(s) for s in statements)
    assert not any(ARR_READ.search(s) for s in statements[:statements.index(
        next(s for s in statements if ARR_READ.search(s)))])


def test_the_syncer_hands_a_source_ownership_index_to_the_publication_and_the_cleanup(world, monkeypatch, tmp_path):
    from media_sources.indexer import SourceOutputOwnerIndex
    from subtitles.tools import subsyncer

    subtitles = _custom_folder(world, monkeypatch, tmp_path)
    context = _movie(world)
    base = context.path.rsplit("/", 1)[1].replace(".mkv", "")
    source = subtitles / f"{base}.en.srt"
    source.write_text(CONTENT.decode())
    stale = subtitles / f"{base}.en.ffsubsync.srt"
    stale.write_text(CONTENT.decode())
    seen = {}

    class Publication:
        def __init__(self, video_path, srt_path, version, **kwargs):
            seen["publication"] = kwargs
            self.state = None

        def release(self):
            pass

    class Runner:
        def run(self, **kwargs):
            seen["after_publish"] = kwargs["after_publish"]
            kwargs["after_publish"]()
            raise RuntimeError("stop after the cleanup")

    monkeypatch.setattr(subsyncer, "SubtitlePublication", Publication)
    monkeypatch.setattr(subsyncer, "SubsyncEngineRunner", Runner)
    statements, stop = _statements(world)
    try:
        with pytest.raises(RuntimeError, match="stop after the cleanup"):
            subsyncer.SubSyncer().sync(
                video_path=context.path, srt_path=str(source), srt_lang="en", hi=False, forced=False,
                max_offset_seconds="60", no_fix_framerate=False, gss=False, output_mode="overwrite",
                enabled_engines=["ffsubsync"], source=context)
    finally:
        stop()
    assert isinstance(seen["publication"]["ownership_index"], SourceOutputOwnerIndex)
    assert not any(ARR_READ.search(s) for s in statements)
    # The generated output of this item was proven its own and quarantined, which an arr lookup would not do.
    assert not stale.exists() and list(subtitles.glob(".bazarr-sync-obsolete-*"))


def test_an_arr_sync_keeps_the_call_it_always_made(world, monkeypatch, tmp_path):
    from subtitles.tools import subsyncer

    context = _movie(world)
    source = tmp_path / "x.en.srt"
    source.write_text(CONTENT.decode())
    seen = {}

    class Publication:
        def __init__(self, *args, **kwargs):
            seen["kwargs"] = kwargs
            self.state = None

        def release(self):
            pass

    class Runner:
        def run(self, **kwargs):
            raise RuntimeError("stop")

    monkeypatch.setattr(subsyncer, "SubtitlePublication", Publication)
    monkeypatch.setattr(subsyncer, "SubsyncEngineRunner", Runner)
    with pytest.raises(RuntimeError):
        subsyncer.SubSyncer().sync(video_path=context.path, srt_path=str(source), srt_lang="en", hi=False,
                                   forced=False, max_offset_seconds="60", no_fix_framerate=False, gss=False,
                                   output_mode="overwrite", enabled_engines=["ffsubsync"])
    assert seen["kwargs"] == {}


# A race between resolving an item and reading it

def test_an_item_deleted_between_resolving_and_reading_it_fails_the_job_by_name(world, monkeypatch):
    from app.jobs_queue import JobFailed
    from media_sources import repository, service

    context = _movie(world)
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: None)
    real = repository.get_item
    calls = []

    def vanishing(item_id, target=None):
        calls.append(item_id)
        return None if len(calls) > 1 else real(item_id, target)

    monkeypatch.setattr(service.repository, "get_item", vanishing)
    with pytest.raises(JobFailed) as failure:
        service.search_source_item(context.item_id, job_id=2)
    assert failure.value.reason == "not_found"


# What the user decided reaches the provider video

def test_a_user_set_identity_overrides_the_year_and_gives_the_ids_to_the_provider_video(world, monkeypatch):
    from media_sources import repository
    from media_sources.context import resolve_item
    from subtitles import utils

    monkeypatch.setattr(utils, "registered_refiners", {"anidb": lambda p, v: None, "anilist": lambda p, v: None})
    movie = _movie(world, "Film.1999.1080p.mkv")
    repository.update_item(movie.item_id, world.db, year="2001", identity_source="user", imdb_id="tt0133093",
                           tmdb_id="603")
    found = utils.get_video(movie.path, "Film", "None", media_type="movie", source=resolve_item(movie.item_id))
    assert (found.year, found.imdb_id, found.tmdb_id) == (2001, "tt0133093", 603 if isinstance(found.tmdb_id, int)
                                                          else "603")
    # An identity read from the file name only fills what the file name left out.
    other = _movie(world, "Other.1998.mkv")
    repository.update_item(other.item_id, world.db, year="2005")
    found = utils.get_video(other.path, "Other", "None", media_type="movie", source=resolve_item(other.item_id))
    assert found.year == 1998

    episode = _episode(world)
    repository.update_item(episode.item_id, world.db, identity_source="user", tvdb_id=777, imdb_id="tt777")
    found = utils.get_video(episode.path, "Bluey", "None", media_type="series", source=resolve_item(episode.item_id))
    assert (found.tvdb_id, found.imdb_id) == (777, "tt777")


# The blacklist outlives its source, by design

def test_the_blacklist_of_a_deleted_source_still_applies_to_its_kind(world):
    from media_sources import history, repository
    from subtitles import pool

    movie = _movie(world)
    history.blacklist_log_source(movie, "alpha", "release-1", "en")
    repository.delete_source(world.source_id)
    other_source, other_library, root = add_source(world.db, world.tmp_path, name="Other", folder="Other")
    other = add_item(world.db, other_source, other_library, root / "X.2003.mkv")
    from media_sources.context import resolve_item
    assert pool._blacklist("movie", None, resolve_item(other)) == [("alpha", "release-1")]


@pytest.mark.parametrize(("wait", "queued_count"), [(0, 1), (10, 0)])
def test_source_download_respects_its_profile_translation_wait(world, monkeypatch, wait, queued_count):
    from app.config import settings
    from media_sources import repository, service
    from subtitles import processing
    from subtitles.tools.translate import main

    monkeypatch.setattr(processing, "translation_available", lambda: SimpleNamespace(available=True))
    monkeypatch.setattr(settings.translator, "min_source_wait_minutes", wait)
    monkeypatch.setattr(settings.translator, "min_source_score", 0)
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: None)
    profile = [{"id": 1, "language": "en", "forced": "False", "hi": "False"},
               {"id": 2, "language": "hu", "forced": "False", "hi": "False", "translate_from": "en"}]
    from app.database import TableLanguagesProfiles, update_profile_id_list
    import json
    world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(profile)))
    update_profile_id_list.invalidate()

    context = _episode(world)
    _download(monkeypatch)
    queued = []
    monkeypatch.setattr(main, "translate_subtitles_file", lambda **kwargs: queued.append(kwargs))

    service.search_source_item(context.item_id, job_id=3)

    assert repository.get_item(context.item_id).missing_subtitles == "['hu']"
    assert len(queued) == queued_count
    if not queued:
        return
    (job,) = queued
    assert job["source"].item_id == context.item_id
    assert job["media_type"] == "episode"
    assert (job["from_lang"], job["to_lang"]) == ("en", "hu")
    assert all(job[name] is None for name in ("sonarr_series_id", "sonarr_episode_id", "radarr_id"))


@pytest.fixture
def isolated_jobs(monkeypatch):
    """Run real queue reservations and job execution without starting its background consumer."""
    from app import jobs_queue as queue
    from media_sources import service
    from subtitles.tools.translate import main
    from subtitles.tools.translate.runtime import service as runtime_service

    monkeypatch.setattr(queue.JobsQueue, "_flush_progress_loop", lambda self: None)
    monkeypatch.setattr(queue, "_job_event", lambda *args, **kwargs: None)
    jobs = queue.JobsQueue()
    for module in (queue, service, main, runtime_service):
        monkeypatch.setattr(module, "jobs_queue", jobs)
    return jobs


@pytest.mark.parametrize("custom", [False, True], ids=["beside-video", "custom-folder"])
def test_episode_download_dispatches_runtime_and_indexes_translation_and_combined_subtitle(
        world, monkeypatch, isolated_jobs, custom):
    import json

    from app import database, notifier
    from app.config import settings
    from app.database import TableLanguagesProfiles, update_profile_id_list
    from media_sources import repository, service
    from subliminal_patch.score import MAX_SCORES
    from subtitles.tools.translate import main
    from subtitles.tools.translate import runtime
    from subtitles.tools.translate.runtime import gate, service as runtime_service
    from subtitles.tools.translate.runtime.summary import TranslationSummary
    from translator_dispatch_helpers import Env

    profile = [{"id": 1, "language": "en", "forced": "False", "hi": "False"},
               {"id": 2, "language": "hu", "forced": "False", "hi": "False", "translate_from": "en"}]
    world.db.execute(sa.update(TableLanguagesProfiles).values(
        items=json.dumps(profile), combine=json.dumps({"languages": ["en", "hu"], "format": "srt"})))
    update_profile_id_list.invalidate()
    monkeypatch.setattr(settings.translator, "runtime_v2", True)
    monkeypatch.setattr(database, "engine", world.db.get_bind())
    monkeypatch.setattr(runtime, "_tables_present", False)
    monkeypatch.setattr(runtime, "_absent_until", 0.0)
    assert runtime.runtime_v2_enabled()
    monkeypatch.setattr(main.TranslatorFactory, "create_translator",
                        Mock(side_effect=AssertionError("runtime test attempted a legacy service")))
    monkeypatch.setattr(settings.translator, "min_source_wait_minutes", 0)
    monkeypatch.setattr(settings.translator, "min_source_score", 0)
    monkeypatch.setattr(settings.translator, "translator_info", False)

    notifications = []
    transport = Mock()
    monkeypatch.setattr(notifier, "Apprise", lambda **kwargs: transport)
    monkeypatch.setattr(notifier, "get_notifier_providers", lambda: [
        SimpleNamespace(name="JSON", url="json://fixture.test/?item={bazarr_source_item_id}"),
    ])
    transport.notify.side_effect = lambda **data: notifications.append(data)

    context = _episode(world)
    destination = _custom_folder(world, monkeypatch, world.tmp_path) if custom else Path(context.path).parent
    _download(monkeypatch)
    before = table_counts(world.db, ARR_AND_SPORTS_TABLES)
    env = Env(world.db, world.tmp_path)
    try:
        env.ai_engine()
        monkeypatch.setattr(gate, "translation_gate", lambda: gate.evaluate(env.store, env.holds, env.workers))
        monkeypatch.setattr(runtime_service, "Dispatcher", env.dispatcher)
        service.search_source_item(context.item_id)
        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        (job,) = isolated_jobs.jobs_pending_queue
        assert (job.module, job.func) == ("subtitles.tools.translate.main", "translate_subtitles_file")
        assert job.job_name == "Translating Bluey - S01E02 - Mum School (EN to HU)"
        assert job.kwargs["source"] == context and job.kwargs["media_type"] == "episode"
        (pending,) = TranslationSummary(world.db).queue()["data"]
        assert (pending["media_type"], pending["media_id"], pending["media_title"]) == (
            "source", context.item_id, "Bluey - S01E02 - Mum School")
        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        assert job.status == "completed"

        (request,) = env.requests()
        assert request["request"]["media"] == {
            "type": "episode", "title": "Bluey - S01E02 - Mum School", "imdb_id": "tt7678620",
        }
        ledger = env.history(env.last_job())
        assert (ledger["media_type"], ledger["media_id"], ledger["media_title"], ledger["status"]) == (
            "source", context.item_id, "Bluey - S01E02 - Mum School", "completed")
    finally:
        env.close()

    translated = destination / Path(context.path).with_suffix(".hu.srt").name
    combined = destination / Path(context.path).with_suffix(".en.combined-hu.srt").name
    assert "[hu] Downloaded" in translated.read_text()
    assert "Downloaded" in combined.read_text()
    item = repository.get_item(context.item_id)
    assert item.missing_subtitles == "[]" and str(translated) in item.subtitles and str(combined) in item.subtitles
    actions = world.db.execute(sa.select(repository.History).order_by(repository.History.c.action)).all()
    assert [row.action for row in actions] == [1, 6]
    row = actions[1]
    assert (row.item_id, row.item_kind, row.video_path, row.subtitles_path, row.score_out_of) == (
        context.item_id, "episode", context.path, str(translated), MAX_SCORES["episode"])
    assert table_counts(world.db, ARR_AND_SPORTS_TABLES) == before
    transport.add.assert_called_once_with(f"json://fixture.test/?item={context.item_id}")
    assert "Bluey - S01E02 - Mum School : English subtitles downloaded" in notifications[0]["body"]
    assert world.events == [
        ("data", {"type": kind, "action": "update", "payload": row_id})
        for kind, row_id in (("source-item", context.item_id), ("source-group", context.group_id))
    ] * 3
    assert [(event.media_type, event.operation, event.source_item_id) for event in world.published] == [
        ("episode", operation, context.item_id) for operation in ("download", "translate", "combine")
    ]


@pytest.mark.parametrize("translator", ["google_translate", "gemini", "lingarr", "openrouter"])
def test_legacy_translation_writes_unmapped_source_history(world, monkeypatch, translator):
    from app.config import settings
    from media_sources import repository
    from subtitles.tools.translate import main
    from subtitles.tools.translate.services import (
        gemini_translator, google_translator, lingarr_translator, openrouter_translator,
    )
    from utilities.path_mappings import path_mappings

    context = _episode(world)
    original = Path(context.path).with_suffix(".en.srt")
    original.write_bytes(CONTENT)
    translated = Path(context.path).with_suffix(".hu.srt")

    monkeypatch.setattr(settings.translator, "translator_type", translator)
    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(settings.translator, "translator_info", False)
    monkeypatch.setattr(main.jobs_queue, "update_job_progress", lambda **kwargs: None)
    monkeypatch.setattr(main.jobs_queue, "update_job_name", lambda **kwargs: None)
    monkeypatch.setattr(main.jobs_queue, "get_job_name", lambda job_id: "Translating Bluey (EN to HU)")

    for name in ("path_replace_reverse", "path_replace_reverse_movie", "path_replace_reverse_instance"):
        monkeypatch.setattr(path_mappings, name, Mock(side_effect=AssertionError("source tried to map a path")))
    monkeypatch.setattr(google_translator.GoogleTranslatorService, "_translate_text",
                        lambda self, text, job_id: "Translated")
    monkeypatch.setattr(gemini_translator.GeminiTranslatorService, "_get_configured_api_keys", lambda self: ["fixture"])
    monkeypatch.setattr(gemini_translator.GeminiTranslatorService, "_translate_with_gemini",
                        lambda self: Path(self.output_file).write_bytes(CONTENT.replace(b"Downloaded", b"Translated")))
    monkeypatch.setattr(lingarr_translator.LingarrTranslatorService, "_translate_content",
                        lambda self, lines, job_id: [{"position": 0, "line": "Translated"}])
    monkeypatch.setattr(openrouter_translator.OpenRouterTranslatorService, "_submit_and_poll",
                        lambda self, lines, bazarr_job_id: [{"position": 0, "line": "Translated"}])

    main.translate_subtitles_file(context.path, str(original), "en", "hu", False, False, "episode",
                                  None, None, None, None, source=context, job_id=42)

    assert "Translated" in translated.read_text()
    (row,) = world.db.execute(sa.select(repository.History)).all()
    assert (row.item_id, row.action, row.video_path, row.subtitles_path, row.language) == (
        context.item_id, 6, context.path, str(translated), "hu")
    (mutation,) = world.published
    assert (mutation.media_type, mutation.source_item_id, mutation.arr_instance_id) == (
        "episode", context.item_id, None)


def test_failed_source_translation_applies_its_own_language_hold(world, monkeypatch):
    from app.config import settings
    from subtitles.tools.translate import failure_record, main

    context = _movie(world)
    failure_record.clear_failed_translations()
    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(main.jobs_queue, "update_job_name", lambda **kwargs: None)

    try:
        with pytest.raises(FileNotFoundError):
            main.translate_subtitles_file(context.path, f"{context.path}.absent.srt",
                                          "en", "hu", False, True, "movies",
                                          None, None, None, None, source=context, job_id=42)
        assert failure_record.translation_recently_failed(None, "source", context.item_id, "en", "hu", False, True)
        assert not failure_record.translation_recently_failed(None, "source", context.item_id, "en", "hu", False, False)
        assert not failure_record.translation_recently_failed(None, "movies", context.item_id, "en", "hu", False, True)
    finally:
        failure_record.clear_failed_translations()


def test_provider_fallback_targets_the_source_item(world, monkeypatch, isolated_jobs):
    from media_sources import repository
    from subtitles.tools.translate.main import _search_providers_instead

    context = _movie(world)
    _download(monkeypatch)

    _search_providers_instead("movies", None, None, None, source=context)

    (job,) = isolated_jobs.jobs_pending_queue
    assert (job.module, job.func) == ("media_sources.service", "search_source_item")
    assert job.kwargs == {"item_id": context.item_id, "job_id": None, "translation_gate": False,
                          "expected_source": context}
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
    assert repository.get_item(context.item_id).missing_subtitles == "[]"


def test_source_refresh_uses_the_show_ids_and_publishes_sports_as_sports(world, monkeypatch):
    from app import database
    from media_servers import dispatcher, resolution
    from media_servers.events import publication_callback
    from utilities.path_mappings import path_mappings

    episode, sports = _episode(world), _sports(world)
    monkeypatch.setattr(database, "session_factory", world.db.session_factory)
    monkeypatch.setattr(path_mappings, "path_replace_reverse_instance",
                        Mock(side_effect=AssertionError("source tried to map a path")))

    for source in (episode, sports):
        publication_callback(source.media_type, source.path, "download", source=source)(
            Path(source.path).with_suffix(".en.srt"))

    episode_event, sports_event = world.published
    episode_metadata = dispatcher._metadata(episode_event)
    assert episode_metadata.provider_ids("episode") == [("imdb", "tt7678620"), ("tvdb", "387380")]
    assert (episode_metadata.title, episode_metadata.season, episode_metadata.episode) == ("Bluey", 1, 2)
    assert resolution.media_metadata("episode", sports.path, None, source_item_id=episode.item_id) is None
    assert sports_event.media_type == "sports" and sports_event.source_item_id == sports.item_id
    assert dispatcher._metadata(sports_event).title == "Monaco Grand Prix Race"


@pytest.mark.parametrize("engine", ["google_translate", "lingarr"])
def test_source_translation_refuses_output_with_no_translated_cue(world, monkeypatch, engine):
    from app.config import settings
    from media_sources import repository
    from subtitles.tools.translate import main
    from subtitles.tools.translate.failure_record import clear_failed_translations
    from subtitles.tools.translate.services import google_translator, lingarr_translator

    context = _episode(world)
    original = Path(context.path).with_suffix(".en.srt")
    original.write_bytes(CONTENT)
    monkeypatch.setattr(settings.translator, "translator_type", engine)
    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(google_translator.GoogleTranslatorService, "_translate_text",
                        lambda self, text, job_id: None)
    monkeypatch.setattr(lingarr_translator.LingarrTranslatorService, "_translate_content",
                        lambda self, lines, job_id: [])
    try:
        with pytest.raises(main.JobFailed, match="no translated cues"):
            main.translate_subtitles_file(context.path, str(original), "en", "hu", False, False, "episode",
                                          None, None, None, None, source=context, job_id=42)
    finally:
        clear_failed_translations()
    assert not Path(context.path).with_suffix(".hu.srt").exists()
    assert world.db.execute(sa.select(repository.History)).all() == []
    assert not world.published


@pytest.mark.parametrize("other_owner", ["source", "arr"])
@pytest.mark.parametrize("shared_is_media_directory", [False, True])
def test_source_outputs_refuse_ambiguous_shared_destination(world, monkeypatch, other_owner,
                                                            shared_is_media_directory):
    from app.config import settings
    from app.database import TableMovies
    from subtitles.tools.combine import main as combine
    from subtitles.tools.translate import main

    destination = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    if shared_is_media_directory:
        destination = Path(context.path).parent
        monkeypatch.setattr(settings.general, "subfolder_custom", str(destination))
    video_name = Path(context.path).name
    other = video(world.tmp_path / "Other" / video_name)
    if other_owner == "source":
        add_item(world.db, world.source_id, world.library_id, other)
    else:
        world.db.execute(sa.insert(TableMovies).values(path=str(other), title="Arr copy", tmdbId="8765"))
    original = destination / Path(video_name).with_suffix(".en.srt")
    original.write_bytes(CONTENT)
    extra = destination / Path(video_name).with_suffix(".de.srt")
    extra.write_bytes(CONTENT)
    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    factory = Mock(side_effect=AssertionError("ambiguous target reached the engine"))
    monkeypatch.setattr(main.TranslatorFactory, "create_translator", factory)

    assert main.translate_subtitles_file(context.path, str(original), "en", "hu", False, False, "movies",
                                         None, None, None, None, source=context, job_id=12) is None
    factory.assert_not_called()
    assert not (destination / Path(video_name).with_suffix(".hu.srt")).exists()
    result = combine.try_combine_for_video(context.path, "movie", languages=["en", "de"], format="srt",
                                           source=context)
    assert result.status == "skipped" and "unique" in result.reason
    assert not (destination / Path(video_name).with_suffix(".en.combined-de.srt")).exists()


@pytest.mark.parametrize("change", [
    "none", "profile", "disable", "reuse", "wait-increased", "score-increased", "profile-during-engine",
    "cutoff-during-engine",
])
def test_source_wait_resumes_in_normal_queue_and_rechecks_item(world, monkeypatch, isolated_jobs, change):
    import json
    import os
    import time
    from datetime import datetime, timedelta

    from app import database
    from app.config import settings
    from app.database import TableLanguagesProfiles, update_profile_id_list
    from media_sources import repository, service
    from media_sources.context import resolve_item
    from subtitles import download
    from subtitles.tools.translate import main, runtime
    from subtitles.tools.translate.failure_record import (
        clear_failed_translations, record_failed_translation, translation_recently_failed,
    )
    from subtitles.tools.translate.runtime import gate, service as runtime_service
    from subtitles.tools.translate.runtime.summary import TranslationSummary
    from translator_dispatch_helpers import Env

    profile = [{"id": 1, "language": "en", "forced": "False", "hi": "False"},
               {"id": 2, "language": "hu", "forced": "False", "hi": "False", "translate_from": "en"}]

    def write_profile(items):
        world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(items)))
        update_profile_id_list.invalidate()

    write_profile(profile)
    monkeypatch.setattr(settings.translator, "runtime_v2", True)
    monkeypatch.setattr(settings.translator, "min_source_wait_minutes", 10)
    monkeypatch.setattr(settings.translator, "min_source_score", 0)
    monkeypatch.setattr(settings.translator, "translator_info", False)
    monkeypatch.setattr(database, "engine", world.db.get_bind())
    monkeypatch.setattr(runtime, "_tables_present", False)
    monkeypatch.setattr(runtime, "_absent_until", 0.0)
    monkeypatch.setattr(main.TranslatorFactory, "create_translator",
                        Mock(side_effect=AssertionError("runtime test attempted a legacy service")))
    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "../Subs")
    context = _movie(world)
    _download(monkeypatch)
    folder = Path(context.path).parent / ".." / "Subs"
    original = folder / Path(context.path).with_suffix(".en.srt").name
    destination = folder / Path(context.path).with_suffix(".hu.srt").name

    env = Env(world.db, world.tmp_path)
    try:
        env.ai_engine()
        monkeypatch.setattr(gate, "translation_gate", lambda: gate.evaluate(env.store, env.holds, env.workers))
        monkeypatch.setattr(runtime_service, "Dispatcher", env.dispatcher)

        service.search_source_item(context.item_id)
        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        assert original.exists() and not isolated_jobs.jobs_pending_queue

        # The source-age policy uses the download record rather than the time of the second search.
        past = time.time() - 660
        os.utime(original, (past, past))
        world.db.execute(sa.update(repository.History).values(timestamp=datetime.now() - timedelta(minutes=11)))
        provider = Mock(side_effect=AssertionError("a queued language reached providers again"))
        monkeypatch.setattr(download, "download_best_subtitles", provider)
        service.search_source_item(context.item_id)
        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        (pending_job,) = isolated_jobs.jobs_pending_queue
        assert pending_job.kwargs["source_srt_file"] == str(original)

        if change == "profile":
            write_profile(profile[:1])
        elif change == "disable":
            repository.update_library(context.library_id, world.db, enabled=0)
        elif change == "reuse":
            failure = (None, "source", context.item_id, "en", "hu", False, False)
            record_failed_translation(*failure, source_created_at=context.created_at)
            repository.delete_items([context.item_id], world.db)
            replacement = add_item(world.db, context.source_id, context.library_id, context.path)
            if world.db.get_bind().dialect.name == "sqlite":
                assert replacement == context.item_id
            new_context = resolve_item(replacement)
            assert new_context.created_at != context.created_at
            assert not translation_recently_failed(None, "source", replacement, "en", "hu", False, False)
        elif change == "wait-increased":
            monkeypatch.setattr(settings.translator, "min_source_wait_minutes", 60)
        elif change == "score-increased":
            world.db.execute(sa.update(repository.History).values(score=1))
            monkeypatch.setattr(settings.translator, "min_source_score", 100)
        elif change in ("profile-during-engine", "cutoff-during-engine"):
            write_output = runtime_service.RuntimeTranslatorService._write_file

            def change_before_publish(engine, *args):
                if change == "cutoff-during-engine":
                    world.db.execute(sa.update(TableLanguagesProfiles).values(cutoff=1))
                    update_profile_id_list.invalidate()
                    assert repository.get_item(context.item_id).missing_subtitles == "['hu']"
                else:
                    write_profile(profile[:1])
                return write_output(engine, *args)

            monkeypatch.setattr(runtime_service.RuntimeTranslatorService, "_write_file", change_before_publish)

        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        assert TranslationSummary(world.db).queue()["data"] == []
        provider.assert_not_called()
        assert env.last_job().state == ("completed" if change == "none" else "cancelled")
        assert len(env.requests()) == (1 if change.endswith("-during-engine") or change == "none" else 0)
        assert destination.exists() == (change == "none")
        actions = world.db.execute(sa.select(repository.History.c.action)).scalars().all()
        assert sorted(actions) == ([1, 6] if change == "none" else [1])
    finally:
        clear_failed_translations()
        env.close()


def test_source_provider_fallback_does_not_search_a_reused_item(world, monkeypatch, isolated_jobs):
    from media_sources import repository, service
    from subtitles.tools.translate.main import _search_providers_instead

    context = _movie(world)
    _search_providers_instead("movies", None, None, None, source=context)
    repository.delete_items([context.item_id], world.db)
    replacement = add_item(world.db, context.source_id, context.library_id, context.path)
    if world.db.get_bind().dialect.name == "sqlite":
        assert replacement == context.item_id
    providers = Mock(side_effect=AssertionError("a replacement item's providers were searched"))
    monkeypatch.setattr(service, "get_providers", providers)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
    providers.assert_not_called()


@pytest.mark.parametrize("collide_after_enqueue", [False, True], ids=["preflight", "at-dispatch"])
def test_ambiguous_translation_input_leaves_provider_search_available(
        world, monkeypatch, isolated_jobs, collide_after_enqueue):
    import json

    from app.config import settings
    from app.database import TableLanguagesProfiles, TableMovies, update_profile_id_list
    from media_sources import service
    from media_sources.indexer import index_item
    from subtitles.tools.combine import main as combine
    from subtitles.tools.translate import main

    profile = [{"id": 1, "language": "en", "forced": "False", "hi": "False"},
               {"id": 2, "language": "hu", "forced": "False", "hi": "False", "translate_from": "en"}]
    world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(profile)))
    update_profile_id_list.invalidate()
    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "..")
    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(settings.translator, "translator_type", "google_translate")
    monkeypatch.setattr(settings.translator, "min_source_wait_minutes", 0)
    monkeypatch.setattr(settings.translator, "min_source_score", 0)
    factory = Mock(side_effect=AssertionError("ambiguous subtitle was sent to a translator"))
    monkeypatch.setattr(main.TranslatorFactory, "create_translator", factory)

    context = _movie(world)
    media = Path(context.path)
    media.with_suffix(".en.srt").write_bytes(CONTENT)
    (world.tmp_path / media.with_suffix(".de.srt").name).write_bytes(CONTENT)
    index_item(context.item_id)

    def add_conflict():
        other = video(media.parent / "Arr" / media.name)
        world.db.execute(sa.insert(TableMovies).values(path=str(other), title="Other film", tmdbId="8675"))

    if not collide_after_enqueue:
        add_conflict()
    _download(monkeypatch, FakeSubtitle(Language("hun")))
    service.search_source_item(context.item_id)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
    if collide_after_enqueue:
        (translation_job,) = isolated_jobs.jobs_pending_queue
        assert translation_job.func == "translate_subtitles_file"
        add_conflict()
        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        (fallback_job,) = isolated_jobs.jobs_pending_queue
        assert fallback_job.func == "search_source_item"
        assert fallback_job.kwargs["translation_gate"] is False
        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
    assert not isolated_jobs.jobs_pending_queue
    factory.assert_not_called()
    assert (world.tmp_path / media.with_suffix(".hu.srt").name).exists()

    result = combine.try_combine_for_video(context.path, "movie", languages=["en", "de"], format="srt",
                                           source=context)
    assert result.status == "skipped" and "input" in result.reason
    assert not (world.tmp_path / media.with_suffix(".en.combined-de.srt").name).exists()


def test_gemini_formatting_cannot_make_unchanged_rtl_dialogue_a_source_translation(world, monkeypatch):
    import srt
    from app.config import settings
    from media_sources import repository
    from subtitles.tools.translate import main
    from subtitles.tools.translate.failure_record import clear_failed_translations
    from subtitles.tools.translate.services.gemini_translator import GeminiTranslatorService

    context = _episode(world)
    original = Path(context.path).with_suffix(".en.srt")
    original.write_bytes(CONTENT.replace(b"Downloaded", "مرحبا".encode()))
    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(settings.translator, "translator_type", "gemini")
    monkeypatch.setattr(GeminiTranslatorService, "_get_configured_api_keys", lambda self: ["fixture"])

    def format_unchanged_lines(engine):
        cues = list(srt.parse(original.read_text()))
        GeminiTranslatorService._process_translated_lines([{"index": "0", "content": cues[0].content}], cues)
        assert cues[0].content.startswith("\u202b")
        Path(engine.output_file).write_text(srt.compose(cues))

    monkeypatch.setattr(GeminiTranslatorService, "_translate_with_gemini", format_unchanged_lines)
    try:
        with pytest.raises(main.JobFailed, match="no translated cues"):
            main.translate_subtitles_file(context.path, str(original), "en", "hu", False, False, "episode",
                                          None, None, None, None, source=context, job_id=42)
    finally:
        clear_failed_translations()
    assert not Path(context.path).with_suffix(".hu.srt").exists()
    assert world.db.execute(sa.select(repository.History)).all() == []


def test_runtime_lingarr_empty_source_translation_is_terminal(world, monkeypatch, isolated_jobs):
    from app import database
    from app.config import settings
    from media_sources import repository
    from subtitles.tools.translate import main, runtime
    from subtitles.tools.translate.failure_record import clear_failed_translations
    from subtitles.tools.translate.runtime import legacy_engines, service as runtime_service
    from subtitles.tools.translate.runtime.summary import TranslationSummary
    from subtitles.tools.translate.services.lingarr_translator import LingarrTranslatorService
    from translator_dispatch_helpers import Env

    context = _movie(world)
    original = Path(context.path).with_suffix(".en.srt")
    original.write_bytes(CONTENT)
    monkeypatch.setattr(settings.translator, "runtime_v2", True)
    monkeypatch.setattr(settings.translator, "lingarr_url", "http://lingarr.example.test:9876")
    monkeypatch.setattr(settings.translator, "translator_type", "lingarr")
    monkeypatch.setattr(database, "engine", world.db.get_bind())
    monkeypatch.setattr(runtime, "_tables_present", False)
    monkeypatch.setattr(runtime, "_absent_until", 0.0)
    requests = Mock(return_value=[])
    monkeypatch.setattr(LingarrTranslatorService, "_translate_content", requests)

    env = Env(world.db, world.tmp_path)
    try:
        env.pointers.engine = env.store.create_instance(legacy_engines.LINGARR_PLUGIN_ID, settings={
            legacy_engines.SAVED_FLAG: True,
        }).id
        monkeypatch.setattr(runtime_service, "Dispatcher", env.dispatcher)
        main.translate_subtitles_file(context.path, str(original), "en", "hu", False, False, "movies",
                                      None, None, None, None, source=context)
        assert not isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        requests.assert_called_once()
        (failed,) = isolated_jobs.jobs_failed_queue
        assert failed.error["reason"] == "empty_translation" and not failed.retryable
        assert env.last_job().state == "failed" and env.history(env.last_job())["status"] == "failed"
        assert TranslationSummary(world.db).queue()["data"] == []
        assert world.db.execute(sa.select(repository.History)).all() == []
        assert not Path(context.path).with_suffix(".hu.srt").exists()
    finally:
        clear_failed_translations()
        env.close()
