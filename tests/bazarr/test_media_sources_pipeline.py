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
    from media_servers import dispatcher
    from media_sources import indexer, service
    from subtitles import download, pool, processing, sync

    profile = add_profile(source_db, [("en", "False", "False")])
    source_id, library_id, root = add_source(source_db, tmp_path, profile_id=profile)
    published = []
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
                           published=published, tmp_path=tmp_path, original_update_pool=original_update_pool)


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
        return {found: [subtitle or FakeSubtitle()]}

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


def test_a_failed_probe_is_retried_once_per_search_even_with_download_reindexing(world, monkeypatch):
    from app.config import settings
    from media_sources import analysis, identity, indexer, repository, service
    from utilities import video_analyzer

    monkeypatch.setattr(settings.general, "embedded_subtitles_parser", "ffprobe")
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **_: None)
    metadata = {"ffprobe": {"audio": [{"language": "eng"}]}, "mediainfo": {}}
    probe = Mock(return_value=metadata)
    monkeypatch.setattr(video_analyzer, "parse_video_metadata", probe)
    context = _movie(world)
    stamp = identity.stat_file(context.path)
    repository.update_item(context.item_id, world.db, file_dev=stamp.dev, file_ino=stamp.ino,
                           file_size=stamp.size, file_mtime_ns=stamp.mtime_ns)
    Path(context.path).write_bytes(b"replacement" * 100)
    probe.return_value = None
    indexer.index_item(context.item_id)
    assert not analysis.streams(analysis.read_metadata(repository.get_item(context.item_id).ffprobe_cache))
    probe.reset_mock()
    _download(monkeypatch)

    service.search_source_item(context.item_id, job_id=3)

    probe.assert_called_once()
    saved = Path(context.path).with_suffix(".en.srt")
    assert saved.read_bytes() == CONTENT
    item = repository.get_item(context.item_id)
    assert str(saved) in item.subtitles and item.missing_subtitles == "[]"
    assert world.db.execute(sa.select(sa.func.count()).select_from(repository.History)).scalar_one() == 1

    probe.return_value = metadata
    service.search_source_item(context.item_id, job_id=4)
    assert probe.call_count == 2
    item = repository.get_item(context.item_id)
    assert analysis.read_metadata(item.ffprobe_cache)["ffprobe"] == metadata["ffprobe"]


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
    for name in ("notify_radarr", "notify_sonarr", "_trigger_auto_translation", "_trigger_combine"):
        getattr(processing, name).assert_not_called()


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
    monkeypatch.setattr(sync, "publication_callback", lambda *args: published.append(args) or (lambda path: None))
    calls = _fake_sync(monkeypatch, context.path.replace(".mkv", ".en.synced.srt"))

    assert sync.sync_subtitles(video_path=context.path, srt_path=context.path.replace(".mkv", ".en.srt"),
                               srt_lang="en", forced=False, hi=False, percent_score=100, force_sync=True,
                               track_job_progress=False, source=context) is True

    assert seen and all(entry == (None, series) for entry in seen)
    assert published == [(publication, context.path, "sync", None)]
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
