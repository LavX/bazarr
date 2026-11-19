"""Same-file identity and arr shadowing: what makes two paths one file, which arr rows can shadow a source item,
what is recorded, and how a shadow is kept true between scans."""
import os
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from media_source_helpers import add_source, stamped_video


@pytest.fixture(autouse=True)
def enabled_languages(languages_loaded, monkeypatch):
    from media_sources import indexer
    monkeypatch.setattr(indexer, "get_language_set", lambda: set())


@pytest.fixture
def toggles(monkeypatch):
    from app.config import settings
    for key in ("use_sonarr", "use_radarr", "use_sportarr"):
        monkeypatch.setattr(settings.general, key, True)
    return settings.general


def add_instance(session, kind="radarr", instance_id=1, name="Radarr", enabled=1, mapping="[]"):
    from app.database import TableArrInstances
    session.execute(sa.insert(TableArrInstances).values(
        id=instance_id, kind=kind, name=name, stable_key=f"{kind}-{instance_id}", port=7000 + instance_id,
        enabled=enabled, is_default=0, path_mappings=mapping))
    return instance_id


def add_movie(session, path, instance_id=1, row_id=1, size=None, file_id=100):
    from app.database import TableMovies
    session.execute(sa.insert(TableMovies).values(
        id=row_id, arr_instance_id=instance_id, radarrId=row_id + 1000, title="Arr Movie", path=path,
        tmdbId=str(row_id), missing_subtitles="[]", subtitles="[]", monitored="True", audio_language="[]",
        tags="[]", alternativeTitles="[]", year="2001", sortTitle="arr movie", file_size=size,
        movie_file_id=file_id))
    return row_id


@pytest.fixture
def movies(source_db, tmp_path):
    source_id, library_id, root = add_source(source_db, tmp_path)
    return SimpleNamespace(session=source_db, source_id=source_id, library_id=library_id, root=root)


def scan(movies):
    from media_sources.scanner import run_scan
    return run_scan(movies.source_id)[0]


def item(movies, path):
    from media_sources import repository
    return repository.get_item_by_path(str(path), movies.session)


class StatSpy:
    """Counts the files statted to decide whether two paths are one file, and can answer for paths that do not
    exist here (a bind mount, a second mount of a network share)."""

    def __init__(self, monkeypatch, fake=None):
        from media_sources import identity
        self.paths = []
        self.fake = fake or {}
        real = identity.stat_file

        def stat(path):
            self.paths.append(path)
            return self.fake[path] if path in self.fake else real(path)
        monkeypatch.setattr(identity, "stat_file", stat)


# The rungs of same_file

def test_same_file_by_path_symlink_hardlink_bind_mount_and_two_mounts(tmp_path, monkeypatch):
    from media_sources import identity

    original = stamped_video(tmp_path / "a" / "Film.mkv")
    link = tmp_path / "b" / "Link.mkv"
    link.parent.mkdir()
    os.link(original, link)
    symlink = tmp_path / "c" / "Sym.mkv"
    symlink.parent.mkdir()
    symlink.symlink_to(original)
    other = stamped_video(tmp_path / "d" / "Other.mkv")

    assert identity.same_file(str(original), str(original)) == "path"
    assert identity.same_file(str(original), str(symlink)) == "path"      # equal real paths
    assert identity.same_file(str(original), str(link)) == "inode"        # a hardlink shares the inode
    assert identity.same_file(str(original), str(other)) is None
    assert identity.same_file(str(original), str(tmp_path / "missing.mkv")) is None

    # A bind mount: another path, the very same device and inode.
    real = identity.stat_file(str(original))
    StatSpy(monkeypatch, {"/bind/Film.mkv": real})
    assert identity.same_file(str(original), "/bind/Film.mkv") == "inode"

    # Two mounts of one network share: another device, the same name, size and mtime.
    mirror = identity.FileStat("999", "5", real.size, real.mtime_ns)
    StatSpy(monkeypatch, {"/share/Film.mkv": mirror})
    assert identity.same_file(str(original), "/share/Film.mkv") is None
    assert identity.same_file(str(original), "/share/Film.mkv", attributes=True) == "attributes"
    # A different name, size or mtime is a different file even then.
    for wrong in (identity.FileStat("999", "5", real.size + 1, real.mtime_ns),
                  identity.FileStat("999", "5", real.size, real.mtime_ns + 1)):
        StatSpy(monkeypatch, {"/share/Film.mkv": wrong})
        assert identity.same_file(str(original), "/share/Film.mkv", attributes=True) is None
    StatSpy(monkeypatch, {"/share/Renamed.mkv": mirror})
    assert identity.same_file(str(original), "/share/Renamed.mkv", attributes=True) is None
    # On one device, an equal name, size and mtime is not enough: the inode decides.
    same_device = identity.FileStat(real.dev, "5", real.size, real.mtime_ns)
    StatSpy(monkeypatch, {"/share/Film.mkv": same_device})
    assert identity.same_file(str(original), "/share/Film.mkv", attributes=True) is None


# Shadowing at a scan

@pytest.mark.parametrize("arr_size", [None, 1], ids=["null-size", "stale-size"])
def test_an_exact_path_match_shadows_with_a_null_or_stale_arr_size(movies, toggles, arr_size):
    film = stamped_video(movies.root / "Film.2001.mkv")
    add_instance(movies.session, mapping=f'[["/arr/movies", "{movies.root}"]]')
    add_movie(movies.session, "/arr/movies/Film.2001.mkv", size=arr_size, file_id=77)

    scan(movies)

    shadowed = item(movies, film)
    assert (shadowed.arr_shadow, shadowed.shadow_rung, shadowed.shadow_instance_id) == (1, "path", 1)
    assert (shadowed.shadow_owner, shadowed.shadow_arr_table, shadowed.shadow_arr_row_id) == ("Radarr", "movie", 1)
    assert (shadowed.shadow_arr_path, shadowed.shadow_arr_file_id) == (str(film), 77)


def test_a_second_mount_and_a_bind_mount_shadow_by_the_stat_rungs_on_same_size_candidates_only(
        movies, toggles, monkeypatch):
    from media_sources import identity

    film = stamped_video(movies.root / "Film.2001.mkv", size=4000)
    other = stamped_video(movies.root / "Other.2002.mkv", size=4100)
    real, real_other = identity.stat_file(str(film)), identity.stat_file(str(other))
    add_instance(movies.session, mapping='[["/arr/movies", "/second/movies"]]')
    add_movie(movies.session, "/arr/movies/Film.2001.mkv", row_id=1, size=4000)
    add_movie(movies.session, "/arr/movies/Other.2002.mkv", row_id=2, size=4100)
    # Many arr rows of other sizes: none of them may be statted.
    for number in range(3, 23):
        add_movie(movies.session, f"/arr/movies/Unrelated {number}.mkv", row_id=number, size=10 + number)
    # A row with no size and the same name is a candidate too; one with no size and another name is not.
    add_movie(movies.session, "/arr/movies/Film.2001.mkv.copy", row_id=30, size=None)
    spy = StatSpy(monkeypatch, {
        "/second/movies/Film.2001.mkv": real,                                                       # a bind mount
        "/second/movies/Other.2002.mkv": identity.FileStat("999", "5", real_other.size, real_other.mtime_ns),
    })

    scan(movies)

    bound, mirrored = item(movies, film), item(movies, other)
    assert (bound.arr_shadow, bound.shadow_rung, bound.shadow_arr_row_id) == (1, "inode", 1)
    assert (mirrored.arr_shadow, mirrored.shadow_rung, mirrored.shadow_arr_row_id) == (1, "attributes", 2)
    statted_arr_paths = {path for path in spy.paths if path.startswith("/second/")}
    assert statted_arr_paths == {"/second/movies/Film.2001.mkv", "/second/movies/Other.2002.mkv"}


def test_a_disabled_instance_or_a_master_toggle_that_is_off_never_shadows(movies, toggles, monkeypatch):
    film = stamped_video(movies.root / "Film.2001.mkv")
    add_instance(movies.session, mapping=f'[["/arr/movies", "{movies.root}"]]', enabled=0)
    add_movie(movies.session, "/arr/movies/Film.2001.mkv")
    scan(movies)
    assert item(movies, film).arr_shadow == 0

    movies.session.execute(sa.text("UPDATE arr_instances SET enabled = 1"))
    monkeypatch.setattr(toggles, "use_radarr", False)
    stamped = stamped_video(movies.root / "Film.2001.mkv", size=2222)
    scan(movies)
    assert item(movies, stamped).arr_shadow == 0

    monkeypatch.setattr(toggles, "use_radarr", True)
    scan(movies)
    assert item(movies, stamped).arr_shadow == 1


def test_a_source_file_the_arr_does_not_hold_is_not_shadowed(movies, toggles):
    film = stamped_video(movies.root / "Film.2001.mkv")
    add_instance(movies.session, mapping=f'[["/arr/movies", "{movies.root}"]]')
    add_movie(movies.session, "/arr/movies/Another.2002.mkv", size=film.stat().st_size + 5)
    scan(movies)
    assert item(movies, film).arr_shadow == 0


# Switching an owner clears its shadows without a scan

def _shadowed_by_bind_mount(movies, monkeypatch, toggles, size=4000):
    from media_sources import identity

    film = stamped_video(movies.root / "Film.2001.mkv", size=size)
    real = identity.stat_file(str(film))
    add_instance(movies.session, mapping='[["/arr/movies", "/second/movies"]]')
    add_movie(movies.session, "/arr/movies/Film.2001.mkv", size=size, file_id=5)
    spy = StatSpy(monkeypatch, {"/second/movies/Film.2001.mkv": real})
    scan(movies)
    assert item(movies, film).arr_shadow == 1
    return film, real, spy


def test_disabling_the_arr_instance_clears_the_shadow_without_a_scan(movies, toggles, monkeypatch):
    from media_sources import shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    movies.session.execute(sa.text("UPDATE arr_instances SET enabled = 0"))

    counts = shadows.run_refresh("radarr")

    cleared = item(movies, film)
    assert (cleared.arr_shadow, cleared.shadow_instance_id, cleared.shadow_rung, cleared.shadow_arr_row_id) == (
        0, None, None, None)
    assert counts["cleared"] == 1


def test_turning_the_master_toggle_off_clears_and_turning_it_on_shadows_again(movies, toggles, monkeypatch):
    from media_sources import shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    monkeypatch.setattr(toggles, "use_radarr", False)
    shadows.run_refresh("radarr")
    assert item(movies, film).arr_shadow == 0

    monkeypatch.setattr(toggles, "use_radarr", True)
    counts = shadows.run_refresh("radarr")
    assert counts["shadowed"] == 1 and item(movies, film).arr_shadow == 1
    # The refresh read the item's stored identity: it never statted the source file itself.
    assert str(film) not in spy.paths


def test_a_flag_whose_instance_is_gone_is_stale_even_with_no_instance_id(movies, toggles, monkeypatch):
    from media_sources import shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    movies.session.execute(sa.text("DELETE FROM arr_instances"))
    assert item(movies, film).shadow_instance_id is None and item(movies, film).arr_shadow == 1

    shadows.run_refresh()

    assert item(movies, film).arr_shadow == 0


def test_the_refresh_is_queued_when_an_arr_instance_is_deleted_whatever_its_kind(movies, toggles, monkeypatch):
    from arr_instances import service
    from media_sources import shadows

    queued = []
    monkeypatch.setattr(shadows, "refresh_shadows", lambda kind=None, job_id=None: queued.append(kind) or 5)
    # The hook's own work after the queue call is not under test, and must not rewrite the settings.
    monkeypatch.setattr(service, "switch_off_kind_without_instances", lambda session, kind: False)
    stamped_video(movies.root / "Film.2001.mkv")
    scan(movies)

    for kind in ("sportarr", "sonarr", "radarr"):
        service.after_instance_deleted(movies.session, kind)
    assert queued == ["sportarr", "sonarr", "radarr"]


def test_nothing_is_queued_on_an_install_with_no_source_item(source_db, monkeypatch):
    from media_sources import shadows

    queued = []
    monkeypatch.setattr(shadows, "refresh_shadows", lambda kind=None, job_id=None: queued.append(kind))
    assert shadows.queue_shadow_refresh("radarr") is None and queued == []


def test_a_failure_queueing_the_refresh_is_logged_and_never_raised(movies, monkeypatch, caplog):
    from arr_instances import service
    from media_sources import shadows

    monkeypatch.setattr(service, "switch_off_kind_without_instances", lambda session, kind: False)
    stamped_video(movies.root / "Film.2001.mkv")
    scan(movies)

    def broken(kind=None, job_id=None):
        raise RuntimeError("queue is down")

    monkeypatch.setattr(shadows, "refresh_shadows", broken)
    service.after_instance_deleted(movies.session, "sportarr")
    assert "could not queue a media source shadow refresh" in caplog.text


def test_the_refresh_job_queues_itself_and_runs(movies, toggles, monkeypatch):
    from media_sources import shadows

    queued = []
    monkeypatch.setattr(shadows.jobs_queue, "add_job_from_function",
                        lambda name, is_progress: queued.append(name) or 12)
    assert shadows.refresh_shadows("radarr") == 12 and queued == ["Refreshing media source shadows (radarr)"]
    ran = []
    monkeypatch.setattr(shadows, "run_refresh", lambda kind=None: ran.append(kind) or {})
    shadows.refresh_shadows("radarr", job_id=3)
    assert ran == ["radarr"]


# Re-validation against the row that set the shadow

def test_a_bind_mount_and_a_second_mount_shadow_survive_while_their_row_holds_the_same_file(movies, toggles,
                                                                                             monkeypatch):
    from media_sources import identity, shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    mirror_file = stamped_video(movies.root / "Mirror.2003.mkv", size=4500)
    mirror = identity.stat_file(str(mirror_file))
    add_movie(movies.session, "/arr/movies/Mirror.2003.mkv", row_id=2, size=4500, file_id=6)
    spy.fake["/second/movies/Mirror.2003.mkv"] = identity.FileStat("999", "5", mirror.size, mirror.mtime_ns)
    scan(movies)
    assert item(movies, mirror_file).shadow_rung == "attributes"
    spy.paths.clear()

    counts = shadows.revalidate_shadows()

    assert counts == {"kept": 2, "cleared": 0, "refreshed": 0}
    assert spy.paths == []  # kept with no stat


def test_a_shadow_is_cleared_when_its_row_is_gone_or_its_owner_no_longer_counts(movies, toggles, monkeypatch):
    from media_sources import shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    movies.session.execute(sa.text("DELETE FROM table_movies"))
    assert shadows.revalidate_shadows() == {"kept": 0, "cleared": 1, "refreshed": 0}
    assert item(movies, film).arr_shadow == 0


def test_a_shadow_is_cleared_when_the_owner_is_disabled_or_its_toggle_is_off(movies, toggles, monkeypatch):
    from media_sources import shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    monkeypatch.setattr(toggles, "use_radarr", False)
    assert shadows.revalidate_shadows()["cleared"] == 1
    assert item(movies, film).arr_shadow == 0


def test_an_upgrade_that_replaces_the_file_clears_a_hardlinked_copys_shadow(movies, toggles, monkeypatch):
    from media_sources import shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    # The arr replaced the file: its row now holds another file id, at the same path, of another size.
    movies.session.execute(sa.text("UPDATE table_movies SET movie_file_id = 6, file_size = 9999"))
    spy.paths.clear()

    counts = shadows.revalidate_shadows()

    assert counts == {"kept": 0, "cleared": 1, "refreshed": 0}
    # The row changed, so the item's file was matched again: the arr file is no longer the same one.
    assert item(movies, film).arr_shadow == 0 and "/second/movies/Film.2001.mkv" not in spy.paths


def test_a_reused_row_id_does_not_keep_a_stale_shadow(movies, toggles, monkeypatch):
    from media_sources import shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    movies.session.execute(sa.text("DELETE FROM table_movies"))
    # SQLite hands the deleted id to a new row, which holds another file.
    add_movie(movies.session, "/arr/movies/Different.2005.mkv", row_id=1, size=1234, file_id=999)

    assert shadows.revalidate_shadows() == {"kept": 0, "cleared": 1, "refreshed": 0}
    assert item(movies, film).arr_shadow == 0


def test_a_row_that_moved_the_same_file_is_matched_again_and_its_record_refreshed(movies, toggles, monkeypatch):
    from media_sources import shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    # The arr renamed the file under its own root: another mapped path, another file id, still the same inode.
    movies.session.execute(sa.text("UPDATE table_movies SET path = '/arr/movies/Renamed.mkv', movie_file_id = 8"))
    spy.fake["/second/movies/Renamed.mkv"] = real

    assert shadows.revalidate_shadows() == {"kept": 0, "cleared": 0, "refreshed": 1}

    refreshed = item(movies, film)
    assert (refreshed.arr_shadow, refreshed.shadow_arr_path, refreshed.shadow_arr_file_id, refreshed.shadow_rung) == (
        1, "/second/movies/Renamed.mkv", 8, "inode")


def test_a_changed_row_with_the_items_own_file_gone_keeps_the_shadow_and_queues_a_scan(movies, toggles, monkeypatch):
    from media_sources import shadows

    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    queued = []
    monkeypatch.setattr(shadows, "_queue_library_scan", lambda row: queued.append((row.source_id, row.library_id)))
    movies.session.execute(sa.text("UPDATE table_movies SET path = '/arr/movies/Renamed.mkv', movie_file_id = 8"))
    film.unlink()

    assert shadows.revalidate_shadows() == {"kept": 1, "cleared": 0, "refreshed": 0}

    assert item(movies, film).arr_shadow == 1
    assert queued == [(movies.source_id, movies.library_id)]


def test_the_scan_that_the_revalidation_queues_is_a_real_library_scan_job(movies, monkeypatch):
    from media_sources import scanner, shadows

    queued = []
    monkeypatch.setattr(scanner.jobs_queue, "add_job_from_function",
                        lambda name, is_progress: queued.append(name) or 9)
    shadows._queue_library_scan(SimpleNamespace(id=1, source_id=movies.source_id, library_id=movies.library_id))
    assert queued == ["Scanning media source Local"]


# Right before an automatic search

def test_the_path_stage_alone_runs_before_a_search_with_no_stat(movies, toggles, monkeypatch):
    from media_sources import shadows

    film = stamped_video(movies.root / "Film.2001.mkv")
    scan(movies)
    assert item(movies, film).arr_shadow == 0
    # The arr row appeared after the scan.
    add_instance(movies.session, mapping=f'[["/arr/movies", "{movies.root}"]]')
    add_movie(movies.session, "/arr/movies/Film.2001.mkv", size=None, file_id=3)
    spy = StatSpy(monkeypatch)

    assert shadows.path_stage_shadow(item(movies, film).id) is True

    assert spy.paths == []
    assert (item(movies, film).arr_shadow, item(movies, film).shadow_rung) == (1, "path")
    # An item no arr row holds is searched.
    other = stamped_video(movies.root / "Other.2002.mkv")
    scan(movies)
    assert shadows.path_stage_shadow(item(movies, other).id) is False


def test_the_path_stage_ignores_a_disabled_owner(movies, toggles):
    from media_sources import shadows

    film = stamped_video(movies.root / "Film.2001.mkv")
    scan(movies)
    add_instance(movies.session, mapping=f'[["/arr/movies", "{movies.root}"]]', enabled=0)
    add_movie(movies.session, "/arr/movies/Film.2001.mkv")
    assert shadows.path_stage_shadow(item(movies, film).id) is False


# What the scan does with shadows it already knows

def test_a_rescan_recomputes_the_shadows_in_full(movies, toggles, monkeypatch):
    film, real, spy = _shadowed_by_bind_mount(movies, monkeypatch, toggles)
    movies.session.execute(sa.text("DELETE FROM table_movies"))

    scan(movies)

    assert item(movies, film).arr_shadow == 0 and item(movies, film).shadow_instance_id is None


def test_a_sportarr_event_and_a_sonarr_episode_shadow_too(movies, toggles):
    from app.database import TableEpisodes, TableShows, TableSportsEvents, TableSportsLeagues

    episode = stamped_video(movies.root / "Ep.S01E01.mkv")
    event = stamped_video(movies.root / "Final.2024.mkv")
    add_instance(movies.session, kind="sonarr", instance_id=1, name="Sonarr", mapping=f'[["/tv", "{movies.root}"]]')
    add_instance(movies.session, kind="sportarr", instance_id=2, name="Sportarr",
                 mapping=f'[["/sports", "{movies.root}"]]')
    movies.session.execute(sa.insert(TableShows).values(
        id=1, arr_instance_id=1, sonarrSeriesId=11, title="S", path="/tv", profileId=None, monitored="True",
        seriesType="standard", tags="[]", alternativeTitles="[]", audio_language="[]", year="2020", sortTitle="s"))
    movies.session.execute(sa.insert(TableEpisodes).values(
        id=1, arr_instance_id=1, series_id=1, sonarrSeriesId=11, sonarrEpisodeId=111, season=1, episode=1,
        title="Pilot", path="/tv/Ep.S01E01.mkv", missing_subtitles="[]", subtitles="[]", monitored="True",
        audio_language="[]", episode_file_id=31))
    movies.session.execute(sa.insert(TableSportsLeagues).values(
        id=1, arr_instance_id=2, sportarrLeagueId=31, title="L", sortTitle="l", profileId=None, monitored="True",
        path="/sports"))
    movies.session.execute(sa.insert(TableSportsEvents).values(
        id=1, arr_instance_id=2, league_id=1, sportarrEventId=41, file_id=51, season=2024, episode=1,
        path="/sports/Final.2024.mkv", title="Final", missing_subtitles="[]", subtitles="[]", monitored="True",
        audio_language="[]"))

    scan(movies)

    shadowed_episode, shadowed_event = item(movies, episode), item(movies, event)
    assert (shadowed_episode.shadow_arr_table, shadowed_episode.shadow_owner, shadowed_episode.shadow_arr_file_id) == (
        "episode", "Sonarr", 31)
    assert (shadowed_event.shadow_arr_table, shadowed_event.shadow_owner, shadowed_event.shadow_arr_file_id) == (
        "sports_event", "Sportarr", 51)
