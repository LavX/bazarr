"""Findings from the independent review of the scanner: what a half-read walk may touch, group deletion under the
root guards, the guards on another library's rows, one scan per library at a time, and the small edges."""
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from media_source_helpers import Lib, add_profile, add_source, stamped_video


@pytest.fixture(autouse=True)
def enabled_languages(languages_loaded, monkeypatch):
    from subzero.language import Language
    from media_sources import indexer
    monkeypatch.setattr(indexer, "get_language_set", lambda: {Language("eng")})


@pytest.fixture(autouse=True)
def no_scan_in_progress():
    from media_sources import scanner
    scanner._library_locks.clear()
    yield
    scanner._library_locks.clear()


@pytest.fixture
def lib(source_db, tmp_path):
    return Lib(source_db, tmp_path)


@pytest.fixture
def movies(source_db, tmp_path):
    return Lib(source_db, tmp_path, name="Local", folder="Movies", content_kind="movie")


def _items():
    from media_sources import repository
    return repository.Items


def _unreadable(monkeypatch, name):
    """A walk that cannot read the directory called ``name``: every scan stays incomplete."""
    real = os.scandir

    def scandir(path):
        if str(path).endswith(name):
            raise PermissionError(13, "denied", str(path))
        return real(path)

    monkeypatch.setattr("media_sources.kinds.folder.os.scandir", scandir)
    return real


# 1. A half-read walk never lets one show take another show's group

@pytest.mark.parametrize("shared", [False, True], ids=["own-names", "shared-names"])
def test_a_swap_met_on_an_incomplete_walk_gives_no_group_to_the_wrong_show(lib, shared, monkeypatch):
    profile = add_profile(lib.session, [("en", "False", "False")])
    lib.show("Show", count=2, prefix="Same" if shared else None)
    lib.show("Show.new", count=2, prefix="Same" if shared else None)
    stamped_video(lib.root / "Third" / "Locked" / "Third - S01E01.mkv")
    lib.scan()
    a, b = lib.group("Show"), lib.group("Show.new")
    lib.set_group("Show", monitored=0, profile_id=profile, identity_source="user", title="Mine", tvdb_id=5)
    ids = {path: row.id for path, row in lib.items().items()}
    os.rename(lib.root / "Show", lib.root / "Show.old")
    os.rename(lib.root / "Show.new", lib.root / "Show")
    real = _unreadable(monkeypatch, "Locked")

    first = lib.scan()
    second = lib.scan()

    assert first.status == second.status == "partial"
    # Nothing is attached to a group that is not its own, and no key moved.
    assert lib.group("Show").id == a.id and lib.group("Show.new").id == b.id
    assert not [path for path in lib.items() if path.startswith("pending:")]
    # The rows wait where they were, in the groups they had.
    assert {path: row.id for path, row in lib.items().items()} == ids
    assert {row.group_id for path, row in lib.items().items() if "/Show/" in path} == {a.id}
    assert {row.group_id for path, row in lib.items().items() if "/Show.new/" in path} == {b.id}
    monkeypatch.setattr("media_sources.kinds.folder.os.scandir", real)

    lib.scan()

    groups = lib.groups()
    assert groups[lib.path("Show.old")].id == a.id and groups[lib.path("Show")].id == b.id
    assert (groups[lib.path("Show.old")].monitored, groups[lib.path("Show.old")].tvdb_id) == (0, 5)
    assert (groups[lib.path("Show")].monitored, groups[lib.path("Show")].tvdb_id) == (1, None)
    items = lib.items()
    assert {row.group_id for path, row in items.items() if "/Show.old/" in path} == {a.id}
    assert {row.group_id for path, row in items.items() if "/Show/" in path} == {b.id}
    assert {row.id for path, row in items.items() if "/Show/" in path or "/Show.old/" in path} == {
        row_id for path, row_id in ids.items() if "/Show" in path}


def test_a_remake_met_on_an_incomplete_walk_is_not_added_to_the_old_group(lib, monkeypatch):
    lib.show("Show", count=2)
    stamped_video(lib.root / "Third" / "Locked" / "Third - S01E01.mkv")
    lib.scan()
    old = lib.group("Show")
    lib.set_group("Show", monitored=0)
    os.rename(lib.root / "Show", lib.root / "Show (2010)")
    lib.show("Show", count=2, prefix="Remake")
    real = _unreadable(monkeypatch, "Locked")

    result = lib.scan()

    assert result.status == "partial"
    assert not [path for path in lib.items() if "Remake" in path]
    assert lib.group("Show").id == old.id
    monkeypatch.setattr("media_sources.kinds.folder.os.scandir", real)
    lib.scan()
    assert lib.group("Show (2010)").id == old.id and lib.group("Show (2010)").monitored == 0
    fresh = lib.group("Show")
    assert fresh.id != old.id and fresh.monitored == 1
    assert {row.group_id for path, row in lib.items().items() if "/Show/" in path} == {fresh.id}


def test_a_path_taken_by_a_deferred_row_is_not_written_to_by_another(lib, monkeypatch):
    """With shared names the swapped files want each other's paths: a row that waits keeps its path blocked."""
    lib.show("Show", count=2, prefix="Same")
    lib.show("Show.new", count=2, prefix="Same")
    stamped_video(lib.root / "Third" / "Locked" / "Third - S01E01.mkv")
    lib.scan()
    os.rename(lib.root / "Show", lib.root / "Show.old")
    os.rename(lib.root / "Show.new", lib.root / "Show")
    _unreadable(monkeypatch, "Locked")

    assert lib.scan().status == "partial"
    assert lib.scan().status == "partial"


def test_a_folder_whose_group_has_a_matched_item_at_home_still_takes_new_files_on_an_incomplete_walk(
        lib, monkeypatch):
    lib.show("Show", count=1)
    stamped_video(lib.root / "Third" / "Locked" / "Third - S01E01.mkv")
    lib.scan()
    group = lib.group("Show")
    stamped_video(lib.root / "Show" / "Show - S01E02.mkv")
    _unreadable(monkeypatch, "Locked")

    result = lib.scan()

    assert (result.inserted, result.deferred) == (1, 0)
    assert {row.group_id for path, row in lib.items().items() if "/Show/" in path} == {group.id}


# 2. Group deletion obeys the root guards

def _groups_without_items(lib):
    from media_sources import repository
    lib.show("Keeps", count=1)
    lib.show("Other", count=1)
    lib.scan()
    lib.set_group("Keeps", monitored=0)
    lib.session.execute(sa.delete(_items()).where(_items().c.library_id == lib.library_id))
    assert repository.count_items(library_id=lib.library_id) == 0


def test_an_emptied_root_over_a_library_of_groups_with_no_items_deletes_no_group(lib):
    _groups_without_items(lib)
    before = {row.id: row.group_key for row in lib.groups().values()}
    shutil.rmtree(lib.root)
    lib.root.mkdir()

    result = lib.scan()

    assert result.status == "root_missing"
    assert {row.id: row.group_key for row in lib.groups().values()} == before
    assert lib.group("Keeps").monitored == 0


def test_a_root_that_holds_the_empty_show_folders_is_a_normal_library(lib):
    _groups_without_items(lib)
    before = {row.id for row in lib.groups().values()}

    result = lib.scan()

    assert result.status == "ok"
    assert {row.id for row in lib.groups().values()} == before


def test_a_remounted_root_that_vouches_for_no_group_deletes_none(lib):
    from media_sources import repository
    _groups_without_items(lib)
    before = {row.id for row in lib.groups().values()}
    lib.session.execute(sa.update(repository.Roots).values(last_dev="77"))
    lib.session.execute(sa.update(repository.Groups).values(dir_dev="77", dir_ino="1"))
    shutil.rmtree(lib.root)
    (lib.root / "Unrelated Show").mkdir(parents=True)

    result = lib.scan()

    assert result.status == "root_missing" and "different device" in result.message
    assert {row.id for row in lib.groups().values()} == before


# 3. Another library's row is closed only when that library can vouch for it

def _movie_hardlinked_into_a_series_library(source_db, tmp_path):
    movies = Lib(source_db, tmp_path, name="Local", folder="Movies", content_kind="movie")
    tv = Lib(source_db, tmp_path, name="TV", folder="TV", content_kind="series")
    original = stamped_video(movies.root / "Show - S01E01.mkv")
    stamped_video(movies.root / "Keep.2001.mkv")
    movies.scan()
    (tv.root / "Show").mkdir(parents=True)
    os.link(original, tv.root / "Show" / "Show - S01E01.mkv")
    return movies, tv, original


def test_a_movie_row_is_not_closed_while_its_library_is_unmounted(source_db, tmp_path):
    movies, tv, original = _movie_hardlinked_into_a_series_library(source_db, tmp_path)
    row = movies.items()[str(original)]
    away = movies.root.with_name("Movies.away")
    os.rename(movies.root, away)

    result = tv.scan()

    assert (result.inserted, result.held) == (0, 1)
    assert "cannot be checked right now" in result.message
    os.rename(away, movies.root)
    assert movies.items()[str(original)].id == row.id and tv.items() == {}


def test_a_movie_row_is_not_closed_when_its_library_changed_device(source_db, tmp_path):
    from media_sources import repository
    movies, tv, original = _movie_hardlinked_into_a_series_library(source_db, tmp_path)
    source_db.execute(sa.update(repository.Roots).where(repository.Roots.c.library_id == movies.library_id)
                      .values(last_dev="77"))
    original.unlink()

    result = tv.scan()

    assert (result.inserted, result.held) == (0, 1)
    assert str(original) in movies.items()


def test_a_movie_row_whose_file_really_moved_is_still_closed(source_db, tmp_path):
    movies, tv, original = _movie_hardlinked_into_a_series_library(source_db, tmp_path)
    original.unlink()

    result = tv.scan()

    # Its library is readable, on the same device, and the old path is really gone: the row follows the rules.
    assert result.inserted == 1
    assert str(original) not in movies.items() and len(movies.items()) == 1


def test_a_deferred_handover_preserves_the_row_at_the_destination_path(movies, tmp_path):
    from media_sources import repository

    other = Lib(movies.session, tmp_path, name="Other", folder="Other", content_kind="movie")
    original = stamped_video(other.root / "Moving.2001.mkv")
    destination = stamped_video(movies.root / "Replacement.2002.mkv")
    other.scan()
    movies.scan()
    original_row = other.items()[str(original)]
    destination_row = movies.items()[str(destination)]
    repository.update_item(destination_row.id, movies.session, monitored=0)
    destination.unlink()
    os.link(original, destination)
    other.root.rename(tmp_path / "Other.away")

    result = movies.scan()

    assert (result.deleted, result.handed_over, result.held) == (0, 0, 1)
    assert repository.get_item(original_row.id).path == str(original)
    kept = movies.items()[str(destination)]
    assert (kept.id, kept.monitored) == (destination_row.id, 0)


def test_a_rejected_handover_keeps_the_holder_and_the_displaced_destination_row(movies, tmp_path, monkeypatch):
    from media_sources import repository

    other = Lib(movies.session, tmp_path, name="Other", folder="Other", content_kind="movie")
    original = stamped_video(other.root / "Moving.2001.mkv")
    stamped_video(other.root / "Kept.2002.mkv")
    destination = stamped_video(movies.root / "Replacement.2003.mkv")
    other.scan()
    movies.scan()
    original_row = other.items()[str(original)]
    destination_row = movies.items()[str(destination)]
    original.replace(destination)
    real_update = repository.update_item

    def reject_handover(item_id, target=None, **values):
        if item_id == original_row.id and values.get("library_id") == movies.library_id:
            raise IntegrityError(None, None, ValueError("move refused"))
        return real_update(item_id, target, **values)

    monkeypatch.setattr(repository, "update_item", reject_handover)

    result = movies.scan()

    assert (result.deleted, result.handed_over, result.deferred) == (0, 0, 1)
    assert other.items()[str(original)].id == original_row.id
    assert movies.items()[str(destination)].id == destination_row.id


def test_a_rejected_insert_keeps_the_foreign_row(source_db, tmp_path, monkeypatch):
    from media_sources import repository

    movies, tv, original = _movie_hardlinked_into_a_series_library(source_db, tmp_path)
    original_row = movies.items()[str(original)]
    original.unlink()
    real_insert = repository.insert_item

    def reject_episode(target=None, **values):
        if values["library_id"] == tv.library_id:
            raise IntegrityError(None, None, ValueError("insert refused"))
        return real_insert(target, **values)

    monkeypatch.setattr(repository, "insert_item", reject_episode)

    result = tv.scan()

    assert (result.deleted, result.inserted, result.skipped) == (0, 0, 1)
    assert movies.items()[str(original)].id == original_row.id


@pytest.mark.parametrize("obstruction", ["empty", "permission"])
def test_a_foreign_row_waits_when_its_library_cannot_complete_a_nonempty_walk(
        source_db, tmp_path, monkeypatch, obstruction):
    movies, tv, original = _movie_hardlinked_into_a_series_library(source_db, tmp_path)
    row = movies.items()[str(original)]
    if obstruction == "empty":
        movies.root.rename(tmp_path / "Movies.away")
        movies.root.mkdir()
    else:
        original.unlink()
        (movies.root / "Locked").mkdir()
        _unreadable(monkeypatch, "Locked")

    result = tv.scan()

    assert (result.inserted, result.deleted, result.held) == (0, 0, 1)
    assert movies.items()[str(original)].id == row.id
    assert tv.items() == {} and tv.groups() == {}


def test_a_destination_remount_does_not_match_a_foreign_rows_inode_on_another_device(movies, tmp_path):
    from media_sources import repository

    other = Lib(movies.session, tmp_path, name="Other", folder="Other", content_kind="movie")
    original = stamped_video(other.root / "Original.2001.mkv")
    kept = stamped_video(movies.root / "Kept.2001.mkv")
    other.scan()
    movies.scan()
    row = other.items()[str(original)]
    copy = movies.root / "Copy.2001.mkv"
    shutil.copy2(original, copy)
    # The other filesystem stores the same inode number for a copy with identical size and timestamps.
    dev = str(copy.stat().st_dev + 1)
    repository.update_item(row.id, movies.session, file_dev=dev, file_ino=str(copy.stat().st_ino))
    movies.session.execute(sa.update(repository.Roots).where(repository.Roots.c.library_id == movies.library_id)
                           .values(last_dev=dev))
    original.unlink()
    stamped_video(other.root / "Other.2002.mkv")

    result = movies.scan()

    assert (result.inserted, result.handed_over, result.deleted) == (1, 0, 0)
    assert set(movies.items()) == {str(copy), str(kept)}
    assert other.items()[str(original)].id == row.id


@pytest.mark.parametrize("replacement", ["vanished", "same-device", "inaccessible"])
def test_a_root_that_changes_after_its_walk_keeps_items_and_empty_groups(lib, tmp_path, monkeypatch, replacement):
    from media_sources.kinds.folder import FolderKind

    _groups_without_items(lib)
    (lib.root / "Keeps" / "Keeps - S01E01.mkv").unlink()
    lib.scan()
    before = {row.id: row.group_key for row in lib.groups().values()}
    items = {path: row.id for path, row in lib.items().items()}
    enumerate_folder = FolderKind.enumerate
    real_access = os.access
    changed = False

    def enumerate_then_change(self, source, library, cancel):
        nonlocal changed
        listing = enumerate_folder(self, source, library, cancel)
        observed = listing.files

        def files():
            nonlocal changed
            yield from observed
            changed = True
            if replacement != "inaccessible":
                lib.root.rename(tmp_path / "TV.away")
                if replacement == "same-device":
                    lib.root.mkdir()

        listing.files = files()
        return listing

    monkeypatch.setattr(FolderKind, "enumerate", enumerate_then_change)
    if replacement == "inaccessible":
        monkeypatch.setattr(os, "access", lambda path, mode: (False if changed and path == str(lib.root)
                                                             else real_access(path, mode)))

    result = lib.scan()

    assert result.status == "root_missing" and result.deleted == 0
    assert {row.id: row.group_key for row in lib.groups().values()} == before
    assert {path: row.id for path, row in lib.items().items()} == items
    assert lib.group("Keeps").monitored == 0


def test_a_nested_directory_replaced_during_the_walk_cannot_vouch_for_the_old_view(lib, tmp_path, monkeypatch):
    _groups_without_items(lib)
    (lib.root / "Keeps" / "Keeps - S01E01.mkv").unlink()
    lib.scan()
    before = {row.id: row.group_key for row in lib.groups().values()}
    real_scandir = os.scandir
    changed = False

    def replace_earlier_directory(path):
        nonlocal changed
        if str(path) == lib.path("Other") and not changed:
            changed = True
            (lib.root / "Keeps").rename(tmp_path / "Keeps.away")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", replace_earlier_directory)

    result = lib.scan()

    assert result.status == "root_missing"
    assert {row.id: row.group_key for row in lib.groups().values()} == before


def test_the_previous_owner_cannot_delete_an_item_during_handover(lib, tmp_path, monkeypatch):
    from media_sources import scanner

    other = Lib(lib.session, tmp_path, name="Other", folder="Other")
    (moving,) = lib.show("Moving", count=1)
    lib.show("Stays", count=1)
    lib.scan()
    old_id = lib.items()[str(moving)].id
    lib.set_group("Moving", monitored=0)
    moving.parent.rename(other.root / "Moving")
    handover_ready, let_handover_finish = Event(), Event()
    previous_started, previous_decided, let_previous_finish = Event(), Event(), Event()
    write, decide = scanner._write_repathed, scanner._decide

    def write_repathed(library, *args):
        if library.id == other.library_id:
            handover_ready.set()
            assert let_handover_finish.wait(10)
        return write(library, *args)

    def decide_deletion(*args):
        decisions = decide(*args)
        if not handover_ready.is_set() or decisions.repathed:
            return decisions
        previous_decided.set()
        assert let_previous_finish.wait(10)
        return decisions

    def scan_previous():
        previous_started.set()
        return lib.scan()

    monkeypatch.setattr(scanner, "_write_repathed", write_repathed)
    monkeypatch.setattr(scanner, "_decide", decide_deletion)
    with ThreadPoolExecutor(max_workers=2) as workers:
        destination = workers.submit(other.scan)
        assert handover_ready.wait(10)
        previous = workers.submit(scan_previous)
        try:
            assert previous_started.wait(10)
            previous_decided.wait(0.5)
            let_handover_finish.set()
            assert destination.result(timeout=10).handed_over == 1
        finally:
            let_handover_finish.set()
            let_previous_finish.set()
        assert previous.result(timeout=10).deleted == 0

    new_group = other.group("Moving")
    (item,) = other.items().values()
    assert (item.id, item.group_id, new_group.monitored) == (old_id, new_group.id, 0)


# 4. Isolation (see the no-change file for the stub import itself)

def test_the_package_still_holds_the_real_repository():
    import media_sources
    from media_sources import repository
    assert repository.Items is not None and media_sources.repository.Sources is not None


# Small edges

def test_two_scans_of_one_library_do_not_run_at_once(movies):
    from media_sources import scanner

    stamped_video(movies.root / "Film.2001.mkv")
    lock = scanner.library_lock(movies.library_id)
    assert lock.acquire(blocking=False)
    try:
        result = movies.scan()
    finally:
        lock.release()
    assert result.status == "busy" and "already running" in result.message and result.inserted == 0
    assert movies.items() == {}
    assert movies.scan().inserted == 1


def test_a_scan_that_fails_releases_its_library_lock(movies, monkeypatch):
    from media_sources import scanner

    monkeypatch.setattr(scanner, "_root_snapshot", lambda roots: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        movies.scan()
    lock = scanner.library_lock(movies.library_id)
    assert lock.acquire(blocking=False)
    lock.release()


def test_a_renamed_show_folder_gives_its_episodes_the_new_names_state(lib):
    stamped_video(lib.root / "2020" / "Show - S01E01.mkv")
    lib.scan()
    assert lib.group("2020").identity_state == "unmatched"
    os.rename(lib.root / "2020", lib.root / "Real Show")

    lib.scan()

    assert lib.group("Real Show").identity_state == "parsed"
    assert {row.identity_state for row in lib.items().values()} == {"parsed"}


@pytest.mark.parametrize("value", [8761, 10 ** 11])
def test_the_scan_frequency_has_an_upper_bound(source_db, tmp_path, value):
    from media_sources import service

    (tmp_path / "A").mkdir()
    payload = {"name": "A", "library": {"path": str(tmp_path / "A"), "content_kind": "movie"}}
    with pytest.raises(service.SourceError) as refused:
        service.create_source({**payload, "scan_frequency_hours": value})
    assert "scan_frequency_hours" in str(refused.value)
    assert service.create_source({**payload, "scan_frequency_hours": 8760})


def test_a_source_with_an_absurd_stored_frequency_does_not_stop_the_others_being_due(source_db, tmp_path):
    from datetime import datetime
    from media_sources import repository, schedule

    if source_db.get_bind().dialect.name != "sqlite":
        pytest.skip("only SQLite can hold a value this large: PostgreSQL refuses the write")
    first, _, _ = add_source(source_db, tmp_path, name="One", folder="One")
    second, _, _ = add_source(source_db, tmp_path, name="Two", folder="Two")
    repository.update_source(first, source_db, scan_frequency_hours=10 ** 11,
                             last_scan_finished_at=datetime(2026, 1, 1))
    assert [row.id for row in schedule.due_sources(datetime(2026, 5, 1), source_db)] == [second]


def test_a_source_whose_libraries_are_all_disabled_is_not_due(source_db, tmp_path):
    from datetime import datetime
    from media_sources import repository, schedule

    source_id, library_id, _ = add_source(source_db, tmp_path)
    repository.update_library(library_id, source_db, enabled=0)
    assert schedule.due_sources(datetime(2026, 5, 1), source_db) == []


def test_a_library_scan_leaves_the_source_timestamps_alone(movies):
    from media_sources import repository
    stamped_video(movies.root / "Film.2001.mkv")
    movies.scan()
    from media_sources.scanner import run_scan
    run_scan(movies.source_id, movies.library_id)
    source = repository.get_source(movies.source_id)
    assert source.last_scan_started_at is None and source.last_scan_finished_at is None


def test_adding_a_library_writes_it_and_its_root_in_one_transaction(source_db, tmp_path, monkeypatch):
    from media_sources import repository, service

    source_id, library_id, root = add_source(source_db, tmp_path)
    (tmp_path / "More").mkdir()
    real = repository._insert

    def failing(target, table, values):
        if table is repository.Roots:
            raise RuntimeError("no root")
        return real(target, table, values)

    monkeypatch.setattr(repository, "_insert", failing)
    with pytest.raises(RuntimeError):
        service.add_library(source_id, {"path": str(tmp_path / "More"), "content_kind": "movie"})
    assert [row.id for row in repository.list_libraries(source_id)] == [library_id]


@pytest.mark.parametrize("body", [{"enabled": False}, {"name": "x"}, {"nope": 1}])
def test_a_new_library_rejects_the_keys_it_would_ignore(source_db, tmp_path, body):
    from media_sources import service

    source_id, _, _ = add_source(source_db, tmp_path)
    (tmp_path / "More").mkdir()
    with pytest.raises(service.SourceError) as refused:
        service.add_library(source_id, {"path": str(tmp_path / "More"), "content_kind": "movie", **body})
    assert "Cannot set" in str(refused.value)


def test_a_new_source_rejects_unknown_keys(tmp_path):
    from media_sources import service

    (tmp_path / "A").mkdir()
    for payload in ({"name": "A", "bogus": 1, "library": {"path": str(tmp_path / "A"), "content_kind": "movie"}},
                    {"name": "A", "library": {"path": str(tmp_path / "A"), "content_kind": "movie", "x": 1}}):
        with pytest.raises(service.SourceError) as refused:
            service.create_source(payload)
        assert "Cannot set" in str(refused.value)


def test_a_root_edit_checks_the_new_roots_and_survives_a_scan_that_cannot_be_queued(source_db, tmp_path, monkeypatch):
    from media_sources import repository, service

    source_id, library_id, root = add_source(source_db, tmp_path)
    other_source, other_library, other_root = add_source(source_db, tmp_path, name="Other", folder="Other")
    monkeypatch.setattr(repository, "get_source", lambda sid, target=None: SimpleNamespace(kind="jellyfin", id=sid))
    (tmp_path / "New").mkdir()

    for roots, message in (([{"local_path": str(tmp_path / "missing")}], "not a readable folder"),
                           ([{"local_path": str(other_root / "inside")}], "overlaps"),
                           ([{"local_path": str(other_root)}], "overlaps")):
        (other_root / "inside").mkdir(exist_ok=True)
        with pytest.raises(service.SourceError) as refused:
            service.change_library_roots(source_id, library_id, roots)
        assert message in str(refused.value)
    assert [row.local_path for row in repository.get_roots(library_id)] == [str(root)]

    def disabled(source_id, library_id=None):
        raise service.SourceError("The library is disabled", 409, "disabled")

    monkeypatch.setattr(service, "queue_scan", disabled)
    # The edit stays committed; the scan runs when the library is enabled again.
    assert service.change_library_roots(source_id, library_id, [{"local_path": str(tmp_path / "New")}]) == 0
    assert [row.local_path for row in repository.get_roots(library_id)] == [str(tmp_path / "New")]


def test_deleting_many_items_goes_in_batches(movies, monkeypatch):
    from media_sources import repository

    for number in range(1200):
        repository.insert_item(movies.session, source_id=movies.source_id, library_id=movies.library_id,
                               kind="movie", path=f"/m/{number}.mkv", title="t", identity_source="filename",
                               identity_state="parsed")
    ids = [row.id for row in repository.list_items(library_id=movies.library_id, limit=2000)]
    statements = []
    real = movies.session.execute
    monkeypatch.setattr(movies.session, "execute", lambda *a, **k: statements.append(a[0]) or real(*a, **k))
    assert repository.delete_items(ids, movies.session) == 1200
    assert len(statements) == 3 and repository.count_items(library_id=movies.library_id) == 0


# The arr rows are read once per scan run, and re-validation reads them in batches

def test_one_scan_run_over_two_libraries_reads_the_arr_rows_once(source_db, tmp_path, monkeypatch):
    from media_sources import identity, repository
    from media_sources.scanner import run_scan

    source_id, first, root = add_source(source_db, tmp_path, folder="One")
    second_root = tmp_path / "Two"
    second_root.mkdir()
    repository.create_library(source_id=source_id, content_kind="movie", locator=str(second_root), target=source_db)
    stamped_video(root / "A.2001.mkv")
    stamped_video(second_root / "B.2002.mkv")
    calls = []
    real = identity.ArrOwners.index
    monkeypatch.setattr(identity.ArrOwners, "index", lambda self, kinds=None: calls.append(kinds) or real(self, kinds))

    run_scan(source_id)

    assert calls == [None]


def test_revalidation_fetches_the_arr_rows_in_one_batch_per_table(source_db, tmp_path, monkeypatch):
    from media_sources import identity, repository, shadows
    from test_media_sources_shadows import add_instance, add_movie

    source_id, library_id, root = add_source(source_db, tmp_path)
    from app.config import settings
    monkeypatch.setattr(settings.general, "use_radarr", True)
    add_instance(source_db, mapping=f'[["/arr", "{root}"]]')
    for number in range(1, 4):
        path = stamped_video(root / f"F{number}.2001.mkv")
        add_movie(source_db, f"/arr/{path.name}", row_id=number, file_id=number)
    from media_sources.scanner import run_scan
    run_scan(source_id)
    assert source_db.execute(sa.select(sa.func.count()).select_from(repository.Items)
                             .where(repository.Items.c.arr_shadow == 1)).scalar_one() == 3
    singles, batches = [], []
    real = identity.ArrOwners.fetch_many
    monkeypatch.setattr(identity.ArrOwners, "fetch", lambda self, table, row_id: singles.append(row_id))
    monkeypatch.setattr(identity.ArrOwners, "fetch_many", lambda self, pairs: batches.append(len(pairs)) or real(self, pairs))

    assert shadows.revalidate_shadows() == {"kept": 3, "cleared": 0, "refreshed": 0}
    assert singles == [] and batches == [3]


def test_a_file_reached_through_an_alias_gets_its_shadow_recomputed_too(source_db, tmp_path, monkeypatch):
    from media_sources import repository
    from media_sources.scanner import run_scan
    from test_media_sources_shadows import add_instance, add_movie

    source_id, library_id, root = add_source(source_db, tmp_path)
    from app.config import settings
    monkeypatch.setattr(settings.general, "use_radarr", True)
    original = stamped_video(root / "A" / "Film.2001.mkv")
    run_scan(source_id)
    (root / "B").mkdir()
    os.link(original, root / "B" / "Film.2001.mkv")
    add_instance(source_db, mapping=f'[["/arr", "{root}/A"]]')
    add_movie(source_db, "/arr/Film.2001.mkv", file_id=4)

    run_scan(source_id)

    assert repository.get_item_by_path(str(original), source_db).arr_shadow == 1


# 9. An oversize path

def test_a_path_too_long_for_the_index_is_logged_and_skipped_not_fatal(movies, monkeypatch, caplog):
    from media_sources import scanner
    from media_sources.kinds import DiscoveredFile
    from test_media_sources_scanner import FakeKind

    # Random text: PostgreSQL compresses an index entry before it counts it, so a repetitive path would fit.
    long_path = f"{movies.root}/" + "/".join(os.urandom(125).hex() for _ in range(12)) + "/Long.2001.mkv"
    files = [DiscoveredFile(long_path, 10, 100, "1", "11"), DiscoveredFile(f"{movies.root}/Fine.2002.mkv", 11, 101,
                                                                           "1", "12")]
    monkeypatch.setattr(scanner, "get_kind", lambda name: FakeKind(files))
    monkeypatch.setattr(scanner, "index_item", lambda *args: None)

    result = movies.scan()

    assert (result.status, result.inserted, result.skipped) == ("ok", 1, 1)
    assert list(movies.items()) == [f"{movies.root}/Fine.2002.mkv"]
    assert caplog.text.count("too long to be stored") == 1
    assert movies.scan().skipped == 1


def test_a_path_the_database_refuses_for_its_size_is_skipped_by_the_fallback_too(movies, monkeypatch, caplog):
    """The scan's own limit is a little under PostgreSQL's. A path in between is refused by the database, which
    raises an operational error, and it is logged and skipped like any other refused row."""
    from media_sources import scanner
    from media_sources.kinds import DiscoveredFile
    from test_media_sources_scanner import FakeKind

    if movies.session.get_bind().dialect.name != "postgresql":
        pytest.skip("only PostgreSQL limits the size of an index entry")
    long_path = f"{movies.root}/" + "/".join(os.urandom(125).hex() for _ in range(12)) + "/Long.2001.mkv"
    files = [DiscoveredFile(long_path, 10, 100, "1", "11"), DiscoveredFile(f"{movies.root}/Fine.2002.mkv", 11, 101,
                                                                           "1", "12")]
    monkeypatch.setattr(scanner, "get_kind", lambda name: FakeKind(files))
    monkeypatch.setattr(scanner, "index_item", lambda *args: None)
    monkeypatch.setattr(scanner, "MAX_PATH_BYTES", 10_000)

    result = movies.scan()

    assert (result.status, result.inserted, result.skipped) == ("ok", 1, 1)
    assert "could not be added and was skipped" in caplog.text


def test_a_group_with_a_file_at_home_reached_through_an_alias_is_not_judged_gone(lib):
    """Two of its files moved to two other folders, and the third is still at home but is found first through a
    hardlink elsewhere: it is where it was, so the group stays and keeps its key."""
    lib.show("Show", count=3)
    lib.scan()
    group = lib.group("Show")
    home = lib.root / "Show" / "Show - S01E01.mkv"
    (lib.root / "A").mkdir()
    (lib.root / "B").mkdir()
    os.link(home, lib.root / "A" / "Linked - S01E01.mkv")
    os.rename(lib.root / "Show" / "Show - S01E02.mkv", lib.root / "A" / "Show - S01E02.mkv")
    os.rename(lib.root / "Show" / "Show - S01E03.mkv", lib.root / "B" / "Show - S01E03.mkv")

    lib.scan()

    assert lib.group("Show").id == group.id
    assert not [key for key in lib.groups() if key.startswith("pending:")]
    assert lib.items()[str(home)].group_id == group.id
