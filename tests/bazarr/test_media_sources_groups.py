"""Resolving groups: every row of the cases table, each in both walk orders, with and without shared file names,
on SQLite and on PostgreSQL. A group follows its items, whatever the old directory now holds."""
import logging
import os
import shutil

import pytest
import sqlalchemy as sa

from media_source_helpers import Lib, add_profile, stamped_video


@pytest.fixture(autouse=True)
def enabled_languages(languages_loaded, monkeypatch):
    from subzero.language import Language
    from media_sources import indexer
    monkeypatch.setattr(indexer, "get_language_set", lambda: {Language("eng")})


@pytest.fixture(params=["forward", "reverse"])
def walk(request, monkeypatch):
    """The order the walk hands its files to the scanner in."""
    if request.param == "reverse":
        from media_sources.kinds import Listing, folder
        real = folder.FolderKind.enumerate

        def reversed_enumerate(self, source, library, cancel=None):
            listing = real(self, source, library, cancel)
            files = list(listing)
            files.reverse()
            return Listing(files=iter(files), complete=listing.complete, problems=listing.problems,
                           skipped_names=listing.skipped_names, ungrouped=listing.ungrouped)
        monkeypatch.setattr(folder.FolderKind, "enumerate", reversed_enumerate)
    return request.param


@pytest.fixture(params=[False, True], ids=["own-names", "shared-names"])
def shared(request):
    return request.param


@pytest.fixture
def lib(source_db, tmp_path):
    return Lib(source_db, tmp_path)


@pytest.fixture
def identified(monkeypatch):
    """The directories a group was identified from, in order."""
    from media_sources import identify
    calls = []
    real = identify.identify_group
    monkeypatch.setattr(identify, "identify_group", lambda directory: calls.append(directory) or real(directory))
    return calls


def _no_stale_keys(lib):
    assert not [key for key in lib.groups() if key.startswith("pending:")]
    assert not [path for path in lib.items() if path.startswith("pending:")]


def test_a_series_scan_makes_a_group_per_show_and_an_episode_per_file(lib):
    lib.show("Bluey (2018)", count=2)
    lib.show("Doctor Who", count=1)
    stamped_video(lib.root / "stray.S01E01.mkv")

    result = lib.scan()

    assert (result.status, result.inserted) == ("ok", 3)
    assert "1 video file(s) directly in the library folder have no show" in result.message
    groups = lib.groups()
    assert set(groups) == {lib.path("Bluey (2018)"), lib.path("Doctor Who")}
    bluey = groups[lib.path("Bluey (2018)")]
    assert (bluey.title, bluey.year, bluey.kind, bluey.identity_state) == ("Bluey", "2018", "show", "parsed")
    assert bluey.dir_ino and bluey.dir_dev
    episodes = lib.items()
    episode = episodes[lib.path("Bluey (2018)", "Bluey (2018) - S01E01.mkv")]
    assert (episode.kind, episode.group_id, episode.season, episode.episode) == ("episode", bluey.id, 1, 1)
    assert episode.identity_state == "parsed" and episode.scene_name == "Bluey (2018) - S01E01"


def test_nested_season_folders_belong_to_the_show_and_an_incomplete_name_is_not_parsed(lib):
    stamped_video(lib.root / "Show" / "Season 2" / "E05.mkv")
    stamped_video(lib.root / "Show" / "Show.S01E01E02.mkv")
    stamped_video(lib.root / "Show" / "Behind The Scenes" / "Featurette.mkv")
    stamped_video(lib.root / "Show" / "random name.mkv")

    lib.scan()

    items = lib.items()
    assert len(lib.groups()) == 1 and len(items) == 3
    nested = items[lib.path("Show", "Season 2", "E05.mkv")]
    assert (nested.season, nested.episode, nested.identity_state) == (2, 5, "parsed")
    # Two episodes in one file, and a name with no numbers, are listed but never searched automatically.
    assert items[lib.path("Show", "Show.S01E01E02.mkv")].identity_state == "unmatched"
    assert items[lib.path("Show", "random name.mkv")].identity_state == "unmatched"


# 1. The remake

@pytest.mark.parametrize("inode_back", [False, True], ids=["new-inode", "old-inode-back"])
def test_a_remake_gets_a_fresh_group_and_the_renamed_show_keeps_its_settings(lib, walk, shared, inode_back,
                                                                           monkeypatch, identified):
    from media_sources import groups as group_rules

    profile = add_profile(lib.session, [("en", "False", "False")])
    lib.show("Show", count=3)
    lib.scan()
    old = lib.group("Show")
    lib.set_group("Show", monitored=0, profile_id=profile)
    old_ids = {path: row.id for path, row in lib.items().items()}
    identified.clear()

    os.rename(lib.root / "Show", lib.root / "Show (2010)")
    lib.show("Show", count=2, prefix="Show" if shared else "Remake")
    if inode_back:
        real = group_rules.stat_directory
        monkeypatch.setattr(group_rules, "stat_directory", lambda path: group_rules.DirState(
            "dir", old.dir_dev, old.dir_ino) if path == lib.path("Show") else real(path))

    lib.scan()

    groups = lib.groups()
    assert set(groups) == {lib.path("Show (2010)"), lib.path("Show")}
    moved, fresh = groups[lib.path("Show (2010)")], groups[lib.path("Show")]
    assert moved.id == old.id and (moved.monitored, moved.profile_id) == (0, profile)
    assert (moved.title, moved.year) == ("Show", "2010")
    assert fresh.id != old.id and (fresh.monitored, fresh.profile_id) == (1, None)
    # The old group is looked up again once, from its new name; the new one once, from its own.
    assert sorted(identified) == sorted([lib.path("Show (2010)"), lib.path("Show")])
    items = lib.items()
    for path, item_id in old_ids.items():
        new_path = path.replace("/Show/", "/Show (2010)/")
        assert items[new_path].id == item_id and items[new_path].group_id == old.id
    remake = [row for path, row in items.items() if path.startswith(lib.path("Show") + os.sep)]
    assert len(remake) == 2 and not ({row.id for row in remake} & set(old_ids.values()))
    assert {row.group_id for row in remake} == {fresh.id}
    _no_stale_keys(lib)


def test_a_user_identity_survives_a_rename_and_is_not_looked_up_again(lib, walk, identified):
    lib.show("Show", count=2)
    lib.scan()
    lib.set_group("Show", identity_source="user", title="My Title", tvdb_id=4242)
    identified.clear()

    os.rename(lib.root / "Show", lib.root / "Renamed Show")
    lib.scan()

    group = lib.group("Renamed Show")
    assert (group.title, group.tvdb_id, group.identity_source) == ("My Title", 4242, "user")
    assert identified == []


# 2. A rename

def test_a_renamed_show_directory_keeps_its_group_id_and_settings_with_no_empty_group_left(lib, walk, shared,
                                                                                         identified):
    profile = add_profile(lib.session, [("en", "False", "False")])
    lib.show("Show", count=3)
    lib.scan()
    old = lib.group("Show")
    lib.set_group("Show", monitored=0, profile_id=profile, tvdb_id=77, identity_source="tmdb", identity_state="identified")
    ids = {path: row.id for path, row in lib.items().items()}
    identified.clear()

    os.rename(lib.root / "Show", lib.root / "Show.new")
    if not shared:
        # The files take the new name too: a rename of the directory and of every file in one go.
        for entry in sorted((lib.root / "Show.new").iterdir()):
            os.rename(entry, lib.root / "Show.new" / entry.name.replace("Show - ", "Show.new - "))
    lib.scan()

    groups = lib.groups()
    assert list(groups) == [lib.path("Show.new")]
    group = groups[lib.path("Show.new")]
    assert (group.id, group.monitored, group.profile_id) == (old.id, 0, profile)
    # Identity is looked up again from the new name (it was not the user's).
    assert (group.identity_source, group.tvdb_id) == ("filename", None)
    assert identified == [lib.path("Show.new")]
    renamed = {path.replace("/Show/", "/Show.new/") if shared else
               path.replace("/Show/", "/Show.new/").replace("Show - ", "Show.new - "): item_id
               for path, item_id in ids.items()}
    assert {path: row.id for path, row in lib.items().items()} == renamed
    _no_stale_keys(lib)


# 3. The swap

def test_swapping_two_show_directories_moves_each_group_with_its_items(lib, walk, shared):
    lib.show("Show", count=2, prefix="Same" if shared else None)
    lib.show("Show.new", count=2, prefix="Same" if shared else None)
    lib.scan()
    a, b = lib.group("Show"), lib.group("Show.new")
    ids = {path: row.id for path, row in lib.items().items()}

    os.rename(lib.root / "Show", lib.root / "Show.old")
    os.rename(lib.root / "Show.new", lib.root / "Show")
    lib.scan()

    groups = lib.groups()
    assert {key: row.id for key, row in groups.items()} == {lib.path("Show.old"): a.id, lib.path("Show"): b.id}
    items = lib.items()
    for path, item_id in ids.items():
        swapped = (path.replace("/Show/", "/Show.old/") if "/Show/" in path
                   else path.replace("/Show.new/", "/Show/"))
        assert items[swapped].id == item_id
    assert {row.group_id for path, row in items.items() if "/Show.old/" in path} == {a.id}
    assert {row.group_id for path, row in items.items() if "/Show/" in path} == {b.id}
    _no_stale_keys(lib)


# 4. The replace

def test_replacing_a_show_directory_deletes_the_displaced_rows_and_moves_the_new_group(lib, walk, shared):
    lib.show("Show", count=3, prefix="Ep")
    lib.show("Show.new", count=3, prefix="Ep" if shared else "New")
    lib.scan()
    old, new = lib.group("Show"), lib.group("Show.new")
    old_ids = {row.id for path, row in lib.items().items() if "/Show/" in path}
    new_ids = {path: row.id for path, row in lib.items().items() if "/Show.new/" in path}

    shutil.rmtree(lib.root / "Show")
    os.rename(lib.root / "Show.new", lib.root / "Show")
    lib.scan()

    groups = lib.groups()
    assert list(groups) == [lib.path("Show")] and groups[lib.path("Show")].id == new.id
    items = lib.items()
    assert {row.id for row in items.values()} == set(new_ids.values()) and not (old_ids & {row.id for row in items.values()})
    assert old.id not in {row.id for row in groups.values()}
    _no_stale_keys(lib)


# 5. The per-season merge

def test_merging_two_season_folders_into_one_show_moves_exactly_one_group_and_notes_it(lib, walk, caplog):
    lib.show("Show S01", count=3, prefix="Show")
    lib.show("Show S02", count=2, prefix="Show2")
    lib.scan()
    first, second = lib.group("Show S01"), lib.group("Show S02")
    ids = {path: row.id for path, row in lib.items().items()}
    (lib.root / "Show").mkdir()
    for folder in ("Show S01", "Show S02"):
        for entry in sorted((lib.root / folder).iterdir()):
            os.rename(entry, lib.root / "Show" / entry.name)
        (lib.root / folder).rmdir()

    with caplog.at_level(logging.INFO):
        lib.scan()

    groups = lib.groups()
    assert list(groups) == [lib.path("Show")]
    # The group with the most held items in the directory wins, then the lowest id: S01 holds three of five.
    winner = groups[lib.path("Show")]
    assert winner.id == first.id
    assert "absorbed Show" in winner.resolution_note
    assert "their identity, profile and monitored settings were not carried" in winner.resolution_note
    assert any(f"group {first.id}" in line and f"group {second.id}" in line for line in caplog.messages)
    assert {row.group_id for row in lib.items().values()} == {winner.id}
    assert {row.id for row in lib.items().values()} == set(ids.values())
    _no_stale_keys(lib)


def test_the_tie_rule_picks_the_lowest_group_id(lib, walk):
    lib.show("Show S01", count=2, prefix="One")
    lib.show("Show S02", count=2, prefix="Two")
    lib.scan()
    first = lib.group("Show S01")
    (lib.root / "Show").mkdir()
    for folder in ("Show S01", "Show S02"):
        for entry in sorted((lib.root / folder).iterdir()):
            os.rename(entry, lib.root / "Show" / entry.name)
        (lib.root / folder).rmdir()

    lib.scan()

    assert lib.group("Show").id == first.id


def test_a_staying_group_that_wins_a_directory_still_writes_the_note_and_the_log_line(lib, walk, caplog):
    lib.show("Show", count=2, prefix="A")
    lib.show("Other", count=2, prefix="B")
    lib.scan()
    stayer, other = lib.group("Show"), lib.group("Other")
    # Every episode of Other goes into Show's directory: Other is absorbed, Show stays.
    for entry in sorted((lib.root / "Other").iterdir()):
        os.rename(entry, lib.root / "Show" / entry.name)
    (lib.root / "Other").rmdir()

    with caplog.at_level(logging.INFO):
        lib.scan()

    groups = lib.groups()
    assert list(groups) == [lib.path("Show")] and groups[lib.path("Show")].id == stayer.id
    assert "absorbed Other" in groups[lib.path("Show")].resolution_note
    assert any(f"group {other.id}" in line for line in caplog.messages)
    assert {row.group_id for row in lib.items().values()} == {stayer.id}


# 6. Misfiled episodes

def test_misfiled_episodes_join_the_new_show_and_take_nothing_of_the_old_group(lib, walk, shared, identified):
    profile = add_profile(lib.session, [("en", "False", "False")])
    lib.show("The Office (UK)", count=4, prefix="The Office")
    lib.scan()
    uk = lib.group("The Office (UK)")
    lib.set_group("The Office (UK)", monitored=0, profile_id=profile, tvdb_id=111, tmdb_id="222",
                  identity_source="user", identity_state="identified", poster="http://poster/uk.jpg")
    ids = {path: row.id for path, row in lib.items().items()}
    moving = [lib.root / "The Office (UK)" / f"The Office - S01E0{number}.mkv" for number in (3, 4)]
    (lib.root / "The Office (US)").mkdir()
    renamed = {}
    for path in moving:
        name = path.name if shared else path.name.replace("The Office", "The Office (US)")
        renamed[path] = name
        os.rename(path, lib.root / "The Office (US)" / name)
    identified.clear()

    lib.scan()

    groups = lib.groups()
    assert set(groups) == {lib.path("The Office (UK)"), lib.path("The Office (US)")}
    stays, new = groups[lib.path("The Office (UK)")], groups[lib.path("The Office (US)")]
    assert stays.id == uk.id and (stays.tvdb_id, stays.monitored, stays.profile_id) == (111, 0, profile)
    assert new.id != uk.id
    assert (new.tvdb_id, new.tmdb_id, new.poster, new.profile_id, new.monitored) == (None, None, None, None, 1)
    assert (new.identity_source, new.title) == ("filename", "The Office")
    assert identified == [lib.path("The Office (US)")]
    items = lib.items()
    moved = [items[lib.path("The Office (US)", renamed[path])] for path in moving]
    assert {row.group_id for row in moved} == {new.id}
    assert {row.id for row in moved} == {ids[str(path)] for path in moving}
    assert {row.group_id for path, row in items.items() if "(UK)" in path} == {uk.id}


# 7. Every episode moved to a new directory

@pytest.mark.parametrize("keep_old", [True, False], ids=["old-kept", "old-removed"])
def test_moving_every_episode_into_a_new_directory_moves_the_group(lib, walk, shared, keep_old, identified):
    lib.show("Show", count=3)
    lib.scan()
    old = lib.group("Show")
    lib.set_group("Show", monitored=0)
    ids = {row.id for row in lib.items().values()}
    (lib.root / "Elsewhere").mkdir()
    for entry in sorted((lib.root / "Show").iterdir()):
        os.rename(entry, lib.root / "Elsewhere" / (entry.name if shared else entry.name.replace("Show", "Moved")))
    if not keep_old:
        (lib.root / "Show").rmdir()
    identified.clear()

    lib.scan()

    groups = lib.groups()
    assert list(groups) == [lib.path("Elsewhere")]
    assert groups[lib.path("Elsewhere")].id == old.id and groups[lib.path("Elsewhere")].monitored == 0
    assert {row.id for row in lib.items().values()} == ids
    assert identified == [lib.path("Elsewhere")]


# 8. A better release under the same name

@pytest.mark.parametrize("count", [1, 3])
def test_a_better_release_saved_under_the_same_name_is_a_path_only_match_in_a_group_that_stays(lib, walk, count):
    lib.show("Show", count=count)
    lib.scan()
    group = lib.group("Show")
    target = lib.path("Show", "Show - S01E01.mkv")
    item = lib.items()[target]
    lib.session.execute(sa.update(lib_items()).where(lib_items().c.id == item.id).values(ffprobe_cache=b"cache"))
    stamped_video(lib.root / "Show" / "Show - S01E01.mkv", size=5000)

    result = lib.scan()

    assert lib.group("Show").id == group.id
    kept = lib.items()[target]
    assert kept.id == item.id and kept.file_size == 5000
    assert (result.updated, result.inserted, result.deleted) == (1, 0, 0)
    assert lib.session.execute(sa.select(lib_items().c.ffprobe_cache).where(lib_items().c.id == item.id)).scalar_one() is None


def lib_items():
    from media_sources import repository
    return repository.Items


# 9. The in-place replacement by another show

@pytest.mark.parametrize("user_identity", [False, True], ids=["identified-again", "user-kept"])
def test_replacing_every_file_in_place_keeps_the_group_and_identifies_it_again(lib, walk, shared, user_identity,
                                                                              identified):
    lib.show("Show", count=3)
    lib.scan()
    group = lib.group("Show")
    if user_identity:
        lib.set_group("Show", identity_source="user", title="Mine")
    # SQLite hands a deleted id to the next row, so kept rows are told from new ones by what the user set.
    lib.session.execute(sa.update(lib_items()).where(lib_items().c.library_id == lib.library_id).values(monitored=0))
    if not shared:
        for entry in (lib.root / "Show").iterdir():
            entry.unlink()
    for number in range(1, 4):
        name = f"Show - S01E0{number}.mkv" if shared else f"Another - S01E0{number}.mkv"
        stamped_video(lib.root / "Show" / name, size=7000 + number)
    identified.clear()

    lib.scan()

    assert lib.group("Show").id == group.id
    items = lib.items()
    assert len(items) == 3 and {row.group_id for row in items.values()} == {group.id}
    # With shared names the rows are path-only matches and are kept; with others the old rows are deleted.
    assert {row.monitored for row in items.values()} == {0 if shared else 1}
    # The group has no held item and an unchanged directory, so it stays; new content looks it up again, unless
    # the identity is the user's.
    assert identified == ([] if user_identity else [lib.path("Show")])
    assert (lib.group("Show").title == "Mine") is user_identity


def test_a_directory_removed_and_recreated_at_the_same_path_with_the_old_inode_is_the_same_group(lib, walk, shared,
                                                                                               monkeypatch,
                                                                                               identified):
    from media_sources import groups as group_rules

    lib.show("Show", count=2)
    lib.scan()
    group = lib.group("Show")
    shutil.rmtree(lib.root / "Show")
    lib.show("Show", count=2, prefix="Show" if shared else "Other")
    real = group_rules.stat_directory
    monkeypatch.setattr(group_rules, "stat_directory", lambda path: group_rules.DirState(
        "dir", group.dir_dev, group.dir_ino) if path == lib.path("Show") else real(path))
    identified.clear()

    lib.scan()

    assert lib.group("Show").id == group.id and identified == [lib.path("Show")]
    assert len(lib.items()) == 2


# 10. Inode reuse after a delete

def test_a_show_deleted_and_another_created_on_its_inode_does_not_inherit_the_group(lib, walk, shared, monkeypatch):
    from media_sources import groups as group_rules

    lib.show("Show", count=2)
    lib.scan()
    old = lib.group("Show")
    lib.set_group("Show", monitored=0, tvdb_id=5)
    shutil.rmtree(lib.root / "Show")
    lib.show("Different", count=2, prefix="Show" if shared else None)
    real = group_rules.stat_directory
    monkeypatch.setattr(group_rules, "stat_directory", lambda path: group_rules.DirState(
        "dir", old.dir_dev, old.dir_ino) if path == lib.path("Different") else real(path))

    lib.scan()

    groups = lib.groups()
    assert list(groups) == [lib.path("Different")]
    new = groups[lib.path("Different")]
    assert new.id != old.id and (new.monitored, new.tvdb_id) == (1, None)


# 11. A move to another series library, in both scan orders

@pytest.mark.parametrize("destination_first", [True, False], ids=["destination-first", "old-first"])
def test_a_show_moved_to_another_library_follows_its_items_or_is_added_new(lib, walk, shared, tmp_path,
                                                                           destination_first):
    from media_sources import repository

    kids = Lib(lib.session, tmp_path, name="Kids", folder="Kids")
    lib.show("Bluey", count=2)
    lib.show("Other Show", count=1)
    lib.scan()
    old = lib.group("Bluey")
    lib.set_group("Bluey", monitored=0, tvdb_id=9)
    old_items = {row.id: row for row in lib.items().values() if "/Bluey/" in row.path}
    for row in old_items.values():
        repository.insert_history(lib.session, item_id=row.id, source_id=lib.source_id, item_kind="episode",
                                  item_title=row.title, group_title="Bluey", source_name="TV", action=1,
                                  description="downloaded")
    shutil.move(str(lib.root / "Bluey"), str(kids.root / "Bluey"))
    if shared:
        # A different show is made at the old place, with the same file names.
        lib.show("Bluey", count=2)

    order = [kids, lib] if destination_first else [lib, kids]
    for library in order:
        library.scan()

    new_group = kids.group("Bluey")
    # The moved directory is the same physical directory, so a handover carries the group's own user settings
    # (not its looked-up ids); with the old library first nothing is carried.
    assert (new_group.monitored, new_group.tvdb_id) == ((0 if destination_first else 1), None)
    assert new_group.id != old.id
    remade = {lib.path("Other Show")} | ({lib.path("Bluey")} if shared else set())
    assert set(lib.groups()) == remade
    if shared:
        # The remake is a fresh show: none of the old group's settings, and no old row moved into it.
        assert (lib.group("Bluey").monitored, lib.group("Bluey").tvdb_id) == (1, None)
        assert lib.group("Bluey").id != old.id
        assert not ({row.id for row in lib.items().values() if "/Bluey/" in row.path} & set(old_items))
    kids_items = kids.items()
    assert len(kids_items) == 2 and {row.group_id for row in kids_items.values()} == {new_group.id}
    history = lib.session.execute(sa.select(repository.History.c.item_id, repository.History.c.item_title)).all()
    if destination_first:
        # The rows were handed over: same ids, and the history still points at them.
        assert {row.id for row in kids_items.values()} == set(old_items)
        assert {row.item_id for row in history} == set(old_items)
        assert {row.library_id for row in kids_items.values()} == {kids.library_id}
    else:
        # The old library saw the files gone first: its rows were deleted, history kept with no item link.
        assert not ({row.id for row in kids_items.values()} & set(old_items))
        assert {row.item_id for row in history} == {None} and len(history) == 2
        assert {row.item_title for row in history} == {row.title for row in old_items.values()}


# 12. A new device number

def _fake_device(lib, device="77"):
    """The share came back with another device number: what the last scan recorded no longer matches."""
    from media_sources import repository
    lib.session.execute(sa.update(repository.Roots).where(repository.Roots.c.library_id == lib.library_id)
                        .values(last_dev=device))
    lib.session.execute(sa.update(repository.Items).where(repository.Items.c.library_id == lib.library_id)
                        .values(file_dev=device))
    lib.session.execute(sa.update(repository.Groups).where(repository.Groups.c.library_id == lib.library_id)
                        .values(dir_dev=device))


def test_a_new_device_number_keeps_every_group_key_setting_and_row(lib, walk, identified):
    from media_sources import repository

    lib.show("Show", count=2)
    lib.show("Other", count=2)
    lib.scan()
    before_groups = {key: row.id for key, row in lib.groups().items()}
    lib.set_group("Show", monitored=0)
    before_items = {path: row.id for path, row in lib.items().items()}
    _fake_device(lib)
    identified.clear()

    result = lib.scan()

    assert result.status == "ok" and (result.inserted, result.deleted, result.moved) == (0, 0, 0)
    assert {key: row.id for key, row in lib.groups().items()} == before_groups
    assert lib.group("Show").monitored == 0
    assert {path: row.id for path, row in lib.items().items()} == before_items
    assert identified == []
    # The device, for the files, the directories and the root, is recorded again.
    real_dev = str(os.stat(lib.root).st_dev)
    assert repository.get_roots(lib.library_id)[0].last_dev == real_dev
    assert {row.file_dev for row in lib.items().values()} == {real_dev}
    assert {row.dir_dev for row in lib.groups().values()} == {real_dev}


# 13. A new device number and first episodes in an empty show directory

def test_new_episodes_in_an_empty_show_directory_join_its_group_after_a_remount(lib, walk, identified):
    lib.show("Show", count=1)
    lib.show("Other", count=1)
    lib.scan()
    os.remove(lib.path("Show", "Show - S01E01.mkv"))
    lib.scan()
    empty = lib.group("Show")
    assert empty is not None and not [row for row in lib.items().values() if row.group_id == empty.id]
    lib.set_group("Show", monitored=0)
    _fake_device(lib)
    lib.show("Show", count=2, prefix="New")
    identified.clear()

    lib.scan()

    group = lib.group("Show")
    assert group.id == empty.id and group.monitored == 0
    assert identified == [lib.path("Show")]
    assert {row.group_id for path, row in lib.items().items() if "/Show/" in path} == {empty.id}


# Empty groups

def test_an_empty_show_directory_keeps_its_group_and_a_gone_one_is_deleted(lib, walk):
    lib.show("Keeps", count=1)
    lib.show("Gone", count=1)
    lib.show("Other", count=1)
    lib.scan()
    os.remove(lib.path("Keeps", "Keeps - S01E01.mkv"))
    shutil.rmtree(lib.root / "Gone")

    lib.scan()

    groups = lib.groups()
    assert set(groups) == {lib.path("Keeps"), lib.path("Other")}
    assert len(lib.items()) == 1


def test_a_directory_at_the_same_path_with_a_new_inode_gets_a_fresh_group(lib, walk):
    lib.show("Show", count=1)
    lib.show("Other", count=1)
    lib.scan()
    old = lib.group("Show")
    lib.set_group("Show", monitored=0)
    shutil.rmtree(lib.root / "Show")
    lib.show("Show", count=1, prefix="Different")
    lib.session.execute(sa.update(lib_groups()).where(lib_groups().c.id == old.id).values(dir_ino="1"))

    lib.scan()

    group = lib.group("Show")
    assert group.id != old.id and group.monitored == 1
    assert old.id not in {row.id for row in lib.groups().values()}


def lib_groups():
    from media_sources import repository
    return repository.Groups


# The root guard keeps every key

def test_an_unmounted_root_an_empty_root_and_an_unreadable_show_write_no_key_at_all(lib, walk, monkeypatch):
    lib.show("Show", count=2)
    lib.show("Other", count=2)
    lib.show("Third", count=2)
    lib.scan()
    snapshot_groups = {row.id: tuple(row) for row in lib.groups().values()}
    snapshot_item_rows = list(lib.items().values())
    snapshot_items = {row.id: tuple(row) for row in snapshot_item_rows}

    # An unmounted root.
    moved = lib.root.with_name("TV.away")
    os.rename(lib.root, moved)
    assert lib.scan().status == "root_missing"
    os.rename(moved, lib.root)
    # An empty root: the share is there and holds nothing.
    hidden = lib.root.with_name("TV.hidden")
    os.rename(lib.root, hidden)
    lib.root.mkdir()
    assert lib.scan().status == "root_missing"
    lib.root.rmdir()
    os.rename(hidden, lib.root)

    assert {row.id: tuple(row) for row in lib.groups().values()} == snapshot_groups
    assert {row.id: tuple(row) for row in lib.items().values()} == snapshot_items

    # A walk that could not read a show directory, with a mover and a gone group waiting on it.
    os.rename(lib.root / "Show", lib.root / "Show.renamed")
    shutil.rmtree(lib.root / "Other")
    real_scandir = os.scandir

    def scandir(path):
        if str(path).endswith("Show.renamed"):
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr("media_sources.kinds.folder.os.scandir", scandir)
    result = lib.scan()

    assert result.status == "partial"
    assert {row.id: tuple(row) for row in lib.groups().values()} == snapshot_groups
    # The files that could be read are refreshed; no row is deleted, re-pathed or moved to another group.
    assert {row.id: (row.path, row.group_id) for row in lib.items().values()} == {
        row.id: (row.path, row.group_id) for row in snapshot_item_rows}
    _no_stale_keys(lib)


def test_a_show_directory_that_cannot_be_statted_is_left_untouched_neither_found_nor_gone(lib, walk, monkeypatch):
    from media_sources import groups as group_rules

    lib.show("Show", count=1)
    lib.show("Other", count=1)
    lib.scan()
    group = lib.group("Show")
    os.remove(lib.path("Show", "Show - S01E01.mkv"))
    real = group_rules.stat_directory
    monkeypatch.setattr(group_rules, "stat_directory", lambda path: group_rules.DirState("unreadable", None, None)
                        if path == lib.path("Show") else real(path))

    lib.scan()

    assert lib.group("Show").id == group.id


def test_a_gone_group_is_deleted_only_when_the_root_guard_held(lib, walk):
    lib.show("Show", count=1)
    lib.show("Other", count=1)
    lib.scan()
    shutil.rmtree(lib.root / "Show")
    # A walk that could not read one directory: the guard trips, so nothing is decided or deleted.
    stamped_video(lib.root / "Other" / "Locked" / "x.mkv")
    os.chmod(lib.root / "Other" / "Locked", 0)
    try:
        if os.access(lib.root / "Other" / "Locked", os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        result = lib.scan()
        assert result.status == "partial"
        assert lib.path("Show") in lib.groups() and len(lib.items()) == 2
    finally:
        os.chmod(lib.root / "Other" / "Locked", 0o755)

    lib.scan()
    assert lib.path("Show") not in lib.groups()


# Renaming many episodes

def test_renaming_200_episode_files_identifies_the_show_zero_times(lib, identified):
    for number in range(200):
        stamped_video(lib.root / "Show" / f"Show - S01E{number + 1:03d}.mkv")
    lib.scan()
    ids = {row.id for row in lib.items().values()}
    identified.clear()
    for path in list(lib.items()):
        os.rename(path, path.replace("Show - S01", "Show.renamed.S01"))

    result = lib.scan()

    assert (result.moved, result.inserted, result.deleted) == (200, 0, 0)
    assert identified == []
    assert {row.id for row in lib.items().values()} == ids
    assert all("Show.renamed" in path for path in lib.items())


def test_file_symlinks_are_followed_and_directory_symlinks_are_not_in_a_series_library(lib, tmp_path):
    real = stamped_video(tmp_path / "elsewhere" / "Real - S01E01.mkv")
    other = stamped_video(tmp_path / "elsewhere" / "Other" / "Other - S01E01.mkv")
    (lib.root / "Show").mkdir()
    (lib.root / "Show" / "Linked - S01E02.mkv").symlink_to(real)
    # The same file twice in one walk is one item.
    (lib.root / "Show" / "Linked again - S01E02.mkv").symlink_to(real)
    # A directory symlink, inside a show and as a show, is never followed.
    (lib.root / "Show" / "Folder Link").symlink_to(other.parent, target_is_directory=True)
    (lib.root / "Linked Show").symlink_to(other.parent, target_is_directory=True)

    lib.scan()

    assert set(lib.groups()) == {lib.path("Show")}
    assert len(lib.items()) == 1
