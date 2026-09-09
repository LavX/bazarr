# ruff: noqa: F401, F811
"""Focused review probes for stale destinations and replacement publication races."""

import json
import pytest
import sqlalchemy as sa
from test_sportarr_workflows import (
    workflow_library,
    manual_library,
    indexed_library,
    migration_engine,
)


def test_absent_old_destination_preserves_current_replacement(workflow_library):
    from app.database import TableHistorySports, TableSportsEvents

    automatic, history, _, _, session, folder = workflow_library
    assert automatic.search_event(61, 1)["status"] == "downloaded"
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    # The historical release is no longer the result offered by the provider.
    session.execute(
        sa.update(TableHistorySports).values(subs_id="examplehub:old-release")
    )
    (folder / "1/event.mkv").rename(folder / "1/replaced.mkv")
    (folder / "1/event.en.srt").rename(folder / "1/replaced.en.srt")
    current = folder / "1/replaced.en.srt"
    current.write_text("newer subtitle for the current video")
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id == 61)
        .values(path="/sports/replaced.mkv")
    )
    before = current.read_bytes()
    result = history.blacklist_history(row.id, 1)
    print(
        json.dumps(
            {
                "case": "absent historical destination",
                "engine": session.get_bind().dialect.name,
                "result": result,
                "current_before": before.decode(),
                "current_after": current.read_text(),
                "old_destination_exists": (folder / "1/event.en.srt").exists(),
                "history_rows": len(
                    session.execute(sa.select(TableHistorySports)).all()
                ),
            }
        )
    )
    assert current.read_bytes() == before, (
        "Blacklisting an absent historical artifact overwrote a newer current subtitle"
    )


def test_blacklist_replacement_preserves_file_installed_during_provider(
    workflow_library, monkeypatch
):
    from app.database import TableHistorySports

    automatic, history, _, _, session, folder = workflow_library
    assert automatic.search_event(61, 1)["status"] == "downloaded"
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    session.execute(
        sa.update(TableHistorySports).values(subs_id="examplehub:old-release")
    )
    current = folder / "1/event.en.srt"
    original = automatic._provider_result
    replacement = b"newer subtitle installed during replacement provider work"

    def changed(*args, **kwargs):
        selected = original(*args)
        assert not current.exists(), "The old artifact should already have been deleted"
        current.write_bytes(replacement)
        return selected

    monkeypatch.setattr(automatic, "_provider_result", changed)
    result = history.blacklist_history(row.id, 1)
    print(
        json.dumps(
            {
                "case": "new artifact during provider work",
                "engine": session.get_bind().dialect.name,
                "result": result,
                "new_artifact": replacement.decode(),
                "current_after": current.read_text(),
                "history_rows": len(
                    session.execute(sa.select(TableHistorySports)).all()
                ),
            }
        )
    )
    assert current.read_bytes() == replacement, (
        "Blacklist replacement overwrote a newer subtitle installed during provider work"
    )


def test_cancellation_after_unlink_keeps_file_and_records_consistent(
    workflow_library, monkeypatch
):
    import pytest
    from threading import Event
    from app.database import TableHistorySports, TableBlacklistSports

    automatic, history, _, _, session, folder = workflow_library
    assert automatic.search_event(61, 1)["status"] == "downloaded"
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    current = folder / "1/event.en.srt"
    original_remove = history.os.remove
    cancelled = Event()

    def remove_then_cancel(path, *args, **kwargs):
        original_remove(path, *args, **kwargs)
        if str(path) == str(current):
            cancelled.set()

    monkeypatch.setattr(history.os, "remove", remove_then_cancel)
    with pytest.raises(ValueError):
        history.blacklist_history(row.id, 1, cancel=cancelled)
    exclusions = list(session.execute(sa.select(TableBlacklistSports)).scalars())
    actions = list(session.execute(sa.select(TableHistorySports.action)).scalars())
    print(
        json.dumps(
            {
                "case": "cancellation after unlink",
                "engine": session.get_bind().dialect.name,
                "file_exists": current.exists(),
                "exclusions": len(exclusions),
                "history_actions": actions,
            }
        )
    )
    assert current.exists() or (exclusions and 0 in actions), (
        "File disappeared while its exclusion and deletion history were rolled back"
    )


@pytest.mark.parametrize(
    "failure", ["owner_disabled", "sql_failure", "destination_conflict"]
)
def test_failed_deletion_recovers_without_overwriting_new_files(
    workflow_library, monkeypatch, failure
):
    from threading import Event
    from app.database import TableArrInstances, TableBlacklistSports, TableHistorySports

    automatic, history, _, _, session, folder = workflow_library
    assert automatic.search_event(61, 1)["status"] == "downloaded"
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    current = folder / "1/event.en.srt"
    before = current.read_bytes()
    mode = current.stat().st_mode
    cancelled = Event()
    real_history = history.sports_history
    real_remove = history.os.remove

    def history_then_fail(writer, *args, **kwargs):
        result = real_history(writer, *args, **kwargs)
        if kwargs.get("action") == 0:
            assert not current.exists()
            writer.flush()
            if failure == "owner_disabled":
                writer.execute(
                    sa.update(TableArrInstances)
                    .where(TableArrInstances.id == 1)
                    .values(enabled=0)
                )
            elif failure == "sql_failure":
                writer.execute(
                    sa.text("SELECT missing_recovery_column FROM table_history_sports")
                )
        return result

    def remove_then_replace(path, *args, **kwargs):
        real_remove(path, *args, **kwargs)
        if str(path) == str(current) and failure == "destination_conflict":
            current.write_bytes(b"new artifact must survive exclusive recovery")
            cancelled.set()

    monkeypatch.setattr(history, "sports_history", history_then_fail)
    monkeypatch.setattr(history.os, "remove", remove_then_replace)
    with pytest.raises(Exception):
        history.blacklist_history(row.id, 1, cancel=cancelled)
    assert list(session.execute(sa.select(TableHistorySports.action)).scalars()) == [1]
    assert not session.execute(sa.select(TableBlacklistSports)).all()
    backups = list(current.parent.glob(".bazarr-sports-recovery-*"))
    if failure == "destination_conflict":
        assert current.read_bytes() == b"new artifact must survive exclusive recovery"
        assert len(backups) == 1 and backups[0].read_bytes() == before
    else:
        assert current.read_bytes() == before
        assert current.stat().st_mode == mode
        assert not backups
        monkeypatch.setattr(history, "sports_history", real_history)
        repeated = history.blacklist_history(row.id, 1)
        assert repeated["replacement"]["status"] == "skipped"
        assert current.read_bytes() == before


@pytest.mark.parametrize("unknown", [False, True])
def test_commit_error_does_not_restore_against_committed_history(
    workflow_library, monkeypatch, unknown
):
    from contextlib import contextmanager
    from app.database import TableHistorySports, TableSportsEvents

    automatic, history, _, _, session, folder = workflow_library
    assert automatic.search_event(61, 1)["status"] == "downloaded"
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    current = folder / "1/event.en.srt"
    before = current.read_bytes()
    real_publication = history.sports_file_publication
    engine = session.get_bind()
    real_connect = engine.connect

    @contextmanager
    def committed_but_error(*args, **kwargs):
        with real_publication(*args, **kwargs) as publication:
            yield publication
        if unknown:

            def unavailable():
                raise OSError("database unavailable while determining commit outcome")

            monkeypatch.setattr(engine, "connect", unavailable)
        raise OSError("deletion commit acknowledgement unavailable")

    monkeypatch.setattr(history, "sports_file_publication", committed_but_error)
    with pytest.raises(OSError) as raised:
        history.blacklist_history(row.id, 1)
    monkeypatch.setattr(engine, "connect", real_connect)
    assert not current.exists()
    assert list(session.execute(sa.select(TableHistorySports.action)).scalars()) == [
        1,
        0,
    ]
    backups = list(current.parent.glob(".bazarr-sports-recovery-*"))
    if unknown:
        assert "uncertain" in str(raised.value).lower()
        assert len(backups) == 1 and backups[0].read_bytes() == before
    else:
        assert not backups
        event = session.get(TableSportsEvents, 61, populate_existing=True)
        assert "en" in event.missing_subtitles


@pytest.mark.parametrize("already_absent", [False, True])
def test_proven_replacement_with_unchanged_destination_still_downloads(
    workflow_library, already_absent
):
    from app.database import TableHistorySports, TableBlacklistSports

    automatic, history, _, _, session, folder = workflow_library
    assert automatic.search_event(61, 1)["status"] == "downloaded"
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    session.execute(
        sa.update(TableHistorySports).values(subs_id="examplehub:old-release")
    )
    current = folder / "1/event.en.srt"
    if already_absent:
        current.unlink()
    result = history.blacklist_history(row.id, 1)
    assert result["file_status"] == ("absent" if already_absent else "deleted")
    assert result["replacement"]["status"] == "downloaded"
    assert b"Sporting event" in current.read_bytes()
    assert not (folder / "2/event.en.srt").exists()
    assert [
        r.arr_instance_id
        for r in session.execute(sa.select(TableBlacklistSports)).scalars()
    ] == [1]
    assert not list(current.parent.glob(".bazarr-sports-recovery-*"))


def test_replacement_skip_is_exposed_by_actual_owned_job(workflow_library, monkeypatch):
    from app.database import TableHistorySports

    automatic, _, workflows, _, session, folder = workflow_library
    assert automatic.search_event(61, 1)["status"] == "downloaded"
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    session.execute(
        sa.update(TableHistorySports).values(subs_id="examplehub:old-release")
    )
    current = folder / "1/event.en.srt"
    provider = automatic._provider_result

    def replace(*args, **kwargs):
        result = provider(*args)
        current.write_bytes(b"new subtitle during owned job")
        return result

    monkeypatch.setattr(automatic, "_provider_result", replace)
    job_id = workflows.blacklist_sports_subtitle(row.id, 1)
    queue = workflows.jobs_queue
    job = queue.jobs_pending_queue.popleft()
    queue.jobs_running_queue.append(job)
    assert queue._run_job(job)
    result = workflows.sports_job_status(session, job_id, 1)
    assert result["status"] == "completed"
    assert result["result"]["replacement"]["status"] == "skipped"
    assert "destination" in result["result"]["replacement"]["message"]
    assert workflows.sports_job_status(session, job_id, 2) is None
    assert current.read_bytes() == b"new subtitle during owned job"


def test_committed_then_owner_cascade_keeps_uncertain_recovery(
    workflow_library, monkeypatch
):
    from contextlib import contextmanager
    from app.database import TableArrInstances, TableHistorySports, TableBlacklistSports

    automatic, history, _, _, session, folder = workflow_library
    assert automatic.search_event(61, 1)["status"] == "downloaded"
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    current = folder / "1/event.en.srt"
    original_bytes = current.read_bytes()
    real_guard = history.sports_file_publication

    @contextmanager
    def committed_then_cascade(*args, **kwargs):
        with real_guard(*args, **kwargs) as publication:
            yield publication
        # The actual owner/file transaction has committed and released its locks.
        assert list(
            session.execute(sa.select(TableHistorySports.action)).scalars()
        ) == [1, 0]
        assert not current.exists()
        # An owner deletion can now commit and cascade the exact receipt away.
        session.execute(sa.delete(TableArrInstances).where(TableArrInstances.id == 1))
        assert not session.execute(sa.select(TableHistorySports)).all()
        raise OSError("deletion commit acknowledgement unavailable")

    monkeypatch.setattr(history, "sports_file_publication", committed_then_cascade)
    with pytest.raises(OSError) as raised:
        history.blacklist_history(row.id, 1)
    backups = list(current.parent.glob(".bazarr-sports-recovery-*"))
    print(
        json.dumps(
            {
                "engine": session.get_bind().dialect.name,
                "owner_exists": session.get(
                    TableArrInstances, 1, populate_existing=True
                )
                is not None,
                "file_restored": current.exists(),
                "restored_bytes_match": current.exists()
                and current.read_bytes() == original_bytes,
                "recovery_count": len(backups),
                "history_count": len(
                    session.execute(sa.select(TableHistorySports)).all()
                ),
                "blacklist_count": len(
                    session.execute(sa.select(TableBlacklistSports)).all()
                ),
                "error": str(raised.value),
            }
        )
    )
    assert "uncertain" in str(raised.value).lower(), (
        "Successful receipt lookup with a missing owner cannot prove rollback"
    )
    assert not current.exists(), (
        "Do not restore against a known possible commit after owner continuity was lost"
    )
    assert len(backups) == 1 and backups[0].read_bytes() == original_bytes


def test_nested_rollback_outcome_is_reset_before_reuse(migration_engine):
    from sqlalchemy.orm import Session
    from sportarr import db

    assert hasattr(db, "SportsTransactionOutcome"), (
        "Recovery needs a confirmed, per-transaction rollback outcome"
    )
    metadata = sa.MetaData()
    table = sa.Table(
        "recovery_transaction_fixture",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    metadata.create_all(migration_engine)
    outcome = db.SportsTransactionOutcome()
    with (
        migration_engine.connect().execution_options(
            isolation_level="SERIALIZABLE"
        ) as connection,
        connection.begin(),
        Session(bind=connection) as session,
    ):
        session.execute(table.insert().values(id=1))
        with pytest.raises(ValueError, match="abort body"):
            with db.sports_transaction(session, outcome=outcome) as writer:
                writer.execute(table.insert().values(id=2))
                raise ValueError("abort body")
        assert outcome.rollback_confirmed
        assert list(session.execute(sa.select(table.c.id)).scalars()) == [1]
        with db.sports_transaction(session, outcome=outcome) as writer:
            writer.execute(table.insert().values(id=3))
        assert not outcome.rollback_confirmed, (
            "A prior rollback must not classify a later commit"
        )
        with db.sports_transaction(session) as writer:
            writer.execute(table.insert().values(id=4))
        assert list(
            session.execute(sa.select(table.c.id).order_by(table.c.id)).scalars()
        ) == [1, 3, 4]
