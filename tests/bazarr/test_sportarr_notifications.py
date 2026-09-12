# coding=utf-8
"""Notifications for sports subtitle events.

send_notifications / send_notifications_movie are called from eight places for
series and movies. Sports had no equivalent at all, so with Apprise, Discord or
Telegram configured every sports download, upgrade and translate happened
silently and the user had no reason to believe anything had run.
"""

from test_sportarr_kind_migration import migration_engine  # noqa: F401
from test_sportarr_indexer import indexed_library, sports  # noqa: F401
from test_sportarr_manual import manual_library  # noqa: F401


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


def test_the_sync_found_nothing_emission_is_gated_exactly_like_sonarr_and_radarr():
    """The sync path emits the found-nothing notification only for a live
    sync, and only when the option is on, the same condition the Sonarr and
    Radarr sync paths use."""
    import inspect

    from sportarr.sync import events

    source = inspect.getsource(events.sync_events)
    assert "is_signalr and settings.general.notify_if_nothing_is_missing_for_signalr_event" in source
    helper = inspect.getsource(events._notify_league_fully_subtitled)
    assert '"There are no missing subtitles in this league."' in helper
    assert "send_notifications_sports_league(" in helper


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
