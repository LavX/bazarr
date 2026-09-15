# coding=utf-8
"""Re-syncing one league's events from its detail page.

The league detail page has had a Sync button since it was built, posting to
/sports/leagues/<id>/sync. Nothing served that path: only the whole-instance
/sports/leagues/sync existed, so the button 405'd and the user got neither a
sync nor an error worth reading.
"""


def test_the_route_exists_and_is_scoped_to_one_league():
    import inspect

    from api.sports import leagues

    source = inspect.getsource(leagues)
    assert "'/sports/leagues/<int:league_id>/sync'" in source
    # The whole-instance route stays: it is what the library page's Sync uses.
    assert "'/sports/leagues/sync'" in source


def test_it_refuses_a_league_the_owner_does_not_have():
    import inspect

    from api.sports import leagues

    source = inspect.getsource(leagues.SportsLeagueEventSync.post)
    assert "library.get_league(database, league_id, owner) is None" in source
    assert "'Sports league not found for this owner'" in source


def test_the_queued_function_accepts_the_injected_job_id():
    """The jobs queue injects job_id into every job's kwargs. sync_events does
    not take one, so queueing it directly raised TypeError the moment the job
    ran: the request returned 202 and the sync never happened."""
    import inspect

    from sportarr.sync.events import sync_one_league

    assert 'job_id' in inspect.signature(sync_one_league).parameters
    assert 'job_id' not in inspect.signature(
        __import__('sportarr.sync.events', fromlist=['sync_events']).sync_events
    ).parameters


def test_the_route_queues_the_wrapper_not_the_bare_sync():
    import inspect

    from api.sports import leagues

    source = inspect.getsource(leagues.SportsLeagueEventSync.post)
    assert "_queue('sync_one_league'" in source


def test_the_job_table_names_a_module_per_function():
    """Two ternaries chose the job name and module. A third job made them wrong
    rather than merely long: it would have been labelled a profile refresh and
    dispatched to the wrong module."""
    from api.sports.leagues import _JOBS

    assert _JOBS['sync_one_league'] == ('Sync league events', 'sportarr.sync.events')
    assert _JOBS['update_sports_for_instance'][1] == 'sportarr.sync.leagues'
