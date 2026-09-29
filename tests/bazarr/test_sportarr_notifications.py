# coding=utf-8
"""Notifications for sports subtitle events.

send_notifications / send_notifications_movie are called from eight places for
series and movies. Sports had no equivalent at all, so with Apprise, Discord or
Telegram configured every sports download, upgrade and translate happened
silently and the user had no reason to believe anything had run.
"""

import pytest

from test_sportarr_kind_migration import migration_engine  # noqa: F401
from test_sportarr_indexer import indexed_library, sports  # noqa: F401
from test_sportarr_manual import manual_library  # noqa: F401
from test_sportarr_events import library as library


def test_a_sports_notifier_exists_alongside_the_series_and_movie_ones():
    from app import notifier

    assert hasattr(notifier, "send_notifications_sports")


def test_the_sports_save_path_notifies_once_history_commits():
    """Recorded at the same point series and movies notify: the download is
    only real once its history row is committed."""
    import inspect

    from sportarr import subtitles

    source = inspect.getsource(subtitles.save_sports_subtitle)
    assert "send_notifications_sports(" in source
    committed_at = source.index('state[phase] = "committed"')
    notify_at = source.index("send_notifications_sports(")
    assert committed_at < notify_at


def test_a_notifier_fault_cannot_break_the_publication():
    """The sports save runs a state machine where a raised exception marks the
    history phase 'uncertain'. A broken webhook URL must not do that."""
    import inspect

    from sportarr import subtitles

    source = inspect.getsource(subtitles.save_sports_subtitle)
    notify_at = source.index("send_notifications_sports(")
    guard_at = source.rindex("try:", 0, notify_at)
    following = source[notify_at:]
    assert "except Exception:" in following[: following.index("phase = \"index\"")]
    assert guard_at < notify_at


def test_the_lookup_is_scoped_to_the_owning_instance(schema_session, monkeypatch):
    """Sports event ids are per-instance, so an unscoped lookup could expand
    another Sportarr's title into a custom-notifier URL."""
    import inspect

    from app import notifier

    source = inspect.getsource(notifier.send_notifications_sports)
    assert "TableSportsEvents.arr_instance_id, arr_instance_id" in source
    # Both the custom-notifier branch and the plain branch must scope.
    assert source.count("TableSportsEvents.arr_instance_id, arr_instance_id") == 2


def test_no_providers_means_no_work(monkeypatch):
    from app import notifier

    monkeypatch.setattr(notifier, "get_notifier_providers", lambda: [])
    # Returns without touching the database at all.
    assert notifier.send_notifications_sports(1, "message") is None


def test_the_plain_branch_avoids_selecting_the_ffprobe_blob():
    """The sports row carries a heavy ffprobe_cache blob; the notification body
    needs two columns. Series and movies avoid the same trap deliberately."""
    import inspect

    from app import notifier

    source = inspect.getsource(notifier.send_notifications_sports)
    assert "select(TableSportsEvents.title, TableSportsEvents.league_id)" in source


def test_manual_sports_downloads_honour_silent_manual_actions():
    """Silent for Manual Actions suppresses the sports manual download sender
    the same way it does for episodes and movies."""
    import inspect

    from sportarr import subtitles

    source = inspect.getsource(subtitles.save_sports_subtitle)
    notify_at = source.index("send_notifications_sports(")
    segment = source[source.rindex("if not", 0, notify_at):notify_at]
    assert "is_manual and settings.general.dont_notify_manual_actions" in segment


def test_silent_manual_actions_suppresses_the_manual_sports_notification(manual_library, monkeypatch):  # noqa: F811
    """Behavioral: with the setting on a manual sports download stays silent,
    and with it off the notification fires."""
    from app.config import settings
    from app import notifier

    service, session, folder = manual_library
    sent = []
    monkeypatch.setattr(notifier, "send_notifications_sports",
                        lambda event_id, message, arr_instance_id=None: sent.append(event_id))

    monkeypatch.setattr(settings.general, "dont_notify_manual_actions", True)
    result = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    service.manual_download_sports(61, result, 1)
    assert sent == []

    monkeypatch.setattr(settings.general, "dont_notify_manual_actions", False)
    result = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    service.manual_download_sports(61, result, 1)
    assert sent == [61]


def test_the_league_fully_subtitled_sender_exists():
    from app import notifier

    assert hasattr(notifier, "send_notifications_sports_league")


@pytest.mark.parametrize('is_signalr', [False, True])
@pytest.mark.parametrize('enabled', [False, True])
def test_the_sync_found_nothing_emission_is_gated_exactly_like_sonarr_and_radarr(library, monkeypatch, is_signalr, enabled):
    """The sync path emits the found-nothing notification only for a live
    sync, and only when the option is on, the same condition the Sonarr and
    Radarr sync paths use."""
    from app import notifier
    from app.config import settings
    from test_sportarr_events import event, remote

    _, events = library
    monkeypatch.setattr(settings.general, 'notify_if_nothing_is_missing_for_signalr_event', enabled)
    remote(monkeypatch, events, [event()])
    sent = []
    monkeypatch.setattr(notifier, 'send_notifications_sports_league',
                        lambda league, message, owner: sent.append((league, message, owner)))
    events.sync_events(51, 1, is_signalr=is_signalr)
    assert sent == ([(51, 'There are no missing subtitles in this league.', 1)] if enabled and is_signalr else [])


def test_the_found_nothing_notification_fires_only_when_nothing_is_missing(schema_session, monkeypatch):
    from app import notifier
    from app.database import TableArrInstances, TableSportsEvents, TableSportsLeagues
    from sportarr.sync import events

    schema_session.add(TableArrInstances(id=1, kind="sportarr", name="1", stable_key="1", port=1867))
    schema_session.commit()
    league = TableSportsLeagues(id=51, arr_instance_id=1, sportarrLeagueId=7, title="League")
    schema_session.add(league)
    schema_session.commit()
    schema_session.add(TableSportsEvents(id=61, arr_instance_id=1, league_id=51, sportarrEventId=8,
                                         file_id=9, path="/sports/a.mkv", title="A",
                                         missing_subtitles="[]"))
    schema_session.add(TableSportsEvents(id=62, arr_instance_id=1, league_id=51, sportarrEventId=9,
                                         file_id=10, path="/sports/b.mkv", title="B",
                                         missing_subtitles="[]"))
    schema_session.commit()

    sent = []
    monkeypatch.setattr(notifier, "send_notifications_sports_league",
                        lambda league_id, message, arr_instance_id=None: sent.append(league_id))
    events._notify_league_fully_subtitled(schema_session, 51, 1)
    assert sent == [51]

    # A league that still has missing languages reports nothing.
    schema_session.add(TableSportsEvents(id=63, arr_instance_id=1, league_id=51, sportarrEventId=10,
                                         file_id=11, path="/sports/c.mkv", title="C",
                                         missing_subtitles="['en']"))
    schema_session.commit()
    sent.clear()
    events._notify_league_fully_subtitled(schema_session, 51, 1)
    assert sent == []

    # An empty library is not what the option describes: nothing fires.
    schema_session.query(TableSportsEvents).filter(
        TableSportsEvents.league_id == 51).delete(synchronize_session=False)
    schema_session.commit()
    sent.clear()
    events._notify_league_fully_subtitled(schema_session, 51, 1)
    assert sent == []


def test_live_syncs_are_the_only_ones_that_can_report_the_found_nothing_notice():
    """The sports analog of the Sonarr/Radarr signalr flag: only the event
    stream and webhook repair path marks a sync as live, and the full-instance
    sync threads that flag into every league sync it runs."""
    import inspect

    from sportarr import sse_client
    from sportarr.sync import leagues

    assert "is_signalr=True" in inspect.getsource(sse_client.reconcile_work)
    source = inspect.getsource(leagues.update_sports_for_instance)
    assert "is_signalr=is_signalr" in source
    # The scheduled and API syncs default to not-live, so a periodic full sync
    # of a fully subtitled library does not notify on its own.
    assert "is_signalr=False" in inspect.getsource(leagues.update_sports_for_instance)


@pytest.fixture
def sports_refresh_targets(monkeypatch):
    """Capture sports publications without a live media server.

    Both sinks are replaced. A module that bound the dispatcher's entry point
    directly would bypass the events one, and the events one is what the
    toolbox fixture silences.
    """
    from media_servers import dispatcher, events

    publications = []
    monkeypatch.setattr(events, 'notify_subtitle_mutation', publications.append)
    monkeypatch.setattr(dispatcher, 'notify_subtitle_mutation', publications.append)

    return publications


def _save_has_let_go(session, event):
    """Whether a refresh worker could start on this publication right now.

    The worker first takes the video's subtitle locks, from its own thread, and
    queuing it takes the dispatcher's configuration lock, which a media server
    settings save holds across a database write. Neither may still be held by
    the save that published.
    """
    import os
    import sqlite3
    import threading
    from subtitles.tools.subsync_engines import subtitle_write_lock

    states = {id(state): state for state in (
        subtitle_write_lock(event.video_path, os.path.dirname(path))
        for path in (event.video_path, event.subtitle_path))}
    free = []

    def probe():
        taken = [state.lock for state in states.values() if state.lock.acquire(blocking=False)]
        for lock in taken:
            lock.release()
        free.append(len(taken) == len(states))

    worker = threading.Thread(target=probe)
    worker.start()
    worker.join()
    url = session.get_bind().url
    if url.get_backend_name() != 'sqlite':
        return free[0]
    writer = sqlite3.connect(url.database, timeout=0)
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.rollback()
    except sqlite3.OperationalError:
        return False
    finally:
        writer.close()
    return free[0]


@pytest.fixture
def released_publications(manual_library, sports_refresh_targets, monkeypatch):  # noqa: F811
    """Each publication with whether the save had released everything by then."""
    from media_servers import dispatcher, events

    _, session, _ = manual_library
    recorded = []

    def record(event):
        recorded.append((event, _save_has_let_go(session, event)))

    monkeypatch.setattr(events, 'notify_subtitle_mutation', record)
    monkeypatch.setattr(dispatcher, 'notify_subtitle_mutation', record)
    return recorded


def _described(event):
    return (event.media_type, event.video_path, event.operation, event.arr_instance_id,
            event.subtitle_path)


@pytest.mark.parametrize('failure', [False, True])
def test_single_provider_publication_refreshes_even_when_processing_fails(manual_library, released_publications, monkeypatch, failure):  # noqa: F811
    from subtitles import processing

    service, session, folder = manual_library
    result = service.manual_search_sports(61, 'en', arr_instance_id=1)[0]
    if failure:
        def failed_processing(*args, **kwargs):
            raise OSError('processing failed after the subtitle was published')
        monkeypatch.setattr(processing, 'process_subtitle', failed_processing)
    outcome = service.manual_download_sports(61, result, 1)
    assert outcome.publication['published'] is True
    assert outcome.publication['status'] == ('published_with_warnings' if failure else 'published')
    assert (folder / '1' / 'event.en.srt').exists()
    assert [_described(event) for event, _ in released_publications] == [
        ('sports', str(folder / '1' / 'event.mkv'), 'download', 1, str(folder / '1' / 'event.en.srt')),
    ]
    # Reported only once the save let go. From inside, the server's single
    # refresh worker would park on the save's locks through processing,
    # history and indexing, and queuing it would wait on a lock whose holder
    # may be waiting for this save's database writer.
    assert [released for _, released in released_publications] == [True]


def test_an_upgrade_reports_the_removed_file_once_the_save_let_go(manual_library, released_publications, monkeypatch):  # noqa: F811
    service, session, folder = manual_library
    replaced = folder / '1' / 'event.en.ass'

    def remove_superseded(path, previous_artifact, written_paths, is_upgrade, on_publish=None):
        on_publish(str(replaced))

    monkeypatch.setattr(service, '_remove_superseded_sports_subtitle', remove_superseded)
    result = service.manual_search_sports(61, 'en', arr_instance_id=1)[0]
    service.manual_download_sports(61, result, 1)
    video = str(folder / '1' / 'event.mkv')
    assert [_described(event) for event, _ in released_publications] == [
        ('sports', video, 'download', 1, str(folder / '1' / 'event.en.srt')),
        ('sports', video, 'delete', 1, str(replaced)),
    ]
    assert [released for _, released in released_publications] == [True, True]
