"""Matching a discovered file to a stored item, re-pathing, the handover between libraries, and the delete rules
that never trust an unreadable, unmounted, empty, remounted or half-read root."""
import os
import shutil
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from media_source_helpers import Lib, add_profile, stamped_video


@pytest.fixture(autouse=True)
def enabled_languages(languages_loaded, monkeypatch):
    from subzero.language import Language
    from media_sources import indexer
    monkeypatch.setattr(indexer, "get_language_set", lambda: {Language("eng")})


@pytest.fixture
def movies(source_db, tmp_path):
    return Lib(source_db, tmp_path, name="Local", folder="Movies", content_kind="movie")


def scan_all(lib):
    from media_sources.scanner import run_scan
    return run_scan(lib.source_id)[0]


def by_path(lib):
    return lib.items()


def _items():
    from media_sources import repository
    return repository.Items


# The rows' own file is followed

def test_a_rename_keeps_the_item_and_everything_the_user_set_and_reads_the_new_name(movies):
    from media_sources import repository

    old = stamped_video(movies.root / "Movie.A.2019.1080p.mkv")
    movies.scan()
    item = movies.items()[str(old)]
    repository.insert_history(movies.session, item_id=item.id, source_id=movies.source_id, item_kind="movie",
                              item_title="Movie A", source_name="Local", action=1, description="downloaded")
    movies.session.execute(sa.update(_items()).where(_items().c.id == item.id).values(
        ffprobe_cache=b"probe", video_codec="h264", resolution="1080p", audio_language="en", monitored=0,
        profile_id=None, failed_attempts="[1]"))
    new = movies.root / "Movie.B.2020.720p.mkv"
    os.rename(old, new)

    result = movies.scan()

    assert (result.moved, result.inserted, result.deleted) == (1, 0, 0)
    moved = movies.items()[str(new)]
    assert moved.id == item.id
    # Recomputed from the new name.
    assert (moved.title, moved.year, moved.scene_name, moved.identity_state) == ("Movie B", "2020",
                                                                                "Movie.B.2020.720p", "parsed")
    # Moves with the item.
    row = movies.session.execute(sa.select(_items()).where(_items().c.id == item.id)).one()
    assert (row.ffprobe_cache, row.video_codec, row.resolution, row.audio_language, row.monitored,
            row.failed_attempts) == (b"probe", "h264", "1080p", "en", 0, "[1]")
    history = movies.session.execute(sa.select(repository.History.c.item_id)).scalars().all()
    assert history == [item.id]


def test_a_rename_that_fixes_an_unmatched_name_makes_the_item_parsed(movies):
    old = stamped_video(movies.root / "holiday clip.avi")
    movies.scan()
    assert movies.items()[str(old)].identity_state == "unmatched"
    new = movies.root / "Holiday Clip 2010.avi"
    os.rename(old, new)

    movies.scan()

    assert movies.items()[str(new)].identity_state == "parsed"


def test_an_item_with_a_user_identity_keeps_it_through_a_rename_and_gets_a_new_release_name(movies):
    from media_sources import repository

    old = stamped_video(movies.root / "Movie.A.2019.mkv")
    movies.scan()
    item = movies.items()[str(old)]
    repository.update_item(item.id, movies.session, identity_source="user", title="Chosen", year="2001",
                           identity_state="identified")
    new = movies.root / "Movie.B.2020.mkv"
    os.rename(old, new)

    movies.scan()

    moved = movies.items()[str(new)]
    assert (moved.id, moved.title, moved.year, moved.identity_source) == (item.id, "Chosen", "2001", "user")
    assert moved.scene_name == "Movie.B.2020"


def test_two_files_trading_paths_each_follow_their_own_row_with_no_constraint_error(movies):
    first = stamped_video(movies.root / "One.2001.mkv", size=3001)
    second = stamped_video(movies.root / "Two.2002.mkv", size=3002)
    movies.scan()
    ids = {path: row.id for path, row in movies.items().items()}
    os.rename(first, movies.root / "tmp")
    os.rename(second, first)
    os.rename(movies.root / "tmp", second)

    result = movies.scan()

    assert (result.moved, result.inserted, result.deleted) == (2, 0, 0)
    items = movies.items()
    assert items[str(first)].id == ids[str(second)] and items[str(second)].id == ids[str(first)]
    assert (items[str(first)].file_size, items[str(second)].file_size) == (3002, 3001)
    assert not [path for path in items if path.startswith("pending:")]


def test_a_row_follows_its_file_even_when_its_old_path_now_holds_another_file(movies):
    old = stamped_video(movies.root / "Film.2001.mkv", size=3100)
    movies.scan()
    item = movies.items()[str(old)]
    moved = movies.root / "Film.2001.1080p.mkv"
    os.rename(old, moved)
    stamped_video(old, size=3200)  # a different file at the old path

    result = movies.scan()

    items = movies.items()
    assert items[str(moved)].id == item.id
    assert items[str(old)].id != item.id and items[str(old)].file_size == 3200
    assert (result.moved, result.inserted, result.deleted) == (1, 1, 0)


def test_a_file_at_a_reused_path_that_no_identity_claimed_is_a_path_only_match_for_a_movie(movies):
    path = stamped_video(movies.root / "Film.2001.mkv", size=3100)
    movies.scan()
    item = movies.items()[str(path)]
    movies.session.execute(sa.update(_items()).where(_items().c.id == item.id).values(ffprobe_cache=b"old"))
    stamped_video(path, size=3300)

    result = movies.scan()

    kept = movies.items()[str(path)]
    assert (kept.id, kept.file_size) == (item.id, 3300)
    assert (result.updated, result.inserted, result.deleted) == (1, 0, 0)
    assert movies.session.execute(sa.select(_items().c.ffprobe_cache).where(_items().c.id == item.id)).scalar_one() is None


def test_the_row_displaced_by_a_replace_is_deleted(movies):
    kept = stamped_video(movies.root / "Keep.2001.mkv", size=3100)
    replaced = stamped_video(movies.root / "Old.2002.mkv", size=3200)
    newer = stamped_video(movies.root / "New.2003.mkv", size=3300)
    movies.scan()
    ids = {path: row.id for path, row in movies.items().items()}
    os.replace(newer, replaced)

    result = movies.scan()

    items = movies.items()
    assert set(items) == {str(kept), str(replaced)}
    assert items[str(replaced)].id == ids[str(newer)]
    assert ids[str(replaced)] not in {row.id for row in items.values()}
    assert (result.moved, result.deleted, result.inserted) == (1, 1, 0)


@pytest.mark.parametrize("differs", ["size", "mtime"])
def test_inode_reuse_with_another_size_or_mtime_does_not_match(movies, differs):
    from media_sources import repository

    old = stamped_video(movies.root / "Old.2001.mkv", size=3100)
    movies.scan()
    item = movies.items()[str(old)]
    stored_mtime = old.stat().st_mtime_ns
    old.unlink()
    # A new file on the very inode the row stored, with another size or another mtime.
    new = stamped_video(movies.root / "New.2002.mkv", size=3101 if differs == "size" else 3100)
    if differs == "size":
        os.utime(new, ns=(stored_mtime, stored_mtime))
    info = new.stat()
    # SQLite hands a deleted id to the next row, so the row is told apart by what the user set on it.
    repository.update_item(item.id, movies.session, file_ino=str(info.st_ino), file_dev=str(info.st_dev),
                           monitored=0, failed_attempts="[1]")

    result = movies.scan()

    assert (result.inserted, result.deleted, result.moved) == (1, 1, 0)
    row = movies.items()[str(new)]
    assert (row.monitored, row.failed_attempts) == (1, None)
    assert str(old) not in movies.items()


def test_the_identity_holds_by_path_size_and_mtime_when_the_inode_changed(movies):
    from media_sources import repository

    path = stamped_video(movies.root / "Film.2001.mkv")
    movies.scan()
    item = movies.items()[str(path)]
    repository.update_item(item.id, movies.session, file_ino="1", file_dev="1")
    movies.session.execute(sa.update(_items()).where(_items().c.id == item.id).values(ffprobe_cache=b"probe"))

    result = movies.scan()

    assert (result.unchanged, result.updated, result.moved, result.inserted) == (1, 0, 0, 0)
    row = movies.session.execute(sa.select(_items()).where(_items().c.id == item.id)).one()
    assert row.ffprobe_cache == b"probe" and row.file_ino == str(path.stat().st_ino)


def test_the_result_does_not_depend_on_the_order_the_walk_lists_the_files_in():
    from media_sources import identity
    from media_sources.kinds import DiscoveredFile

    def row(item_id, path, ino, size, mtime):
        return SimpleNamespace(id=item_id, path=path, file_ino=ino, file_dev="1", file_size=size,
                               file_mtime_ns=mtime, identity_source="filename", group_id=None)

    rows = [row(1, "/m/a", "11", 10, 100), row(2, "/m/b", "12", 20, 200), row(3, "/m/c", "13", 30, 300)]
    files = [DiscoveredFile("/m/a", 20, 200, "1", "12"), DiscoveredFile("/m/b", 10, 100, "1", "11"),
             DiscoveredFile("/m/c", 30, 300, "1", "13"), DiscoveredFile("/m/d", 40, 400, "1", "14")]

    def run(listing):
        match = identity.match_library(listing, rows, ignore_dev=lambda f: False, foreign_lookup=lambda i, p: [],
                                       library_content_kind="movie")
        return (sorted((r.id, f.path) for r, f in match.unchanged), sorted((r.id, f.path) for r, f in match.moved),
                sorted(f.path for f in match.new))

    expected = run(files)
    assert expected == ([(3, "/m/c")], [(1, "/m/b"), (2, "/m/a")], ["/m/d"])
    assert run(list(reversed(files))) == expected


# Between libraries and sources

def _second(session, tmp_path, name="Other", folder="Other", content_kind="movie"):
    return Lib(session, tmp_path, name=name, folder=folder, content_kind=content_kind)


def test_an_rsync_mirror_with_equal_name_size_and_mtime_gets_its_own_item(movies, tmp_path):
    original = stamped_video(movies.root / "Film.2001.mkv")
    mirror = _second(movies.session, tmp_path)
    mirror.root.mkdir(exist_ok=True)
    shutil.copy2(original, mirror.root / "Film.2001.mkv")
    movies.scan()

    result = mirror.scan()

    assert (result.inserted, result.held) == (1, 0)
    assert len(mirror.items()) == 1 and len(movies.items()) == 1


def test_a_hardlink_and_a_symlinked_file_in_a_second_source_are_one_item_not_two(movies, tmp_path):
    original = stamped_video(movies.root / "Film.2001.mkv")
    other_original = stamped_video(movies.root / "Two.2002.mkv")
    second = _second(movies.session, tmp_path)
    second.root.mkdir(exist_ok=True)
    os.link(original, second.root / "Film.2001.mkv")
    (second.root / "Two.2002.mkv").symlink_to(other_original)
    movies.scan()

    result = second.scan()

    assert (result.inserted, result.held) == (0, 2)
    assert "2 already supplied by Local" in result.message
    assert second.items() == {} and len(movies.items()) == 2


def test_a_bind_mount_of_the_same_folder_is_one_item_not_two(movies, tmp_path, monkeypatch):
    """Another path, the very same device and inode: what a bind mount of a folder looks like to the scan."""
    from media_sources import scanner
    from media_sources.kinds import DiscoveredFile

    original = stamped_video(movies.root / "Film.2001.mkv")
    movies.scan()
    info = original.stat()
    second = _second(movies.session, tmp_path)
    found = DiscoveredFile(str(second.root / "Film.2001.mkv"), info.st_size, info.st_mtime_ns, str(info.st_dev),
                           str(info.st_ino))
    from test_media_sources_scanner import FakeKind
    monkeypatch.setattr(scanner, "get_kind", lambda name: FakeKind([found]))

    result = second.scan()

    assert (result.inserted, result.held) == (0, 1) and second.items() == {}


def test_a_second_source_does_not_insert_a_file_an_enabled_holder_holds_by_identity(movies, tmp_path):
    original = stamped_video(movies.root / "Film.2001.mkv")
    movies.scan()
    second = _second(movies.session, tmp_path)
    second.root.mkdir(exist_ok=True)
    os.link(original, second.root / "Linked.mkv")

    result = second.scan()

    assert result.inserted == 0 and result.held == 1


def test_a_file_that_moved_to_another_library_of_the_same_kind_follows_its_row(movies, tmp_path):
    from media_sources import repository

    keep = stamped_video(movies.root / "Keep.2001.mkv")
    moving = stamped_video(movies.root / "Moving.2002.mkv")
    movies.scan()
    item = movies.items()[str(moving)]
    repository.insert_history(movies.session, item_id=item.id, source_id=movies.source_id, item_kind="movie",
                              item_title="Moving", source_name="Local", action=1, description="downloaded")
    movies.session.execute(sa.update(_items()).where(_items().c.id == item.id).values(monitored=0))
    other = _second(movies.session, tmp_path)
    other.root.mkdir(exist_ok=True)
    os.rename(moving, other.root / "Moving.2002.mkv")

    result = other.scan()

    assert result.handed_over == 1 and result.inserted == 0
    handed = other.items()[str(other.root / "Moving.2002.mkv")]
    assert (handed.id, handed.monitored, handed.library_id, handed.source_id) == (
        item.id, 0, other.library_id, other.source_id)
    # The first library then finds the file gone and removes nothing else.
    assert movies.scan().deleted == 0 and set(movies.items()) == {str(keep)}
    assert movies.session.execute(sa.select(repository.History.c.item_id)).scalars().all() == [item.id]


def test_a_file_moved_into_a_library_of_another_content_kind_closes_the_row_and_is_added_new(movies, tmp_path):
    keep = stamped_video(movies.root / "Keep.2001.mkv")
    moving = stamped_video(movies.root / "Show - S01E01.mkv")
    movies.scan()
    item = movies.items()[str(moving)]
    movies.session.execute(sa.update(_items()).where(_items().c.id == item.id).values(monitored=0))
    tv = _second(movies.session, tmp_path, name="TV", folder="TV", content_kind="series")
    (tv.root / "Show").mkdir(parents=True)
    os.rename(moving, tv.root / "Show" / "Show - S01E01.mkv")

    result = tv.scan()

    assert result.inserted == 1 and result.handed_over == 0
    (episode,) = tv.items().values()
    # Nothing of the closed movie row is carried: the episode is a new item (its id may be the closed row's, as
    # SQLite reuses ids).
    assert (episode.kind, episode.monitored, episode.library_id) == ("episode", 1, tv.library_id)
    assert set(movies.items()) == {str(keep)}


def test_a_removed_sources_files_come_back_as_new_in_another_source(movies, tmp_path):
    from media_sources import repository

    original = stamped_video(movies.root / "Film.2001.mkv")
    movies.scan()
    old = movies.items()[str(original)]
    movies.session.execute(sa.update(_items()).where(_items().c.id == old.id).values(monitored=0))
    other = _second(movies.session, tmp_path)
    other.root.mkdir(exist_ok=True)
    os.link(original, other.root / "Film.2001.mkv")
    repository.delete_source(movies.source_id, movies.session)

    result = other.scan()

    assert result.inserted == 1 and result.held == 0
    (item,) = other.items().values()
    assert (item.monitored, item.library_id) == (1, other.library_id)


def test_adopting_an_episode_from_a_disabled_source_moves_it_to_the_destination_group(movies, tmp_path):
    from media_sources import repository

    first = Lib(movies.session, tmp_path, name="First", folder="First", content_kind="series")
    second = Lib(movies.session, tmp_path, name="Second", folder="Second", content_kind="series")
    episode = stamped_video(first.root / "Show" / "Show - S01E01.mkv")
    first.scan()
    old_group = first.group("Show")
    first.set_group("Show", monitored=0)
    item = first.items()[str(episode)]
    movies.session.execute(sa.update(repository.Sources).where(repository.Sources.c.id == first.source_id)
                           .values(enabled=0))
    (second.root / "Show").mkdir(parents=True)
    os.link(episode, second.root / "Show" / "Show - S01E01.mkv")

    result = second.scan()

    assert (result.handed_over, result.inserted) == (1, 0)
    adopted = repository.get_item(item.id)
    new_group = second.group("Show")
    assert (adopted.source_id, adopted.library_id, adopted.group_id) == (second.source_id, second.library_id,
                                                                         new_group.id)
    # Another directory: nothing group-level is carried over.
    assert new_group.id != old_group.id and new_group.monitored == 1
    # Removing the old source takes its group and leaves the adopted item.
    repository.delete_source(first.source_id, movies.session)
    assert repository.get_item(item.id) is not None
    assert repository.get_group(new_group.id) is not None


def test_a_handover_to_an_alias_of_the_same_directory_carries_the_groups_own_settings(movies, tmp_path, monkeypatch):
    from media_sources import groups as group_rules, repository

    first = Lib(movies.session, tmp_path, name="First", folder="First", content_kind="series")
    second = Lib(movies.session, tmp_path, name="Second", folder="Second", content_kind="series")
    episode = stamped_video(first.root / "Show" / "Show - S01E01.mkv")
    first.scan()
    old_group = first.group("Show")
    profile = add_profile(movies.session, [("en", "False", "False")])
    first.set_group("Show", monitored=0, profile_id=profile, identity_source="user", title="Mine", tvdb_id=9)
    movies.session.execute(sa.update(repository.Sources).where(repository.Sources.c.id == first.source_id)
                           .values(enabled=0))
    (second.root / "Show").mkdir(parents=True)
    os.link(episode, second.root / "Show" / "Show - S01E01.mkv")
    # The destination directory is another path to the same physical directory.
    real = group_rules.stat_directory
    monkeypatch.setattr(group_rules, "stat_directory", lambda path: group_rules.DirState(
        "dir", old_group.dir_dev, old_group.dir_ino) if path == second.path("Show") else real(path))

    second.scan()

    carried = second.group("Show")
    assert (carried.monitored, carried.profile_id, carried.title, carried.tvdb_id, carried.identity_source) == (
        0, profile, "Mine", 9, "user")


def test_a_file_held_by_a_disabled_library_of_another_content_kind_is_not_adopted(movies, tmp_path):
    from media_sources import repository

    original = stamped_video(movies.root / "Show - S01E01.mkv")
    movies.scan()
    movies.session.execute(sa.update(repository.Libraries).where(repository.Libraries.c.id == movies.library_id)
                           .values(enabled=0))
    tv = _second(movies.session, tmp_path, name="TV", folder="TV", content_kind="series")
    (tv.root / "Show").mkdir(parents=True)
    os.link(original, tv.root / "Show" / "Show - S01E01.mkv")

    result = tv.scan()

    assert (result.inserted, result.handed_over, result.held) == (0, 0, 1)
    assert "held by Local, a movie library, which is disabled" in result.message
    assert tv.items() == {} and len(movies.items()) == 1


# Delete safety

def test_a_root_remounted_on_another_device_with_unrelated_files_deletes_and_adds_nothing(movies):
    from media_sources import repository

    old = [stamped_video(movies.root / f"Old.{2000 + number}.mkv") for number in range(3)]
    movies.scan()
    before = {path: row.id for path, row in movies.items().items()}
    # The share came back on another device, holding other videos.
    movies.session.execute(sa.update(repository.Roots).values(last_dev="77"))
    movies.session.execute(sa.update(repository.Items).values(file_dev="77"))
    for path in old:
        path.unlink()
    stamped_video(movies.root / "Replacement.2010.mkv")

    result = movies.scan()

    assert result.status == "root_missing" and "different device" in result.message
    assert (result.inserted, result.deleted) == (0, 0)
    assert {path: row.id for path, row in movies.items().items()} == before
    assert repository.get_roots(movies.library_id)[0].last_dev == "77"


def test_a_remount_that_still_holds_the_known_files_goes_on_with_inode_identity_and_refreshes_the_device(movies):
    from media_sources import repository

    kept = stamped_video(movies.root / "Kept.2001.mkv")
    gone = stamped_video(movies.root / "Gone.2002.mkv")
    movies.scan()
    ids = {path: row.id for path, row in movies.items().items()}
    movies.session.execute(sa.update(repository.Roots).values(last_dev="77"))
    movies.session.execute(sa.update(repository.Items).values(file_dev="77"))
    gone.unlink()

    result = movies.scan()

    assert result.status == "ok" and (result.unchanged, result.deleted, result.inserted) == (1, 1, 0)
    assert movies.items()[str(kept)].id == ids[str(kept)]
    real_dev = str(os.stat(movies.root).st_dev)
    assert repository.get_roots(movies.library_id)[0].last_dev == real_dev
    assert movies.items()[str(kept)].file_dev == real_dev


def test_a_remount_on_another_device_does_not_match_by_inode_when_the_device_is_recorded_unchanged(movies):
    """Without a device change, an equal inode on another device is another file."""
    from media_sources import identity
    from media_sources.kinds import DiscoveredFile

    row = SimpleNamespace(id=1, path="/m/a", file_ino="5", file_dev="1", file_size=10, file_mtime_ns=100,
                          identity_source="filename", group_id=None)
    moved = DiscoveredFile("/m/b", 10, 100, "2", "5")
    kwargs = dict(foreign_lookup=lambda i, p: [], library_content_kind="movie")
    assert identity.match_library([moved], [row], ignore_dev=lambda f: False, **kwargs).new == [moved]
    assert identity.match_library([moved], [row], ignore_dev=lambda f: True, **kwargs).moved == [(row, moved)]


def test_an_unreadable_root_and_a_missing_one_delete_nothing(movies):
    stamped_video(movies.root / "Film.2001.mkv")
    movies.scan()
    os.chmod(movies.root, 0)
    try:
        if os.access(movies.root, os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        result = movies.scan()
    finally:
        os.chmod(movies.root, 0o755)
    assert result.status == "root_missing" and len(movies.items()) == 1


def test_an_incomplete_series_walk_defers_what_needs_a_group_decision_and_deletes_nothing(source_db, tmp_path,
                                                                                         monkeypatch):
    lib = Lib(source_db, tmp_path)
    stamped_video(lib.root / "Show" / "Show - S01E01.mkv")
    stamped_video(lib.root / "Other" / "Other - S01E01.mkv")
    lib.scan()
    before = {path: (row.id, row.group_id) for path, row in lib.items().items()}
    # A new show appears, a file in an existing show is renamed, and one show cannot be read.
    stamped_video(lib.root / "Brand New" / "Brand New - S01E01.mkv")
    os.rename(lib.root / "Show" / "Show - S01E01.mkv", lib.root / "Show" / "Show - S01E01 - Pilot.mkv")
    real_scandir = os.scandir

    def scandir(path):
        if str(path).endswith("Other"):
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr("media_sources.kinds.folder.os.scandir", scandir)

    result = lib.scan()

    assert result.status == "partial" and result.deferred == 1 and result.deleted == 0
    assert "left for a scan that can read the whole folder" in result.message
    assert lib.path("Brand New") not in lib.groups()
    items = lib.items()
    # The rename inside the same show is safe and is followed; the unreadable show is untouched.
    assert items[lib.path("Show", "Show - S01E01 - Pilot.mkv")].id == before[lib.path("Show", "Show - S01E01.mkv")][0]
    assert items[lib.path("Other", "Other - S01E01.mkv")].id == before[lib.path("Other", "Other - S01E01.mkv")][0]
    monkeypatch.setattr("media_sources.kinds.folder.os.scandir", real_scandir)
    assert lib.scan().deferred == 0 and lib.path("Brand New") in lib.groups()


def test_a_displaced_row_is_not_deleted_on_an_incomplete_walk_and_what_wants_its_path_waits(movies, monkeypatch):
    from media_sources import scanner
    from media_sources.kinds import DiscoveredFile

    replaced = stamped_video(movies.root / "Old.2002.mkv", size=3200)
    newer = stamped_video(movies.root / "New.2003.mkv", size=3300)
    movies.scan()
    ids = {path: row.id for path, row in movies.items().items()}
    os.replace(newer, replaced)
    info = replaced.stat()
    found = DiscoveredFile(str(replaced), info.st_size, info.st_mtime_ns, str(info.st_dev), str(info.st_ino))
    from test_media_sources_scanner import FakeKind
    real_get_kind = scanner.get_kind
    monkeypatch.setattr(scanner, "get_kind", lambda name: FakeKind([found], complete=False))

    result = movies.scan()

    assert result.status == "partial" and result.deleted == 0 and result.deferred == 1
    assert {row.id for row in movies.items().values()} == set(ids.values())
    monkeypatch.setattr(scanner, "get_kind", real_get_kind)
    assert movies.scan().deleted == 1


def test_a_pending_identification_is_asked_again_on_every_scan_and_a_users_is_left_alone(movies, monkeypatch):
    from media_sources import identify, repository

    path = stamped_video(movies.root / "Film.2001.mkv")
    mine = stamped_video(movies.root / "Mine.2002.mkv")
    movies.scan()
    items = movies.items()
    repository.update_item(items[str(path)].id, movies.session, identify_pending=1, title="stale")
    repository.update_item(items[str(mine)].id, movies.session, identify_pending=1, identity_source="user",
                           title="Mine")
    asked = []
    real = identify.identify_item
    monkeypatch.setattr(identify, "identify_item", lambda *args: asked.append(args[1]) or real(*args))

    movies.scan()

    items = movies.items()
    assert items[str(path)].identify_pending == 0 and items[str(path)].title == "Film"
    assert (items[str(mine)].identify_pending, items[str(mine)].title) == (0, "Mine")
    assert asked == [str(path)]


def test_twenty_thousand_entries_are_written_in_batches_not_one_transaction(movies, monkeypatch):
    from media_sources import scanner
    from media_sources.kinds import DiscoveredFile
    from test_media_sources_scanner import FakeKind

    files = [DiscoveredFile(f"{movies.root}/Film {number}.mkv", 1000 + number, 5_000_000_000 + number, "1",
                            str(10_000 + number)) for number in range(20_000)]
    monkeypatch.setattr(scanner, "get_kind", lambda name: FakeKind(files))
    monkeypatch.setattr(scanner, "index_item", lambda *args: None)
    monkeypatch.setattr(scanner.identify, "identify_item", lambda content_kind, path, *rest: dict(
        title=os.path.basename(path), sort_title=os.path.basename(path).lower(), parsed_title=None, year=None,
        identity_source="filename", identity_state="unmatched", scene_name=os.path.basename(path),
        identify_pending=0))
    opened, largest = [], []
    real = scanner.source_transaction

    class Counting:
        def __init__(self, *args, **kwargs):
            self.manager = real(*args, **kwargs)
            opened.append(1)

        def __enter__(self):
            self.connection = self.manager.__enter__()
            return self.connection

        def __exit__(self, *exc):
            largest.append(self.connection.execute(sa.text("SELECT count(*) FROM table_source_items")).scalar_one())
            return self.manager.__exit__(*exc)

    monkeypatch.setattr(scanner, "source_transaction", Counting)

    result = movies.scan()

    assert result.inserted == 20_000
    assert len(opened) == 40
    # No transaction ever held more than one batch of rows beyond what earlier batches had already committed.
    assert [after - before for before, after in zip([0] + largest, largest)] == [500] * 40


def test_the_first_scan_of_a_series_library_adds_what_it_can_read_even_when_part_of_it_cannot_be(source_db, tmp_path,
                                                                                               monkeypatch):
    lib = Lib(source_db, tmp_path)
    stamped_video(lib.root / "Show" / "Show - S01E01.mkv")
    stamped_video(lib.root / "Locked" / "Locked - S01E01.mkv")
    real_scandir = os.scandir

    def scandir(path):
        if str(path).endswith("Locked"):
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr("media_sources.kinds.folder.os.scandir", scandir)

    result = lib.scan()

    assert (result.status, result.inserted, result.deferred) == ("partial", 1, 0)
    assert set(lib.groups()) == {lib.path("Show")}


def test_a_device_change_met_on_an_incomplete_walk_is_not_recorded_so_the_next_scan_still_ignores_it(movies,
                                                                                                      monkeypatch):
    from media_sources import repository, scanner
    from media_sources.kinds import DiscoveredFile
    from test_media_sources_scanner import FakeKind

    first = stamped_video(movies.root / "One.2001.mkv")
    second = stamped_video(movies.root / "Two.2002.mkv")
    movies.scan()
    movies.session.execute(sa.update(repository.Roots).values(last_dev="77"))
    movies.session.execute(sa.update(repository.Items).values(file_dev="77"))
    info = first.stat()
    found = DiscoveredFile(str(first), info.st_size, info.st_mtime_ns, str(info.st_dev), str(info.st_ino))
    real_get_kind = scanner.get_kind
    monkeypatch.setattr(scanner, "get_kind", lambda name: FakeKind([found], complete=False))

    assert movies.scan().status == "partial"
    assert repository.get_roots(movies.library_id)[0].last_dev == "77"

    monkeypatch.setattr(scanner, "get_kind", real_get_kind)
    renamed = movies.root / "Two.2002.1080p.mkv"
    os.rename(second, renamed)
    result = movies.scan()
    # The second file still carries the old device number, and is found by its inode all the same.
    assert (result.moved, result.inserted, result.deleted) == (1, 0, 0)
    assert repository.get_roots(movies.library_id)[0].last_dev == str(os.stat(movies.root).st_dev)
