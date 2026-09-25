# coding=utf-8
"""Rebuilding a combined subtitle for a sports event on demand.

The combine engine has understood sports since it was written: it takes a
sports_operation and publishes through the owned-file guard. But it only ever
ran as a side effect of a download or a translation. Episodes, movies and whole
series each have a manual trigger; sports had none, so once the source files
changed on disk the combined output could not be rebuilt through Bazarr at all.
"""


def test_the_route_exists_and_is_scoped_to_an_owner():
    import inspect

    from api.sports import subtitles

    source = inspect.getsource(subtitles.SportsEventSubtitlesCombine)
    assert '_owner(body.get("arr_instance_id")' in source
    route = inspect.getsource(subtitles)
    assert '"/sports/events/<int:event_id>/subtitles/combine"' in route


def test_it_composes_through_the_captured_profile_operation():
    """try_combine_for_video refuses a sports composition without one, and
    refuses an ad-hoc override alongside one. Passing the video path with no
    operation would raise inside the engine and come back a bare 500."""
    import inspect

    from api.sports import subtitles

    source = inspect.getsource(subtitles.SportsEventSubtitlesCombine)
    assert "capture_profile_operation(context, candidate_signature(context))" in source
    assert "sports_operation=operation" in source
    assert "languages" not in source.split("try_combine_for_video(")[1].split(")")[0]


def test_an_event_without_a_profile_is_skipped_not_failed():
    """No profile means no combine rule, which is a configuration state rather
    than an error; reporting it as failed would put a red toast in front of a
    user who simply has not assigned a profile to the league."""
    import inspect

    from api.sports import subtitles

    source = inspect.getsource(subtitles.SportsEventSubtitlesCombine)
    skipped = source[source.index("if not operation.profile:"):]
    assert '"status": "skipped"' in skipped.split("200")[0]


def test_the_engine_rejects_an_override_for_sports():
    """Pinned because the route relies on it: a sports composition must follow
    the profile it captured, not whatever the request body asked for."""
    import inspect

    from subtitles.tools.combine.main import try_combine_for_video

    source = inspect.getsource(try_combine_for_video)
    sports = source[source.index("if media_type == 'sports':"):source.index("else:")]
    assert "if languages is not None or format is not None:" in sports
    assert "sports_operation is None" in sports


def test_a_published_combine_with_a_failed_follow_up_is_not_reported_clean():
    """`built` with an `error` is a partial success, not a success.

    When the composition is published but finalize() fails, most often because
    the index refresh cannot probe the recording, the engine returns
    status='built' carrying the error. Every consumer counted that as a clean
    build, so the operator got a green summary for a subtitle the event may not
    list until it is reindexed. The league combine runs as a queued job now, and
    its tally keeps that distinction.
    """
    from subtitles.tools.combine.batch import CombineTally

    tally = CombineTally('events')
    tally.add('Race 1', {'status': 'built', 'path': '/x.srt', 'reason': '', 'error': 'index refresh failed'})
    tally.add('Race 2', {'status': 'built', 'path': '/y.srt', 'reason': '', 'error': ''})

    assert tally.summary()['built'] == 2
    assert tally.summary()['warnings'] == 1
    assert tally.failed == 0
    assert '1 needing attention' in tally.counts()


def test_the_league_route_queues_one_combine_job():
    import inspect

    from api.sports import subtitles

    source = inspect.getsource(subtitles.SportsLeagueSubtitlesCombine)
    assert 'func="combine_league_subtitles"' in source
    assert 'try_combine_for_video' not in source


def test_both_combine_routes_refuse_while_use_sportarr_is_off(monkeypatch):
    """Turning the switch off leaves the instance rows enabled, so Combine on
    the Sports page still built and published a composition. With the switch
    on, the same request goes on to resolve the event or league."""
    from flask import Flask

    from api.sports import subtitles as api_mod
    from app.config import settings
    from app.jobs_queue import jobs_queue
    from sportarr import identity
    from sportarr.errors import SportsNotFound

    resolved, queued = [], []

    def not_found(*args, **kwargs):
        resolved.append(args)
        raise SportsNotFound('Sports event not found for this owner')

    monkeypatch.setattr(identity, 'resolve_event_in_session', not_found)
    monkeypatch.setattr(api_mod, '_league_owner', not_found)
    monkeypatch.setattr(jobs_queue, 'feed_jobs_pending_queue', lambda **kwargs: queued.append(kwargs))

    def post(resource, item_id):
        with Flask(__name__).test_request_context(
                f'/sports/{item_id}/subtitles/combine', method='POST', json={'arr_instance_id': 1}):
            return resource.post.__wrapped__(resource(), item_id)

    routes = [(api_mod.SportsEventSubtitlesCombine, 61), (api_mod.SportsLeagueSubtitlesCombine, 51)]

    monkeypatch.setattr(settings.general, 'use_sportarr', False)
    off = ({'message': 'Sportarr is turned off. Turn on Use Sportarr in Settings first.'}, 400)
    for resource, item_id in routes:
        assert post(resource, item_id) == off
    assert resolved == [] and queued == []

    monkeypatch.setattr(settings.general, 'use_sportarr', True)
    for resource, item_id in routes:
        assert post(resource, item_id)[1] == 404
    assert len(resolved) == 2 and queued == []
