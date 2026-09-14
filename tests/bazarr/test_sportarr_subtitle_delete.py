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
    from sportarr import subtitles as sports_subtitles
    from subtitles.indexer import sports
    from subtitles.tools import delete

    session, folder = indexed_library
    # The route deletes what the event's own index lists, which is what the UI
    # offers; the fixture indexes nothing on its own.
    _index(session, 61, [['en:hi', '/sports/event.en.hi.srt', 42]])
    monkeypatch.setattr(events, 'database', session)
    monkeypatch.setattr(history, 'database', session)
    # The route publishes inside sports_manual_operation, which reads the
    # session off this module; left unpatched it keeps whichever session first
    # imported it, which is another test's database.
    monkeypatch.setattr(sports_subtitles, 'database', session)
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

    # A refusal raised while the deletion is under way says the event or its
    # recording moved underneath the request, which is a conflict rather than a
    # malformed one: the route answers 409 for all of them.
    assert status == {'reindex': 204, 'remove': 409, 'guard': 409}[failure], body
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
    assert "publication_callback(media_type, media_path, 'delete'," in sports
    assert "arr_instance_id)" in sports.split("publication_callback(media_type, media_path, 'delete',")[1]


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


def _index(session, event_id, entries):
    from app.database import TableSportsEvents

    session.execute(sa.update(TableSportsEvents)
                    .where(TableSportsEvents.id == event_id)
                    .values(subtitles=str(entries)))
    session.commit()


def test_a_subtitle_the_event_does_not_own_is_refused(indexed_library, monkeypatch):  # noqa: F811
    """Containment answers "inside the library", not "belongs to this event".

    Sports recordings share a directory routinely, and a custom subtitle folder
    is one absolute directory for all of them, so a sibling event's subtitle
    passed the containment check and was deleted with the history written
    against the event named in the URL.
    """
    from flask import Flask
    from api.sports import events
    from app.config import settings
    from app.database import TableHistorySports
    from sportarr import history
    from sportarr import subtitles as sports_subtitles

    session, folder = indexed_library
    _index(session, 61, [['en:hi', '/sports/event.en.hi.srt', 42]])
    monkeypatch.setattr(events, 'database', session)
    monkeypatch.setattr(history, 'database', session)
    # The route publishes inside sports_manual_operation, which reads the
    # session off this module; left unpatched it keeps whichever session first
    # imported it, which is another test's database.
    monkeypatch.setattr(sports_subtitles, 'database', session)
    monkeypatch.setattr(settings.general, 'use_plex', False)
    monkeypatch.setattr(settings.general, 'use_jellyfin', False)

    # Same directory as event 61's own recording, so containment cannot tell
    # them apart. Written here the way a second recording's subtitle would be.
    neighbour = folder / '1/neighbour.fr.srt'
    neighbour.write_text('1\n00:00:00,000 --> 00:00:01,000\nAnother event.\n')

    with Flask(__name__).test_request_context(
        '/sports/events/61/subtitles', method='DELETE',
        json={'arr_instance_id': 1, 'language': 'fr', 'path': '/sports/neighbour.fr.srt'},
    ):
        body, status = events.SportsEventSubtitles.delete.__wrapped__(events.SportsEventSubtitles(), 61)

    assert status == 403, body
    assert neighbour.exists()
    assert session.execute(sa.select(TableHistorySports)).scalars().all() == []


def test_the_unlink_happens_inside_the_owned_boundary(indexed_library, monkeypatch):  # noqa: F811
    """Ownership was checked, then the file was removed outside any boundary.

    A reconciliation that reassigns or replaces the recording in that window
    leaves the request unlinking a path another event has since adopted, and
    then recording the deletion against this one. The removal now runs inside
    the same publication guard the other sports mutations use, which locks the
    event row and revalidates the recording signature either side of it.
    """
    from contextlib import contextmanager

    from flask import Flask
    from api.sports import events
    from app.config import settings
    from media_servers import events as publication
    from sportarr import history, notify
    from sportarr import subtitles as sports_subtitles
    from subtitles.indexer import sports
    from subtitles.tools import delete

    session, folder = indexed_library
    _index(session, 61, [['en:hi', '/sports/event.en.hi.srt', 42]])
    for module, name in ((events, 'database'), (history, 'database'),
                         (sports_subtitles, 'database')):
        monkeypatch.setattr(module, name, session)
    monkeypatch.setattr(settings.general, 'use_plex', False)
    monkeypatch.setattr(settings.general, 'use_jellyfin', False)
    monkeypatch.setattr(notify, 'notify_rescan', lambda *a: None)
    monkeypatch.setattr(publication, 'notify_subtitle_mutation', lambda *a: None)
    monkeypatch.setattr(delete, 'event_stream', lambda **kw: None)
    monkeypatch.setattr(delete, 'call_external_webhook', lambda **kw: None)
    monkeypatch.setattr(sports, 'store_subtitles_sports', lambda *a, **kw: None)

    order = []
    real_operation = sports_subtitles.sports_manual_operation
    real_unlink = delete._delete_subtitle_file

    @contextmanager
    def watched_operation(event_id, arr_instance_id, cancel=None):
        with real_operation(event_id, arr_instance_id, cancel) as operation:
            context, validate, guard, video = operation

            @contextmanager
            def watched_guard(**kwargs):
                order.append('guard-entered')
                with guard(**kwargs) as inner:
                    yield inner
                order.append('guard-left')

            yield context, validate, watched_guard, video

    def watched_unlink(*args, **kwargs):
        order.append('unlinked')
        return real_unlink(*args, **kwargs)

    monkeypatch.setattr(sports_subtitles, 'sports_manual_operation', watched_operation)
    monkeypatch.setattr(delete, '_delete_subtitle_file', watched_unlink)

    with Flask(__name__).test_request_context(
        '/sports/events/61/subtitles', method='DELETE',
        json={'arr_instance_id': 1, 'language': 'en', 'hi': True, 'path': '/sports/event.en.hi.srt'},
    ):
        body, status = events.SportsEventSubtitles.delete.__wrapped__(events.SportsEventSubtitles(), 61)

    assert status == 204, body
    assert order == ['guard-entered', 'unlinked', 'guard-left']
    assert not (folder / '1/event.en.hi.srt').exists()
