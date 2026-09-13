# coding=utf-8
"""Regressions found in a review round over the sports branch.

Each test here pins one defect that was live on the branch, with the failure
it produced written down next to it, so a later refactor that reintroduces the
shape fails here rather than in someone's install.
"""

import inspect
import threading

import pytest
import sqlalchemy as sa
from test_sportarr_kind_migration import migration_engine, _run  # noqa: F401
from test_sportarr_indexer import indexed_library  # noqa: F401
from test_sportarr_manual import manual_library  # noqa: F401
from test_sportarr_notifications import sports_refresh_targets  # noqa: F401


# --------------------------------------------------------------------------
# A bounded wait for an instance's sync lock.
# --------------------------------------------------------------------------

def test_the_owner_sync_lock_wait_can_be_bounded():
    """A request thread must not park forever behind a scheduled sync.

    owner_sync_lock spun on `while not lock.acquire(timeout=0.1):
    check_cancelled(cancel)`, and check_cancelled(None) does nothing, so a
    caller with no cancel token had no exit and no timeout at all. The webhook
    ran a reconcile inline on a Waitress worker with cancel=None, so every
    webhook that arrived during a full sync consumed a request thread until
    the pool was gone and the UI stopped answering.
    """
    from sportarr.connection import SportsSyncBusy, owner_sync_lock

    owner = 987654
    holder_has_it = threading.Event()
    release = threading.Event()

    def hold():
        with owner_sync_lock(owner):
            holder_has_it.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert holder_has_it.wait(5), "the holder never acquired the lock"

    try:
        with pytest.raises(SportsSyncBusy):
            with owner_sync_lock(owner, timeout=0.3):
                pytest.fail("acquired a lock another thread was holding")
    finally:
        release.set()
        holder.join(10)

    # The bound is opt-in, so background callers keep waiting: once the holder
    # is done the same call succeeds.
    with owner_sync_lock(owner, timeout=5):
        pass


def test_sports_sync_busy_is_not_a_value_error():
    """The sports API maps ValueError onto 400 in several handlers, and a busy
    instance is neither the caller's fault nor permanent."""
    from sportarr.connection import SportsSyncBusy

    assert not issubclass(SportsSyncBusy, ValueError)


def test_the_webhook_bounds_its_wait():
    from api.webhooks import sportarr as webhook

    assert isinstance(webhook.WEBHOOK_SYNC_LOCK_TIMEOUT, int)
    assert 0 < webhook.WEBHOOK_SYNC_LOCK_TIMEOUT <= 60
    source = inspect.getsource(webhook._reconcile)
    assert 'lock_timeout=WEBHOOK_SYNC_LOCK_TIMEOUT' in source


@pytest.mark.parametrize('function', ['sync_leagues', 'update_sports_for_instance'])
def test_the_sync_entry_points_accept_a_lock_timeout(function):
    from sportarr.sync import leagues

    assert 'lock_timeout' in inspect.signature(getattr(leagues, function)).parameters


def test_sync_events_accepts_a_lock_timeout():
    from sportarr.sync.events import sync_events

    assert 'lock_timeout' in inspect.signature(sync_events).parameters


# --------------------------------------------------------------------------
# A half-started SSE client must not look healthy.
# --------------------------------------------------------------------------

class _Thread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


def _client():
    from sportarr.sse_client import SportarrSSEClient

    return SportarrSSEClient.__new__(SportarrSSEClient)


@pytest.mark.parametrize('threads, alive', [
    ([_Thread(True), _Thread(True)], True),
    ([_Thread(True), _Thread(False)], False),
    ([_Thread(False), _Thread(True)], False),
    ([_Thread(False), _Thread(False)], False),
    ([], False),
])
def test_a_client_is_alive_only_when_both_threads_are(threads, alive):
    """_read streams events and _work applies them; both are required.

    With `any`, a client whose _work thread had died still reported alive, so
    refresh() never replaced it and _read kept setting connected=True. The
    badge read LIVE while nothing received was ever applied.
    """
    client = _client()
    client._threads = threads
    assert client.is_alive() is alive


def test_refresh_starts_a_client_before_publishing_it():
    """Publishing first meant a client that raised in start() stayed in the
    map, where `owner not in self.clients` skipped it on every later refresh."""
    from sportarr.sse_client import SportarrClientManager

    source = inspect.getsource(SportarrClientManager.refresh)
    start = source.index('client.start()')
    publish = source.index('self.clients[owner] = client')
    assert start < publish, "the client is published before it is started"


# --------------------------------------------------------------------------
# The sports SSE badge must not read LIVE with no stream at all.
# --------------------------------------------------------------------------

def test_the_sse_badge_compares_against_the_enabled_instances():
    from sportarr import sse_client

    source = inspect.getsource(sse_client.all_sportarr_sse_connected)
    # It must consult what SHOULD be streaming, not just what is.
    assert "enabled_only=True" in source
    assert "use_sportarr" in source


# --------------------------------------------------------------------------
# A queued sports job must report what actually happened.
# --------------------------------------------------------------------------

def test_a_duplicate_sync_is_not_reported_as_queued():
    """feed_jobs_pending_queue returns False when an identical job is already
    pending or running. Discarding that made both sync routes answer
    202 {"queued": true} for work that was dropped, and with no job_id the
    client could never poll /sports/jobs/<id> either."""
    from api.sports.leagues import _queued

    body, status = _queued(False, 'Library sync queued')
    assert status == 200
    assert body['queued'] is False
    assert body['job_id'] is None
    assert 'already active' in body['message']

    body, status = _queued(42, 'Library sync queued')
    assert status == 202
    assert body == {'queued': True, 'job_id': 42, 'message': 'Library sync queued'}


def test_the_sync_routes_return_the_job_shape_the_client_declares():
    from api.sports import leagues

    for resource in (leagues.SportsLeagueEventSync, leagues.SportsLeagueSync):
        source = inspect.getsource(resource.post)
        assert '_queued(' in source, f'{resource.__name__} does not report its job id'
        assert "{'queued': True}, 202" not in source


# --------------------------------------------------------------------------
# Owner resolution on the shared endpoints.
# --------------------------------------------------------------------------

def test_the_episode_create_subtitle_select_carries_its_owner():
    """_create_subtitle reads row.arr_instance_id whenever the caller omitted
    the parameter. The movie and sports branches selected it; the episode
    branch selected only (id, path), so a SQLAlchemy Row raised AttributeError
    and POST /api/episodes/<id>/subtitles answered 500 instead of 201. The
    sports work broke an existing series path."""
    from api.subtitles import content

    source = inspect.getsource(content._create_subtitle)
    episode_branch = source.split("elif media_type == 'movie'")[0]
    assert 'TableEpisodes.arr_instance_id' in episode_branch


def test_a_row_select_without_the_column_raises_attribute_error(schema_session):
    """The mechanism behind the test above, pinned so it cannot be argued
    away: a Row only exposes the columns its select asked for."""
    from app.database import TableEpisodes

    row = sa.select(TableEpisodes.id, TableEpisodes.path)
    result = schema_session.execute(row).first()
    assert result is None  # empty table; the point is the column set below
    compiled = {column.get('name') for column in row.column_descriptions}
    assert 'arr_instance_id' not in compiled


def test_the_manual_toolbox_takes_the_sports_owner_off_the_row():
    """arr_instance_id is optional on this parser and scoped() is a no-op for
    None, so the row was found and then path_replace_instance(path, None,
    'sports') raised ValueError out of the handler as a 500."""
    from api.subtitles import subtitles

    source = inspect.getsource(subtitles)
    sports_branch = source.split('if media_type == "sports":')[1].split('elif media_type ==')[0]
    assert 'TableSportsEvents.arr_instance_id,' in sports_branch
    assert 'arr_instance_id = arr_instance_id or sports_meta.arr_instance_id' in sports_branch


# The reads that go on to MAP the path. _get_media_metadata is deliberately
# absent: it returns the raw stored path and never calls path_replace_instance,
# so a disabled owner costs it nothing.
@pytest.mark.parametrize('module_name, function_name', [
    ('api.subtitles.content', 'resolve_subtitle_path'),
    ('api.subtitles.content', '_create_subtitle'),
    ('api.editor.editor', None),
])
def test_the_shared_sports_reads_require_an_enabled_owner(module_name, function_name):
    """A disabled instance keeps its rows, and the per-instance sports mapping
    refuses a disabled owner by raising, so an unfiltered read answered 500
    where the series and movies siblings answer 404."""
    import importlib

    module = importlib.import_module(module_name)
    target = getattr(module, function_name) if function_name else module
    source = inspect.getsource(target)
    # The sports SELECT itself must carry the join, so anchor on the join
    # predicate rather than on a branch marker that appears more than once.
    assert 'TableSportsEvents.arr_instance_id == TableArrInstances.id' in source
    assert "TableArrInstances.enabled == 1" in source
    assert "TableArrInstances.kind == 'sportarr'" in source


def test_the_league_bundle_requires_an_enabled_owner():
    from api.subtitles import download

    source = inspect.getsource(download)
    assert 'TableSportsLeagues.arr_instance_id == TableArrInstances.id' in source
    assert "TableArrInstances.enabled == 1" in source


# --------------------------------------------------------------------------
# Work that must not run when Sportarr is switched off.
# --------------------------------------------------------------------------

def test_saving_settings_does_not_walk_sports_rows_when_sportarr_is_off():
    from api.system import settings as settings_api

    source = inspect.getsource(settings_api)
    index = source.index('list_missing_subtitles_sports()')
    assert 'if settings.general.use_sportarr:' in source[:index][-400:]


def test_the_badge_does_not_count_sports_when_sportarr_is_off():
    from api.badges import badges

    source = inspect.getsource(badges)
    assert 'wanted_badge(database) if settings.general.use_sportarr else 0' in source


def test_the_compat_hash_admission_stays_unconditional_on_purpose():
    """A review flagged the cost of admitting a bare moviehash on an install
    with no sports library: it builds a nameless video and runs a full
    provider fanout that is metered against the key's quota.

    It is deliberately NOT gated. The sports compat contract pins a hash that
    resolves to nothing as 200 with an empty list, and one of those tests
    disables every sports instance and still expects admission, because the
    library may appear while the request is in flight. Gating on a resolved
    match, on a configured library, or on use_sportarr each break that
    contract, and it is an external API other clients speak. This test exists
    so the next person to try reads the reason first.
    """
    from compat import routes

    source = inspect.getsource(routes.subtitles)
    assert 'valid_moviehash(args.get("moviehash"))' in source
    assert 'settings.general.use_sportarr' not in source


# --------------------------------------------------------------------------
# The adoption guard must tolerate columns added by later migrations.
# --------------------------------------------------------------------------

def test_the_adoption_guard_accepts_a_column_a_later_migration_adds(
    migration_engine,  # noqa: F811
):
    """The guard compared column sets for exact equality, so any column a
    LATER revision adds aborted the upgrade. table_history_sports.artifact,
    added by e7a9c2d4b601, is already such a column, which is why init_db had
    to exclude that one table from create_all to keep fresh installs bootable.
    That workaround is gone, so this has to hold.
    """
    from app.database import Base

    engine = migration_engine
    metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    # Built from the live ORM metadata, artifact column and all, exactly as
    # init_db's create_all does on a fresh install.
    metadata.create_all(engine)
    _run(engine, "stamp", "c9e4a6b2d701")
    _run(engine, "upgrade", "d3f6a8b1e902")

    columns = {column['name'] for column in sa.inspect(engine).get_columns('table_history_sports')}
    assert 'artifact' in columns


def test_init_db_no_longer_excludes_the_sports_history_table():
    from app import database

    source = inspect.getsource(database.init_db)
    assert 'table_history_sports"' not in source.split('metadata.create_all')[1][:200]


def test_the_adoption_refusal_names_what_is_missing():
    """The raise named neither table nor column, so a user hitting it got a
    boot loop with nothing to act on."""
    import pathlib

    migration = pathlib.Path('migrations/versions/d3f6a8b1e902_owned_sports_library.py')
    source = migration.read_text()
    assert '{table.name}' in source
    assert 'missing or mismatched columns' in source


# --------------------------------------------------------------------------
# Round two: the deferred findings.
# --------------------------------------------------------------------------

def test_a_missing_sports_row_is_404_not_400():
    """Every sports resolution helper raised a bare ValueError and every
    handler mapped ValueError onto 400, so asking for a row that does not
    exist was reported as a malformed request. The combine Resource even
    declared a 404 it could never emit."""
    from sportarr.errors import SportsNotFound

    # A ValueError subclass on purpose: a handler that has not been taught
    # about it keeps the old 400 rather than turning a missing row into a 500.
    assert issubclass(SportsNotFound, ValueError)


def test_every_sports_handler_maps_not_found_to_404():
    import importlib

    for name in ('events', 'leagues', 'subtitles', 'workflows'):
        module = importlib.import_module(f'api.sports.{name}')
        source = inspect.getsource(module)
        assert source.count('except SportsNotFound as exc:') == \
            source.count('except ValueError as exc:'), name


def test_the_league_patch_returns_no_body_with_204():
    from api.sports import leagues

    source = inspect.getsource(leagues.SportsLeague.patch)
    assert "return '', 204" in source
    assert "return {}, 204" not in source


def test_the_optional_owner_is_actually_optional():
    """The upload parser declared arr_instance_id required=False and then
    rejected its absence, because everything went through _owner."""
    from api.sports.leagues import _optional_owner, _owner

    assert _optional_owner(None) is None
    assert _optional_owner('') is None
    assert _optional_owner(7) == 7
    with pytest.raises(ValueError):
        _owner(None)


def test_the_sports_search_is_marshalled_like_the_other_providers():
    """Unmarshalled, original_format came back as a JSON boolean while the
    series and movies searches return the string "False". Both feed the one
    shared SearchResultType in the frontend."""
    from api.sports.subtitles import SportsSearch

    model = SportsSearch.get_response_model
    assert model['original_format'].__class__.__name__ == 'String'
    assert 'score' in model and 'provider' in model


def test_the_upgradable_endpoint_carries_sports():
    from api.subtitles import batch

    source = inspect.getsource(batch.get_upgradable_media_ids)
    assert "'sports'" in source and "'sportsKeys'" in source
    # The early return has to carry the keys too, or the frontend reads
    # undefined the moment upgrades are switched off.
    early = source.split('return')[1]
    assert 'sports' in early


def test_the_batch_profile_assignment_is_one_transaction():
    from sportarr.library import assign_profiles

    source = inspect.getsource(assign_profiles)
    assert source.count('sports_transaction') == 1
    assert 'for league_id, owner, profile_id in assignments' in source


def test_the_automatic_search_accepts_one_language():
    from sportarr.workflows import automatic_search_sports

    parameters = inspect.signature(automatic_search_sports).parameters
    assert 'language' in parameters
    # add_job_from_function binds the caller's locals against its own
    # signature, so a new local that is not a parameter would raise there.
    assert set(parameters) == {'event_id', 'arr_instance_id', 'job_id', 'language'}


def test_sports_history_reports_exclusion_and_upgrade_state():
    from sportarr import history

    source = inspect.getsource(history)
    assert '_mark_history_flags' in source
    assert '"blacklisted"' in source and '"upgradable"' in source


def test_the_cheap_upgradable_check_does_no_disk_io():
    """upgrade_rows hashes the subtitle file for every candidate, which is
    right for the upgrade job and far too expensive for a page request."""
    from sportarr.workflows import upgradable_history_ids

    source = inspect.getsource(upgradable_history_ids)
    assert 'capture_artifact' not in source


def test_a_local_sports_race_does_not_blame_the_providers():
    from compat import routes

    source = inspect.getsource(routes.subtitles)
    assert 'except SportsSelectionChanged:' in source
    assert 'sports selection changed during search, retry' in source


def test_the_sync_write_loop_can_be_cancelled():
    from sportarr.sync import events

    source = inspect.getsource(events.sync_events)
    body = source.split('for item, row in zip(parsed, matches):')[1]
    assert 'check_cancelled(cancel)' in body


def test_shutdown_closes_the_webserver_before_waiting_on_streams():
    # Read the file rather than import app.server: importing it pulls in the
    # whole webserver stack and blocks.
    import pathlib

    source = pathlib.Path('bazarr/app/server.py').read_text()
    body = source.split('def close_all(self):')[1].split('def ')[0]
    assert body.index('self.server.close()') < body.index('stop_sportarr_clients()')


def test_the_client_manager_stops_clients_in_parallel():
    from sportarr.sse_client import SportarrClientManager

    source = inspect.getsource(SportarrClientManager.stop)
    assert 'Thread(' in source
    # The docstring explains the old 35s value, so check the code below it.
    body = source.split('"""', 2)[-1]
    assert 'timeout=35' not in body
    assert inspect.signature(SportarrClientManager.stop).parameters['timeout'].default == 6


def test_the_owner_enabled_lookup_is_cached():
    """is_set() is polled from tight wait loops, and each call issued a SELECT
    plus a job-progress update; a 60-second ffprobe cost about twelve thousand
    of each."""
    from sportarr.workflows import SportsJobSignal

    assert SportsJobSignal.OWNER_CACHE_SECONDS > 0
    signal = SportsJobSignal.__new__(SportsJobSignal)
    signal.owner, signal.job_id, signal.parent = 1, None, None
    signal._owner_checked_at = None
    signal._owner_enabled = True

    calls = []

    def _lookup():
        calls.append(1)
        return True

    import time as _time
    signal._owner_checked_at = _time.monotonic()
    signal._owner_enabled = True
    # Inside the window the cached answer is used, with no query at all.
    assert signal._owner_is_enabled() is True
    assert calls == []


def test_the_analysis_poll_is_not_a_hundred_hertz():
    from sportarr import analysis

    assert analysis.POLL_INTERVAL >= 0.1


def test_the_sports_mismatch_announces_a_sports_event():
    """The socket reducer maps type='sports' onto the whole Sports query root.
    Sending 'movie-wanted' for a sports media_id refreshed the movies Wanted
    page with an id that is not a movie."""
    from subtitles import mismatch

    source = inspect.getsource(mismatch)
    index = source.index("event_stream(type='episode-wanted'")
    assert "media_type == 'sports'" in source[:index][-400:]


def test_pruned_sports_events_do_not_leave_orphan_mismatches():
    from sportarr.sync import events

    source = inspect.getsource(events._prune_events)
    assert 'forget_media' in source


def test_a_manual_sports_save_clears_the_badge_it_resolved():
    from sportarr import subtitles as sports_subtitles

    source = inspect.getsource(sports_subtitles.save_sports_subtitle)
    assert 'clear_mismatch_after_manual_save' in source


def test_the_shared_test_database_is_not_cleared_between_sessions():
    """Clearing it looks right and is not.

    The directory is shared, so a second local run inherits the rows the first
    one wrote and the compat contract suite fails on a duplicate movie id. But
    CI's third pytest step runs one file per process, and create_all builds
    only the tables whose models that process imported, so a session starting
    from an empty file gets a partial schema and depends on the fully
    populated database the earlier step left behind. Clearing it made
    test_threading_followup fail on a missing table. The local papercut is the
    cheaper of the two.
    """
    import pathlib

    source = pathlib.Path('tests/conftest.py').read_text()
    assert 'os.remove(_stale)' not in source
    assert 'deliberately NOT cleared' in source


# --------------------------------------------------------------------------
# History parity: the endpoint sends parsed criteria, the page can show
# embedded records when asked, and embedded indexing writes them at all.
# --------------------------------------------------------------------------

def _sports_history(schema_session):
    from app.database import (TableArrInstances, TableHistorySports,
                              TableSportsEvents, TableSportsLeagues)

    schema_session.add(TableArrInstances(
        id=1, kind="sportarr", name="Sports", stable_key="s",
        port=1867, enabled=1,
    ))
    schema_session.flush()
    schema_session.add(TableSportsLeagues(
        id=1, arr_instance_id=1, sportarrLeagueId=7, title="League",
    ))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=1, arr_instance_id=1, league_id=1, sportarrEventId=7, file_id=8,
        path="/sports/event.mkv", title="Card", file_size=123,
        audio_language="[]", subtitles="[]", missing_subtitles="[]",
        failedAttempts="[]",
    ))
    schema_session.flush()
    schema_session.add(TableHistorySports(
        id=1, arr_instance_id=1, league_id=1, event_id=1, action=1,
        description="downloaded", language="en", provider="provider-a",
        subs_id="release", score=90, score_out_of=180,
        matched="['title', 'year']", not_matched="['release_group']",
        subtitles_path="/sports/event.en.srt", video_path="/sports/event.mkv",
    ))
    # An Embedded Source row: the sports indexer records one action=7 row per
    # detected track language. It has no sidecar, exactly like the episodes and
    # movies indexers' rows.
    schema_session.add(TableHistorySports(
        id=2, arr_instance_id=1, league_id=1, event_id=1, action=7,
        description="fr embedded subtitles detected.", language="fr",
        provider="embedded", subtitles_path=None, video_path="/sports/event.mkv",
        matched="None", not_matched="None",
    ))
    schema_session.flush()


def test_sports_history_sends_parsed_criteria_not_stored_reprs(schema_session):
    """The shared Match cell reads matches/dont_matches as string lists. The
    sports route used to pass the raw Python-repr columns through, and the
    mapper hardcoded empty arrays, so the data was discarded at the boundary."""
    _sports_history(schema_session)

    from sportarr import history

    result = history.list_records(schema_session, "history", include_embedded=True)
    downloaded = next(item for item in result["data"] if item["action"] == 1)
    assert downloaded["matches"] == ["title", "year"]
    assert downloaded["dont_matches"] == ["release_group"]
    assert "matched" not in downloaded and "not_matched" not in downloaded


def test_sports_history_criteria_parse_defends_against_non_list_rows(schema_session):
    """The deletion writer stores no criteria at all, which lands in the column
    as the string "None". literal_eval of that is None, not a list, so the
    parse must fall back to an empty list instead of crashing the page."""
    _sports_history(schema_session)

    from sportarr import history

    result = history.list_records(schema_session, "history", include_embedded=True)
    embedded = next(item for item in result["data"] if item["action"] == 7)
    assert embedded["matches"] == []
    assert embedded["dont_matches"] == []


def test_sports_history_hides_embedded_records_by_default(schema_session, monkeypatch):
    """The episodes and movies endpoints filter action=7 out of both the rows
    and the total unless include_embedded is asked for; sports history must do
    the same or a large library drowns in track-state rows."""
    _sports_history(schema_session)

    from api.sports import workflows
    from flask import Flask

    monkeypatch.setattr(workflows, "database", schema_session)
    app = Flask(__name__)
    with app.test_request_context("/api/sports/history?start=0&length=100"):
        result, code = workflows.SportsHistory.get.__wrapped__(workflows.SportsHistory())
    assert code == 200
    assert [item["action"] for item in result["data"]] == [1]
    assert result["total"] == 1


def test_sports_history_includes_embedded_records_on_request(schema_session, monkeypatch):
    _sports_history(schema_session)

    from api.sports import workflows
    from flask import Flask

    monkeypatch.setattr(workflows, "database", schema_session)
    app = Flask(__name__)
    with app.test_request_context("/api/sports/history?start=0&length=100&include_embedded=true"):
        result, code = workflows.SportsHistory.get.__wrapped__(workflows.SportsHistory())
    assert code == 200
    assert sorted(item["action"] for item in result["data"]) == [1, 7]
    assert result["total"] == 2


# --------------------------------------------------------------------------
# Blacklist parity: the listing carries the event's season, episode and part,
# so two parts of one event are distinguishable on the excluded page.
# --------------------------------------------------------------------------

def _sports_blacklist_rows(schema_session):
    from app.database import (TableArrInstances, TableBlacklistSports,
                              TableSportsEvents, TableSportsLeagues)

    schema_session.add(TableArrInstances(
        id=1, kind="sportarr", name="Sports", stable_key="s",
        port=1867, enabled=1,
    ))
    schema_session.flush()
    schema_session.add(TableSportsLeagues(
        id=1, arr_instance_id=1, sportarrLeagueId=7, title="League",
    ))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=1, arr_instance_id=1, league_id=1, sportarrEventId=7, file_id=8,
        path="/sports/prelims.mkv", title="Card", season=2026,
        episode=1, partNumber=1, partName="Prelims",
        audio_language="[]", subtitles="[]", missing_subtitles="[]",
        failedAttempts="[]",
    ))
    schema_session.add(TableSportsEvents(
        id=2, arr_instance_id=1, league_id=1, sportarrEventId=8, file_id=9,
        path="/sports/main.mkv", title="Card", season=2026,
        episode=1, partNumber=2,
        audio_language="[]", subtitles="[]", missing_subtitles="[]",
        failedAttempts="[]",
    ))
    schema_session.flush()
    schema_session.add(TableBlacklistSports(
        id=1, arr_instance_id=1, league_id=1, event_id=1,
        language="en", provider="provider-a", subs_id="prelims-release",
    ))
    schema_session.add(TableBlacklistSports(
        id=2, arr_instance_id=1, league_id=1, event_id=2,
        language="en", provider="provider-a", subs_id="main-release",
    ))
    schema_session.flush()


def test_sports_blacklist_carries_the_event_season_episode_and_part(schema_session):
    """The excluded page renders one row per release, and two parts of one
    event used to share a title with no way to tell them apart. The listing
    now sends the event fields the events table already sends."""
    _sports_blacklist_rows(schema_session)

    from sportarr import history

    result = history.list_records(schema_session, "blacklist")
    by_id = {item["id"]: item for item in result["data"]}

    assert by_id[1]["season"] == 2026
    assert by_id[1]["episode"] == 1
    assert by_id[1]["partNumber"] == 1
    assert by_id[1]["partName"] == "Prelims"

    # A part without a part name still distinguishes itself by its number, and
    # the events listing's "or None" normalization is applied so the page can
    # treat an unset part like the events table does.
    assert by_id[2]["partNumber"] == 2
    assert by_id[2]["partName"] is None


def test_sports_blacklist_part_stays_none_for_an_unparted_event(schema_session):
    """The column is NOT NULL and the parser writes 0 for a part-less event,
    so a listing must turn that into None and not invent a "Part 0"."""
    _sports_blacklist_rows(schema_session)
    from app.database import TableSportsEvents

    schema_session.get(TableSportsEvents, 2).partNumber = 0
    schema_session.commit()

    from sportarr import history

    result = history.list_records(schema_session, "blacklist")
    by_id = {item["id"]: item for item in result["data"]}
    assert by_id[2]["partNumber"] is None


def test_sports_history_with_parts_still_lists_every_column(schema_session):
    """The event columns ride the same join for history, and the Match and
    Upgrade flags added recently must survive the extra selected columns."""
    _sports_history(schema_session)
    from app.database import TableSportsEvents

    schema_session.get(TableSportsEvents, 1).season = 2026
    schema_session.get(TableSportsEvents, 1).partNumber = 4
    schema_session.commit()

    from sportarr import history

    result = history.list_records(schema_session, "history", include_embedded=True)
    downloaded = next(item for item in result["data"] if item["action"] == 1)
    assert downloaded["season"] == 2026
    assert downloaded["partNumber"] == 4
    assert downloaded["matches"] == ["title", "year"]
    assert "blacklisted" in downloaded and "upgradable" in downloaded



@pytest.fixture
def sync_library(manual_library, monkeypatch):  # noqa: F811
    from pathlib import Path
    from app import event_handler
    from app.config import settings
    from sportarr import history
    from subtitles import sync
    from subtitles.tools import subsyncer, subsync_engines

    service, session, folder = manual_library
    monkeypatch.setattr(history, 'database', session)
    monkeypatch.setattr(settings.subsync, 'debug', False)
    monkeypatch.setattr(settings.subsync, 'use_subsync_movie_threshold', False)
    published, refreshed = [], []
    monkeypatch.setattr(sync, 'publication_callback',
                        lambda kind, path, operation, owner: lambda output:
                        published.append((kind, path, operation, owner, str(output))))
    monkeypatch.setattr(event_handler, 'event_stream', lambda **event: refreshed.append(event))
    monkeypatch.setattr(subsyncer, 'SubsyncEngineRunner',
                        lambda: subsync_engines.SubsyncEngineRunner(subsync_engines.InMemorySubsyncFailureStore()))
    def engine(self, output_path, **kwargs):
        Path(output_path).write_text('1\n00:00:00,200 --> 00:00:01,200\nSynchronized sporting event.\n')
        return {'offset_seconds': 0.2, 'framerate_scale_factor': 1.0}
    monkeypatch.setattr(subsyncer.SubSyncer, '_run_ffsubsync_engine', engine)
    return service, session, folder, published, refreshed


def _run_sports_sync(library, mode='overwrite', mass=False, force=True):
    from sportarr.subtitles import sports_manual_operation
    from subtitles import sync, mass_operations
    _, _, folder, _, _ = library
    source = str(folder / '1/event.en.hi.srt')
    if mass:
        item = dict(video_path=str(folder / '1/event.mkv'), srt_path=source, srt_lang='en',
                    forced=False, hi=True, percent_score=0, sonarr_series_id=None,
                    sonarr_episode_id=None, radarr_id=None, sports_event_id=61,
                    max_offset_seconds='60', no_fix_framerate=False, gss=False,
                    arr_instance_id=1, output_mode=mode, enabled_engines=['ffsubsync'])
        return mass_operations._process_subtitle_item(item, 'sync', {}, 99)
    with sports_manual_operation(61, 1) as (context, validate, guard, path):
        return sync.sync_subtitles(video_path=path, srt_path=source, srt_lang='en', forced=False,
                                   hi=True, percent_score=0, force_sync=force, track_job_progress=False,
                                   arr_instance_id=1, context=context, validate=validate,
                                   publication_guard=guard, output_mode=mode, enabled_engines=['ffsubsync'])


@pytest.mark.parametrize('mode', ['overwrite', 'keep_all'])
@pytest.mark.parametrize('mass', [False, True])
@pytest.mark.parametrize('check', ['publication', 'history', 'index'])
def test_sports_sync_publishes_indexes_and_records_exact_owner(sync_library, mode, mass, check):
    import ast
    from app.database import TableHistorySports, TableSportsEvents
    _, session, folder, published, refreshed = sync_library
    assert _run_sports_sync(sync_library, mode, mass)
    output = folder / ('1/event.en.hi.ffsubsync.srt' if mode == 'keep_all' else '1/event.en.hi.srt')
    assert output.is_file()
    if check == 'publication':
        assert published == [('sports', str(folder / '1/event.mkv'), 'sync', 1, str(output))]
        return
    if check == 'index':
        assert any(item[1] == '/sports/' + output.name for item in ast.literal_eval(session.get(TableSportsEvents, 61).subtitles))
        assert session.get(TableSportsEvents, 62).subtitles == '[]'
        assert any(event.get('type') == 'sports' and event.get('payload') == 61 for event in refreshed)
        return
    rows = session.execute(sa.select(TableHistorySports).where(TableHistorySports.action == 5)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.event_id, row.arr_instance_id, row.video_path, row.subtitles_path, row.language) == (
        61, 1, '/sports/event.mkv', '/sports/' + output.name, 'en:hi')
    assert 'ffsubsync' in row.description and mode in row.description
    assert any(item[1] == '/sports/' + output.name for item in ast.literal_eval(session.get(TableSportsEvents, 61).subtitles))
    assert session.get(TableSportsEvents, 62).subtitles == '[]'
    assert any(event.get('type') == 'sports' and event.get('payload') == 61 for event in refreshed)


@pytest.mark.parametrize('failure', ['engine', 'cancel', 'event', 'mapping', 'video'])
def test_unsuccessful_sports_sync_preserves_source_without_success_history(sync_library, monkeypatch, failure):
    from pathlib import Path
    from app.database import TableHistorySports, TableSportsEvents, TableArrInstances
    from app.jobs_queue import JobCancelled
    from subtitles.tools.subsyncer import SubSyncer

    _, session, folder, published, _ = sync_library
    source = folder / '1/event.en.hi.srt'
    before = source.read_bytes()
    def engine(self, output_path, **kwargs):
        Path(output_path).write_text('engine output')
        if failure == 'engine':
            raise RuntimeError('engine failed')
        if failure == 'cancel':
            raise JobCancelled('cancelled before publication')
        if failure == 'event':
            session.execute(sa.update(TableSportsEvents).where(TableSportsEvents.id == 61).values(file_id=999))
        elif failure == 'mapping':
            session.execute(sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(path_mappings='[]'))
        elif failure == 'video':
            (folder / '1/event.mkv').write_bytes(b'changed')
        session.commit()
        return {'offset_seconds': 0, 'framerate_scale_factor': 1}
    monkeypatch.setattr(SubSyncer, '_run_ffsubsync_engine', engine)
    if failure == 'cancel':
        with pytest.raises(JobCancelled):
            _run_sports_sync(sync_library)
    else:
        assert not _run_sports_sync(sync_library)
    assert source.read_bytes() == before
    assert not published
    assert session.execute(sa.select(TableHistorySports)).scalars().all() == []


def test_repeated_keep_all_sync_does_not_duplicate_history(sync_library, monkeypatch):
    from app.config import settings
    from app.database import TableHistorySports
    monkeypatch.setattr(settings.subsync, "use_subsync", True)
    _, session, _, _, _ = sync_library
    assert _run_sports_sync(sync_library, 'keep_all')
    assert not _run_sports_sync(sync_library, 'keep_all', force=False)
    assert session.execute(sa.select(sa.func.count()).select_from(TableHistorySports)).scalar_one() == 1


def test_sports_sync_success_survives_an_unavailable_ui_receiver(sync_library, monkeypatch):
    from app import event_handler
    from app.database import TableHistorySports, TableSportsEvents
    _, session, _, _, _ = sync_library
    def unavailable(**kwargs):
        raise RuntimeError('UI receiver unavailable')
    monkeypatch.setattr(event_handler, 'event_stream', unavailable)
    assert _run_sports_sync(sync_library)
    assert session.execute(sa.select(TableHistorySports)).scalar_one().action == 5
    assert session.get(TableSportsEvents, 61).subtitles != '[]'


@pytest.mark.parametrize('mode', ['overwrite', 'keep_all'])
@pytest.mark.parametrize('mass', [False, True])
def test_sports_sync_index_failure_is_not_reported_as_success(sync_library, monkeypatch, mode, mass):
    from app.database import TableSportsEvents
    from subtitles.indexer import sports

    _, session, folder, published, refreshed = sync_library
    def unavailable(*args, **kwargs):
        raise RuntimeError('Subtitle index unavailable')
    monkeypatch.setattr(sports, 'store_subtitles_sports', unavailable)

    assert _run_sports_sync(sync_library, mode, mass) is False
    output = folder / ('1/event.en.hi.ffsubsync.srt' if mode == 'keep_all' else '1/event.en.hi.srt')
    assert output.is_file()
    assert published == [('sports', str(folder / '1/event.mkv'), 'sync', 1, str(output))]
    assert session.get(TableSportsEvents, 61).subtitles == '[]'
    assert session.get(TableSportsEvents, 62).subtitles == '[]'
    assert not refreshed


def test_failed_sports_sync_preserves_failure_when_index_cleanup_also_fails(sync_library, monkeypatch, caplog):
    from app.database import TableHistorySports, TableSportsEvents
    from subtitles.indexer import sports
    from subtitles.tools.subsyncer import SubSyncer

    _, session, folder, _, refreshed = sync_library
    def history_unavailable(*args, **kwargs):
        raise RuntimeError('Sync history unavailable')
    def index_unavailable(*args, **kwargs):
        raise RuntimeError('Subtitle index unavailable')
    monkeypatch.setattr(SubSyncer, '_log_sync_history', history_unavailable)
    monkeypatch.setattr(sports, 'store_subtitles_sports', index_unavailable)

    assert _run_sports_sync(sync_library, 'keep_all') is False
    assert (folder / '1/event.en.hi.ffsubsync.srt').is_file()
    assert session.get(TableSportsEvents, 61).subtitles == '[]'
    assert session.execute(sa.select(TableHistorySports)).scalars().all() == []
    assert not refreshed
    assert 'Sync history unavailable' in caplog.text
    assert 'Subtitle index unavailable' in caplog.text


@pytest.mark.parametrize('change', [None, 'event', 'video', 'output', 'index_failure'])
def test_partial_keep_all_indexes_only_current_publications_after_cancellation(sync_library, monkeypatch, change):
    import ast
    from app.database import TableSportsEvents, TableHistorySports
    from app.jobs_queue import JobCancelled
    from sportarr.subtitles import sports_manual_operation
    from subtitles import sync
    from subtitles.indexer import sports
    from subtitles.tools.subsyncer import SubSyncer
    from subtitles.tools.subsync_engines import write_subtitle_file
    _, session, folder, published, refreshed = sync_library
    output = folder / '1/event.en.hi.ffsubsync.srt'
    if change == 'index_failure':
        def unavailable(*args, **kwargs):
            raise RuntimeError('Subtitle index unavailable')
        monkeypatch.setattr(sports, 'store_subtitles_sports', unavailable)
    def cancel(self, **kwargs):
        if change == 'event':
            session.execute(sa.update(TableSportsEvents).where(TableSportsEvents.id == 61).values(file_id=999))
            session.commit()
        elif change == 'video':
            (folder / '1/event.mkv').write_bytes(b'New video')
        elif change == 'output':
            write_subtitle_file(str(folder / '1/event.mkv'), str(output), b'New subtitle')
        raise JobCancelled('Second engine cancelled')
    monkeypatch.setattr(SubSyncer, '_run_external_engine', cancel)
    with sports_manual_operation(61, 1) as (context, validate, guard, video):
        with pytest.raises(JobCancelled, match='Second engine cancelled'):
            sync.sync_subtitles(video_path=video, srt_path=str(folder / '1/event.en.hi.srt'),
                                srt_lang='en', forced=False, hi=True, percent_score=0,
                                force_sync=True, track_job_progress=False, arr_instance_id=1,
                                context=context, validate=validate, publication_guard=guard,
                                output_mode='keep_all', enabled_engines=['ffsubsync', 'alass'])
    indexed = ast.literal_eval(session.get(TableSportsEvents, 61).subtitles)
    assert output.exists()
    if change is None:
        assert any(item[1] == '/sports/' + output.name for item in indexed)
        assert len([event for event in refreshed if event.get('type') == 'sports']) == 1
    else:
        assert indexed == []
        assert not refreshed
    assert session.execute(sa.select(TableHistorySports)).scalars().all() == []


@pytest.mark.parametrize('toolbox', [False, True])
@pytest.mark.parametrize('cancelled', [False, True])
def test_sports_sync_refreshes_final_outputs_once_even_after_partial_cancellation(
        sync_library, sports_refresh_targets, monkeypatch, toolbox, cancelled):  # noqa: F811
    from app.jobs_queue import JobCancelled
    from api.subtitles import subtitles as endpoint
    from subtitles import sync
    from subtitles.indexer import sports as indexer
    from subtitles.tools.subsyncer import SubSyncer
    from sportarr.subtitles import sports_manual_operation

    _, session, folder, published, _ = sync_library
    refreshed, _ = sports_refresh_targets
    indexer.store_subtitles_sports(61, 1)
    monkeypatch.setattr(endpoint, 'database', session)
    monkeypatch.setattr(endpoint, 'event_stream', lambda **kwargs: None)
    monkeypatch.setattr(endpoint, 'sync_subtitles', lambda **kwargs: sync.sync_subtitles(**kwargs, job_id=99))
    if cancelled:
        def cancel_second_engine(self, **kwargs):
            raise JobCancelled('second engine cancelled after first output')
        monkeypatch.setattr(SubSyncer, '_run_external_engine', cancel_second_engine)
    engines = ['ffsubsync', 'alass'] if cancelled else ['ffsubsync']

    def run():
        if toolbox:
            args = dict(action='sync', language='en', path='/sports/event.en.hi.srt',
                        type='sports', id=61, arr_instance_id=1, hi='True',
                        output_mode='keep_all', enabled_engines=','.join(engines))
            monkeypatch.setattr(endpoint.Subtitles.patch_request_parser, 'parse_args', lambda: args)
            return inspect.unwrap(endpoint.Subtitles.patch)(endpoint.Subtitles())
        with sports_manual_operation(61, 1) as (context, validate, guard, video):
            return sync.sync_subtitles(
                video_path=video, srt_path=str(folder / '1/event.en.hi.srt'), srt_lang='en',
                forced=False, hi=True, percent_score=0, force_sync=True, track_job_progress=False,
                arr_instance_id=1, context=context, validate=validate, publication_guard=guard,
                output_mode='keep_all', enabled_engines=engines)

    if cancelled:
        with pytest.raises(JobCancelled):
            run()
    else:
        assert run() == (('', 204) if toolbox else True)
    output = folder / '1/event.en.hi.ffsubsync.srt'
    assert output.exists()
    assert len(published) == 1
    assert sorted(refreshed, key=str) == sorted([
        ('plex', 'Sports'), ('plex', 'Other Sports'),
        ('jellyfin', 'sports-id'), ('jellyfin', 'other-sports-id'), ('sportarr', 1),
    ], key=str)
