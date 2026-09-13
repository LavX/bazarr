# coding=utf-8
"""Removing a subtitle Bazarr placed on a sports event.

Episodes and movies have had a delete route since forever. Sports had none at
all, so a bad sports subtitle could not be removed through Bazarr: the only way
out was deleting the file by hand and waiting for a re-index to notice.
"""

import pytest
import sqlalchemy as sa

from test_sportarr_kind_migration import migration_engine  # noqa: F401
from test_sportarr_indexer import indexed_library  # noqa: F401


@pytest.mark.parametrize('failure', ['reindex', 'remove', 'guard'])
def test_sports_deletion_finalizes_only_after_file_removal(indexed_library, monkeypatch, failure):  # noqa: F811
    from flask import Flask
    from api.sports import events
    from app.config import settings
    from app.database import TableHistorySports
    from media_servers import events as publication
    from sportarr import history, notify
    from subtitles.indexer import sports
    from subtitles.tools import delete

    session, folder = indexed_library
    monkeypatch.setattr(events, 'database', session)
    monkeypatch.setattr(history, 'database', session)
    monkeypatch.setattr(settings.general, 'use_plex', False)
    monkeypatch.setattr(settings.general, 'use_jellyfin', False)
    emissions, rescans, notifications, webhooks = [], [], [], []
    monkeypatch.setattr(delete, 'event_stream', lambda **kw: emissions.append(kw))
    monkeypatch.setattr(notify, 'notify_rescan', rescans.append)
    monkeypatch.setattr(publication, 'notify_subtitle_mutation', notifications.append)
    monkeypatch.setattr(delete, 'call_external_webhook', lambda **kw: webhooks.append(kw))

    def broken_index(event_id, owner):
        assert (event_id, owner) == (61, 1)
        raise OSError('fixture reindex unavailable')

    monkeypatch.setattr(sports, 'store_subtitles_sports', broken_index)
    source = folder / '1/event.en.hi.srt'
    sibling = folder / '2/event.de.forced.srt'
    sibling_before = sibling.read_bytes()
    if failure == 'remove':
        source.unlink()
        monkeypatch.setattr(sports, 'store_subtitles_sports', lambda *a: None)
    elif failure == 'guard':
        def stopped_removal(*args, **kwargs):
            raise ValueError('fixture stopped before removal')
        monkeypatch.setattr(delete, '_delete_subtitle_file', stopped_removal)

    with Flask(__name__).test_request_context(
        '/sports/events/61/subtitles', method='DELETE',
        json={'arr_instance_id': 1, 'language': 'en', 'hi': True, 'path': '/sports/event.en.hi.srt'},
    ):
        body, status = events.SportsEventSubtitles.delete.__wrapped__(events.SportsEventSubtitles(), 61)

    assert status == {'reindex': 204, 'remove': 409, 'guard': 400}[failure], body
    assert source.exists() is (failure == 'guard')
    assert sibling.read_bytes() == sibling_before
    rows = session.execute(sa.select(TableHistorySports)).scalars().all()
    if failure == 'reindex':
        assert len(rows) == 1
        assert (rows[0].event_id, rows[0].arr_instance_id, rows[0].action) == (61, 1, 0)
        assert rows[0].subtitles_path == '/sports/event.en.hi.srt'
        assert rescans == [1]
        assert emissions == [{'type': 'sports', 'action': 'update', 'payload': 61}]
        assert len(notifications) == len(webhooks) == 1
    else:
        assert rows == rescans == emissions == notifications == webhooks == []


def test_the_route_exists():
    """On api.sports.events, which owns that path.

    Delete started out in api.sports.subtitles. Adding upload moved it onto the
    resource that already owned the path, because registering a second
    flask_restx Resource there silently shadows the first, and this assertion
    was left pointing at the module delete had just left.
    """
    import inspect

    from api.sports import events

    source = inspect.getsource(events)
    assert "'/sports/events/<int:event_id>/subtitles'" in source
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
    start = source.index("removed = _delete_subtitle_file(media_path, pr(subtitles_path)")
    sports = source[start : source.index("sports_history_log(") + 40]
    removed_at = sports.index("if not removed:")
    history_at = sports.index("sports_history_log(")
    assert removed_at < history_at


def test_no_item_level_media_server_refresh_for_sports():
    """The item-level Plex/Jellyfin refresh helpers stay out of the sports
    branch: a sports event has no identifiers for them to resolve. The sports
    branch refreshes the configured sports libraries and requests the
    Sportarr whole-library rescan instead."""
    import inspect

    from subtitles.tools import delete

    source = inspect.getsource(delete.delete_subtitles)
    start = source.rindex("if media_type == 'sports':")
    sports = source[start : source.index("if media_type == 'series':", start)]
    assert "plex_refresh_item" not in sports
    assert "jellyfin_refresh_item" not in sports
    assert "plex_update_sports_library()" in sports
    assert "jellyfin_update_sports_library()" in sports


def test_sports_delete_requests_one_rescan_and_publishes_to_the_dispatcher():
    """Deleting a sports subtitle asks Sportarr for one whole-library rescan,
    and reaches the native Emby and Silo refresh through the same publication
    callback the series and movie branches use."""
    import inspect

    from subtitles.tools import delete

    source = inspect.getsource(delete.delete_subtitles)
    start = source.rindex("if media_type == 'sports':")
    sports = source[start : source.index("if media_type == 'series':", start)]
    assert "notify_rescan(arr_instance_id)" in sports
    assert "publication_callback(media_type, media_path, 'delete', arr_instance_id)" in sports


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


def test_a_path_outside_the_event_directory_is_refused():
    """The only check on a caller-supplied path was its file extension, so an
    authenticated DELETE naming any subtitle Bazarr can reach, including one
    belonging to a different media item, had it removed. The route now applies
    the same containment guard the shared toolbox endpoint does."""
    import inspect

    from api.sports import events

    source = inspect.getsource(events.SportsEventSubtitles.delete)
    guard_at = source.index("subtitle_path_within_area(")
    delete_at = source.index("delete_subtitles(")
    assert guard_at < delete_at
    assert "'Subtitle path is outside the media library.'" in source
    # The mapped path is what gets removed, so the mapped path is what has to
    # be contained; checking the stored remote path would prove nothing.
    mapped_at = source.index("path_replace_instance(")
    assert mapped_at < guard_at


def test_the_guard_is_the_shared_one():
    """A second implementation would drift from the toolbox endpoint's."""
    import inspect

    from api.sports import events
    from utilities.security_guards import subtitle_path_within_area

    assert "subtitle_path_within_area" in inspect.getsource(events)
    assert subtitle_path_within_area("/media/x/a.srt", "/media/x/a.mkv") is True
    assert subtitle_path_within_area("/etc/passwd.srt", "/media/x/a.mkv") is False
