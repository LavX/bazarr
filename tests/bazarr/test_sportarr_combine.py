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
