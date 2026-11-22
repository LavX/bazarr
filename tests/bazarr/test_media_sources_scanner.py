"""The folder scan: what it finds, what it keeps, and what it refuses to delete."""
import os

import pytest
import sqlalchemy as sa

from media_source_helpers import add_profile, add_source, video

MATRIX = "The.Matrix.1999.1080p.BluRay.x264-GRP"
ALIEN = "Alien.1979.Directors.Cut.1080p.BluRay.x264-GRP"


@pytest.fixture(autouse=True)
def enabled_languages(languages_loaded, monkeypatch):
    """The two languages these tests search for, without the settings table the language lookup reads."""
    from subzero.language import Language
    from media_sources import indexer
    monkeypatch.setattr(indexer, "get_language_set", lambda: {Language("eng"), Language("hun")})


@pytest.fixture
def library(source_db, tmp_path):
    source_id, library_id, root = add_source(source_db, tmp_path)
    return source_db, source_id, library_id, root


def _scan(source_id, library_id=None, **kwargs):
    from media_sources.scanner import run_scan
    return run_scan(source_id, library_id, **kwargs)


def _items(library_id):
    from media_sources import repository
    return {row.path: row for row in repository.list_items(library_id=library_id, limit=1000)}


def test_a_scan_lists_the_movies_and_parses_their_names(library):
    session, source_id, library_id, root = library
    video(root / MATRIX / f"{MATRIX}.mkv")
    video(root / f"{ALIEN}.mp4")
    video(root / "holiday clip.avi")

    (result,) = _scan(source_id)

    assert (result.status, result.inserted, result.deleted) == ("ok", 3, 0)
    items = _items(library_id)
    matrix = items[str(root / MATRIX / f"{MATRIX}.mkv")]
    assert (matrix.title, matrix.year, matrix.identity_state, matrix.identity_source) == ("The Matrix", "1999",
                                                                                          "parsed", "filename")
    assert matrix.scene_name == MATRIX and matrix.kind == "movie" and matrix.group_id is None
    alien = items[str(root / f"{ALIEN}.mp4")]
    assert (alien.title, alien.year, alien.identity_state) == ("Alien", "1979", "parsed")
    # No year, so the parse is not complete: listed, searchable by hand, never searched automatically.
    clip = items[str(root / "holiday clip.avi")]
    assert (clip.title, clip.identity_state) == ("holiday clip", "unmatched")
    assert matrix.file_size == 1000 and matrix.file_mtime_ns and matrix.file_ino and matrix.file_dev
    assert matrix.monitored == 1 and matrix.arr_shadow == 0 and matrix.last_seen_at is not None


def test_the_walk_skips_what_is_not_a_title(library):
    session, source_id, library_id, root = library
    video(root / MATRIX / f"{MATRIX}.mkv")
    video(root / MATRIX / "sample" / "sample.mkv")
    video(root / MATRIX / f"{MATRIX}-sample.mkv")
    video(root / MATRIX / "Extras" / "interview.mkv")
    video(root / MATRIX / "Featurettes" / "making of.mkv")
    video(root / MATRIX / "Behind The Scenes" / "bts.mkv")
    video(root / ".hidden" / "Movie.2000.mkv")
    video(root / ".Movie.2001.mkv")
    (root / "notes.txt").write_text("not a video")
    (root / "poster.jpg").write_bytes(b"\0")

    _scan(source_id)

    assert list(_items(library_id)) == [str(root / MATRIX / f"{MATRIX}.mkv")]


def test_a_file_symlink_is_followed_a_directory_symlink_is_not_and_one_file_is_listed_once(library, tmp_path):
    session, source_id, library_id, root = library
    real = video(tmp_path / "elsewhere" / "Real.Movie.2001.mkv")
    (root / "Link.2001.mkv").symlink_to(real)
    (root / "folder-link").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    original = video(root / "Hard.2002.mkv")
    os.link(original, root / "Hard.copy.2002.mkv")

    _scan(source_id)

    paths = sorted(_items(library_id))
    assert str(root / "Link.2001.mkv") in paths
    assert not any("folder-link" in path for path in paths)
    assert len([path for path in paths if "Hard" in path]) == 1


def test_a_second_scan_keeps_unchanged_files_and_updates_changed_new_and_gone_ones(library):
    from media_sources import repository

    session, source_id, library_id, root = library
    kept = video(root / "Keep.2001.mkv")
    changed = video(root / "Change.2002.mkv")
    gone = video(root / "Gone.2003.mkv")
    _scan(source_id)
    before = _items(library_id)
    # The codec cache of the changed file describes content that is about to be replaced.
    repository.update_item(before[str(changed)].id, session, ffprobe_cache=b"cache")
    repository.update_item(before[str(kept)].id, session, ffprobe_cache=b"cache")
    history_id = repository.insert_history(
        session, item_id=before[str(gone)].id, source_id=source_id, item_kind="movie", action=1,
        description="downloaded", video_path=str(gone), item_title="Gone", source_name="Local")
    changed.write_bytes(b"\0" * 2500)
    gone.unlink()
    video(root / "New.2004.mkv")

    (result,) = _scan(source_id)

    assert (result.inserted, result.updated, result.unchanged, result.deleted) == (1, 1, 1, 1)
    after = _items(library_id)
    assert set(after) == {str(kept), str(changed), str(root / "New.2004.mkv")}
    assert after[str(kept)].id == before[str(kept)].id and after[str(kept)].ffprobe_cache == b"cache"
    assert after[str(changed)].id == before[str(changed)].id
    assert after[str(changed)].file_size == 2500 and after[str(changed)].ffprobe_cache is None
    row = session.execute(sa.select(repository.History).where(repository.History.c.id == history_id)).one()
    assert row.item_id is None and row.item_title == "Gone" and row.video_path == str(gone)


def test_user_settings_survive_a_rescan(library):
    from media_sources import repository

    session, source_id, library_id, root = library
    path = video(root / "Mine.2001.mkv")
    _scan(source_id)
    item = _items(library_id)[str(path)]
    repository.update_item(item.id, session, monitored=0, profile_id=None, identity_source="user", title="Mine")
    path.write_bytes(b"\0" * 1234)

    _scan(source_id)

    assert repository.get_item(item.id).monitored == 0


def test_a_subtitle_added_beside_an_unchanged_video_is_indexed_on_the_next_scan(library, monkeypatch):
    from media_sources import repository

    session, source_id, library_id, root = library
    profile = add_profile(session, [("en", "False", "False"), ("hu", "False", "False")])
    # The library carries the profile and the items inherit it.
    session.execute(sa.update(repository.Libraries).values(profile_id=profile))
    path = video(root / "Film.2001.mkv")
    _scan(source_id)
    item = _items(library_id)[str(path)]
    assert item.subtitles == "[]" and item.missing_subtitles == "['en', 'hu']"
    assert item.indexed_at is not None

    path.with_name("Film.2001.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    _scan(source_id)

    item = repository.get_item(item.id)
    assert "Film.2001.en.srt" in item.subtitles
    assert item.missing_subtitles == "['hu']"


def test_an_unmounted_root_deletes_nothing_and_says_so(library):
    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    _scan(source_id)
    for entry in root.iterdir():
        entry.unlink()
    root.rmdir()

    (result,) = _scan(source_id)

    assert result.status == "root_missing" and result.deleted == 0
    assert len(_items(library_id)) == 1
    from media_sources import repository
    assert repository.get_library(library_id).last_scan_status == "root_missing"
    assert repository.get_source(source_id).last_scan_status == "partial"


def test_an_emptied_root_over_a_library_with_items_deletes_nothing(library):
    session, source_id, library_id, root = library
    path = video(root / "Film.2001.mkv")
    _scan(source_id)
    path.unlink()

    (result,) = _scan(source_id)

    assert result.status == "root_missing" and result.deleted == 0 and len(_items(library_id)) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_an_unreadable_subdirectory_deletes_nothing_below_the_root(library):
    session, source_id, library_id, root = library
    video(root / "Open" / "A.2001.mkv")
    video(root / "Closed" / "B.2002.mkv")
    _scan(source_id)
    (root / "Closed").chmod(0)
    try:
        (result,) = _scan(source_id)
    finally:
        (root / "Closed").chmod(0o755)

    assert result.status == "partial" and result.deleted == 0
    assert len(_items(library_id)) == 2
    from media_sources import repository
    assert "Cannot read" in repository.get_library(library_id).last_scan_message


def test_an_item_is_deleted_only_when_its_own_file_is_gone(library, monkeypatch):
    """A listing that no longer contains a file is not proof it is gone: the disk decides."""
    from media_sources import identity, repository

    session, source_id, library_id, root = library
    video(root / "Other.2002.mkv")
    # A row for a file the walk does not list (it sits in a folder the rules skip) but that is on disk.
    unlisted = video(root / "Extras" / "Unlisted.2001.mkv")
    item_id = repository.insert_item(
        session, source_id=source_id, library_id=library_id, kind="movie", path=str(unlisted), title="Unlisted",
        identity_source="filename", identity_state="parsed")

    (result,) = _scan(source_id)
    assert (result.status, result.deleted) == ("ok", 0)
    assert repository.get_item(item_id) is not None

    unlisted.unlink()
    (result,) = _scan(source_id)
    assert result.deleted == 1 and repository.get_item(item_id) is None

    # A stat failure other than "not found" means "do not know", not "gone".
    real_stat = os.stat

    def denied(path, *args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(identity.os, "stat", denied)
    assert identity.path_is_gone(str(root / "Other.2002.mkv")) is False
    monkeypatch.setattr(identity.os, "stat", real_stat)
    assert identity.path_is_gone(str(root / "nothing.mkv")) is True


def test_writes_go_in_batches_and_every_batch_commits(library, monkeypatch):
    from media_sources import scanner

    session, source_id, library_id, root = library
    for number in range(10):
        video(root / f"Film.{2000 + number}.mkv")
    opened = []
    real = scanner.source_transaction

    def counting(*args, **kwargs):
        opened.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(scanner, "BATCH_SIZE", 3)
    monkeypatch.setattr(scanner, "source_transaction", counting)

    (result,) = _scan(source_id)

    assert result.inserted == 10 and len(_items(library_id)) == 10
    assert len(opened) == 4


def test_a_failed_batch_rolls_back_whole(library, monkeypatch):
    from media_sources import repository, scanner

    session, source_id, library_id, root = library
    for number in range(4):
        video(root / f"Film.{2000 + number}.mkv")
    real = repository.insert_item
    calls = []

    def failing(target=None, **values):
        calls.append(values["path"])
        if len(calls) == 3:
            raise RuntimeError("controlled failure")
        return real(target, **values)

    monkeypatch.setattr(scanner.repository, "insert_item", failing)
    with pytest.raises(RuntimeError, match="controlled failure"):
        _scan(source_id)

    # The batch of four is one transaction: the two rows written before the failure are not there.
    assert _items(library_id) == {}
    assert repository.get_source(source_id).last_scan_status == "error"


def test_a_cancel_stops_the_scan_and_keeps_what_was_read(library):
    from app.jobs_queue import JobCancelled
    from media_sources import repository

    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    calls = []

    def cancel():
        calls.append(1)
        if len(calls) > 2:
            raise JobCancelled("stop")

    with pytest.raises(JobCancelled):
        _scan(source_id, cancel=cancel)

    assert repository.get_source(source_id).last_scan_status == "partial"


def test_a_disabled_library_is_not_scanned_and_a_named_library_must_belong_to_the_source(library):
    from media_sources import repository

    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    session.execute(sa.update(repository.Libraries).values(enabled=0))
    assert _scan(source_id) == []
    assert _items(library_id) == {}
    other_source, other_library, _ = add_source(session, root.parent, name="Other", folder="Other")
    with pytest.raises(LookupError):
        _scan(source_id, other_library)
    with pytest.raises(LookupError):
        _scan(999)


def test_the_root_device_is_recorded_as_unsigned_text(library):
    from media_sources import repository

    session, source_id, library_id, root = library
    _scan(source_id)
    assert repository.get_roots(library_id)[0].last_dev == str(os.stat(root).st_dev)


def test_the_scan_job_queues_itself_and_runs_the_scan(library, monkeypatch):
    from media_sources import scanner

    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    queued = []
    monkeypatch.setattr(scanner.jobs_queue, "add_job_from_function",
                        lambda name, is_progress: queued.append(name) or 41)
    assert scanner.scan_media_source(source_id) == 41
    assert queued == ["Scanning media source Local"]

    progress = []
    monkeypatch.setattr(scanner.jobs_queue, "update_job_progress", lambda **kwargs: progress.append(kwargs))
    monkeypatch.setattr(scanner.jobs_queue, "raise_if_cancelled", lambda job_id: None)
    scanner.scan_media_source(source_id, job_id=7)
    assert len(_items(library_id)) == 1
    assert progress[-1]["progress_value"] == "max" and "1 added" in progress[-1]["progress_message"]
    from app.jobs_queue import JobFailed
    with pytest.raises(JobFailed):
        scanner.scan_media_source(999, job_id=8)


class FakeKind:
    """A kind that lists what it is told, to drive the shared part of the scanner without a disk."""
    kind = "folder"

    def __init__(self, files, complete=True):
        self.files, self.complete = files, complete

    def validate_library(self, source, library):
        return []

    def enumerate(self, source, library, cancel=None):
        from media_sources.kinds import Listing
        listing = Listing(files=iter(self.files))
        listing.complete = self.complete
        return listing


def test_an_incomplete_listing_updates_what_it_found_and_deletes_nothing(library, monkeypatch):
    from media_sources import scanner
    from media_sources.kinds import DiscoveredFile

    session, source_id, library_id, root = library
    gone = video(root / "Gone.2001.mkv")
    kept = video(root / "Kept.2002.mkv")
    _scan(source_id)
    gone.unlink()
    info = kept.stat()
    found = DiscoveredFile(str(kept), info.st_size, info.st_mtime_ns, str(info.st_dev), str(info.st_ino))
    monkeypatch.setattr(scanner, "get_kind", lambda name: FakeKind([found], complete=False))

    (result,) = _scan(source_id)

    assert result.status == "partial" and result.deleted == 0
    assert len(_items(library_id)) == 2
    monkeypatch.setattr(scanner, "get_kind", lambda name: FakeKind([found], complete=True))
    (result,) = _scan(source_id)
    assert result.status == "ok" and result.deleted == 1 and len(_items(library_id)) == 1


def test_the_context_resolves_an_items_profile_item_then_group_then_library(library):
    from media_sources import repository
    from media_sources.context import SourceItemGone, resolve_item

    session, source_id, library_id, root = library
    add_profile(session, [("en", "False", "False")], profile_id=1)
    add_profile(session, [("hu", "False", "False")], profile_id=2)
    session.execute(sa.update(repository.Libraries).values(profile_id=1))
    path = video(root / "Film.2001.mkv")
    _scan(source_id)
    item = _items(library_id)[str(path)]
    context = resolve_item(item.id)
    assert (context.profile_id, context.kind, context.media_type, context.publication_type) == (1, "movie", "movie",
                                                                                                "movie")
    assert (context.title, context.year, context.path, context.group_title) == ("Film", 2001, str(path), None)
    repository.update_item(item.id, session, profile_id=2)
    assert resolve_item(item.id).profile_id == 2
    repository.delete_items([item.id], session)
    with pytest.raises(SourceItemGone):
        resolve_item(item.id)


def test_the_context_of_an_episode_and_of_a_sports_event_carry_what_the_pipeline_branches_on(library):
    from media_sources.context import resolve_item
    from media_source_helpers import add_group, add_item

    session, source_id, library_id, root = library
    show = add_group(session, source_id, library_id, kind="show", title="Bluey", tvdb_id=387380, imdb_id="tt7678620")
    episode = add_item(session, source_id, library_id, root / "e.mkv", kind="episode", group_id=show, title="Mum School",
                       season=1, episode=2, tvdb_id=9, imdb_id="tt9")
    league = add_group(session, source_id, library_id, kind="league", title="Formula 1", key="/library/F1")
    event = add_item(session, source_id, library_id, root / "s.mkv", kind="sports_event", group_id=league,
                     title="Monaco Grand Prix", part_name="Qualifying", year="2024", event_date="2024-05-25",
                     season=2024, episode=8)

    context = resolve_item(episode)
    assert (context.media_type, context.publication_type, context.group_title) == ("series", "episode", "Bluey")
    assert (context.series_tvdb_id, context.series_imdb_id, context.tvdb_id, context.imdb_id) == (387380,
                                                                                                "tt7678620", 9, "tt9")
    assert (context.season, context.episode) == (1, 2)
    sports = resolve_item(event)
    assert (sports.media_type, sports.publication_type, sports.group_title) == ("movie", "sports", "Formula 1")
    # The provider-facing title carries the part, and the year is the date's.
    assert (sports.title, sports.year, sports.event_date) == ("Monaco Grand Prix Qualifying", 2024, "2024-05-25")
    assert (sports.series_tvdb_id, sports.series_imdb_id) == (None, None)


def test_the_folder_kind_refuses_paths_it_cannot_use(tmp_path):
    from types import SimpleNamespace
    from media_sources.kinds import get_kind, UnknownSourceKind
    from app.get_args import args

    kind = get_kind("folder")

    def problems(path, content_kind="movie"):
        return {problem.code for problem in kind.validate_library(None, SimpleNamespace(
            locator=path, content_kind=content_kind))}

    assert problems(str(tmp_path)) == set()
    assert problems("relative/path") == {"not_absolute"}
    assert problems(str(tmp_path / "missing")) == {"not_a_directory"}
    assert "root" in problems(os.sep)
    assert "config_directory" in problems(args.config_dir)
    assert "config_directory" in problems(os.path.dirname(args.config_dir))
    assert problems(str(tmp_path), "series") == set()
    assert "content_kind" in problems(str(tmp_path), "sports")
    with pytest.raises(UnknownSourceKind):
        get_kind("jellyfin")


# A disabled source or library is not scanned

def test_a_disabled_source_is_not_scanned_even_when_a_library_is_named(library):
    from media_sources import repository

    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    session.execute(sa.update(repository.Sources).values(enabled=0))
    assert _scan(source_id) == []
    assert _scan(source_id, library_id) == []
    assert _items(library_id) == {}
    assert repository.get_library(library_id).last_scan_status is None


def test_a_disabled_library_is_not_scanned_even_when_named(library):
    from media_sources import repository

    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    session.execute(sa.update(repository.Libraries).values(enabled=0))
    assert _scan(source_id, library_id) == []
    assert _items(library_id) == {}


def test_the_scan_job_of_a_disabled_source_reports_it_and_scans_nothing(library, monkeypatch):
    from media_sources import repository, scanner

    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    session.execute(sa.update(repository.Sources).values(enabled=0))
    progress = []
    monkeypatch.setattr(scanner.jobs_queue, "update_job_progress", lambda **kwargs: progress.append(kwargs))
    monkeypatch.setattr(scanner.jobs_queue, "raise_if_cancelled", lambda job_id: None)
    scanner.scan_media_source(source_id, job_id=7)
    assert _items(library_id) == {}
    assert "disabled" in progress[-1]["progress_message"]


# Two libraries over the same or nested paths

def _second_library(session, root, name="Other", nested=None):
    from media_sources import repository

    source_id = repository.create_source(kind="folder", name=name, target=session)
    path = nested or root
    return source_id, repository.create_library(source_id=source_id, content_kind="movie", locator=str(path),
                                                target=session)


@pytest.mark.parametrize("nested", [False, True])
def test_a_file_another_library_holds_is_skipped_and_reported_not_inserted(library, nested):
    from media_sources import repository

    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    video(root / "Sub" / "Deep.2002.mkv")
    _scan(source_id)
    other_source, other_library = _second_library(session, root, nested=root / "Sub" if nested else None)
    video(root / "Sub" / "New.2003.mkv")

    (result,) = _scan(other_source)

    assert result.status == "ok"
    # Only the file the first library has not seen yet is new; the others are held and reported.
    paths = {row.path for row in repository.list_items(library_id=other_library, limit=100)}
    assert paths == {str(root / "Sub" / "New.2003.mkv")}
    assert (result.inserted, result.held) == (1, 1 if nested else 2)
    assert "already supplied by Local" in result.message
    assert len(_items(library_id)) == 2
    # The file the second library took is not the first library's to insert, and nothing is deleted.
    (first,) = _scan(source_id)
    assert (first.inserted, first.held, first.deleted) == (0, 1, 0)


def test_a_file_held_by_a_disabled_library_is_adopted_by_the_scanning_source(library):
    """A disabled holder hands an aliased file over: the item keeps its id and moves to the scanning library."""
    from media_sources import repository

    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    _scan(source_id)
    (item,) = _items(library_id).values()
    session.execute(sa.update(repository.Libraries).where(repository.Libraries.c.id == library_id).values(enabled=0))
    other_source, other_library = _second_library(session, root)
    (result,) = _scan(other_source)
    assert (result.inserted, result.handed_over) == (0, 1)
    adopted = repository.get_item(item.id)
    assert (adopted.library_id, adopted.source_id) == (other_library, other_source)


def test_a_batch_that_hits_the_unique_path_anyway_falls_back_to_one_file_at_a_time(library, monkeypatch):
    from media_sources import repository, scanner

    session, source_id, library_id, root = library
    video(root / "Good.2001.mkv")
    bad = video(root / "Bad.2002.mkv")
    video(root / "Fine.2003.mkv")
    real = repository.insert_item

    def refuse_bad(target=None, **values):
        if values["path"] == str(bad):
            raise sa.exc.IntegrityError("insert", {}, Exception("duplicate"))
        return real(target, **values)

    monkeypatch.setattr(scanner.repository, "insert_item", refuse_bad)
    (result,) = _scan(source_id)
    assert result.inserted == 2 and result.skipped == 1
    assert sorted(row.title for row in _items(library_id).values()) == ["Fine", "Good"]
    assert "could not be added" in result.message


# The library is checked again at scan time

def test_a_locator_that_became_unusable_is_refused_at_scan_time_and_changes_nothing(library, monkeypatch):
    from media_sources import repository
    from media_sources.kinds import folder

    session, source_id, library_id, root = library
    video(root / "Film.2001.mkv")
    _scan(source_id)
    monkeypatch.setattr(folder, "config_directory", lambda: str(root))
    video(root / "Late.2002.mkv")

    (result,) = _scan(source_id)

    assert result.status == "error" and "configuration folder" in result.message
    assert len(_items(library_id)) == 1
    assert repository.get_library(library_id).last_scan_status == "error"


# A rescan keeps what the user set

def test_a_changed_file_keeps_a_title_and_identity_the_user_set(library):
    from media_sources import repository

    session, source_id, library_id, root = library
    path = video(root / "Mine.2001.mkv")
    _scan(source_id)
    item = _items(library_id)[str(path)]
    repository.update_item(item.id, session, identity_source="user", identity_state="identified",
                           title="Chosen Title", year="1999", tmdb_id="603")
    path.write_bytes(b"\0" * 2222)

    _scan(source_id)

    kept = repository.get_item(item.id)
    assert (kept.title, kept.year, kept.identity_source, kept.identity_state, kept.tmdb_id) == (
        "Chosen Title", "1999", "user", "identified", "603")
    assert kept.file_size == 2222 and kept.ffprobe_cache is None


def test_an_emptied_folder_is_covered_by_the_empty_walk_rule_and_the_message_says_what_to_do(library):
    session, source_id, library_id, root = library
    path = video(root / "Film.2001.mkv")
    _scan(source_id)
    path.unlink()
    (result,) = _scan(source_id)
    assert result.status == "root_missing" and result.deleted == 0
    assert "remove the library" in result.message


# File names the database cannot hold, and parsing outside the write lock

def test_a_name_that_is_not_valid_utf8_is_skipped_and_reported_and_the_scan_goes_on(library):
    from media_sources import repository

    session, source_id, library_id, root = library
    video(root / "Good.2001.mkv")
    bad_file = root / os.fsdecode(b"Bad\xe9.2002.mkv")
    video(bad_file)
    bad_directory = root / os.fsdecode(b"Dir\xff")
    video(bad_directory / "Inside.2003.mkv")
    video(root / "Fine" / "Fine.2004.mkv")

    (result,) = _scan(source_id)

    assert result.status == "ok" and result.inserted == 2
    assert sorted(row.title for row in _items(library_id).values()) == ["Fine", "Good"]
    assert "2 file(s) with a name that is not valid UTF-8 were skipped" in result.message
    assert "UTF-8" in repository.get_library(library_id).last_scan_message
    # A second scan skips them again without failing, and deletes nothing.
    (again,) = _scan(source_id)
    assert (again.inserted, again.deleted) == (0, 0)


def test_changed_files_are_parsed_before_the_write_transaction_opens(library, monkeypatch):
    from media_sources import identify, scanner

    session, source_id, library_id, root = library
    paths = [video(root / f"Film.{2000 + number}.mkv") for number in range(3)]
    _scan(source_id)
    for path in paths:
        path.write_bytes(b"\0" * 2000)
    inside = []
    real_transaction, real_parse = scanner.source_transaction, identify.parse_movie_file

    class Tracking:
        def __init__(self, *args, **kwargs):
            self.manager = real_transaction(*args, **kwargs)

        def __enter__(self):
            inside.append(True)
            return self.manager.__enter__()

        def __exit__(self, *exc):
            result = self.manager.__exit__(*exc)
            inside.pop()
            return result

    def parse(path):
        assert not inside, "a file was parsed while a write transaction was open"
        return real_parse(path)

    monkeypatch.setattr(scanner, "source_transaction", Tracking)
    monkeypatch.setattr(identify, "parse_movie_file", parse)
    (result,) = _scan(source_id)
    assert result.updated == 3
