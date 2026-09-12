# coding=utf-8
"""Uploading a subtitle for a sports event.

Episodes and movies have had an upload route since forever; sports had none, so
a subtitle you already owned could not be handed to Bazarr for a sports event.
"""


def test_the_upload_route_does_not_shadow_the_index_route():
    """POST /sports/events/<id>/subtitles was already taken by the indexer.

    Registering a second Resource on the same path silently shadowed it and
    broke indexing, which is why upload lives on its own path.
    """
    import inspect

    from api.sports import events, subtitles

    upload = inspect.getsource(subtitles)
    index = inspect.getsource(events)
    assert '"/sports/events/<int:event_id>/subtitles/upload"' in upload
    assert '"/sports/events/<int:event_id>/subtitles")' not in upload
    assert "'/sports/events/<int:event_id>/subtitles'" in index


def test_delete_lives_on_the_resource_that_owns_the_path():
    """Rather than a second Resource registered over the index POST."""
    import inspect

    from api.sports import events

    source = inspect.getsource(events.SportsEventSubtitles)
    assert "def post(self, event_id):" in source
    assert "def delete(self, event_id):" in source


def test_manual_upload_takes_a_sports_event():
    import inspect

    from subtitles import upload

    assert "sportsEventId" in inspect.signature(upload.manual_upload_subtitle).parameters


def test_the_language_profile_is_joined_from_the_league():
    """An event carries no profileId column; the profile lives on the league.
    Selecting it off the event raised AttributeError inside the queued job,
    so the upload returned 204 and then silently did nothing."""
    import inspect

    from subtitles import upload

    source = inspect.getsource(upload.manual_upload_subtitle)
    assert "TableSportsLeagues.profileId" in source
    assert "TableSportsEvents.profileId" not in source


def test_the_upload_is_recorded_and_the_event_refreshed():
    import inspect

    from subtitles import upload

    source = inspect.getsource(upload.manual_upload_subtitle)
    assert "sports_history_log(4, sportsEventId, arr_instance_id, result)" in source
    assert "event_stream(type='sports', action='update', payload=sportsEventId)" in source


def test_the_upload_notifies_like_series_and_movies_do():
    import inspect

    from subtitles import upload

    source = inspect.getsource(upload.manual_upload_subtitle)
    assert "send_notifications_sports" in source
    assert "dont_notify_manual_actions" in source


def test_no_consumer_refresh_is_attempted_for_sports():
    """Sportarr offers only an untargeted library scan and the media servers
    refresh by an imdbId a sports event has not got."""
    import inspect

    from subtitles import upload

    source = inspect.getsource(upload._refresh_upload_consumers)
    sports = source[source.index("if media_type == 'sports':"):source.index("if media_type == 'series':")]
    assert "return" in sports
    assert "notify_sonarr" not in sports
    assert "plex_refresh_item" not in sports


def test_an_invalid_extension_is_rejected_before_any_work():
    import inspect

    from api.sports import subtitles

    source = inspect.getsource(subtitles.SportsEventSubtitleUpload.post)
    ext_at = source.index("SUBTITLE_EXTENSIONS")
    upload_at = source.index("manual_upload_subtitle(")
    assert ext_at < upload_at


def test_upload_consumers_for_sports_request_one_rescan(monkeypatch):
    """An upload is a live write: Sportarr gets exactly one whole-library
    rescan request for the owner, and unconfigured media servers are left
    alone."""
    from app.config import settings
    from sportarr import notify as sportarr_notify
    from subtitles import upload

    requested = []
    refreshes = []
    monkeypatch.setattr(sportarr_notify, "notify_rescan", lambda owner: requested.append(owner))
    monkeypatch.setattr(upload, "plex_update_sports_library",
                        lambda: refreshes.append("plex"))
    monkeypatch.setattr(upload, "jellyfin_update_sports_library",
                        lambda: refreshes.append("jellyfin"))
    monkeypatch.setattr(settings.general, "use_plex", True)
    monkeypatch.setattr(settings.general, "use_jellyfin", True)
    monkeypatch.setattr(settings.plex, "sports_library", [])
    monkeypatch.setattr(settings.jellyfin, "sports_library_ids", [])

    upload._refresh_upload_consumers("sports", None, 7)
    assert requested == [7]
    assert refreshes == []

    monkeypatch.setattr(settings.plex, "sports_library", ["Sports"])
    monkeypatch.setattr(settings.jellyfin, "sports_library_ids", ["10"])
    upload._refresh_upload_consumers("sports", None, 7)
    assert requested == [7, 7]
    assert refreshes == ["plex", "jellyfin"]
