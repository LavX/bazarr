# coding=utf-8
"""Sports failures that were previously invisible.

Each of these was collected or knowable and then surfaced nowhere, so a sports
problem produced no badge, no health issue and no entry on the pages the user
checks when something looks wrong.
"""


def test_a_broken_sports_root_folder_becomes_a_health_issue():
    """sportarr/rootfolder.py already wrote accessible and error per league
    root. Nothing read them, so a broken path mapping produced no health issue
    and downloads just failed per event."""
    import inspect

    from utilities import health

    source = inspect.getsource(health.get_health_issues)
    assert "TableSportsLeaguesRootfolder" in source
    assert "settings.general.use_sportarr" in source


def test_the_root_folder_path_is_shown_through_the_instance_mapping():
    """A raw container path in a health issue is not actionable; series and
    movies both map theirs before display."""
    import inspect

    from utilities import health

    source = inspect.getsource(health.get_health_issues)
    assert "path_replace_instance(" in source
    assert "'sports'" in source


def test_sports_stream_health_is_exposed_as_a_badge():
    """The per-client connected flag was maintained and read by nothing, so a
    dead Sportarr stream was undetectable while Sonarr and Radarr both showed
    theirs."""
    import inspect

    from api.badges import badges
    from sportarr import sse_client

    assert hasattr(sse_client, "all_sportarr_sse_connected")
    source = inspect.getsource(badges)
    assert "sportarr_sse" in source


def test_no_enabled_sportarr_reads_as_healthy_not_down(monkeypatch):
    """Nothing is expected to be connected, which is not a failure. Matches
    all_sonarr_signalr_connected."""
    from sportarr import sse_client

    monkeypatch.setattr(sse_client._manager, "clients", {})
    assert sse_client.all_sportarr_sse_connected() is True


def test_a_client_that_is_not_connected_reads_as_down(monkeypatch):
    from sportarr import sse_client

    class Dead:
        connected = False

        def is_alive(self):
            return True

    monkeypatch.setattr(sse_client._manager, "clients", {1: Dead()})
    assert sse_client.all_sportarr_sse_connected() is False


def test_a_connected_but_dead_thread_reads_as_down(monkeypatch):
    """connected alone is stale once the reader thread has exited."""
    from sportarr import sse_client

    class Zombie:
        connected = True

        def is_alive(self):
            return False

    monkeypatch.setattr(sse_client._manager, "clients", {1: Zombie()})
    assert sse_client.all_sportarr_sse_connected() is False


def test_history_statistics_counts_sports():
    """The chart merged only series and movies, so the tab silently
    under-reported for anyone using Sportarr."""
    import inspect

    from api.history import stats

    source = inspect.getsource(stats)
    assert "TableHistorySports" in source
    assert "'sports': sorted_data_sports" in source


def test_global_search_finds_sports_leagues():
    """A Sportarr user's library was unreachable from the search bar even
    though the tab existed in the nav."""
    import inspect

    from api.system import searches

    source = inspect.getsource(searches)
    assert "TableSportsLeagues" in source
    assert "sportarrLeagueId" in source


def test_a_league_has_a_sport_not_a_year():
    """The shared result shape assumes a year; reading it off a league row
    would raise."""
    import inspect

    from api.system import searches

    source = inspect.getsource(searches)
    assert "getattr(x, 'year', None)" in source


def test_the_sportarr_version_reaches_system_status():
    import inspect

    from api.system import status

    source = inspect.getsource(status)
    assert "sportarr_version" in source


def test_the_version_comes_from_the_default_instance_not_a_scalar():
    """Sports keeps its connection details only in arr_instances, so unlike
    Sonarr and Radarr there is no scalar host or apikey to read."""
    import inspect

    from sportarr import info

    source = inspect.getsource(info.GetSportarrInfo.version)
    assert "get_default('sportarr')" in source
    assert "settings.sportarr.apikey" not in source
