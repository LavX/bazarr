# coding=utf-8
"""POST and DELETE /api/episodes/forced-search, and the pending flag in the episodes listing.

Setting the request only records it: the search runs elsewhere. Every case runs
on SQLite and on PostgreSQL, both on an AUTOCOMMIT engine, the way the app runs.
"""
import pytest
import sqlalchemy as sa
from flask import Flask

from app.database import TableEpisodes, TableShows
from test_embedded_track_hidden import hidden_db  # noqa: F401

HEADERS = {"X-API-KEY": "synthetic-bazarr-key"}
ROOT = "/api/episodes/forced-search"


@pytest.fixture
def forced_api(hidden_db, monkeypatch):  # noqa: F811
    from api import api_bp
    from api.episodes import episodes as api_mod
    from app.config import settings as app_settings

    session = hidden_db
    monkeypatch.setitem(app_settings.auth, "apikey", "synthetic-bazarr-key")
    monkeypatch.setattr(api_mod, "database", session)
    events = []
    monkeypatch.setattr(api_mod, "event_stream", lambda **kw: events.append(kw))

    # The same upstream series and episode ids in two instances, and one more
    # episode that only exists once.
    session.add_all([
        TableShows(id=1, arr_instance_id=1, sonarrSeriesId=5, title="S", path="/tv"),
        TableShows(id=2, arr_instance_id=2, sonarrSeriesId=5, title="S", path="/tv2"),
    ])
    session.flush()
    for row_id, owner, series_id, upstream in ((1, 1, 1, 51), (2, 2, 2, 51), (3, 1, 1, 52)):
        session.add(TableEpisodes(id=row_id, arr_instance_id=owner, series_id=series_id, sonarrSeriesId=5,
                                  sonarrEpisodeId=upstream, title="E", path=f"/tv/e{row_id}.mkv",
                                  season=1, episode=row_id))
    session.commit()

    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client(), session, events


def _flags(session):
    session.expire_all()
    return dict(session.execute(sa.select(TableEpisodes.id, TableEpisodes.forced_search_requested_at)
                                .order_by(TableEpisodes.id)).all())


def _form(episode=52, **extra):
    return {"seriesid": 5, "episodeid": episode, **extra}


def test_requires_authentication(forced_api):
    client, _session, _events = forced_api
    assert client.post(ROOT, data=_form()).status_code == 401
    assert client.delete(ROOT, data=_form()).status_code == 401


def test_setting_and_clearing_the_request(forced_api):
    client, session, events = forced_api
    assert _flags(session) == {1: None, 2: None, 3: None}

    assert client.post(ROOT, data=_form(), headers=HEADERS).status_code == 204
    flags = _flags(session)
    assert flags[3] is not None and flags[1] is None and flags[2] is None
    assert events == [{"type": "series", "action": "update", "payload": 5}]

    assert client.delete(ROOT, data=_form(), headers=HEADERS).status_code == 204
    assert _flags(session) == {1: None, 2: None, 3: None}
    assert len(events) == 2


def test_asking_again_keeps_the_first_request_time(forced_api):
    client, session, _events = forced_api
    assert client.post(ROOT, data=_form(), headers=HEADERS).status_code == 204
    first = _flags(session)[3]
    assert client.post(ROOT, data=_form(), headers=HEADERS).status_code == 204
    assert _flags(session)[3] == first


def test_clearing_a_request_that_is_not_there_succeeds(forced_api):
    client, session, _events = forced_api
    assert client.delete(ROOT, data=_form(), headers=HEADERS).status_code == 204
    assert _flags(session) == {1: None, 2: None, 3: None}


@pytest.mark.parametrize("verb", ["post", "delete"])
def test_the_ids_can_come_in_the_query_string(forced_api, verb):
    client, session, _events = forced_api
    url = ROOT + "?seriesid=5&episodeid=51&arr_instance_id=2"
    assert client.post(url, headers=HEADERS).status_code == 204
    assert _flags(session)[2] is not None
    if verb == "delete":
        assert client.delete(url, headers=HEADERS).status_code == 204
        assert _flags(session)[2] is None
    # Without the owner the query string alone is still ambiguous.
    assert getattr(client, verb)(ROOT + "?seriesid=5&episodeid=51", headers=HEADERS).status_code == 409


def test_the_query_string_wins_over_a_body(forced_api):
    client, session, _events = forced_api
    assert client.post(ROOT + "?seriesid=5&episodeid=52", data=_form(episode=999),
                       headers=HEADERS).status_code == 204
    assert _flags(session)[3] is not None
    assert client.delete(ROOT, json=_form(), headers=HEADERS).status_code == 204
    assert _flags(session)[3] is None


@pytest.mark.parametrize("verb", ["post", "delete"])
@pytest.mark.parametrize("form", [_form(episode=999), {"seriesid": 6, "episodeid": 52}])
def test_an_unknown_episode_is_not_found(forced_api, verb, form):
    client, session, events = forced_api
    assert getattr(client, verb)(ROOT, data=form, headers=HEADERS).status_code == 404
    assert _flags(session) == {1: None, 2: None, 3: None}
    assert events == []


@pytest.mark.parametrize("verb", ["post", "delete"])
@pytest.mark.parametrize("form", [{"seriesid": 5}, {"episodeid": 52}, {}])
def test_both_ids_are_required(forced_api, verb, form):
    client, _session, _events = forced_api
    assert getattr(client, verb)(ROOT, data=form, headers=HEADERS).status_code == 400


@pytest.mark.parametrize("verb", ["post", "delete"])
def test_an_id_shared_by_two_instances_needs_an_owner(forced_api, verb):
    client, session, events = forced_api
    assert getattr(client, verb)(ROOT, data=_form(episode=51), headers=HEADERS).status_code == 409
    assert _flags(session) == {1: None, 2: None, 3: None}
    assert events == []


def test_the_owner_picks_the_row_in_a_multi_instance_library(forced_api):
    client, session, _events = forced_api
    assert client.post(ROOT, data=_form(episode=51, arr_instance_id=2), headers=HEADERS).status_code == 204
    flags = _flags(session)
    assert flags[2] is not None and flags[1] is None

    # The other instance's copy of the same upstream id is not touched.
    assert client.delete(ROOT, data=_form(episode=51, arr_instance_id=1), headers=HEADERS).status_code == 204
    assert _flags(session)[2] is not None
    assert client.delete(ROOT, data=_form(episode=51, arr_instance_id=2), headers=HEADERS).status_code == 204
    assert _flags(session)[2] is None


def test_an_owner_that_does_not_have_the_episode_is_not_found(forced_api):
    client, _session, _events = forced_api
    assert client.post(ROOT, data=_form(episode=52, arr_instance_id=2), headers=HEADERS).status_code == 404


def test_the_episodes_listing_reports_the_pending_request(forced_api):
    client, _session, _events = forced_api
    listing = "/api/episodes?id[]=1&id[]=3"

    def pending():
        body = client.get(listing, headers=HEADERS)
        assert body.status_code == 200
        return {row["id"]: row["forced_search_pending"] for row in body.json["data"]}

    assert pending() == {1: False, 3: False}
    assert client.post(ROOT, data=_form(), headers=HEADERS).status_code == 204
    assert pending() == {1: False, 3: True}
    assert client.delete(ROOT, data=_form(), headers=HEADERS).status_code == 204
    assert pending() == {1: False, 3: False}



@pytest.mark.parametrize("verb", ["post", "delete"])
def test_a_reused_local_id_is_not_written(forced_api, monkeypatch, verb):
    """A sync can delete the episode after the lookup and the id can go to another episode."""
    from api.episodes import episodes as api_mod

    client, session, _events = forced_api
    real = api_mod.database
    swapped = []

    class Swapping:
        def execute(self, statement, *args, **kwargs):
            if not swapped and getattr(statement, "is_update", False):
                swapped.append(True)
                real.execute(sa.delete(TableEpisodes).where(TableEpisodes.id == 3))
                real.execute(sa.insert(TableEpisodes).values(
                    id=3, arr_instance_id=1, series_id=1, sonarrSeriesId=5, sonarrEpisodeId=53, title="Other",
                    path="/tv/other.mkv", season=1, episode=9,
                    forced_search_requested_at=(None if verb == "post" else sa.func.current_timestamp())))
            return real.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(api_mod, "database", Swapping())
    assert getattr(client, verb)(ROOT, data=_form(), headers=HEADERS).status_code == 204
    assert swapped
    flag = _flags(session)[3]
    # The new episode that took the id is exactly as it was inserted.
    assert (flag is None) if verb == "post" else (flag is not None)


WANTED = "/api/episodes/wanted"


@pytest.fixture
def wanted_api(forced_api, monkeypatch):
    from api.episodes import wanted as wanted_mod

    client, session, events = forced_api
    monkeypatch.setattr(wanted_mod, "database", session)
    return client, session, events


def _wanted(client):
    body = client.get(WANTED, headers=HEADERS)
    assert body.status_code == 200
    return body.json


def test_the_wanted_list_stays_as_it_is_and_reports_pending_requests_apart(wanted_api):
    client, session, _events = wanted_api

    # Nothing is missing anywhere: the list is empty before and after the request, and only the count moves.
    before = _wanted(client)
    assert before["data"] == [] and before["total"] == 0 and before["forced_pending"] == 0

    assert client.post(ROOT, data=_form(), headers=HEADERS).status_code == 204
    assert client.post(ROOT, data=_form(episode=51, arr_instance_id=2), headers=HEADERS).status_code == 204
    pending = _wanted(client)
    assert pending["data"] == [] and pending["total"] == 0
    assert pending["forced_pending"] == 2

    assert client.delete(ROOT, data=_form(), headers=HEADERS).status_code == 204
    assert _wanted(client)["forced_pending"] == 1


@pytest.mark.usefixtures("languages_loaded")
def test_a_pending_request_on_an_episode_that_is_also_missing_something_is_listed_once(wanted_api):
    client, session, _events = wanted_api
    session.execute(sa.update(TableEpisodes).where(TableEpisodes.id == 3).values(
        missing_subtitles="['fr']", audio_language="[]", subtitles="[]"))
    session.commit()
    assert client.post(ROOT, data=_form(), headers=HEADERS).status_code == 204

    body = _wanted(client)

    assert body["total"] == 1 and len(body["data"]) == 1
    assert body["forced_pending"] == 1


def test_wanted_pending_count_follows_the_wanted_exclusions(wanted_api, monkeypatch):
    from app.config import settings as app_settings

    client, session, _events = wanted_api
    session.execute(sa.update(TableEpisodes).where(TableEpisodes.id == 3).values(monitored="False"))
    session.commit()
    assert client.post(ROOT, data=_form(), headers=HEADERS).status_code == 204
    assert _wanted(client)["forced_pending"] == 1

    monkeypatch.setattr(app_settings.sonarr, "only_monitored", True)
    assert _wanted(client)["forced_pending"] == 0
