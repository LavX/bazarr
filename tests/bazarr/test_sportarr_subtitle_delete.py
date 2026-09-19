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
    """No media server is called from the sports branch at all: a sports event
    has no identifiers for an item-level refresh to resolve, and every
    destination now reaches its configured sports library through the deletion
    this branch already published. Calling one from here as well would refresh
    it twice, and only for the two kinds that used to be singletons."""
    import inspect

    from subtitles.tools import delete

    source = inspect.getsource(delete.delete_subtitles)
    start = source.rindex("if media_type == 'sports':")
    sports = source[start : source.index("if media_type == 'series':", start)]
    assert "plex" not in sports
    assert "jellyfin" not in sports


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


def test_the_recording_is_rechecked_under_the_locks_before_the_unlink(indexed_library, monkeypatch):  # noqa: F811
    """Ownership was checked, then the file was removed with nothing re-asked.

    A reconciliation that reassigns or replaces the recording in that window
    left the request unlinking a path another event had adopted, and then
    recording the deletion against this one. The operation's own signature
    check now runs inside the subtitle write locks, which is where the indexer
    takes its own, and before the removal rather than after it: a refusal has
    to mean nothing was deleted.
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
    for module in (events, history, sports_subtitles):
        monkeypatch.setattr(module, 'database', session)
    monkeypatch.setattr(settings.general, 'use_plex', False)
    monkeypatch.setattr(settings.general, 'use_jellyfin', False)
    monkeypatch.setattr(notify, 'notify_rescan', lambda *a: None)
    monkeypatch.setattr(publication, 'notify_subtitle_mutation', lambda *a: None)
    monkeypatch.setattr(delete, 'event_stream', lambda **kw: None)
    monkeypatch.setattr(delete, 'call_external_webhook', lambda **kw: None)
    monkeypatch.setattr(sports, 'store_subtitles_sports', lambda *a, **kw: None)

    held, order = [], []
    real_locks = delete.subtitle_write_locks
    real_operation = sports_subtitles.sports_manual_operation

    @contextmanager
    def watched_locks(*args, **kwargs):
        with real_locks(*args, **kwargs) as states:
            held.append(True)
            try:
                yield states
            finally:
                held.pop()

    @contextmanager
    def watched_operation(event_id, arr_instance_id, cancel=None):
        with real_operation(event_id, arr_instance_id, cancel) as operation:
            context, validate, guard, video = operation

            def watched_validate():
                # The locks the indexer also takes are held by now, so the
                # recording cannot be swapped between here and the unlink.
                assert held, 'the recording was re-checked outside the write locks'
                order.append('revalidated')
                return validate()

            yield context, watched_validate, guard, video

    monkeypatch.setattr(delete, 'subtitle_write_locks', watched_locks)
    monkeypatch.setattr(sports_subtitles, 'sports_manual_operation', watched_operation)

    subtitle = folder / '1/event.en.hi.srt'

    def request():
        with Flask(__name__).test_request_context(
            '/sports/events/61/subtitles', method='DELETE',
            json={'arr_instance_id': 1, 'language': 'en', 'hi': True,
                  'path': '/sports/event.en.hi.srt'},
        ):
            return events.SportsEventSubtitles.delete.__wrapped__(events.SportsEventSubtitles(), 61)

    body, status = request()
    assert status == 204, body
    assert order == ['revalidated']
    assert not subtitle.exists()


def test_a_recording_that_moved_mid_request_keeps_its_subtitle(indexed_library, monkeypatch):  # noqa: F811
    """A refusal raised before the unlink leaves the file and the history alone."""
    from contextlib import contextmanager

    from flask import Flask
    from api.sports import events
    from app.config import settings
    from app.database import TableHistorySports
    from sportarr import history
    from sportarr import subtitles as sports_subtitles

    session, folder = indexed_library
    _index(session, 61, [['en:hi', '/sports/event.en.hi.srt', 42]])
    for module in (events, history, sports_subtitles):
        monkeypatch.setattr(module, 'database', session)
    monkeypatch.setattr(settings.general, 'use_plex', False)
    monkeypatch.setattr(settings.general, 'use_jellyfin', False)

    real_operation = sports_subtitles.sports_manual_operation

    @contextmanager
    def refusing_operation(event_id, arr_instance_id, cancel=None):
        with real_operation(event_id, arr_instance_id, cancel) as operation:
            context, _validate, guard, video = operation

            def refuse():
                raise ValueError('Sports file changed. Please try again.')

            yield context, refuse, guard, video

    monkeypatch.setattr(sports_subtitles, 'sports_manual_operation', refusing_operation)

    with Flask(__name__).test_request_context(
        '/sports/events/61/subtitles', method='DELETE',
        json={'arr_instance_id': 1, 'language': 'en', 'hi': True,
              'path': '/sports/event.en.hi.srt'},
    ):
        body, status = events.SportsEventSubtitles.delete.__wrapped__(events.SportsEventSubtitles(), 61)

    assert status == 409, body
    assert (folder / '1/event.en.hi.srt').exists()
    assert session.execute(sa.select(TableHistorySports)).scalars().all() == []


def test_a_malformed_delete_is_still_a_bad_request(indexed_library, monkeypatch):  # noqa: F811
    """The conflict status covers the operation, not the request's shape.

    Answering "arr_instance_id is required" with 409 tells the caller the
    server is in a state it can retry out of, and the GET and POST on this same
    resource still answer 400 for the identical body.
    """
    from flask import Flask
    from api.sports import events

    session, _ = indexed_library
    monkeypatch.setattr(events, 'database', session)

    for payload in ({'language': 'en', 'path': '/sports/event.en.hi.srt'},
                    {'arr_instance_id': 0, 'language': 'en', 'path': '/x.srt'},
                    {'arr_instance_id': 1}):
        with Flask(__name__).test_request_context(
            '/sports/events/61/subtitles', method='DELETE', json=payload,
        ):
            body, status = events.SportsEventSubtitles.delete.__wrapped__(
                events.SportsEventSubtitles(), 61)
        assert status == 400, (payload, body)


def test_a_vanished_recording_is_a_conflict_not_a_crash(indexed_library, monkeypatch):  # noqa: F811
    """Pinning the event stats the recording, and Sportarr can have moved it.

    Before the operation was entered this route never touched the video, so an
    orphaned subtitle could still be removed. It has to answer as the index
    POST beside it does rather than raise out of the handler.
    """
    from flask import Flask
    from api.sports import events
    from sportarr import subtitles as sports_subtitles

    session, folder = indexed_library
    _index(session, 61, [['en:hi', '/sports/event.en.hi.srt', 42]])
    for module in (events, sports_subtitles):
        monkeypatch.setattr(module, 'database', session)
    (folder / '1/event.mkv').unlink()

    with Flask(__name__).test_request_context(
        '/sports/events/61/subtitles', method='DELETE',
        json={'arr_instance_id': 1, 'language': 'en', 'hi': True,
              'path': '/sports/event.en.hi.srt'},
    ):
        body, status = events.SportsEventSubtitles.delete.__wrapped__(events.SportsEventSubtitles(), 61)

    assert status == 409, body
    assert 'sports file' in body['message'].lower()
