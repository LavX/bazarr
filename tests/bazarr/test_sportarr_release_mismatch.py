# coding=utf-8
"""Release-type mismatch detection for sports.

Series and movies report this from generate_subtitles, which the sports search
bypasses entirely, so no candidate sink was ever collected and the "subtitles
exist, but only for a different release" diagnosis, its notification and its
Wanted flag were all unavailable for sports.
"""


def test_the_sports_search_collects_the_candidates_it_scored():
    """Without the sink there is no evidence of what the providers hold, and
    reading it costs no extra provider request."""
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic.search_event)
    assert "candidate_sink = []" in source
    assert "candidate_sink=candidate_sink" in source


def test_it_reports_only_when_the_search_found_nothing():
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic.search_event)
    assert "if not selected and language is None and upgraded_from_id is None:" in source


def test_an_upgrade_never_reports_a_mismatch():
    """An upgrade rejects candidates by construction, so every one of them
    would look like a mismatch. The shared path guards the same way."""
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic.search_event)
    guard = source.index("if not selected and language is None")
    assert "upgraded_from_id is None" in source[guard : guard + 120]


def test_a_reporting_failure_never_costs_the_user_a_search():
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic.search_event)
    report_at = source.index("report_release_type_mismatch(")
    following = source[report_at:]
    assert "except Exception:" in following[: following.index("for subtitle in selected:")]


def test_sports_events_are_identified_by_path_not_an_upstream_id():
    """A sports event carries no arr-side id on the video object, so the path
    is its identity."""
    import inspect

    from subtitles import mismatch

    source = inspect.getsource(mismatch._resolve_media_id)
    assert "if media_type == 'sports':" in source
    assert "_resolve_media_id_by_path(media_type, video, arr_instance_id)" in source


def test_the_path_resolver_knows_the_sports_table():
    import inspect

    from subtitles import mismatch

    source = inspect.getsource(mismatch._resolve_media_id_by_path)
    assert "TableSportsEvents, 'sports'" in source


def test_the_mismatch_notification_uses_the_sports_notifier():
    import inspect

    from subtitles import mismatch

    source = inspect.getsource(mismatch.report_release_type_mismatch)
    assert "send_notifications_sports(media_id, body" in source


def test_the_wanted_endpoint_carries_the_flag():
    """Recorded and never shown is the same as not recorded, from the user's
    side of the screen."""
    import inspect

    from sportarr import workflows

    source = inspect.getsource(workflows.list_wanted)
    assert "flagged_media_ids(session, \"sports\"" in source
    assert 'row["release_mismatch"]' in source


def test_the_flag_is_computed_for_the_page_not_the_whole_library():
    """wanted_rows can be long; flagging every row would query ids the caller
    never returns."""
    import inspect

    from sportarr import workflows

    source = inspect.getsource(workflows.list_wanted)
    # The slice is taken first, whatever its exact shape: it now has to handle
    # a fetch-all page, where the limit is None and the slice runs to the end.
    page_at = source.index("page = rows[")
    flag_at = source.index("flagged_media_ids(")
    assert page_at < flag_at
    assert 'for row in page' in source
    assert 'rows[start:]' in source and 'rows[start : start + limit]' in source
