# coding=utf-8
"""Notifications for sports subtitle events.

send_notifications / send_notifications_movie are called from eight places for
series and movies. Sports had no equivalent at all, so with Apprise, Discord or
Telegram configured every sports download, upgrade and translate happened
silently and the user had no reason to believe anything had run.
"""


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
