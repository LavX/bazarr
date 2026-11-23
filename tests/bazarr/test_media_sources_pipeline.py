"""A source item through the shared subtitle pipeline: the seam, the pool, the blacklist, sync and history."""
import ast
import logging
import re
import sqlite3
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


@pytest.mark.parametrize('same_file_alias', [False, True])
def test_download_keeps_another_registered_videos_exact_engine_named_sidecar(world, monkeypatch, same_file_alias):
    from media_sources.indexer import SourcePublicationOwnerIndex
    from subtitles.indexer.utils import add_sync_engine_outputs

    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world, "Film.mkv")
    if same_file_alias:
        Path(context.path).with_suffix(".en.mkv").symlink_to(context.path)
    owner, library, other_root = add_source(world.db, world.tmp_path, name="Other", folder="Other")
    competing = video(other_root / "Film.en.ffsubsync.mkv")
    add_item(world.db, owner, library, competing)
    protected = folder / "Film.en.ffsubsync.srt"
    obsolete = folder / "Film.en.alass.srt"
    protected.write_bytes(CONTENT.replace(b"Downloaded", b"Other video"))
    obsolete.write_bytes(CONTENT)
    _download(monkeypatch)

    assert len(_generate(context)) == 1
    assert (folder / "Film.en.srt").read_bytes() == CONTENT
    assert protected.read_bytes() == CONTENT.replace(b"Downloaded", b"Other video")
    assert not obsolete.exists()
    found = add_sync_engine_outputs(folder, {}, video_path=str(competing),
                                    ownership_index=SourcePublicationOwnerIndex())
    assert protected.name in found
    local_found = add_sync_engine_outputs(competing.parent, {}, video_path=str(competing),
                                          ownership_index=SourcePublicationOwnerIndex())
    assert competing.name not in local_found


@pytest.mark.parametrize('target_kind', ['file', 'directory', 'dangling'])
def test_source_download_distinguishes_video_aliases_from_resolved_directory_aliases(world, monkeypatch, target_kind):
    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world, 'Film.mkv')
    target = world.tmp_path / 'resolved' / 'Film.en.mkv'
    target.parent.mkdir()
    if target_kind == 'file':
        video(target)
    elif target_kind == 'directory':
        target.mkdir()
    (Path(context.path).parent / 'Alias.mkv').symlink_to(target, target_is_directory=target_kind == 'directory')
    destination = folder / 'Film.en.srt'
    obsolete = folder / 'Film.en.alass.srt'
    obsolete.write_bytes(CONTENT.replace(b'Downloaded', b'Previous subtitle'))
    _download(monkeypatch)

    results = _generate(context)

    if target_kind == 'directory':
        assert len(results) == 1
        assert destination.read_bytes() == CONTENT
        assert not obsolete.exists()
        assert len(world.published) == 1
    else:
        assert results == []
        assert not destination.exists()
        assert obsolete.read_bytes() == CONTENT.replace(b'Downloaded', b'Previous subtitle')
        assert not world.published


def test_source_download_does_not_clean_generated_siblings_after_releasing_the_owner_proof(world, monkeypatch):
    from contextlib import contextmanager

    from media_sources import indexer
    from subtitles.download import _write_source_download

    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world, 'Film.mkv')
    other_source, other_library, other_root = add_source(world.db, world.tmp_path, name='Other', folder='Other')
    destination = folder / 'Film.en.srt'
    obsolete = folder / 'Film.en.alass.srt'
    obsolete.write_bytes(CONTENT)
    protected = folder / 'Film.en.ffsubsync.srt'
    original_publication = indexer.source_item_publication
    before_commit = []

    @contextmanager
    def publish_then_admit(source, *, shared=False):
        with original_publication(source, shared=shared) as outcome:
            yield outcome
            before_commit.append(obsolete.exists())
        # The scanner and the other writer land as soon as the registry is writable again.
        add_item(world.db, other_source, other_library, video(other_root / 'Film.en.ffsubsync.mkv'))
        protected.write_bytes(CONTENT)

    monkeypatch.setattr(indexer, 'source_item_publication', publish_then_admit)
    assert _write_source_download(context.path, str(destination), CONTENT, source=context,
                                  language=Language('eng'), recheck_missing=False, is_upgrade=False,
                                  written_paths=[], on_publish=None)
    assert before_commit == [False]
    assert destination.read_bytes() == protected.read_bytes() == CONTENT


def test_source_download_keeps_the_primary_write_when_a_shared_subtitle_folder_cannot_be_listed(world, monkeypatch):
    import os

    from subtitles.download import _write_source_download

    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world, 'Film.mkv')
    destination = folder / 'Film.en.srt'
    generated = folder / 'Film.en.alass.srt'
    generated.write_bytes(CONTENT)
    scandir = os.scandir
    refused = []

    def no_subtitle_listing(path):
        if isinstance(path, (str, os.PathLike)) and Path(path) == folder:
            refused.append(True)
            raise PermissionError('The subtitle folder is write-only')
        return scandir(path)

    monkeypatch.setattr(os, 'scandir', no_subtitle_listing)
    assert _write_source_download(context.path, str(destination), CONTENT, source=context,
                                  language=Language('eng'), recheck_missing=False, is_upgrade=False,
                                  written_paths=[], on_publish=None)
    assert refused == [True]
    assert destination.read_bytes() == generated.read_bytes() == CONTENT


@pytest.mark.parametrize('unreadable', ['directory', 'metadata', 'content'])
def test_scheduled_source_download_skips_publication_when_missing_languages_cannot_be_verified(
        world, monkeypatch, caplog, unreadable):
    import os

    from app.jobs_queue import JobFailed
    from media_sources import indexer, repository, service
    from subtitles import download

    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world, 'Film.mkv')
    destination = folder / 'Film.en.srt'
    generated = folder / 'Film.en.alass.srt'
    generated.write_bytes(CONTENT)
    initial = repository.get_item(context.item_id)
    asked = _download(monkeypatch)
    jobs = []
    monkeypatch.setattr(service.jobs_queue, 'update_job_progress', lambda **kwargs: jobs.append(kwargs))
    untagged = folder / 'Film.srt'
    best = download.download_best_subtitles
    refused = []

    def return_subtitles_and_add_sidecar(*args, **kwargs):
        result = best(*args, **kwargs)
        if unreadable != 'directory':
            untagged.write_bytes(CONTENT)
        return result

    monkeypatch.setattr(download, 'download_best_subtitles', return_subtitles_and_add_sidecar)
    module, name, denied = {
        'directory': (os, 'scandir', folder),
        'metadata': (os, 'lstat', untagged),
        'content': (indexer, 'read_regular_subtitle', untagged),
    }[unreadable]
    operation = getattr(module, name)

    def refuse(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == denied:
            refused.append(True)
            raise PermissionError('This subtitle candidate is unreadable')
        return operation(path, *args, **kwargs)

    with monkeypatch.context() as patch, caplog.at_level(logging.INFO):
        patch.setattr(module, name, refuse)
        with pytest.raises(JobFailed, match='The source missing languages could not be verified') as failed:
            service.search_source_item(context.item_id, job_id=3)

    assert failed.value.reason == 'save_failed'
    assert asked['video'].source_context.item_id == context.item_id
    assert refused
    assert not destination.exists()
    assert generated.read_bytes() == CONTENT
    if unreadable != 'directory':
        assert untagged.read_bytes() == CONTENT
    assert not list(folder.glob('.bazarr-*'))
    current = repository.get_item(context.item_id)
    assert (current.subtitles, current.missing_subtitles) == (initial.subtitles, initial.missing_subtitles)
    assert world.db.execute(sa.select(sa.func.count()).select_from(repository.History)).scalar_one() == 0
    assert not world.published
    assert not any(job.get('progress_value') == 'max' for job in jobs)
    assert 'Error saving Subtitles file to disk' not in caplog.text


@pytest.mark.parametrize('late_change', ['video', 'subtitle'])
def test_source_download_preserves_own_folder_siblings_that_change_after_planning(world, monkeypatch, late_change):
    import tempfile
    from contextlib import contextmanager

    from app.config import settings
    from media_sources import indexer
    from subtitles.download import _write_source_download

    monkeypatch.setattr(settings.general, 'subfolder', 'current')
    context = _movie(world, 'Film.mkv')
    media = Path(context.path)
    destination = media.with_suffix('.en.srt')
    protected = media.with_suffix('.en.ffsubsync.srt')
    obsolete = media.with_suffix('.en.alass.srt')
    protected.write_bytes(CONTENT)
    obsolete.write_bytes(CONTENT)
    publication = indexer.source_item_publication
    mkstemp = tempfile.mkstemp
    expected = CONTENT
    changed = []

    def late_video(*args, **kwargs):
        result = mkstemp(*args, **kwargs)
        if kwargs.get('prefix', '').startswith(f'.bazarr-sync-obsolete-{protected.name}'):
            # This video arrives while the quarantine placeholder is created,
            # after planning but before moving the unchanged sidecar.
            changed.append(video(protected.with_suffix('.mkv')))
        return result

    @contextmanager
    def late_sibling(source, *, shared=False):
        nonlocal expected
        with publication(source, shared=shared) as outcome:
            if late_change == 'subtitle':
                expected = CONTENT.replace(b'Downloaded', b'New version')
                replacement = protected.with_suffix('.tmp')
                replacement.write_bytes(expected)
                replacement.replace(protected)
                changed.append(protected)
            yield outcome

    if late_change == 'video':
        monkeypatch.setattr(tempfile, 'mkstemp', late_video)
    monkeypatch.setattr(indexer, 'source_item_publication', late_sibling)
    assert _write_source_download(context.path, str(destination), CONTENT, source=context,
                                  language=Language('eng'), recheck_missing=False, is_upgrade=False,
                                  written_paths=[], on_publish=None)
    assert destination.read_bytes() == CONTENT
    assert len(changed) == 1
    assert protected.read_bytes() == expected
    assert not obsolete.exists()
    assert len(list(media.parent.glob('.bazarr-sync-obsolete-*.bak'))) == 1


def test_download_keeps_a_provider_result_from_reaching_a_replaced_video(world, monkeypatch):
    from media_sources import repository
    from subtitles import download

    context = _movie(world, "Film.mkv")
    path = Path(context.path)
    original_inode = path.stat().st_ino

    def replace_during_provider_request(videos, **kwargs):
        replacement = video(path.with_name("replacement.mkv"))
        replacement.replace(path)
        status = path.stat()
        assert status.st_ino != original_inode
        repository.update_item(context.item_id, world.db, file_dev=str(status.st_dev), file_ino=str(status.st_ino),
                               file_size=status.st_size, file_mtime_ns=status.st_mtime_ns)
        (requested,) = videos
        return {requested: [FakeSubtitle()]}

    monkeypatch.setattr(download, "download_best_subtitles", replace_during_provider_request)
    assert _generate(context) == []
    assert repository.get_item(context.item_id).created_at == context.created_at
    assert not path.with_suffix(".en.srt").exists()
    assert not world.published


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


@pytest.mark.parametrize(("admitted", "listing_failure"), [(False, False), (True, False), (True, True)],
                         ids=["replacement", "admitted", "admitted-with-listing-failure"])
def test_a_failed_probe_is_retried_once_per_search_even_with_download_reindexing(
        world, monkeypatch, admitted, listing_failure):
    from app.config import settings
    from media_sources import analysis, identity, indexer, repository, scanner, service
    from utilities import video_analyzer

    monkeypatch.setattr(settings.general, "embedded_subtitles_parser", "ffprobe")
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **_: None)
    metadata = {"ffprobe": {"audio": [{"language": "eng"}]}, "mediainfo": {}}
    probe = Mock(return_value=metadata)
    monkeypatch.setattr(video_analyzer, "parse_video_metadata", probe)
    if admitted:
        path = video(world.root / "Film.2001.1080p.BluRay.x264-GRP.mkv")
        external = path.with_suffix(".hu.srt")
        external.write_bytes(CONTENT)
        probe.return_value = None
        scanner.run_scan(world.source_id, world.library_id)
        (item,) = repository.list_items(library_id=world.library_id)
        item_id = item.id
        assert str(external) in item.subtitles
        assert identity.stored_content_stamp(item) == identity.stat_file(path)
        probe.assert_called_once()
    else:
        context = _movie(world)
        item_id = context.item_id
        path = Path(context.path)
        stamp = identity.stat_file(path)
        repository.update_item(item_id, world.db, file_dev=stamp.dev, file_ino=stamp.ino,
                               file_size=stamp.size, file_mtime_ns=stamp.mtime_ns)
        path.write_bytes(b"replacement" * 100)
        probe.return_value = None
        indexer.index_item(item_id)
    initial = repository.get_item(item_id)
    assert initial.missing_subtitles == "['en']"
    assert not analysis.streams(analysis.read_metadata(initial.ffprobe_cache))
    probe.reset_mock()
    saved = path.with_suffix(".en.srt")
    list_sidecars = indexer.list_external_subtitles
    index_attempts = []

    def list_after_save(*args, **kwargs):
        if not kwargs.get("publication"):
            index_attempts.append(args)
            if listing_failure and len(index_attempts) == 1:
                assert saved.is_file()
                raise FileNotFoundError("An old sidecar disappeared while reading its size")
        return list_sidecars(*args, **kwargs)

    monkeypatch.setattr(indexer, "list_external_subtitles", list_after_save)

    def providers_after_probe():
        probe.assert_called_once()
        return ["fake"]

    monkeypatch.setattr(service, "get_providers", providers_after_probe)
    _download(monkeypatch)

    service.search_source_item(item_id, job_id=3)

    probe.assert_called_once()
    assert saved.read_bytes() == CONTENT
    item = repository.get_item(item_id)
    assert str(saved) in item.subtitles and item.missing_subtitles == "[]"
    assert len(index_attempts) == (2 if listing_failure else 1)
    assert identity.stored_content_stamp(item) == identity.stored_content_stamp(initial)
    assert world.db.execute(sa.select(sa.func.count()).select_from(repository.History)).scalar_one() == 1

    probe.return_value = metadata
    service.search_source_item(item_id, job_id=4)
    assert probe.call_count == 2
    item = repository.get_item(item_id)
    assert analysis.read_metadata(item.ffprobe_cache)["ffprobe"] == metadata["ffprobe"]
    listing_count = len(index_attempts)
    service.search_source_item(item_id, job_id=5)
    assert len(index_attempts) == listing_count
    assert probe.call_count == 2


def test_search_reindexes_a_saved_subtitle_from_a_custom_result(world, monkeypatch):
    from media_sources import repository, service
    from subtitles import download

    context = _movie(world)
    saved = Path(context.path).with_suffix(".en.srt")

    def custom_download(*args, **kwargs):
        saved.write_bytes(CONTENT)
        yield SimpleNamespace(path=context.path, subs_path=str(saved), language_code="en", score=100,
                              provider="fake", subs_id="sub-1", message="Downloaded", matched=None, not_matched=None)

    monkeypatch.setattr(download, "generate_subtitles", custom_download)
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **_: None)

    service.search_source_item(context.item_id, job_id=3)

    assert saved.read_bytes() == CONTENT
    item = repository.get_item(context.item_id)
    assert str(saved) in item.subtitles and item.missing_subtitles == "[]"
    (row,) = world.db.execute(sa.select(repository.History)).all()
    assert row.subtitles_path == str(saved)


@pytest.mark.parametrize("custom_result", [False, True], ids=["shared-processing", "custom-result"])
def test_search_keeps_the_saved_file_and_history_when_post_save_index_transactions_fail(
        world, monkeypatch, custom_result):
    from media_sources import repository, service
    from subtitles import download

    context = _movie(world)
    initial = repository.get_item(context.item_id)
    saved = Path(context.path).with_suffix(".en.srt")
    write_index = repository.write_item_index
    failed_writes = []
    jobs = []

    def fail_after_index_write(*args, **kwargs):
        written = write_index(*args, **kwargs)
        if saved.is_file():
            assert written
            assert saved.read_bytes() == CONTENT
            failed_writes.append(args[0])
            raise sa.exc.OperationalError("UPDATE table_source_items", {},
                                          sqlite3.OperationalError("database is locked"))
        return written

    monkeypatch.setattr(repository, "write_item_index", fail_after_index_write)
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: jobs.append(kwargs))
    if custom_result:
        def custom_download(*args, **kwargs):
            saved.write_bytes(CONTENT)
            yield SimpleNamespace(path=context.path, subs_path=str(saved), language_code="en", score=100,
                                  provider="fake", subs_id="sub-1", message="Downloaded", matched=None, not_matched=None)

        monkeypatch.setattr(download, "generate_subtitles", custom_download)
    else:
        _download(monkeypatch)

    service.search_source_item(context.item_id, job_id=3)

    assert failed_writes == [context.item_id] * (1 if custom_result else 2)
    assert saved.read_bytes() == CONTENT
    item = repository.get_item(context.item_id)
    assert (item.subtitles, item.missing_subtitles, item.indexed_at) == (
        initial.subtitles, initial.missing_subtitles, initial.indexed_at)
    (row,) = world.db.execute(sa.select(repository.History)).all()
    assert (row.item_id, row.source_id, row.action, row.video_path, row.subtitles_path) == (
        context.item_id, world.source_id, 1, context.path, str(saved))
    assert (row.language, row.provider, row.subs_id) == ("en", "fake", "sub-1")
    assert jobs[-1]["progress_message"] == "1 subtitle(s) downloaded"


def test_post_save_indexing_leaves_a_moved_items_new_path_untouched(world, monkeypatch):
    from media_sources import indexer, repository, service

    context = _movie(world)
    initial = repository.get_item(context.item_id)
    saved = Path(context.path).with_suffix(".en.srt")
    replacement = video(world.root / "Replacement.2002.mkv")
    replacement_subtitle = replacement.with_suffix(".en.srt")
    replacement_subtitle.write_bytes(b"Replacement subtitle\n")
    index_item = indexer.index_item
    attempted_paths = []

    def move_before_index(item_id, *args, expected_path=None, **kwargs):
        if expected_path is not None:
            assert saved.read_bytes() == CONTENT
            attempted_paths.append(expected_path)
            if len(attempted_paths) == 1:
                repository.update_item(item_id, world.db, path=str(replacement))
        return index_item(item_id, *args, expected_path=expected_path, **kwargs)

    monkeypatch.setattr(indexer, "index_item", move_before_index)
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **_: None)
    _download(monkeypatch)

    service.search_source_item(context.item_id, job_id=3)

    assert attempted_paths == [context.path]
    assert saved.read_bytes() == CONTENT
    assert replacement_subtitle.read_bytes() == b"Replacement subtitle\n"
    item = repository.get_item(context.item_id)
    assert item.path == str(replacement)
    assert (item.subtitles, item.missing_subtitles, item.indexed_at, item.ffprobe_cache) == (
        initial.subtitles, initial.missing_subtitles, initial.indexed_at, initial.ffprobe_cache)
    (row,) = world.db.execute(sa.select(repository.History)).all()
    assert (row.item_id, row.video_path, row.subtitles_path) == (context.item_id, context.path, str(saved))


def test_shared_folder_owner_disconnect_preserves_the_saved_subtitle_and_history(world, monkeypatch):
    from app.database import update_profile_id_list
    from media_sources import repository, service

    engine = world.db.get_bind()
    if engine.dialect.name != "sqlite":
        pytest.skip("This regression injects a SQLite driver disconnect")
    subtitles = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    saved = subtitles / Path(context.path).with_suffix(".en.srt").name
    generated = subtitles / Path(context.path).with_suffix(".hu.ffsubsync.srt").name
    generated.write_bytes(CONTENT)
    failure = sqlite3.OperationalError("simulated transient disconnect")
    failure.sqlite_errorcode = sqlite3.SQLITE_IOERR
    owner_reads = []
    jobs = []

    def fail_owner_read(cursor, statement, parameters, execution_context):
        if saved.is_file() and statement.startswith("SELECT table_source_items.path"):
            owner_reads.append(statement)
            if len(owner_reads) == 1:
                update_profile_id_list.invalidate()
                raise failure

    def mark_disconnect(exception_context):
        if exception_context.original_exception is failure:
            exception_context.is_disconnect = True

    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: jobs.append(kwargs))
    _download(monkeypatch)
    event.listen(engine, "do_execute", fail_owner_read)
    event.listen(engine, "handle_error", mark_disconnect)
    try:
        service.search_source_item(context.item_id, job_id=3)
    finally:
        event.remove(engine, "do_execute", fail_owner_read)
        event.remove(engine, "handle_error", mark_disconnect)

    assert len(owner_reads) == 2
    assert saved.read_bytes() == CONTENT
    assert generated.read_bytes() == CONTENT
    item = repository.get_item(context.item_id)
    assert str(saved) in item.subtitles and str(generated) in item.subtitles
    assert item.missing_subtitles == "[]"
    assert jobs[-1]["progress_message"] == "1 subtitle(s) downloaded"
    (row,) = world.db.execute(sa.select(repository.History)).all()
    assert (row.item_id, row.video_path, row.subtitles_path) == (context.item_id, context.path, str(saved))
    assert world.db.execute(sa.select(sa.literal(1))).scalar_one() == 1


@pytest.mark.parametrize("stored", ["[", "[None]", "[['en', ['invalid-path'], 1]]", "invalid-language"],
                         ids=["invalid-literal", "invalid-row", "invalid-path", "invalid-language"])
def test_indexing_leaves_a_malformed_stored_listing_incomplete(world, stored):
    from media_sources import indexer, repository

    context = _movie(world)
    if stored == "invalid-language":
        sidecar = Path(context.path).with_suffix(".srt")
        sidecar.write_bytes(CONTENT)
        stored = repr([[123, str(sidecar), len(CONTENT)]])
    repository.update_item(context.item_id, world.db, subtitles=stored)
    initial = repository.get_item(context.item_id)

    assert indexer.index_item(context.item_id) is None

    item = repository.get_item(context.item_id)
    assert (item.subtitles, item.missing_subtitles, item.indexed_at) == (
        initial.subtitles, initial.missing_subtitles, initial.indexed_at)


@pytest.mark.parametrize(("cached_language", "guessed_language", "indexed_language"), [
    ("en:hi:forced", "hu", "hu"),
    ("en:hi:forced", None, None),
    ("aaa", "hu", "hu"),
    ("aaa", None, None),
    ("pob", None, "pt-BR"),
], ids=["content-fallback", "unclassified-fallback", "unsupported-content-fallback",
        "unsupported-unclassified-fallback", "custom-alias"])
def test_search_preserves_history_when_a_cached_language_needs_reclassification(
        world, monkeypatch, cached_language, guessed_language, indexed_language):
    from languages import get_languages
    from media_sources import repository, service
    from subtitles import download
    from subtitles.indexer import utils

    monkeypatch.setattr(get_languages, "languages_dict", get_languages.languages_dict + [
        {"code2": "pt", "code3": "por", "name": "Portuguese", "code3b": "por"}])
    monkeypatch.setattr(utils, "guess_language", lambda text: guessed_language)
    context = _movie(world)
    sidecar = Path(context.path).with_suffix(".srt")
    sidecar.write_bytes(CONTENT)
    saved = Path(context.path).with_suffix(".en.srt")
    _download(monkeypatch)
    best = download.download_best_subtitles
    cached = []
    jobs = []

    def download_with_cached_language(*args, **kwargs):
        # The search refreshes the index before downloading. Replace the cache afterwards,
        # so the real post-save indexer encounters this entry without blocking the search.
        repository.update_item(context.item_id, world.db,
                               subtitles=repr([[cached_language, str(sidecar), len(CONTENT)]]))
        cached.append(repository.get_item(context.item_id))
        return best(*args, **kwargs)

    monkeypatch.setattr(download, "download_best_subtitles", download_with_cached_language)
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: jobs.append(kwargs))

    service.search_source_item(context.item_id, job_id=3)

    assert saved.read_bytes() == sidecar.read_bytes() == CONTENT
    (row,) = world.db.execute(sa.select(repository.History)).all()
    assert (row.item_id, row.subtitles_path, row.language) == (context.item_id, str(saved), "en")
    assert jobs[-1]["progress_message"] == "1 subtitle(s) downloaded"
    item = repository.get_item(context.item_id)
    if indexed_language is not None:
        listing = {path: language for language, path, _ in ast.literal_eval(item.subtitles)}
        assert listing == {str(saved): "en", str(sidecar): indexed_language}
        assert item.missing_subtitles == "[]"
        assert cached_language not in item.subtitles
    else:
        (initial,) = cached
        assert (item.subtitles, item.missing_subtitles, item.indexed_at) == (
            initial.subtitles, initial.missing_subtitles, initial.indexed_at)


@pytest.mark.parametrize(("cached_language", "indexed_language"), [
    ("pob:forced", "pt-BR:forced"),
    ("pob:hi", "pt-BR:hi"),
], ids=["forced", "hearing-impaired"])
def test_a_cached_custom_language_keeps_its_variant_across_index_passes(
        world, monkeypatch, cached_language, indexed_language):
    from languages import get_languages
    from media_sources import indexer, repository
    from subtitles.indexer import utils

    monkeypatch.setattr(get_languages, "languages_dict", get_languages.languages_dict + [
        {"code2": "pt", "code3": "por", "name": "Portuguese", "code3b": "por"}])
    guess = Mock(return_value=None)
    monkeypatch.setattr(utils, "guess_language", guess)
    context = _movie(world)
    sidecar = Path(context.path).with_suffix(".srt")
    sidecar.write_bytes(CONTENT)
    repository.update_item(context.item_id, world.db,
                           subtitles=repr([[cached_language, str(sidecar), len(CONTENT)]]))
    expected = [[indexed_language, str(sidecar), len(CONTENT)]]

    for _ in range(2):
        assert indexer.index_item(context.item_id) == expected
        assert ast.literal_eval(repository.get_item(context.item_id).subtitles) == expected

    guess.assert_not_called()


@pytest.mark.parametrize(("locale", "suffix", "variant"), [
    ("en", "", ""),
    ("en", ".forced", ":forced"),
    ("en", ".hi", ":hi"),
    ("pt", "", ""),
    ("pt", ".forced", ":forced"),
    ("pt", ".hi", ":hi"),
    ("pt", ".forced.hi", ":forced"),
], ids=["en-normal", "en-forced", "en-hi", "pt-normal", "pt-forced", "pt-hi", "pt-both"])
def test_sync_sidecar_variants_keep_movie_indexer_precedence_across_passes(world, monkeypatch, locale, suffix, variant):
    from languages import get_languages
    from media_sources import indexer, repository

    monkeypatch.setattr(get_languages, "languages_dict", get_languages.languages_dict + [
        {"code2": "pt", "code3": "por", "name": "Portuguese", "code3b": "por"}])
    context = _movie(world)
    sidecar = Path(context.path).with_suffix(f".{locale}{suffix}.ffsubsync.srt")
    sidecar.write_bytes(CONTENT)
    expected = [[f"{locale}{variant}:sync-ffsubsync", str(sidecar), len(CONTENT)]]

    for _ in range(2):
        assert indexer.index_item(context.item_id) == expected
        assert ast.literal_eval(repository.get_item(context.item_id).subtitles) == expected


@pytest.mark.parametrize("after_save", [False, True], ids=["ordinary-index", "post-save-index"])
def test_a_malformed_sidecar_keeps_the_index_incomplete_and_preserves_a_saved_result(
        world, monkeypatch, after_save):
    from media_sources import indexer, repository, service
    from subtitles import download
    from subtitles.indexer import utils

    context = _movie(world)
    initial = repository.get_item(context.item_id)
    bad = Path(context.path).with_suffix(".hu.srt")
    # Exercise the real read and decode when an encoding detector guesses incorrectly.
    monkeypatch.setattr(utils, "detect", lambda text: {"encoding": "utf-8"})
    jobs = []
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: jobs.append(kwargs))
    saved = Path(context.path).with_suffix(".en.srt")

    if after_save:
        write = download._write_source_download

        def add_malformed_after_write(*args, **kwargs):
            written = write(*args, **kwargs)
            if written:
                bad.write_bytes(CONTENT + b"\xff")
            return written

        # A malformed file present during publication now prevents the write.
        # Introduce it after the real save to exercise post-save recovery.
        monkeypatch.setattr(download, "_write_source_download", add_malformed_after_write)
        _download(monkeypatch)
        service.search_source_item(context.item_id, job_id=3)
        assert saved.read_bytes() == CONTENT
        (row,) = world.db.execute(sa.select(repository.History)).all()
        assert (row.item_id, row.subtitles_path) == (context.item_id, str(saved))
        assert jobs[-1]["progress_message"] == "1 subtitle(s) downloaded"
    else:
        bad.write_bytes(CONTENT + b"\xff")
        assert indexer.index_item(context.item_id) is None

    assert bad.read_bytes() == CONTENT + b"\xff"
    item = repository.get_item(context.item_id)
    assert (item.subtitles, item.missing_subtitles, item.indexed_at) == (
        initial.subtitles, initial.missing_subtitles, initial.indexed_at)


@pytest.mark.parametrize("failure", ["programming-error", "query-cancelled", "job-cancelled", "runtime-error"])
def test_post_save_indexing_propagates_errors_from_the_real_owner_helper(world, monkeypatch, failure):
    from app.jobs_queue import JobCancelled
    from media_sources import indexer

    class QueryCancelled(Exception):
        pgcode = "57014"

    errors = {
        "programming-error": sa.exc.ProgrammingError("invalid SQL", {}, sqlite3.ProgrammingError("invalid SQL")),
        "query-cancelled": sa.exc.OperationalError("SELECT table_source_items.path", {}, QueryCancelled()),
        "job-cancelled": JobCancelled(),
        "runtime-error": RuntimeError("ownership query bug"),
    }
    error = errors[failure]
    subtitles = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    generated = subtitles / Path(context.path).with_suffix(".en.ffsubsync.srt").name
    generated.write_bytes(CONTENT)
    owner_query = Mock(side_effect=error)
    monkeypatch.setattr(indexer.SourceOutputOwnerIndex, "_load_owners", owner_query)

    with pytest.raises(type(error)) as raised:
        indexer.index_saved_subtitle(context.item_id, expected_path=context.path)

    assert raised.value is error
    owner_query.assert_called_once()


@pytest.mark.parametrize("failure", ["programming-error", "missing-table", "invalidated-programming-error",
                                    "invalidated-missing-table", "invalid-data", "invalidated-invalid-data",
                                    "runtime-error", "cancelled", "interrupt"])
def test_post_save_indexing_propagates_programming_and_control_errors(source_db, monkeypatch, failure):
    from app.jobs_queue import JobCancelled
    from media_sources import indexer

    errors = {
        "programming-error": sa.exc.ProgrammingError("invalid SQL", {}, sqlite3.ProgrammingError("invalid SQL")),
        "missing-table": sa.exc.OperationalError("SELECT * FROM absent", {},
                                                sqlite3.OperationalError("no such table: absent")),
        "invalidated-programming-error": sa.exc.ProgrammingError(
            "invalid SQL", {}, sqlite3.ProgrammingError("invalid SQL"), connection_invalidated=True),
        "invalidated-missing-table": sa.exc.OperationalError(
            "SELECT * FROM absent", {}, sqlite3.OperationalError("no such table: absent"),
            connection_invalidated=True),
        "invalid-data": sa.exc.IntegrityError("UPDATE table_source_items", {},
                                            sqlite3.IntegrityError("constraint failed")),
        "invalidated-invalid-data": sa.exc.IntegrityError(
            "UPDATE table_source_items", {}, sqlite3.IntegrityError("constraint failed"),
            connection_invalidated=True),
        "runtime-error": RuntimeError("indexing bug"),
        "cancelled": JobCancelled(),
        "interrupt": KeyboardInterrupt(),
    }
    error = errors[failure]
    monkeypatch.setattr(indexer, "index_item", Mock(side_effect=error))

    with pytest.raises(type(error)) as raised:
        indexer.index_saved_subtitle(1, expected_path="/fixture/Film.mkv")

    assert raised.value is error


@pytest.mark.parametrize(("sqlstate", "invalidated", "recoverable"), [
    ("08006", False, True), ("40001", False, True), ("53100", False, True), ("55P03", False, True),
    ("57P01", False, True), ("25P02", False, True), ("42P01", False, False), ("42501", False, False),
    ("23505", False, False), ("57014", False, False), ("57014", True, False), ("42P01", True, False),
])
def test_post_save_indexing_classifies_postgres_database_failures(
        source_db, monkeypatch, sqlstate, invalidated, recoverable):
    from media_sources import indexer

    class PostgresFailure(Exception):
        pgcode = sqlstate

    error = sa.exc.OperationalError("UPDATE table_source_items", {}, PostgresFailure("database failure"),
                                    connection_invalidated=invalidated)
    monkeypatch.setattr(indexer, "index_item", Mock(side_effect=error))

    if recoverable:
        assert indexer.index_saved_subtitle(1, expected_path="/fixture/Film.mkv") is False
    else:
        with pytest.raises(sa.exc.OperationalError) as raised:
            indexer.index_saved_subtitle(1, expected_path="/fixture/Film.mkv")
        assert raised.value is error


def test_search_retries_the_active_parser_when_previous_parser_metadata_remains(world, monkeypatch):
    from app.config import settings
    from media_sources import analysis, repository, service
    from utilities import video_analyzer

    monkeypatch.setattr(settings.general, "embedded_subtitles_parser", "ffprobe")
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **_: None)
    probe = Mock(return_value={"ffprobe": {"audio": [{"language": "eng"}]}, "mediainfo": {}})
    monkeypatch.setattr(video_analyzer, "parse_video_metadata", probe)
    context = _movie(world)
    original = repository.get_item(context.item_id)
    assert analysis.read_metadata(original.ffprobe_cache)["ffprobe"]

    monkeypatch.setattr(settings.general, "embedded_subtitles_parser", "mediainfo")
    probe.return_value = None
    _download(monkeypatch)
    service.search_source_item(context.item_id, job_id=3)
    indexed = repository.get_item(context.item_id)
    assert indexed.missing_subtitles == "[]" and indexed.ffprobe_cache == original.ffprobe_cache

    probe.return_value = {"ffprobe": {}, "mediainfo": {"audio": [{"language": "hun"}]}}
    service.search_source_item(context.item_id, job_id=4)

    refreshed = repository.get_item(context.item_id)
    assert analysis.read_metadata(refreshed.ffprobe_cache)["mediainfo"] == probe.return_value["mediainfo"]
    assert refreshed.missing_subtitles == "[]"


def test_a_search_with_nothing_missing_or_no_profile_does_not_ask_a_provider(world, monkeypatch):
    from media_sources import repository, service
    from media_sources.indexer import index_item

    context = _movie(world)
    asked = _download(monkeypatch)
    monkeypatch.setattr(service.jobs_queue, "update_job_progress", lambda **kwargs: None)
    Path(context.path).with_suffix(".en.srt").write_bytes(CONTENT)
    index_item(context.item_id)
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


def test_check_if_still_required_rechecks_the_items_indexed_subtitles(world, monkeypatch):
    from media_sources import repository
    from media_sources.indexer import index_item

    context = _movie(world)
    asked = _download(monkeypatch)
    existing = Path(context.path).with_suffix(".en.srt")
    existing.write_bytes(CONTENT)
    index_item(context.item_id)
    repository.update_item(context.item_id, world.db, missing_subtitles="['en']")
    assert _generate(context, check_if_still_required=True) == []
    assert "video" not in asked
    existing.unlink()
    repository.update_item(context.item_id, world.db, missing_subtitles="[]")
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

    assert (result.path, result.subs_path, result.provider) == (context.path, subtitle.storage_path, "fake")
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


@pytest.mark.parametrize("change", ["move", "delete-item", "delete-source"])
def test_source_download_records_history_when_the_item_changes_after_save(world, monkeypatch, isolated_jobs, change):
    from app import notifier
    from media_sources import repository, service
    from subtitles import download, processing

    context = _movie(world)
    saved = Path(context.path).with_suffix(".en.srt")
    replacement = video(world.root / "Replacement.mkv")
    notify = Mock()
    monkeypatch.setattr(notifier, "send_notifications_source", notify)
    _download(monkeypatch)

    def process_after_change(**kwargs):
        assert saved.read_bytes() == CONTENT
        if change == "move":
            repository.update_item(context.item_id, world.db, path=str(replacement), title="Replacement")
        elif change == "delete-item":
            repository.delete_items([context.item_id], world.db)
        else:
            repository.delete_source(context.source_id, world.db)
        return processing.process_subtitle(**kwargs)

    monkeypatch.setattr(download, "process_subtitle", process_after_change)
    service.search_source_item(context.item_id, translation_gate=False)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())

    (result,) = world.db.execute(sa.select(repository.History)).all()
    assert result.action == 1 and result.provider == "fake" and result.language == "en"
    assert (result.video_path, result.subtitles_path, result.item_title, result.source_name) == (
        context.path, str(saved), "Film", "Local")
    assert result.item_id == (context.item_id if change == "move" else None)
    assert result.source_id == (None if change == "delete-source" else context.source_id)
    assert not replacement.with_suffix(".en.srt").exists()
    notify.assert_not_called()


def test_source_publication_holds_the_item_until_the_registry_writer_can_continue(world):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from media_sources import repository
    from media_sources.db import source_transaction
    from media_sources.indexer import SourcePublicationOwnerIndex

    context = _movie(world)
    replacement = video(world.root / "Replacement.mkv")
    started = Event()

    def move():
        started.set()
        with source_transaction() as connection:
            repository.update_item(context.item_id, connection, path=str(replacement))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SourcePublicationOwnerIndex().publication(context) as (current, connection, _):
            assert current == context and connection.in_transaction()
            pending = pool.submit(move)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.25)
            assert repository.get_item(context.item_id, connection).path == context.path
        pending.result(timeout=5)

    assert repository.get_item(context.item_id).path == str(replacement)


@pytest.mark.parametrize("same_stem", [False, True], ids=["distinct-owner", "competing-owner"])
def test_source_provider_rechecks_scanner_admission_after_content_detection(world, monkeypatch, isolated_jobs,
                                                                         same_stem):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from media_sources import repository, scanner, service
    from subtitles import download
    from subtitles.indexer import utils
    from utilities import video_analyzer

    monkeypatch.setattr(video_analyzer, "parse_video_metadata", lambda *args, **kwargs: {
        "ffprobe": {"audio": [{"language": "eng"}]}, "mediainfo": {},
    })
    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    media = Path(context.path)
    other_source, other_library, other_root = add_source(world.db, world.tmp_path, name="Other", folder="Other")
    other = video(other_root / (media.name if same_stem else "Unrelated.2001.mkv"))
    admitted = Event()
    index_item = scanner.index_item

    def record_admitted(item_id, *args, **kwargs):
        assert repository.get_item(item_id).path == str(other)
        admitted.set()
        return index_item(item_id, *args, **kwargs)

    def fetch(videos, languages, **kwargs):
        assert {str(language) for language in languages} == {"en"}
        media.with_suffix(".srt").write_bytes(CONTENT)
        return {next(iter(videos)): [FakeSubtitle()]}

    monkeypatch.setattr(scanner, "index_item", record_admitted)
    monkeypatch.setattr(download, "download_best_subtitles", fetch)
    pending = []
    detected_after_admission = []

    with ThreadPoolExecutor(max_workers=1) as pool:
        def detect_language(text):
            if not pending:
                pending.append(pool.submit(scanner.run_scan, other_source, other_library))
                # The scanner may still be waiting to index the new item while this video is locked.
                detected_after_admission.append(admitted.wait(10))
            return "fr"

        monkeypatch.setattr(utils, "guess_language", detect_language)
        service.search_source_item(context.item_id, translation_gate=False)
        assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
        (scan,) = pending[0].result(timeout=10)
        assert scan.inserted == 1

    assert detected_after_admission == [True]
    assert repository.get_item_by_path(str(other)) is not None
    saved = folder / media.with_suffix(".en.srt").name
    assert saved.is_file() is not same_stem
    assert len(world.db.execute(sa.select(repository.History)).all()) == (0 if same_stem else 1)


def test_source_shared_publication_serializes_a_scanner_admission(world, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from media_sources import repository, scanner
    from media_sources.indexer import SourcePublicationOwnerIndex
    from subtitles.tools.subsync_engines import sync_output_owner_is_unique
    from utilities import video_analyzer

    monkeypatch.setattr(video_analyzer, "parse_video_metadata", lambda *args, **kwargs: {
        "ffprobe": {"audio": [{"language": "eng"}]}, "mediainfo": {},
    })
    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    media = Path(context.path)
    destination = folder / media.with_suffix(".en.srt").name
    other_source, other_library, other_root = add_source(world.db, world.tmp_path, name="Other", folder="Other")
    other = video(other_root / media.name)
    admitting = Event()
    insert_batch = scanner._insert_batch

    def admit(*args, **kwargs):
        admitting.set()
        return insert_batch(*args, **kwargs)

    monkeypatch.setattr(scanner, "_insert_batch", admit)
    owners = SourcePublicationOwnerIndex()

    def prepare():
        assert sync_output_owner_is_unique(context.path, destination, ownership_index=owners)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with owners.publication(context, prepare, destination=destination) as (current, connection, _):
            assert current == context
            pending = pool.submit(scanner.run_scan, other_source, other_library)
            assert admitting.wait(10)
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.25)
            assert repository.get_item_by_path(str(other), connection) is None
        (result,) = pending.result(timeout=10)

    assert result.inserted == 1 and repository.get_item_by_path(str(other)) is not None


def _legacy_owner_insert(world, owner, path):
    from app.database import TableArrInstances, TableMovies, TableSportsEvents, TableSportsLeagues

    instance = world.db.execute(sa.insert(TableArrInstances).values(
        kind=owner, name="Fixture owner", stable_key=owner, port=5000,
    )).inserted_primary_key[0]
    if owner == "radarr":
        return sa.insert(TableMovies).values(arr_instance_id=instance, path=str(path), title="Other film",
                                             radarrId=1, tmdbId="1")
    league = world.db.execute(sa.insert(TableSportsLeagues).values(
        arr_instance_id=instance, sportarrLeagueId=2, title="Fixture league",
    )).inserted_primary_key[0]
    return sa.insert(TableSportsEvents).values(arr_instance_id=instance, league_id=league, sportarrEventId=3,
                                               file_id=4, path=str(path), title="Other event")


@pytest.mark.parametrize("owner", ["radarr", "sportarr"])
def test_shared_source_download_rechecks_legacy_owners_added_after_preparation(world, monkeypatch, owner):
    from media_sources.db import source_transaction
    from media_sources.indexer import SourcePublicationOwnerIndex
    from subtitles.download import _write_source_download

    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world, "Film.mkv")
    other = video(world.tmp_path / "Other" / "Film.mkv")
    destination = folder / "Film.en.srt"
    insertion = _legacy_owner_insert(world, owner, other)
    registry_snapshot = SourcePublicationOwnerIndex._registry_snapshot
    committed = []

    def snapshot_then_admit(self, target=None):
        snapshot = registry_snapshot(self, target)
        if target is None and not committed:
            with source_transaction() as connection:
                connection.execute(insertion)
            committed.append(True)
        return snapshot

    monkeypatch.setattr(SourcePublicationOwnerIndex, "_registry_snapshot", snapshot_then_admit)
    assert not _write_source_download(context.path, str(destination), CONTENT, source=context,
                                      language=Language("eng"), recheck_missing=False, is_upgrade=False,
                                      written_paths=[], on_publish=None)
    assert committed == [True]
    assert not destination.exists()


@pytest.mark.parametrize("owner", ["radarr", "sportarr"])
def test_shared_source_publication_holds_concurrent_legacy_admission_until_exit(world, monkeypatch, owner):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from media_sources.db import source_transaction
    from media_sources.indexer import SourcePublicationOwnerIndex
    from subtitles.tools.subsync_engines import sync_output_owner_is_unique

    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world, "Film.mkv")
    other = video(world.tmp_path / "Other" / "Film.mkv")
    destination = folder / "Film.en.srt"
    insertion = _legacy_owner_insert(world, owner, other)
    started = Event()
    owners = SourcePublicationOwnerIndex()

    def prepare():
        assert sync_output_owner_is_unique(context.path, destination, ownership_index=owners)

    def admit():
        started.set()
        with source_transaction() as connection:
            if connection.dialect.name == "postgresql":
                connection.exec_driver_sql("SET LOCAL lock_timeout = '3s'")
            connection.execute(insertion)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with owners.publication(context, prepare, destination=destination) as (current, _, _):
            assert current == context
            pending = pool.submit(admit)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.15)
        pending.result(timeout=4)
    assert not sync_output_owner_is_unique(context.path, destination, ownership_index=SourcePublicationOwnerIndex())


@pytest.mark.parametrize('mapping', ['radarr-global', 'radarr-instance', 'sportarr-global'])
def test_source_download_rechecks_mappings_for_an_existing_owner_in_a_shared_relative_folder(
        world, monkeypatch, mapping):
    import json

    from app.config import settings
    from app.database import TableArrInstances
    from discover.metadata import CONFIG_LOCK
    from media_sources.db import source_transaction
    from media_sources.indexer import SourcePublicationOwnerIndex
    from subtitles.download import _write_source_download
    from subtitles.tools.subsync_engines import sync_output_owner_is_unique
    from utilities.path_mappings import path_mappings

    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', 'Subs')
    monkeypatch.setattr(path_mappings, 'path_mapping_series', [])
    monkeypatch.setattr(path_mappings, 'path_mapping_movies', [])
    monkeypatch.setattr(settings.general, 'path_mappings_sports', [])
    context = _movie(world, 'Film.mkv')
    folder = Path(context.path).parent / 'Subs'
    folder.mkdir()
    other = video(world.tmp_path / 'mapped' / 'Film.mkv')
    (other.parent / 'Subs').symlink_to(folder, target_is_directory=True)
    remote = str(world.tmp_path / 'remote')
    owner, scope = mapping.split('-')
    insertion = _legacy_owner_insert(world, owner, Path(remote) / 'Film.mkv')
    with source_transaction() as connection:
        connection.execute(insertion)
    destination = folder / 'Film.en.srt'
    assert sync_output_owner_is_unique(context.path, str(destination), ownership_index=SourcePublicationOwnerIndex())

    snapshot = SourcePublicationOwnerIndex._registry_snapshot
    applied = []

    def prepare_then_change_mapping(self, target=None):
        rows = snapshot(self, target)
        if target is None and not applied:
            pairs = [[remote, str(other.parent)]]
            with CONFIG_LOCK:
                if scope == 'instance':
                    with source_transaction() as connection:
                        connection.execute(sa.update(TableArrInstances).where(TableArrInstances.kind == owner)
                                           .values(path_mappings=json.dumps(pairs)))
                elif owner == 'sportarr':
                    monkeypatch.setattr(settings.general, 'path_mappings_sports', pairs)
                else:
                    monkeypatch.setattr(settings.general, 'path_mappings', [])
                    monkeypatch.setattr(settings.general, 'path_mappings_movie', pairs)
                    path_mappings.update()
            applied.append(True)
        return rows

    monkeypatch.setattr(SourcePublicationOwnerIndex, '_registry_snapshot', prepare_then_change_mapping)
    assert not _write_source_download(context.path, str(destination), CONTENT, source=context,
                                      language=Language('eng'), recheck_missing=False, is_upgrade=False,
                                      written_paths=[], on_publish=None)
    assert applied == [True] and not destination.exists()
    assert not sync_output_owner_is_unique(context.path, str(destination), ownership_index=SourcePublicationOwnerIndex())


@pytest.mark.parametrize('filename', ['Film.mkv', 'Other.mkv'])
@pytest.mark.parametrize('alias_location', ['mapped folder', 'media folder'])
def test_source_download_pairs_a_mapped_alias_stem_with_its_relative_subtitle_folder(
        world, monkeypatch, filename, alias_location):
    from app.config import settings
    from utilities.helper import get_target_folder
    from utilities.path_mappings import path_mappings

    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', 'Subs')
    context = _movie(world, filename)
    folder = world.tmp_path / 'Shared subtitles'
    folder.mkdir()
    media_folder = Path(context.path).parent
    (media_folder / 'Subs').symlink_to(folder, target_is_directory=True)
    actual = video(world.tmp_path / 'library' / 'Other.mkv')
    alias_folder = media_folder if alias_location == 'media folder' else world.tmp_path / 'aliases'
    alias = alias_folder / ('Film.mp4' if alias_location == 'media folder' else 'Film.mkv')
    if alias_folder != media_folder:
        alias_folder.mkdir()
        (alias_folder / 'Subs').symlink_to(folder, target_is_directory=True)
    alias.symlink_to(actual)
    remote = world.tmp_path / 'remote'
    monkeypatch.setattr(path_mappings, 'path_mapping_movies', [[str(remote), str(alias.parent)]])
    world.db.execute(_legacy_owner_insert(world, 'radarr', remote / alias.name))
    assert Path(get_target_folder(str(alias), create=False)).resolve() == folder
    assert Path(get_target_folder(str(actual), create=False)).resolve() != folder

    destination = folder / Path(filename).with_suffix('.en.srt')
    existing = folder / 'Film.en.alass.srt'
    existing_content = CONTENT.replace(b'Downloaded', b'Alias subtitle')
    existing.write_bytes(existing_content)
    _download(monkeypatch)

    results = _generate(context, check_if_still_required=True)

    if filename == 'Film.mkv':
        assert results == []
        assert not destination.exists()
        assert not world.published
    else:
        assert len(results) == 1
        assert destination.read_bytes() == CONTENT
    assert existing.read_bytes() == existing_content


@pytest.mark.parametrize('competing_parent', ['separate', 'resolved'])
def test_source_download_uses_its_logical_filename_in_a_shared_relative_folder(world, monkeypatch, competing_parent):
    from app.config import settings
    from media_sources.context import resolve_item
    from media_sources.indexer import index_item

    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', 'Subs')
    shared = world.tmp_path / 'Shared subtitles'
    shared.mkdir()
    (world.root / 'Subs').symlink_to(shared, target_is_directory=True)
    actual = video(world.tmp_path / 'library' / 'Other.mkv')
    alias = world.root / 'Film.mkv'
    alias.symlink_to(actual)
    item_id = add_item(world.db, world.source_id, world.library_id, alias, title='Film')
    index_item(item_id)
    context = resolve_item(item_id)
    assert context.path == str(alias)
    if competing_parent == 'resolved':
        # The resolved movie has a true sibling named for the planned
        # subtitle, but that sibling's directory has its own relative output.
        competing = video(actual.parent / 'Film.en.mkv')
        (competing.parent / 'Subs').mkdir()
        existing = competing.parent / 'Subs' / 'Film.en.srt'
    else:
        competing = video(world.tmp_path / 'competing' / 'Other.mkv')
        (competing.parent / 'Subs').symlink_to(shared, target_is_directory=True)
        existing = shared / 'Other.en.srt'
    world.db.execute(_legacy_owner_insert(world, 'radarr', competing))
    existing_content = CONTENT.replace(b'Downloaded', b'Existing subtitle')
    existing.write_bytes(existing_content)
    _download(monkeypatch)

    assert len(_generate(context, check_if_still_required=True)) == 1
    assert (shared / 'Film.en.srt').read_bytes() == CONTENT
    assert existing.read_bytes() == existing_content


@pytest.mark.parametrize("layout", ["relative", "absolute"])
def test_source_publication_survives_an_unrelated_scanner_writer(world, monkeypatch, layout):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from contextlib import contextmanager
    from threading import Event

    from app.config import settings
    from media_sources import indexer, repository, scanner
    from subtitles.download import _write_source_download
    from utilities import video_analyzer

    if world.db.get_bind().dialect.name != "postgresql":
        pytest.skip("SQLite serializes writers independently of destination geometry")
    monkeypatch.setattr(video_analyzer, "parse_video_metadata", lambda *args, **kwargs: {
        "ffprobe": {"audio": [{"language": "eng"}]}, "mediainfo": {},
    })
    if layout == "relative":
        monkeypatch.setattr(settings.general, "subfolder", "relative")
        monkeypatch.setattr(settings.general, "subfolder_custom", "Subs")
        folder = world.root / "Subs"
        folder.mkdir()
    else:
        folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    destination = folder / Path(context.path).with_suffix(".en.srt").name
    other_source, other_library, other_root = add_source(world.db, world.tmp_path, name="Other", folder="Other")
    other = video(other_root / "Unrelated.2002.mkv")
    inserted, release_scanner, attempted = Event(), Event(), Event()
    insert_item = repository.insert_item
    publication = indexer.source_item_publication

    def pause_insert(*args, **kwargs):
        item_id = insert_item(*args, **kwargs)
        if kwargs["path"] == str(other):
            inserted.set()
            assert release_scanner.wait(10)
        return item_id

    @contextmanager
    def observe_publication(source, *, shared=False):
        assert shared
        attempted.set()
        with publication(source, shared=shared) as result:
            yield result

    monkeypatch.setattr(repository, "insert_item", pause_insert)
    monkeypatch.setattr(indexer, "source_item_publication", observe_publication)

    with ThreadPoolExecutor(max_workers=2) as pool:
        scan = pool.submit(scanner.run_scan, other_source, other_library)
        try:
            assert inserted.wait(10)
            save = pool.submit(_write_source_download, context.path, str(destination), CONTENT,
                               source=context, language=Language("eng"), recheck_missing=True,
                               is_upgrade=False, written_paths=[], on_publish=None)
            assert attempted.wait(10)
            with pytest.raises(TimeoutError):
                save.result(timeout=0.10)
            assert not destination.exists()
        finally:
            release_scanner.set()
        assert scan.result(timeout=10)[0].inserted == 1
        assert save.result(timeout=10)
    assert destination.read_bytes() == CONTENT


def test_source_shared_publication_yields_to_a_registry_row_writer(world, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from contextlib import contextmanager
    from threading import Event

    from media_sources import indexer, repository
    from media_sources.db import source_transaction
    from subtitles.download import _write_source_download

    if world.db.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL row lock order")
    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    destination = folder / Path(context.path).with_suffix(".en.srt").name
    held, can_update, attempted = Event(), Event(), Event()
    publication = indexer.source_item_publication

    def update():
        with source_transaction() as connection:
            assert repository.get_item(context.item_id, connection, for_update=True) is not None
            held.set()
            assert can_update.wait(10)
            repository.update_item(context.item_id, connection, title="Updated")

    @contextmanager
    def observe_publication(source, *, shared=False):
        assert shared
        attempted.set()
        with publication(source, shared=shared) as result:
            yield result

    monkeypatch.setattr(indexer, "source_item_publication", observe_publication)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(update)
        try:
            assert held.wait(10)
            save = pool.submit(_write_source_download, context.path, str(destination), CONTENT,
                               source=context, language=Language("eng"), recheck_missing=True,
                               is_upgrade=False, written_paths=[], on_publish=None)
            assert attempted.wait(10)
            with pytest.raises(TimeoutError):
                save.result(timeout=0.10)
        finally:
            can_update.set()
        writer.result(timeout=10)
        assert save.result(timeout=10)
    assert repository.get_item(context.item_id).title == "Updated"
    assert destination.read_bytes() == CONTENT


def test_source_translation_rechecks_library_enablement_after_preparation(world, monkeypatch):
    from contextlib import contextmanager

    from media_sources import indexer, repository
    from subtitles.tools.subsync_engines import write_subtitle_file
    from subtitles.tools.translate.core.translator_utils import (SourceTranslationChanged, translation_write_kwargs,
                                                               translated_destination_path)

    _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    original = Path(context.path).with_suffix(".en.srt")
    original.write_bytes(CONTENT)
    destination = translated_destination_path(context.path, "hu", False, False)
    publication = indexer.SourcePublicationOwnerIndex.publication

    @contextmanager
    def disable_after_preparation(self, source, prepare, **kwargs):
        def disable():
            prepared = prepare()
            repository.update_library(context.library_id, world.db, enabled=0)
            return prepared
        with publication(self, source, disable, **kwargs) as result:
            yield result

    monkeypatch.setattr(indexer.SourcePublicationOwnerIndex, "publication", disable_after_preparation)
    translator = SimpleNamespace(source=context, video_path=context.path, source_srt_file=str(original),
                                 dest_srt_file=destination, from_lang="en", orig_to_lang="hu", forced=False, hi=False)
    with pytest.raises(SourceTranslationChanged, match="disabled"):
        write_subtitle_file(context.path, destination, CONTENT, **translation_write_kwargs(translator, None))

    assert not Path(destination).exists()


@pytest.mark.parametrize('alternate_format', ['ass', 'ssa'])
@pytest.mark.parametrize('deny_shared_listing', [False, True])
@pytest.mark.parametrize('replace_alternate', [False, True])
def test_source_combine_sweeps_stale_siblings_without_scanning_shared_folder_under_writer(
        world, monkeypatch, replace_alternate, deny_shared_listing, alternate_format):
    import os
    from contextlib import contextmanager

    from media_sources import indexer
    from subtitles.tools.combine import main as combine

    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    media = Path(context.path)
    media.with_suffix(".en.srt").write_bytes(CONTENT)
    media.with_suffix(".hu.srt").write_bytes(CONTENT)
    superseded = folder / media.with_suffix(f".en.combined-hu.{alternate_format}").name
    superseded.write_bytes(CONTENT)
    superseded_variant = superseded.with_suffix(f'.ffsubsync.{alternate_format}')
    current_variant = superseded.with_suffix('.alass.srt')
    superseded_variant.write_bytes(CONTENT)
    current_variant.write_bytes(CONTENT)
    replacement = CONTENT.replace(b'Downloaded', b'Updated')
    publication = indexer.source_item_publication
    scandir = os.scandir
    locked_scans = []
    exists_before_unlock = []
    writer_open = False

    @contextmanager
    def observe_writer(source, *, shared=False):
        nonlocal writer_open
        with publication(source, shared=shared) as result:
            writer_open = True
            try:
                if replace_alternate:
                    updated = superseded.with_suffix('.tmp')
                    updated.write_bytes(replacement)
                    updated.replace(superseded)
                yield result
                exists_before_unlock.append(
                    (superseded.exists(), superseded_variant.exists(), current_variant.exists()))
            finally:
                writer_open = False

    def observe_scan(path):
        shared = Path(path).resolve() == folder.resolve()
        if shared and deny_shared_listing:
            raise PermissionError('Shared subtitle folder cannot be listed')
        if writer_open and shared:
            locked_scans.append(path)
        return scandir(path)

    monkeypatch.setattr(indexer, "source_item_publication", observe_writer)
    monkeypatch.setattr(os, "scandir", observe_scan)
    result = combine.try_combine_for_video(context.path, "movie", languages=["en", "hu"], format="srt", source=context)

    assert result.status == "built" and Path(result.path).is_file()
    assert exists_before_unlock == [(replace_alternate, replace_alternate, False)]
    if replace_alternate:
        assert superseded.read_bytes() == replacement
        assert superseded_variant.read_bytes() == CONTENT
    assert not locked_scans


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
    owners = SourceOutputOwnerIndex()._load_owners()
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


def test_automatic_source_sync_keeps_a_mapped_arr_owners_exact_variant(world, monkeypatch):
    from app.config import settings
    from media_sources import repository
    from subtitles import processing, sync
    from subtitles.tools import subsyncer
    from utilities.path_mappings import path_mappings

    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world, 'Film.mkv')
    competing = video(world.tmp_path / 'arr' / 'Film.en.ffsubsync.mkv')
    remote = world.tmp_path / 'remote'
    monkeypatch.setattr(path_mappings, 'path_mapping_movies', [[str(remote), str(competing.parent)]])
    world.db.execute(_legacy_owner_insert(world, 'radarr', remote / competing.name))
    protected = folder / 'Film.en.ffsubsync.srt'
    protected_content = CONTENT.replace(b'Downloaded', b'Arr subtitle')
    protected.write_bytes(protected_content)
    obsolete = folder / 'Film.en.alass.srt'
    synced_content = CONTENT.replace(b'Downloaded', b'Synchronized')
    calls = []

    def align(self, engine, output_path, video_path, no_fix_framerate):
        calls.append((engine, video_path))
        output_path.write_bytes(synced_content)
        # A generated variant owned by this source appears after the download
        # completed and before auto sync claims its final output.
        obsolete.write_bytes(CONTENT)
        return {'offset_seconds': 1.5, 'framerate_scale_factor': 1.0, 'quality_of_fit': 0.9}

    _download(monkeypatch)
    monkeypatch.setattr(processing, '_defaul_sync_checker', lambda subtitle: True)
    monkeypatch.setattr(sync, '_report_progress', lambda *a, **k: None)
    monkeypatch.setattr(subsyncer.SubSyncer, '_run_external_engine', align)
    monkeypatch.setattr(settings.subsync, 'output_mode', 'overwrite')
    monkeypatch.setattr(settings.subsync, 'enabled_engines', ['alass'])
    assert len(_generate(context, job_id=1, auto_translate=False)) == 1

    assert calls == [('alass', context.path)]
    assert (folder / 'Film.en.srt').read_bytes() == synced_content
    assert protected.read_bytes() == protected_content
    assert not obsolete.exists()
    assert world.db.scalar(sa.select(repository.History.c.action).where(repository.History.c.item_id == context.item_id)) == 5


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


def test_completed_source_translation_keeps_history_during_owner_removal(world, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from media_sources import repository
    from media_sources.db import source_transaction
    from subtitles.tools.translate.core.translator_utils import create_process_result, record_source_translation

    context = _movie(world)
    translated = Path(context.path).with_suffix(".hu.srt")
    translated.write_bytes(CONTENT)
    result = create_process_result("Fixture translation", context.path, "hu", False, False,
                                   str(translated), "movie", source=context)
    deleting = Event()
    insert_history = repository.insert_history

    def delete_source():
        deleting.set()
        with source_transaction() as connection:
            if connection.dialect.name == "postgresql":
                connection.exec_driver_sql("SET LOCAL lock_timeout = '3s'")
            repository.delete_source(world.source_id, connection)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = []

        def start_removal(target, **fields):
            assert target.in_transaction()
            pending.append(pool.submit(delete_source))
            assert deleting.wait(2)
            with pytest.raises(TimeoutError):
                pending[0].result(timeout=0.15)
            return insert_history(target, **fields)

        monkeypatch.setattr(repository, "insert_history", start_removal)
        assert record_source_translation(context, result)
        pending[0].result(timeout=4)

    (row,) = world.db.execute(sa.select(repository.History)).all()
    assert row.action == 6 and row.source_id is None and row.item_id is None
    assert (row.source_name, row.item_title, row.video_path, row.subtitles_path) == (
        context.source_name, context.title, context.path, str(translated),
    )


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


@pytest.mark.parametrize("edit", ["cutoff-set", "cutoff-cleared", "cutoff-during-fetch"])
def test_source_provider_fallback_uses_current_cutoff(world, monkeypatch, isolated_jobs, edit):
    import json

    from app.database import TableLanguagesProfiles, update_profile_id_list
    from media_sources import repository, service
    from media_sources.indexer import index_item
    from subtitles import download

    profile = [{"id": 1, "language": "en", "forced": "False", "hi": "False"},
               {"id": 2, "language": "hu", "forced": "False", "hi": "False"}]

    def set_cutoff(cutoff):
        world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(profile), cutoff=cutoff))
        update_profile_id_list.invalidate()

    set_cutoff(1 if edit == "cutoff-cleared" else None)
    context = _movie(world)
    media = Path(context.path)
    media.with_suffix(".en.srt").write_bytes(CONTENT)
    index_item(context.item_id)
    assert repository.get_item(context.item_id).missing_subtitles == ("[]" if edit == "cutoff-cleared" else "['hu']")

    if edit == "cutoff-cleared":
        set_cutoff(None)
    elif edit == "cutoff-set":
        set_cutoff(1)

    def fetch(videos, languages, **kwargs):
        assert {str(language) for language in languages} == {"hu"}
        if edit == "cutoff-during-fetch":
            set_cutoff(1)
        return {next(iter(videos)): [FakeSubtitle(Language("hun"))]}

    attempted = Mock(side_effect=fetch)
    monkeypatch.setattr(download, "download_best_subtitles", attempted)
    service.search_source_item(context.item_id, translation_gate=False)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())

    assert attempted.call_count == (0 if edit == "cutoff-set" else 1)
    assert media.with_suffix(".hu.srt").is_file() == (edit == "cutoff-cleared")


@pytest.mark.parametrize("location", [
    "media", "configured", "untagged", "sync", "custom-hi", "custom-forced",
    "regional-plain", "regional-hi", "regional-forced", "unknown-numeric",
])
def test_source_provider_sees_cutoff_sidecar_before_index_refresh(world, monkeypatch, isolated_jobs, location):
    import json

    from app.config import settings
    from app.database import TableLanguagesProfiles, update_profile_id_list
    from languages import get_languages
    from media_sources import repository, service
    from media_sources.indexer import index_item
    from subtitles import download

    if location.startswith("custom-"):
        cutoff_language = "pb"
        monkeypatch.setattr(get_languages, "languages_dict", [
            *get_languages.languages_dict,
            {"code2": "pb", "code3": "pob", "name": "Portuguese (Brazil)", "code3b": "pob"},
            {"code2": "pt", "code3": "por", "name": "Portuguese", "code3b": "por"},
        ])
    elif location.startswith("regional-"):
        cutoff_language = "ea"
        monkeypatch.setattr(get_languages, "languages_dict", [
            *get_languages.languages_dict,
            {"code2": "ea", "code3": "spl", "name": "Spanish (Latino)", "code3b": "spl"},
            {"code2": "es", "code3": "spa", "name": "Spanish", "code3b": "spa"},
        ])
    else:
        cutoff_language = "en"
    profile = [{"id": 1, "language": cutoff_language, "forced": str(location.endswith("-forced")),
                "hi": str(location.endswith("-hi"))},
               {"id": 2, "language": "hu", "forced": "False", "hi": "False"}]
    world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(profile), cutoff=1))
    update_profile_id_list.invalidate()
    if location == "configured":
        monkeypatch.setattr(settings.general, "subfolder", "relative")
        monkeypatch.setattr(settings.general, "subfolder_custom", "Subs")
    context = _movie(world)
    media = Path(context.path)
    folder = media.parent / "Subs" if location == "configured" else media.parent
    folder.mkdir(exist_ok=True)
    suffix = {"untagged": ".srt", "sync": ".en.ffsubsync.srt", "custom-hi": ".pt-BR.hi.srt",
              "custom-forced": ".pt-BR.forced.srt", "regional-plain": ".es-419.srt",
              "regional-hi": ".es-419.hi.srt", "regional-forced": ".es-419.forced.srt",
              "unknown-numeric": ".en-123.srt"}.get(location, ".en.srt")
    name = media.with_suffix(suffix).name
    cutoff = folder / name
    content = CONTENT.replace(b"Downloaded", b"The children sat by the river while their mother read a story. "
                              b"It was a warm afternoon, and they could hear the birds in the trees above them.")

    def fetch(videos, languages, **kwargs):
        if {str(language) for language in languages} == {"hu"}:
            cutoff.write_bytes(content)
            assert cutoff_language in repository.get_item(context.item_id).missing_subtitles
            return {next(iter(videos)): [FakeSubtitle(Language("hun"))]}
        return {}

    monkeypatch.setattr(download, "download_best_subtitles", fetch)
    service.search_source_item(context.item_id, translation_gate=False)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())

    assert cutoff.read_bytes() == content
    if location == "unknown-numeric":
        assert (folder / media.with_suffix(".hu.srt").name).is_file()
        assert all(entry[1] != str(cutoff) for entry in index_item(context.item_id))
        return
    assert not (folder / media.with_suffix(".hu.srt").name).exists()
    assert world.db.execute(sa.select(repository.History)).all() == []
    assert not world.published
    if cutoff_language in {"ea", "pb"}:
        (entry,) = index_item(context.item_id)
        variant = location.rsplit("-", 1)[1]
        code = cutoff_language if variant == "plain" else f"{cutoff_language}:{variant}"
        assert entry[:2] == [code, str(cutoff)]
        assert repository.get_item(context.item_id).missing_subtitles == "[]"


def test_source_classifies_same_untagged_basename_by_full_path(world, monkeypatch, isolated_jobs):
    import json

    from app.config import settings
    from app.database import TableLanguagesProfiles, update_profile_id_list
    from media_sources import repository, service
    from media_sources.indexer import index_item
    from subtitles import download
    from subtitles.indexer import utils

    profile = [{"id": 1, "language": "hu", "forced": "False", "hi": "False"},
               {"id": 2, "language": "fr", "forced": "False", "hi": "False"}]
    world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(profile), cutoff=1))
    update_profile_id_list.invalidate()
    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "Subs")
    monkeypatch.setattr(utils, "guess_language", lambda text: "hu" if "Hungarian" in text else "en")
    context = _movie(world)
    media = Path(context.path)
    original = media.with_suffix(".srt")
    original.write_bytes(CONTENT)
    configured = media.parent / "Subs" / original.name
    configured.parent.mkdir()
    assert [entry[:2] for entry in index_item(context.item_id)] == [["en", str(original)]]

    def fetch(videos, languages, **kwargs):
        if {str(language) for language in languages} != {"fr"}:
            return {}
        configured.write_bytes(CONTENT.replace(b"Downloaded", b"Hungarian dialogue"))
        return {next(iter(videos)): [FakeSubtitle(Language("fra"))]}

    monkeypatch.setattr(download, "download_best_subtitles", fetch)
    service.search_source_item(context.item_id, translation_gate=False)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())

    assert configured.is_file()
    assert not (configured.parent / media.with_suffix(".fr.srt").name).exists()
    assert world.db.execute(sa.select(repository.History)).all() == []
    assert [entry[:2] for entry in index_item(context.item_id)] == [["en", str(original)], ["hu", str(configured)]]


def test_source_index_keeps_local_sidecars_when_custom_folder_is_a_file(world, monkeypatch):
    from app.config import settings
    from media_sources.indexer import index_item

    context = _movie(world)
    media = Path(context.path)
    local = media.with_suffix(".en.srt")
    local.write_bytes(CONTENT)
    (media.parent / "Subs").write_bytes(b"ordinary file")
    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "Subs")

    assert index_item(context.item_id) == [["en", str(local), len(CONTENT)]]


@pytest.mark.parametrize("indexed_before_fetch", [False, True])
def test_source_provider_cutoff_excludes_vobsub_pairs(world, monkeypatch, isolated_jobs, indexed_before_fetch):
    import json

    from app.database import TableLanguagesProfiles, update_profile_id_list
    from media_sources import repository, service
    from media_sources.indexer import index_item
    from subtitles import download

    profile = [{"id": 1, "language": "en", "forced": "False", "hi": "False"},
               {"id": 2, "language": "hu", "forced": "False", "hi": "False"}]
    world.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(profile)))
    update_profile_id_list.invalidate()
    context = _movie(world)
    media = Path(context.path)
    sidecar = media.with_suffix(".en.sub" if indexed_before_fetch else ".en.ffsubsync.sub")
    if indexed_before_fetch:
        sidecar.write_bytes(CONTENT)
        assert any(entry[1] == str(sidecar) for entry in index_item(context.item_id))

    def fetch(videos, languages, **kwargs):
        if {str(language) for language in languages} != {"hu"}:
            return {}
        world.db.execute(sa.update(TableLanguagesProfiles).values(cutoff=1))
        update_profile_id_list.invalidate()
        sidecar.write_bytes(b"bitmap data")
        sidecar.with_suffix(".IDX").write_bytes(b"bitmap index")
        return {next(iter(videos)): [FakeSubtitle(Language("hun"))]}

    monkeypatch.setattr(download, "download_best_subtitles", fetch)
    service.search_source_item(context.item_id, translation_gate=False)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())

    assert media.with_suffix(".hu.srt").read_bytes()
    assert all(entry[1] != str(sidecar) for entry in index_item(context.item_id))
    (result,) = world.db.execute(sa.select(repository.History)).all()
    assert result.subtitles_path == str(media.with_suffix(".hu.srt"))


@pytest.mark.parametrize("conflict", ["shared", "shared-modifier", "arrived-during-fetch"])
def test_source_provider_write_preserves_an_unavailable_destination(world, monkeypatch, isolated_jobs, conflict):
    from app.config import settings
    from app.database import TableMovies
    from media_sources import repository, service
    from subtitles import download

    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "../Sidecars")
    context = _movie(world)
    media = Path(context.path)
    destination = world.tmp_path / "Sidecars" / media.with_suffix(".en.srt").name
    if conflict in {"shared", "shared-modifier"}:
        other_name = media.with_suffix(".en.mkv").name if conflict == "shared-modifier" else media.name
        other = video(world.tmp_path / "Arr" / other_name)
        world.db.execute(sa.insert(TableMovies).values(path=str(other), title="Another film", tmdbId="7711"))

    def fetch(videos, **kwargs):
        if conflict == "arrived-during-fetch":
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(CONTENT.replace(b"Downloaded", b"Already present"))
        return {next(iter(videos)): [FakeSubtitle(Language("eng", hi=conflict == "shared-modifier"))]}

    monkeypatch.setattr(download, "download_best_subtitles", fetch)
    service.search_source_item(context.item_id, translation_gate=False)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())

    if conflict == "shared-modifier":
        assert not destination.with_suffix(".hi.srt").exists()
    elif conflict == "shared":
        assert not destination.exists()
    else:
        assert b"Already present" in destination.read_bytes()
    assert world.db.execute(sa.select(repository.History)).all() == []
    assert not world.published


def test_source_single_language_save_checks_custom_locale_ownership(world, monkeypatch, isolated_jobs):
    from app.config import settings
    from app.database import TableMovies
    from media_sources import repository, service

    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "../Sidecars")
    monkeypatch.setattr(settings.general, "single_language", True)
    context = _movie(world, "Film.es-419.mkv")
    dupe = video(world.tmp_path / "Arr" / "Film.mkv")
    world.db.execute(sa.insert(TableMovies).values(path=str(dupe), title="Another film", tmdbId="7711"))
    _download(monkeypatch)

    service.search_source_item(context.item_id, translation_gate=False)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())

    assert not (world.tmp_path / "Sidecars" / "Film.es-419.srt").exists()
    assert world.db.execute(sa.select(repository.History)).all() == []
    assert not world.published


@pytest.mark.parametrize("file_mode", ["link", "stream"])
def test_source_combine_requires_ordinary_inputs(world, monkeypatch, file_mode):
    import os
    import stat
    from subtitles.tools.combine import main as combine

    context = _movie(world)
    media = Path(context.path)
    primary = media.with_suffix(".en.srt")
    primary.write_bytes(CONTENT)
    media.with_suffix(".hu.srt").write_bytes(CONTENT)
    real_lstat = os.lstat

    def reported_file_type(path, *args, **kwargs):
        if os.fsdecode(path) == str(primary):
            mode = stat.S_IFLNK if file_mode == "link" else stat.S_IFIFO
            return SimpleNamespace(st_mode=mode)
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as reported:
        reported.setattr(os, "lstat", reported_file_type)
        result = combine.try_combine_for_video(context.path, "movie", languages=["en", "hu"], format="srt",
                                               source=context)
    assert result.status == "skipped" and "input" in result.reason
    assert not media.with_suffix(".en.combined-hu.srt").exists()


def test_source_combine_rechecks_input_ownership_at_publication(world, monkeypatch):
    from app.config import settings
    from app.database import TableMovies
    from subtitles.tools.combine import main as combine

    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "..")
    context = _movie(world)
    media = Path(context.path)
    media.with_suffix(".en.srt").write_bytes(CONTENT)
    (world.tmp_path / media.with_suffix(".hu.srt").name).write_bytes(CONTENT)
    compose = combine.compose

    def compose_then_add_owner(**kwargs):
        result = compose(**kwargs)
        arr = video(media.parent / "Arr" / media.name)
        world.db.execute(sa.insert(TableMovies).values(path=str(arr), title="Arr copy", tmdbId="9876"))
        return result

    monkeypatch.setattr(combine, "compose", compose_then_add_owner)
    result = combine.try_combine_for_video(context.path, "movie", languages=["en", "hu"], format="srt",
                                           source=context)
    assert result.status == "failed" and "input" in result.error
    assert not (world.tmp_path / media.with_suffix(".en.combined-hu.srt").name).exists()


@pytest.mark.parametrize('language', ['en', 'hu'])
@pytest.mark.parametrize(('input_location', 'alias'), [
    ('media folder', False), ('shared folder', False), ('shared folder', True),
])
@pytest.mark.parametrize('arrival', ['before reading', 'after preparation'])
def test_source_combine_rechecks_local_input_ownership_after_preparation(
        world, monkeypatch, language, input_location, alias, arrival):
    from contextlib import contextmanager

    from media_sources import indexer, repository
    from subtitles.tools.combine import main as combine

    folder = _custom_folder(world, monkeypatch, world.tmp_path)
    context = _movie(world)
    media = Path(context.path)
    input_folder = media.parent if input_location == 'media folder' else folder
    primary = input_folder / media.with_suffix('.en.srt').name
    secondary = input_folder / media.with_suffix('.hu.srt').name
    primary.write_bytes(CONTENT)
    secondary.write_bytes(CONTENT)
    destination = folder / media.with_suffix('.en.combined-hu.srt').name
    alternate = destination.with_suffix('.ssa')
    variant = alternate.with_suffix('.ffsubsync.ssa')
    for path in (destination, alternate, variant):
        path.write_bytes(CONTENT)
    publication = indexer.source_item_publication
    compose = combine.compose
    read = combine.read_regular_subtitle
    consumed = []

    def add_competing_video():
        sibling = media.with_suffix(f'.{language}.mkv')
        if alias:
            target = video(world.tmp_path / 'Another video' / sibling.name)
            (media.parent / 'Alias.mkv').symlink_to(target)
        else:
            video(sibling)

    def arrive_before_reading(**kwargs):
        if arrival == 'before reading':
            add_competing_video()
        return compose(**kwargs)

    def record_read(path, version):
        consumed.append(path)
        return read(path, version)

    @contextmanager
    def add_local_owner(source, *, shared=False):
        with publication(source, shared=shared) as result:
            if arrival == 'after preparation':
                add_competing_video()
            yield result

    monkeypatch.setattr(indexer, 'source_item_publication', add_local_owner)
    monkeypatch.setattr(combine, 'compose', arrive_before_reading)
    monkeypatch.setattr(combine, 'read_regular_subtitle', record_read)
    result = combine.try_combine_for_video(context.path, 'movie', languages=['en', 'hu'], format='srt',
                                           source=context)

    assert result.status == 'failed'
    if arrival == 'before reading':
        assert 'input lost its unique owner before reading' in result.error
        assert str(primary if language == 'en' else secondary) not in consumed
    else:
        assert ('input lost its unique owner before publication' in result.error
                or 'Another video now owns the subtitle destination' in result.error)
    for path in (primary, secondary, destination, alternate, variant):
        assert path.read_bytes() == CONTENT
    assert world.db.execute(sa.select(repository.History)).all() == []
    assert not world.published


def test_source_combine_refuses_an_input_with_an_intermediate_owner(world, monkeypatch):
    from app.config import settings
    from app.database import TableMovies
    from subtitles.tools.combine import main as combine

    monkeypatch.setattr(settings.general, "subfolder", "relative")
    monkeypatch.setattr(settings.general, "subfolder_custom", "../Sidecars")
    context = _movie(world)
    media = Path(context.path)
    sidecars = world.tmp_path / "Sidecars"
    sidecars.mkdir()
    (sidecars / media.with_suffix(".en.hi.srt").name).write_bytes(CONTENT)
    media.with_suffix(".hu.srt").write_bytes(CONTENT)
    other = video(world.tmp_path / "Arr" / media.with_suffix(".en.mkv").name)
    world.db.execute(sa.insert(TableMovies).values(path=str(other), title="Another film", tmdbId="7711"))

    result = combine.try_combine_for_video(context.path, "movie", languages=["en", "hu"], format="srt",
                                           source=context)
    assert result.status == "skipped" and "input" in result.reason
    assert not (sidecars / media.with_suffix(".en.combined-hu.srt").name).exists()


def test_source_provider_guard_preserves_the_selected_subtitle_format(world, monkeypatch, isolated_jobs):
    import pysubs2
    from app.database import TableLanguagesProfiles, update_profile_id_list
    from media_sources import repository, service

    world.db.execute(sa.update(TableLanguagesProfiles).values(originalFormat=1))
    update_profile_id_list.invalidate()
    context = _movie(world)
    media = Path(context.path)
    subtitle = FakeSubtitle()
    subtitle.format = "ass"
    subtitle.use_original_format = True
    subtitle.content = pysubs2.SSAFile.from_string(CONTENT.decode()).to_string("ass").encode()
    _download(monkeypatch, subtitle)
    service.search_source_item(context.item_id, translation_gate=False)
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())

    destination = media.with_suffix(".en.ass")
    assert "[Script Info]" in destination.read_text()
    assert not media.with_suffix(".en.srt").exists()
    (result,) = world.db.execute(sa.select(repository.History)).all()
    assert result.subtitles_path == str(destination)


def test_saver_continues_after_a_declined_candidate_in_single_language_mode(tmp_path):
    from subliminal_patch.core import save_subtitles

    first, second = FakeSubtitle(), FakeSubtitle()
    writer = Mock(side_effect=[False, None])

    result = save_subtitles(str(tmp_path / "Film.mkv"), [first, second], single=True, write_subtitle=writer)

    assert writer.call_count == 2
    assert result == [second] and first.storage_path is None
