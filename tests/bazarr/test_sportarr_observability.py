# coding=utf-8
"""Sports failures that were previously invisible.

Each of these was collected or knowable and then surfaced nowhere, so a sports
problem produced no badge, no health issue and no entry on the pages the user
checks when something looks wrong.
"""


def test_health_refreshes_enabled_sports_roots_and_preserves_disabled_owner(schema_session, monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from app import database as app_database
    from app.database import TableArrInstances, TableSportsLeaguesRootfolder
    from sportarr import rootfolder
    from utilities import backup, health

    for owner in (1, 2, 3):
        schema_session.add(TableArrInstances(id=owner, kind='sportarr', name=str(owner),
                                             stable_key=str(owner), port=1867, enabled=int(owner != 2),
                                             path_mappings=json.dumps([['/sports', str(tmp_path / str(owner))]])))
    schema_session.flush()
    for owner in (1, 2, 3):
        schema_session.add(TableSportsLeaguesRootfolder(arr_instance_id=owner, rootfolder_id=7,
                                                       path='/sports', accessible=int(owner == 3), error='stale'))
    schema_session.commit()
    (tmp_path / '1').mkdir()
    for module in (health, rootfolder, app_database):
        monkeypatch.setattr(module, 'database', schema_session)
    monkeypatch.setattr(health.settings.general, 'use_sportarr', True)
    monkeypatch.setattr(health.settings.general, 'use_sonarr', False)
    monkeypatch.setattr(health.settings.general, 'use_radarr', False)
    monkeypatch.setattr(health, 'event_stream', lambda **kwargs: None)
    monkeypatch.setattr(rootfolder, 'notify', lambda *args: None)
    monkeypatch.setattr(backup, 'backup_rotation', lambda: None)
    monkeypatch.setattr(health.jobs_queue, 'update_job_name', lambda **kwargs: None)
    fetched = []

    def from_row(self, row):
        fetched.append(row.id)
        return SimpleNamespace(get=lambda path: SimpleNamespace(
            status_code=200, json=lambda: [{'id': 7, 'path': '/sports'}]))

    monkeypatch.setattr(rootfolder.ArrClientFactory, 'from_row', from_row)
    health.check_health(job_id=7)
    assert fetched == [1, 3]
    schema_session.expire_all()
    roots = {row.arr_instance_id: row for row in schema_session.query(TableSportsLeaguesRootfolder)}
    assert roots[1].accessible == 1 and roots[1].error == ''
    assert roots[2].accessible == 0 and roots[2].error == 'stale'
    assert roots[3].accessible == 0 and 'not accessible' in roots[3].error
    issues = health.get_health_issues()
    assert any(item['object'] == str(tmp_path / '3') for item in issues)
    assert not any(item['object'] in (str(tmp_path / '1'), str(tmp_path / '2')) for item in issues)
    fetched.clear()
    monkeypatch.setattr(health.settings.general, 'use_sportarr', False)
    health.check_health(job_id=7)
    assert fetched == []


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


def test_a_failed_sportarr_version_probe_is_cached_until_it_expires(monkeypatch):
    """The cache rejected "unknown", so while Sportarr was down every System
    Status load waited out the instance's HTTP timeout again. A hit was also
    written back, which would have kept a cached failure alive forever."""
    from types import SimpleNamespace

    from dogpile.cache import make_region

    from arr_instances import resolution
    from sportarr import info

    region = make_region().configure('dogpile.cache.memory')
    monkeypatch.setattr(info, 'region', region)
    monkeypatch.setattr(info.settings.general, 'use_sportarr', True)
    monkeypatch.setattr(info.ArrInstanceRepository, 'get_default',
                        lambda self, kind: SimpleNamespace(id=1))
    probes = []
    monkeypatch.setattr(resolution, 'client_for_instance', lambda *args, **kwargs: SimpleNamespace(
        test_connection=lambda: probes.append(True) or {'ok': False}))

    assert info.get_sportarr_info.version() == 'unknown'
    writes = []
    monkeypatch.setattr(region, 'set', lambda *args, **kwargs: writes.append(args))
    assert info.get_sportarr_info.version() == 'unknown'

    assert len(probes) == 1
    assert writes == []
