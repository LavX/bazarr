# coding=utf-8
"""The manual subtitle toolbox for sports.

/api/subtitles declared its type as episode-or-movie and its else branch
treated anything non-episode as a movie, so a sports request came back
404 "Movie not found". That put the whole user-initiated toolbox, subsync with
its reference and engine choice, AI translate, and every subzero mod, out of
reach for sports while the automatic versions worked fine.
"""


def test_the_endpoint_declares_the_third_media_type():
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles)
    assert '["episode", "movie", "sports"]' in source


def test_a_sports_event_resolves_its_own_row_not_a_movie():
    """The else branch used to swallow sports and answer 'Movie not found'."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles.patch)
    assert 'if media_type == "sports":' in source
    assert '"Sports event not found", 404' in source
    # Sports takes the leading branch, which is what pushed episode to elif.
    # Without that, sports fell through to the movie else and 404'd.
    assert 'elif media_type == "episode":' in source


def test_the_video_path_uses_the_sports_instance_mapping():
    """Everything downstream resolves against the owning instance, and a
    global mapping points at another instance's library when the two differ."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles.patch)
    assert "path_replace_instance(\n                metadata.path, arr_instance_id, 'sports')" in source


def test_sync_runs_under_the_owned_publication_boundary():
    """Without it an operation that began before a resync replaced the
    recording could publish over the new file."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles.patch)
    assert "sports_manual_operation(id, arr_instance_id)" in source
    assert "context=sports_context" in source
    assert "validate=sports_validate" in source
    assert "publication_guard=sports_guard" in source


def test_the_other_two_media_types_keep_their_behaviour():
    """Episodes and movies must pass None and go down exactly the path they
    always did."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles.patch)
    assert "nullcontext((None, None, None, None))" in source


def test_the_helper_pins_the_file_signature():
    import inspect

    from sportarr import subtitles as sports_subtitles

    source = inspect.getsource(sports_subtitles.sports_manual_operation)
    assert "candidate_signature(context)" in source
    assert '"Sports file changed. Please try again."' in source


def test_postprocessing_reindexes_the_event_and_pushes_a_refresh():
    """The toolbox result is invisible until the event is re-indexed."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.postprocess_subtitles)
    assert "store_subtitles_sports(id, arr_instance_id)" in source
    assert 'event_stream(type="sports", payload=id)' in source


def test_postprocessing_makes_no_media_server_call_for_sports():
    """Both refresh helpers key on imdbId, which a sports event has not got."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.postprocess_subtitles)
    sports_branch = source[source.index('if media_type == "sports":'):source.index('if media_type == "episode":')]
    assert "plex_refresh_item" not in sports_branch
    assert "jellyfin_refresh_item" not in sports_branch
