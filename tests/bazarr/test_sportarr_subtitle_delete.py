# coding=utf-8
"""Removing a subtitle Bazarr placed on a sports event.

Episodes and movies have had a delete route since forever. Sports had none at
all, so a bad sports subtitle could not be removed through Bazarr: the only way
out was deleting the file by hand and waiting for a re-index to notice.
"""


def test_the_route_exists():
    import inspect

    from api.sports import subtitles

    source = inspect.getsource(subtitles)
    assert '"/sports/events/<int:event_id>/subtitles"' in source
    assert "def delete(self, event_id):" in source


def test_delete_subtitles_takes_a_sports_event():
    import inspect

    from subtitles.tools import delete

    assert "sports_event_id" in inspect.signature(delete.delete_subtitles).parameters


def test_paths_resolve_through_the_owning_instance_mapping():
    """A second Sportarr can mount its media somewhere else entirely."""
    import inspect

    from subtitles.tools import delete

    source = inspect.getsource(delete.delete_subtitles)
    sports = source[source.index("if media_type == 'sports':"):source.index("elif media_type == 'series':")]
    assert 'path_replace_instance(p, arr_instance_id, "sports")' in sports
    assert 'path_replace_reverse_instance(p, arr_instance_id, "sports")' in sports


def test_the_event_is_reindexed_and_the_deletion_recorded():
    import inspect

    from subtitles.tools import delete

    source = inspect.getsource(delete.delete_subtitles)
    assert "store_subtitles_sports(sports_event_id, arr_instance_id)" in source
    assert "sports_history_log(0, sports_event_id, arr_instance_id, result)" in source


def test_a_failed_removal_records_no_history():
    """Logging a deletion that did not happen would put a lie in the history."""
    import inspect

    from subtitles.tools import delete

    source = inspect.getsource(delete.delete_subtitles)
    start = source.index("removed = _delete_subtitle_file(media_path, pr(subtitles_path))")
    sports = source[start : source.index("sports_history_log(") + 40]
    removed_at = sports.index("if not removed:")
    history_at = sports.index("sports_history_log(")
    assert removed_at < history_at


def test_no_media_server_refresh_for_sports():
    import inspect

    from subtitles.tools import delete

    source = inspect.getsource(delete.delete_subtitles)
    start = source.index("if media_type == 'sports':")
    sports = source[start : source.index("if media_type == 'series':", start)]
    assert "plex_refresh_item" not in sports
    assert "jellyfin_refresh_item" not in sports


def test_the_history_writer_uses_the_result_attributes_not_its_kwargs():
    """ProcessSubtitlesResult renames its constructor arguments: reversed_path
    lands on .path and reversed_subtitles_path on .subs_path. Reading the
    constructor names raised AttributeError after the file was already gone."""
    import inspect

    from sportarr import history

    source = inspect.getsource(history.sports_history_log)
    assert "result.path" in source
    assert "result.subs_path" in source
    assert "result.language_code" in source
    assert "reversed_path" not in source.replace("reversed paths", "")
