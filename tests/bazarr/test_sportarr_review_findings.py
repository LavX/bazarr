# coding=utf-8
"""One test per defect a full review of the Sportarr branch turned up.

The branch had been through three rounds of automated review, so what is left
here is what those rounds do not reach: a reindex that turns a completed write
into a failure, a commit that lands before the probe it describes, a gate that
one caller enforces and its neighbours do not, a cleanup that skips the bulk
paths, and a trigger that taxes installs which never asked for the feature.
"""

from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------
# History statistics: a filter the parser hands over as a string.
# --------------------------------------------------------------------------

def test_history_stats_rejects_a_non_numeric_action():
    """`action` is parsed as a string with an 'All' sentinel, so anything else
    may still not be a number. int() on it raised out of the handler."""
    from api.history.stats import HistoryStats

    resource = HistoryStats()
    parsed = {'timeFrame': 'month', 'action': 'not-a-number', 'provider': 'All', 'language': 'All'}
    resource.get_request_parser = SimpleNamespace(parse_args=lambda: parsed)

    # authenticate wraps the handler; call the function it wrapped.
    handler = HistoryStats.get
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    body, code = handler(resource)
    assert code == 400
    assert 'number' in body


# --------------------------------------------------------------------------
# Sports path mappings: one unusable row must not void the table.
# --------------------------------------------------------------------------

def test_one_incomplete_mapping_row_does_not_void_the_rest():
    """The settings page's Add button appends an empty pair, and the config
    layer persists what was posted, so a table saved mid-edit always carries
    one. Refusing the whole table left every sports path unmapped silently."""
    from utilities.path_mappings import usable_sports_mappings

    assert usable_sports_mappings(
        [['/remote', '/local'], ['', ''], ['/other', '/here']], 'global') == [
        ['/remote', '/local'], ['/other', '/here']]


def test_a_duplicated_prefix_drops_only_the_later_row():
    from utilities.path_mappings import usable_sports_mappings

    assert usable_sports_mappings(
        [['/remote', '/local'], ['/remote/', '/elsewhere'], ['/other', '/here']], 'global') == [
        ['/remote', '/local'], ['/other', '/here']]


def test_a_usable_global_table_is_unchanged():
    from utilities.path_mappings import usable_sports_mappings

    table = [['/remote', '/local'], ['/other', '/here']]
    assert usable_sports_mappings(table, 'global') == table


# --------------------------------------------------------------------------
# The enabled-owner gate, applied by every sports lookup or by none.
# --------------------------------------------------------------------------

@pytest.fixture
def disabled_owner(schema_session):
    """An event whose Sportarr instance has been switched off."""
    from app.database import (TableArrInstances, TableSportsEvents, TableSportsLeagues)

    schema_session.add(TableArrInstances(
        id=42, kind='sportarr', name='Sports', stable_key='s', port=1867, enabled=0))
    schema_session.flush()
    schema_session.add(TableSportsLeagues(
        id=7, arr_instance_id=42, sportarrLeagueId=1, title='League'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=11, arr_instance_id=42, league_id=7, sportarrEventId=2, file_id=3,
        path='/sportarr/Event.mkv', title='Event', audio_language='[]',
        subtitles="[['en', '/sportarr/Event.en.srt', 10]]", missing_subtitles='[]',
        failedAttempts='[]'))
    schema_session.commit()
    return schema_session


def test_sync_references_answers_empty_for_a_disabled_owner(disabled_owner, monkeypatch):
    """The sports path mapping has no global fallback and raises for a disabled
    owner, and this function's only API caller has no try/except, so the manual
    sync modal answered 500 where the row is simply not reachable."""
    from utilities import video_analyzer

    monkeypatch.setattr(video_analyzer, 'database', disabled_owner)
    monkeypatch.setattr(video_analyzer, 'parse_video_metadata',
                        lambda *a, **kw: pytest.fail('a disabled owner must not be probed'))

    assert video_analyzer.subtitles_sync_references('', sports_event_id=11) == {
        'audio_tracks': [], 'embedded_subtitles_tracks': [], 'external_subtitles_tracks': []}


def test_media_metadata_answers_nothing_for_a_disabled_owner(disabled_owner, monkeypatch):
    """resolve_subtitle_path joins to an enabled owner and says why. This one
    did not, so it answered 200 with the league title, the event title and the
    raw Sportarr-side path past the gate every other sports endpoint applies,
    and the follow-up create then 404d."""
    from api.subtitles import content

    monkeypatch.setattr(content, 'database', disabled_owner)
    assert content._get_media_metadata('sports', 11) is None


def test_resolve_subtitle_path_and_media_metadata_agree(disabled_owner, monkeypatch):
    from api.subtitles import content

    monkeypatch.setattr(content, 'database', disabled_owner)
    assert content.resolve_subtitle_path('sports', 11, 'en')[1] == 404


# --------------------------------------------------------------------------
# The publication boundary: contention is not a database fault.
# --------------------------------------------------------------------------

def test_a_caller_fault_inside_the_guard_keeps_its_own_error(monkeypatch):
    """The except spans the yield, so an OperationalError raised by the body
    inside the guard was relabelled "Subtitle destination owners are busy",
    hiding a real database fault behind lock-contention wording."""
    from contextlib import contextmanager

    from sqlalchemy.exc import OperationalError

    from sportarr import subtitles as sports_subtitles

    fault = OperationalError('SELECT 1', {}, Exception('disk I/O error'))

    @contextmanager
    def transaction(*args, **kwargs):
        yield SimpleNamespace(execute=lambda *a, **kw: SimpleNamespace(scalar_one=lambda: None),
                              flush=lambda: None)

    monkeypatch.setattr(sports_subtitles, 'sports_transaction', transaction)
    monkeypatch.setattr(sports_subtitles, 'lock_output_owners', lambda *a, **kw: None)
    monkeypatch.setattr(sports_subtitles, 'validate_context', lambda *a, **kw: None)
    monkeypatch.setattr(sports_subtitles, '_signature', lambda *a, **kw: 'sig')
    monkeypatch.setattr(sports_subtitles, 'SportsOutputNamespace',
                        lambda *a, **kw: SimpleNamespace(validate=lambda session: None))

    context = SimpleNamespace(event_id=11, league_id=7, arr_instance_id=42)
    with pytest.raises(OperationalError) as raised:
        with sports_subtitles.sports_file_publication(context, 'sig'):
            raise fault
    assert raised.value is fault


def test_guard_contention_is_reported_as_a_retryable_busy_error(monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy.exc import OperationalError

    from sportarr import subtitles as sports_subtitles
    from sportarr.errors import SportsOwnersBusy

    # Shaped like the real thing: PostgreSQL reports a NOWAIT lock it could not
    # take as 55P03. A bare OperationalError with no code is a database fault,
    # not contention, and must not be answered as busy.
    class Contended(Exception):
        pgcode = '55P03'

    @contextmanager
    def transaction(*args, **kwargs):
        raise OperationalError('LOCK TABLE', {}, Contended('could not obtain lock'))
        yield  # pragma: no cover

    monkeypatch.setattr(sports_subtitles, 'sports_transaction', transaction)
    monkeypatch.setattr(sports_subtitles, 'SportsOutputNamespace',
                        lambda *a, **kw: SimpleNamespace(validate=lambda session: None))

    context = SimpleNamespace(event_id=11, league_id=7, arr_instance_id=42)
    with pytest.raises(SportsOwnersBusy):
        with sports_subtitles.sports_file_publication(context, 'sig'):
            pytest.fail('the body must never run')
    # Subclasses ValueError so every handler that already maps one keeps working.
    assert issubclass(SportsOwnersBusy, ValueError)


# --------------------------------------------------------------------------
# The provider slot, when the worker that would free it never runs.
# --------------------------------------------------------------------------

def test_a_thread_that_cannot_start_leaks_no_provider_slot(monkeypatch):
    """terminate() and release() live only in the worker's finally, but the
    pool is built and the slot taken before the thread starts. Four failures
    would have left no slot for any later sports search."""
    from sportarr import automatic

    terminated = []
    pool = SimpleNamespace(terminate=lambda: terminated.append(True))

    class Unstartable:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(automatic, 'Thread', Unstartable)
    before = automatic._provider_slots._value

    with pytest.raises(RuntimeError):
        automatic._provider_result(object(), set(), pool, 0, {'originalFormat': 0}, None)

    assert terminated == [True]
    assert automatic._provider_slots._value == before


# --------------------------------------------------------------------------
# Ownership triggers: the tax an install that never enables Sportarr pays.
# --------------------------------------------------------------------------

def _trigger_names(session):
    from sqlalchemy import text
    return {name for (name,) in session.execute(
        text("SELECT name FROM sqlite_master WHERE type='trigger'")).all()}


def test_no_sportarr_instance_installs_no_ownership_triggers(schema_session):
    """OWNER_TABLES covers table_episodes and table_movies, the two busiest
    tables in the schema, and every row change on them takes one global
    revision row, serialising unrelated writers."""
    from app.ownership_revision import install_ownership_revision

    install_ownership_revision(schema_session.connection())
    assert not any(name.startswith('ownership_revision') for name in _trigger_names(schema_session))


def test_a_sportarr_instance_installs_them(schema_session):
    from app.database import TableArrInstances
    from app.ownership_revision import OWNER_TABLES, install_ownership_revision

    schema_session.add(TableArrInstances(
        id=42, kind='sportarr', name='Sports', stable_key='s', port=1867, enabled=1))
    schema_session.commit()

    install_ownership_revision(schema_session.connection())
    installed = _trigger_names(schema_session)
    for table in OWNER_TABLES:
        for action in ('insert', 'update', 'delete'):
            assert any(table in name and action in name.lower() for name in installed), (
                f'no {action} trigger for {table}')


def test_the_publication_boundary_installs_them_when_it_needs_them(schema_session):
    """Startup cannot be the only place that decides: the first instance is
    created from a running process, and a migration that rebuilds a table drops
    the triggers under a process that is already up."""
    from app.database import TableArrInstances
    from app.ownership_revision import (ensure_ownership_protection, install_ownership_revision,
                                        verify_ownership_protection)

    install_ownership_revision(schema_session.connection())
    with pytest.raises(ValueError):
        verify_ownership_protection(schema_session)

    schema_session.add(TableArrInstances(
        id=42, kind='sportarr', name='Sports', stable_key='s', port=1867, enabled=1))
    schema_session.commit()

    ensure_ownership_protection(schema_session)
    verify_ownership_protection(schema_session)


def test_a_disabled_sportarr_instance_still_keeps_them(schema_session):
    """An instance switched off for an evening still owns rows whose ownership
    must stay tracked, and reinstalling on every toggle would churn the
    snapshot generation for nothing."""
    from app.database import TableArrInstances
    from app.ownership_revision import install_ownership_revision, verify_ownership_protection

    schema_session.add(TableArrInstances(
        id=42, kind='sportarr', name='Sports', stable_key='s', port=1867, enabled=0))
    schema_session.commit()

    install_ownership_revision(schema_session.connection())
    verify_ownership_protection(schema_session)


# --------------------------------------------------------------------------
# Bulk deletes and the mismatch rows nothing cascades.
# --------------------------------------------------------------------------

def test_pruning_a_league_forgets_its_events_mismatches(schema_session):
    """release_type_mismatches has no foreign key and matches on media_id
    alone, so SQLite reusing a deleted event id badges a new recording with a
    mismatch it never earned, and the dedup then suppresses its real one."""
    from sqlalchemy import select

    from app.database import (TableArrInstances, TableReleaseTypeMismatch,
                              TableSportsEvents, TableSportsLeagues)
    from sportarr.sync.leagues import _prune_leagues

    schema_session.add(TableArrInstances(
        id=42, kind='sportarr', name='Sports', stable_key='s', port=1867, enabled=1))
    schema_session.flush()
    schema_session.add(TableSportsLeagues(
        id=7, arr_instance_id=42, sportarrLeagueId=1, title='Gone'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=11, arr_instance_id=42, league_id=7, sportarrEventId=2, file_id=3,
        path='/sportarr/Event.mkv', title='Event', audio_language='[]',
        subtitles='[]', missing_subtitles='[]', failedAttempts='[]'))
    schema_session.add(TableReleaseTypeMismatch(
        media_type='sports', media_id=11, arr_instance_id=42, language='en',
        video_release_type='web', subtitle_release_type='bluray'))
    schema_session.commit()

    _prune_leagues(schema_session, 42, [999])
    schema_session.commit()

    assert schema_session.execute(select(TableSportsEvents)).scalars().all() == []
    assert schema_session.execute(select(TableReleaseTypeMismatch)).scalars().all() == []


def test_a_failed_event_mismatch_cleanup_does_not_abort_the_prune(schema_session, monkeypatch):
    """The cleanup and the deletes share one transaction, and on PostgreSQL a
    failed statement aborts it, so swallowing the failure took the deletes down
    with "current transaction is aborted" and killed the whole league sync."""
    from sqlalchemy import select

    from app.database import (TableArrInstances, TableSportsEvents, TableSportsLeagues)
    from sportarr.sync import events as sports_events
    from subtitles import mismatch

    schema_session.add(TableArrInstances(
        id=42, kind='sportarr', name='Sports', stable_key='s', port=1867, enabled=1))
    schema_session.flush()
    schema_session.add(TableSportsLeagues(
        id=7, arr_instance_id=42, sportarrLeagueId=1, title='League'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=11, arr_instance_id=42, league_id=7, sportarrEventId=2, file_id=3,
        path='/sportarr/Event.mkv', title='Event', audio_language='[]',
        subtitles='[]', missing_subtitles='[]', failedAttempts='[]'))
    schema_session.commit()

    nested = []

    def explode(session, *args, **kwargs):
        # SQLite would carry on regardless, so the point being pinned is that
        # the failing statement is contained: PostgreSQL only rolls back to the
        # savepoint instead of aborting the transaction the deletes need.
        nested.append(session().in_nested_transaction()
                      if hasattr(session, 'registry') else session.in_nested_transaction())
        raise RuntimeError('cleanup failed')

    monkeypatch.setattr(mismatch, 'forget_media', explode)

    sports_events._prune_events(schema_session, 7, 42, keep=[])
    schema_session.commit()

    assert nested == [True], 'the cleanup must run inside its own savepoint'
    assert schema_session.execute(select(TableSportsEvents)).scalars().all() == []


# --------------------------------------------------------------------------
# The incremental snapshot's IN list.
# --------------------------------------------------------------------------

def test_the_incremental_ownership_read_never_overruns_the_bind_limit(schema_session, monkeypatch):
    """subtitle_ownership_changes accumulates one row per changed media row and
    is pruned only at startup, so a large Sonarr sync between restarts hands
    this more ids than a driver will take bind parameters for.

    Driven rather than read: the previous version of this test asserted that
    the source contained `in_chunks(ids)`, which would have stayed green under
    a chunk size above the limit, a loop over the wrong list, or a rename.
    What matters is the width of the statements that actually reach the driver,
    so that is what is measured.
    """
    from sqlalchemy import event, text

    from app.database import TableEpisodes
    from app.ownership_revision import install_ownership_revision
    from sportarr import output
    from utilities.sql_limits import MAX_IN_CLAUSE

    install_ownership_revision(schema_session.connection())
    # More changed rows than any driver takes bind parameters for in one IN.
    changed = list(range(1, (MAX_IN_CLAUSE * 3) + 7))
    schema_session.execute(
        TableEpisodes.__table__.insert(),
        [{'id': i, 'sonarrEpisodeId': i, 'season': 1, 'episode': i,
          'title': f'E{i}', 'path': f'/tv/E{i}.mkv'} for i in changed])
    # Past the '*' full-resync marker the install writes at the current
    # revision: a cached snapshot older than that one is discarded and rebuilt
    # in full, which reads no IN list at all.
    schema_session.execute(
        text('UPDATE subtitle_ownership_revision SET revision = revision + 1 WHERE id = 1'))
    revision = schema_session.execute(
        text('SELECT revision FROM subtitle_ownership_revision WHERE id = 1')).scalar_one()
    schema_session.execute(
        text('INSERT INTO subtitle_ownership_changes VALUES (:t, :r, :v)'),
        [{'t': 'table_episodes', 'r': i, 'v': revision} for i in changed])
    schema_session.commit()

    # Measured at the driver, not at compile time: SQLAlchemy renders IN as a
    # single expanding parameter and only expands it on the way to the cursor,
    # so a compile-time count is 1 however many ids were handed over.
    widths = []

    @event.listens_for(schema_session.get_bind(), 'before_cursor_execute')
    def record(conn, cursor, statement, parameters, context, executemany):
        if not executemany and parameters is not None:
            widths.append(len(parameters))

    # Seed a cached snapshot so the incremental branch, the one that builds the
    # IN list, is the branch that runs. It has to carry the live generation and
    # a revision at or below the current one, or _snapshot discards it and
    # rebuilds in full, which reads no IN list at all.
    generation = schema_session.execute(text(
        "SELECT revision FROM subtitle_ownership_changes "
        "WHERE table_name='generation' AND row_id=0")).scalar_one()
    output._snapshots[schema_session.get_bind()] = output._OwnershipSnapshot(
        schema_session, output._configuration(), generation, revision - 1)
    from sportarr import db as sports_db
    monkeypatch.setattr(sports_db, 'needs_sports_transaction', lambda session: True)

    try:
        output._snapshot(schema_session, output._configuration(),
                         output.ownership_revision(schema_session))
    finally:
        event.remove(schema_session.get_bind(), 'before_cursor_execute', record)

    assert widths, 'no statement was measured'
    assert max(widths) <= MAX_IN_CLAUSE, (
        f'a statement bound {max(widths)} parameters, over the {MAX_IN_CLAUSE} cap')


# --------------------------------------------------------------------------
# The history phase, which runs after the file is already on disk.
# --------------------------------------------------------------------------

def test_a_contended_history_write_is_retried(monkeypatch):
    """The subtitle is published before this phase runs, and the boundary is
    NOWAIT, so an overlapping Sonarr or Radarr write can fail it outright.
    Losing the row loses the subtitle for good: upgrades walk history and so
    does the blacklist, and nothing writes it later."""
    from sportarr import subtitles as sports_subtitles
    from sportarr.errors import SportsOwnersBusy

    monkeypatch.setattr(sports_subtitles.time, 'sleep', lambda seconds: None)
    attempts = []

    def busy_twice():
        attempts.append(len(attempts))
        if len(attempts) < 3:
            raise SportsOwnersBusy('busy')
        return 'written'

    assert sports_subtitles.retry_while_owners_busy(busy_twice) == 'written'
    assert len(attempts) == 3


def test_a_persistently_contended_history_write_still_reports_failure(monkeypatch):
    """Bounded: the owner tables are busy for as long as one arr write takes,
    so a boundary that never clears is not contention and must be reported."""
    from sportarr import subtitles as sports_subtitles
    from sportarr.errors import SportsOwnersBusy

    monkeypatch.setattr(sports_subtitles.time, 'sleep', lambda seconds: None)
    attempts = []

    def always_busy():
        attempts.append(len(attempts))
        raise SportsOwnersBusy('busy')

    with pytest.raises(SportsOwnersBusy):
        sports_subtitles.retry_while_owners_busy(always_busy)
    assert len(attempts) == sports_subtitles._HISTORY_PUBLICATION_ATTEMPTS


def test_any_other_failure_is_not_retried(monkeypatch):
    from sportarr import subtitles as sports_subtitles

    attempts = []

    def fails():
        attempts.append(len(attempts))
        raise ValueError('Sports file changed. Please try again.')

    with pytest.raises(ValueError):
        sports_subtitles.retry_while_owners_busy(fails)
    assert len(attempts) == 1


# --------------------------------------------------------------------------
# Post-processing: which branch holds the mutation locks, and why.
# --------------------------------------------------------------------------

def test_a_guarded_external_command_runs_without_the_write_locks(monkeypatch, tmp_path):
    """Raised by a reviewer as a violation of the invariant stated in this
    function, and it is not one.

    That invariant covers the unguarded branch, where the command is pointed at
    the published subtitle itself. The guarded branch stages into an O_EXCL file
    nothing else can name and re-takes the locks to publish, so holding them
    across an operator-configured command would only block every other writer
    for as long as that command runs. test_sportarr_subtitle_upload pins the
    consequence: a later writer arriving during post-processing must be able to
    take the coordinator and must survive.
    """
    from contextlib import contextmanager

    from subtitles import post_processing

    video = tmp_path / 'Event.mkv'
    subtitle = tmp_path / 'Event.en.srt'
    video.touch()
    subtitle.write_text('subtitle')
    held = []

    @contextmanager
    def write_locks(path, *paths, **kwargs):
        held.append('acquired')
        try:
            yield {}
        finally:
            held.append('released')

    @contextmanager
    def staged(video_path, destination, **kwargs):
        temporary = tmp_path / 'staged.srt'
        temporary.touch()
        yield str(temporary)

    monkeypatch.setattr(post_processing, 'subtitle_write_locks', write_locks)
    monkeypatch.setattr('subtitles.tools.subsync_engines.staged_subtitle_write', staged)
    monkeypatch.setattr('subtitles.tools.subsync_engines._report_subtitle_publication',
                        lambda *a, **kw: None)
    monkeypatch.setattr(post_processing, '_postprocessing_locked',
                        lambda command, path: held.append('command'))

    post_processing.postprocessing('cmd', str(video), subtitle_path=str(subtitle),
                                   publication_guard=lambda **kw: None,
                                   command_builder=lambda temporary: 'cmd')

    assert held == ['acquired', 'released', 'command']


def test_an_unguarded_external_command_runs_holding_them(monkeypatch, tmp_path):
    """The other half of the same contract: here the command can mutate the
    published subtitle in place, so nothing else may write while it runs."""
    from contextlib import contextmanager

    from subtitles import post_processing

    video = tmp_path / 'Event.mkv'
    video.touch()
    held = []

    @contextmanager
    def write_locks(path, *paths, **kwargs):
        held.append('acquired')
        try:
            yield {}
        finally:
            held.append('released')

    @contextmanager
    def mutation(path, *paths, **kwargs):
        yield

    monkeypatch.setattr(post_processing, 'subtitle_write_locks', write_locks)
    monkeypatch.setattr(post_processing, 'subtitle_mutation', mutation)
    monkeypatch.setattr(post_processing, '_postprocessing_locked',
                        lambda command, path: held.append('command'))

    post_processing.postprocessing('cmd', str(video))

    assert held == ['acquired', 'command', 'released']


# --------------------------------------------------------------------------
# Applying mods: the post-work below the sports pass-through.
# --------------------------------------------------------------------------

def test_applying_mods_to_a_recording_announces_a_sports_event(monkeypatch, tmp_path):
    """The wrapper gained a sports pass-through, but the post-work below it
    re-indexes only episode and movie and fell through to the movie branch for
    sports, announcing a sports event id as a movie id."""
    from app import event_handler
    from subtitles.indexer import sports as sports_indexer
    from subtitles.tools import mods
    from app import jobs_queue as jobs_queue_module

    subtitle = tmp_path / 'Event.en.srt'
    subtitle.write_text('subtitle')
    announced = []
    reindexed = []

    monkeypatch.setattr(mods, 'subtitles_apply_mods', lambda **kwargs: None)
    monkeypatch.setattr(event_handler, 'event_stream',
                        lambda **kwargs: announced.append(kwargs))
    monkeypatch.setattr(sports_indexer, 'store_subtitles_sports',
                        lambda event_id, owner=None, **kw: reindexed.append((event_id, owner)))
    monkeypatch.setattr(jobs_queue_module.jobs_queue, 'update_job_name',
                        lambda **kwargs: None)

    mods.apply_subtitle_mods('en', str(subtitle), ['remove_HI'], str(tmp_path / 'Event.mkv'),
                             media_type='sports', media_id=11, job_id=1, arr_instance_id=42)

    assert reindexed == [(11, 42)]
    assert announced == [{'type': 'sports', 'action': 'update', 'payload': 11}]


# --------------------------------------------------------------------------
# Round two: the fixes above that landed only in part.
# --------------------------------------------------------------------------

def test_the_publication_boundary_does_not_relabel_a_fault_in_its_own_flush(monkeypatch):
    """sports_history only calls session.add, so the history INSERT is emitted
    by the boundary's flush after the yield, not inside the body.

    Resetting the in_body flag before that flush meant a read-only or full
    database still answered "Subtitle destination owners are busy", and the
    retry then repeated it four times.
    """
    from contextlib import contextmanager

    from sqlalchemy.exc import OperationalError

    from sportarr import subtitles as sports_subtitles

    fault = OperationalError('INSERT', {}, Exception('attempt to write a readonly database'))

    class Session:
        def execute(self, *args, **kwargs):
            return SimpleNamespace(scalar_one=lambda: None)

        def flush(self):
            raise fault

    @contextmanager
    def transaction(*args, **kwargs):
        yield Session()

    monkeypatch.setattr(sports_subtitles, 'sports_transaction', transaction)
    monkeypatch.setattr(sports_subtitles, 'lock_output_owners', lambda *a, **kw: None)
    monkeypatch.setattr(sports_subtitles, 'validate_context', lambda *a, **kw: None)
    monkeypatch.setattr(sports_subtitles, '_signature', lambda *a, **kw: 'sig')
    monkeypatch.setattr(sports_subtitles, 'SportsOutputNamespace',
                        lambda *a, **kw: SimpleNamespace(validate=lambda session: None))

    context = SimpleNamespace(event_id=11, league_id=7, arr_instance_id=42)
    with pytest.raises(OperationalError) as raised:
        with sports_subtitles.sports_file_publication(context, 'sig'):
            pass  # the caller adds rows; the boundary flushes them
    assert raised.value is fault


def test_a_failure_after_the_body_ran_is_not_retried(monkeypatch):
    """Retrying is only safe while the caller's rows cannot already have landed.

    An OperationalError from the commit leaves rollback_confirmed False, and on
    PostgreSQL it can mean the connection dropped with the commit in flight. A
    retry would then write a second history row for one download, which is what
    upgrades and the blacklist walk.
    """
    from contextlib import contextmanager

    from sqlalchemy.exc import OperationalError

    from sportarr import subtitles as sports_subtitles
    from sportarr.errors import SportsOwnersBusy

    @contextmanager
    def transaction(*args, **kwargs):
        yield SimpleNamespace(execute=lambda *a, **kw: SimpleNamespace(scalar_one=lambda: None),
                              flush=lambda: None)
        raise OperationalError('COMMIT', {}, Exception('server closed the connection'))

    monkeypatch.setattr(sports_subtitles, 'sports_transaction', transaction)
    monkeypatch.setattr(sports_subtitles, 'lock_output_owners', lambda *a, **kw: None)
    monkeypatch.setattr(sports_subtitles, 'validate_context', lambda *a, **kw: None)
    monkeypatch.setattr(sports_subtitles, '_signature', lambda *a, **kw: 'sig')
    monkeypatch.setattr(sports_subtitles, 'SportsOutputNamespace',
                        lambda *a, **kw: SimpleNamespace(validate=lambda session: None))

    context = SimpleNamespace(event_id=11, league_id=7, arr_instance_id=42)
    with pytest.raises(OperationalError) as raised:
        with sports_subtitles.sports_file_publication(context, 'sig'):
            pass
    # Not SportsOwnersBusy, so retry_while_owners_busy passes it straight
    # through rather than repeating a commit that may have landed.
    assert not isinstance(raised.value, SportsOwnersBusy)


def test_a_cancelled_wait_for_a_provider_slot_frees_the_pool(monkeypatch):
    """The caller terminates the pool on every path that fails before this call
    and the worker's finally frees it after. A stop while waiting for a slot
    falls between the two."""
    from sportarr import automatic

    terminated = []
    pool = SimpleNamespace(terminate=lambda: terminated.append(True))

    class Stopped:
        def is_set(self):
            return True

    # Hold every slot so the acquire loop has to wait.
    held = []
    while automatic._provider_slots.acquire(blocking=False):
        held.append(True)
    try:
        with pytest.raises(ValueError):
            automatic._provider_result(object(), set(), pool, 0, {'originalFormat': 0}, Stopped())
    finally:
        for _ in held:
            automatic._provider_slots.release()

    assert terminated == [True], 'the pool must be terminated when the wait is cancelled'


def test_creating_a_subtitle_survives_a_reindex_that_cannot_run(tmp_path, monkeypatch):
    """The original finding named edit, create and promote. Create kept its
    reindex outside every handler, so it still answered 500 after the file was
    written and published, and the retry then answered 409 against that file.
    """
    from unittest.mock import Mock

    from flask import Flask

    from api.subtitles import content

    video = tmp_path / 'Actual.mkv'
    video.touch()
    subtitle = video.with_suffix('.en.srt')
    metadata = {'mediaPath': '/upstream/Actual.mkv', 'arrInstanceId': 7}
    monkeypatch.setattr(content.path_mappings, 'path_replace_instance',
                        lambda path, owner, kind: str(video))
    monkeypatch.setattr(content, 'resolve_subtitle_path',
                        lambda kind, ident, language, **kwargs: (str(subtitle), metadata))
    monkeypatch.setattr(content, 'get_target_folder', lambda path: None)
    monkeypatch.setattr(content, 'event_stream', lambda **kwargs: None)
    row = Mock(path='/upstream/Actual.mkv', id=4, arr_instance_id=7)
    monkeypatch.setattr(content, 'database',
                        Mock(execute=Mock(return_value=Mock(first=Mock(return_value=row)))))
    # The shape the sports indexer raises on a probe failure or its timeout.
    monkeypatch.setattr(content, 'store_subtitles_movie',
                        Mock(side_effect=OSError('Could not analyze sports video')))

    app = Flask(__name__)
    with app.test_request_context(json={'content': 'Created', 'language': 'en', 'format': 'srt'}):
        result = content._create_subtitle('movie', 42, arr_instance_id=7)

    assert subtitle.exists(), 'the subtitle must still have been created'
    assert result[1] == 201, f'a completed creation must not answer {result[1]}'


def test_a_second_sportarr_instance_does_not_rebuild_the_triggers(schema_session, monkeypatch):
    """install_ownership_revision is a full rebuild: it bumps the revision,
    clears the change log and drops and recreates every trigger. Running it when
    the answer has not changed throws away a valid snapshot and, on PostgreSQL,
    takes ACCESS EXCLUSIVE on the two busiest tables from a request thread."""
    from app.database import TableArrInstances
    from app.ownership_revision import install_ownership_revision
    from arr_instances.repository import ArrInstanceRepository

    schema_session.add(TableArrInstances(
        id=42, kind='sportarr', name='First', stable_key='first', port=1867, enabled=1))
    schema_session.commit()
    install_ownership_revision(schema_session.connection())

    rebuilt = []
    monkeypatch.setattr('app.ownership_revision.install_ownership_revision',
                        lambda connection: rebuilt.append(True))

    repo = ArrInstanceRepository(schema_session)
    repo._refresh_ownership_triggers()

    assert rebuilt == [], 'the triggers already match; nothing should be rebuilt'


# --------------------------------------------------------------------------
# Round three: classifying by what the error is, not where it happened.
# --------------------------------------------------------------------------

def _operational(orig):
    from sqlalchemy.exc import OperationalError
    return OperationalError('SQL', {}, orig)


@pytest.mark.parametrize('pgcode,contention', [
    ('55P03', True),    # NOWAIT lock this boundary asked for and could not take
    ('40001', True),    # serialization failure: the server discarded the whole transaction
    ('40P01', True),    # deadlock victim, likewise discarded
    ('57014', False),   # statement timeout
    ('53100', False),   # disk full
    ('08006', False),   # connection failure, which may have committed
])
def test_only_a_discarded_transaction_counts_as_owner_contention(pgcode, contention):
    """Position was the wrong axis twice.

    Classifying by where the error happened called a plain SELECT failure in
    the pre-body region contention, and called a serialization failure at
    commit unrepeatable when it is the one thing that most wants repeating.
    """
    from sportarr.subtitles import _is_owner_contention

    class Orig(Exception):
        pass

    orig = Orig('boom')
    orig.pgcode = pgcode
    assert _is_owner_contention(_operational(orig)) is contention


def test_sqlite_reports_contention_without_a_code():
    from sportarr.subtitles import _is_owner_contention

    assert _is_owner_contention(_operational(Exception('database is locked')))
    assert not _is_owner_contention(_operational(Exception('attempt to write a readonly database')))


def test_a_serialization_failure_at_commit_is_retried(monkeypatch):
    """SERIALIZABLE is the boundary's isolation level, so 40001 at commit is
    routine. Nothing was written, and losing the history row loses the upgrade
    and blacklist trail for good, so this is the case the retry exists for."""
    from contextlib import contextmanager

    from sqlalchemy.exc import OperationalError

    from sportarr import subtitles as sports_subtitles
    from sportarr.errors import SportsOwnersBusy

    class Serialization(Exception):
        pgcode = '40001'

    @contextmanager
    def transaction(*args, **kwargs):
        yield SimpleNamespace(execute=lambda *a, **kw: SimpleNamespace(scalar_one=lambda: None),
                              flush=lambda: None)
        raise OperationalError('COMMIT', {}, Serialization('could not serialize access'))

    monkeypatch.setattr(sports_subtitles, 'sports_transaction', transaction)
    monkeypatch.setattr(sports_subtitles, 'lock_output_owners', lambda *a, **kw: None)
    monkeypatch.setattr(sports_subtitles, 'validate_context', lambda *a, **kw: None)
    monkeypatch.setattr(sports_subtitles, '_signature', lambda *a, **kw: 'sig')
    monkeypatch.setattr(sports_subtitles, 'SportsOutputNamespace',
                        lambda *a, **kw: SimpleNamespace(validate=lambda session: None))

    context = SimpleNamespace(event_id=11, league_id=7, arr_instance_id=42)
    with pytest.raises(SportsOwnersBusy):
        with sports_subtitles.sports_file_publication(context, 'sig'):
            pass


def test_a_drifted_trigger_set_is_rebuilt_rather_than_left(schema_session):
    """ownership_triggers_present answers "is any one there", which a partial
    or drifted set satisfies while verify_ownership_protection still refuses
    it. Gating on presence alone left the one state the publication boundary
    declines to repair, with nothing else to repair it before a restart."""
    from sqlalchemy import text

    from app.database import TableArrInstances
    from app.ownership_revision import (install_ownership_revision, ownership_triggers_match,
                                        ownership_triggers_present)

    schema_session.add(TableArrInstances(
        id=42, kind='sportarr', name='Sports', stable_key='s', port=1867, enabled=1))
    schema_session.commit()
    install_ownership_revision(schema_session.connection())
    assert ownership_triggers_match(schema_session, True)

    # Drop one of the set, the way an interrupted migration or a restored dump can.
    schema_session.execute(text('DROP TRIGGER ownership_revision_table_movies_insert'))

    assert ownership_triggers_present(schema_session), 'the rest of the set is still there'
    assert not ownership_triggers_match(schema_session, True), (
        'a partial set must not read as already correct')


def test_the_trigger_refresh_does_not_take_a_bare_savepoint(monkeypatch, schema_session):
    """The app session is bound to an AUTOCOMMIT engine, where PostgreSQL
    rejects a bare SAVEPOINT outside a transaction block. sports_transaction is
    the repo's answer to that and nests when a transaction is already open."""
    from arr_instances.repository import ArrInstanceRepository
    from sportarr import db as sports_db

    # Fail the way PostgreSQL does when a bare SAVEPOINT reaches a connection
    # in autocommit, so a refresh that took one would be caught here.
    def refuse_bare_savepoint(*args, **kwargs):
        raise AssertionError('SAVEPOINT can only be used in transaction blocks')

    monkeypatch.setattr(type(schema_session), 'begin_nested', refuse_bare_savepoint,
                        raising=False)

    used = []
    real = sports_db.sports_transaction

    def counted(session, **kwargs):
        used.append(True)
        return real(session, **kwargs)

    monkeypatch.setattr(sports_db, 'sports_transaction', counted)
    ArrInstanceRepository(schema_session)._refresh_ownership_triggers()
    assert used == [True], 'the refresh must go through sports_transaction'
