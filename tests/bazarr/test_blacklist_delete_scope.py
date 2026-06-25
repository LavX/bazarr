# coding=utf-8
"""Blacklist delete must never silently fan out across instances (#156).

Finding 4: ``blacklist_delete`` / ``blacklist_delete_movie`` wrap a scoped()
DELETE keyed only on (provider, subs_id). When ``arr_instance_id`` is None the
scope is a no-op, so a provider+subs_id pair shared across two instances gets
deleted in EVERY instance. The fix must refuse an unscoped fan-out when more
than one instance owns the same key, while keeping single-instance behaviour
identical.
"""

from datetime import datetime

from app.database import TableBlacklist, TableBlacklistMovie


def _seed_movie_blacklist(db):
    # Same provider+subs_id blacklisted under TWO instances (2 and 3).
    for bid, owner in [(1, 2), (2, 3)]:
        db.add(TableBlacklistMovie(
            id=bid, movie_id=None, radarr_id=99, arr_instance_id=owner,
            provider="provider-a", subs_id="shared-sub", language="en",
            timestamp=datetime(2026, 6, 12, 12, 0, bid)))
    db.flush()


def test_movie_blacklist_delete_scoped_removes_only_target(schema_session, monkeypatch):
    from radarr import blacklist

    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist, "event_stream", lambda *a, **k: None)
    _seed_movie_blacklist(schema_session)

    blacklist.blacklist_delete_movie(provider="provider-a", subs_id="shared-sub",
                                     arr_instance_id=2)

    remaining = schema_session.execute(
        TableBlacklistMovie.__table__.select()).fetchall()
    owners = sorted(r.arr_instance_id for r in remaining)
    assert owners == [3], f"only instance 2's row should be gone; left {owners}"


def test_movie_blacklist_delete_unscoped_refuses_fanout(schema_session, monkeypatch):
    from radarr import blacklist

    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist, "event_stream", lambda *a, **k: None)
    _seed_movie_blacklist(schema_session)

    # arr_instance_id None + a multi-instance collision: MUST NOT wipe both rows.
    blacklist.blacklist_delete_movie(provider="provider-a", subs_id="shared-sub",
                                     arr_instance_id=None)

    remaining = schema_session.execute(
        TableBlacklistMovie.__table__.select()).fetchall()
    owners = sorted(r.arr_instance_id for r in remaining)
    assert owners == [2, 3], (
        f"unscoped delete must refuse the cross-instance fan-out; left {owners}")


def test_movie_blacklist_delete_single_instance_legacy_unaffected(schema_session, monkeypatch):
    from radarr import blacklist

    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist, "event_stream", lambda *a, **k: None)
    # one row, NULL owner (pre-backfill / single default install)
    schema_session.add(TableBlacklistMovie(
        id=1, movie_id=None, radarr_id=99, arr_instance_id=None,
        provider="provider-a", subs_id="shared-sub", language="en",
        timestamp=datetime(2026, 6, 12, 12, 0, 1)))
    schema_session.flush()

    blacklist.blacklist_delete_movie(provider="provider-a", subs_id="shared-sub",
                                     arr_instance_id=None)

    remaining = schema_session.execute(
        TableBlacklistMovie.__table__.select()).fetchall()
    assert remaining == [], "legacy single-instance delete must still work"


def _seed_episode_blacklist(db):
    for bid, owner in [(1, 8), (2, 9)]:
        db.add(TableBlacklist(
            id=bid, episode_id=None, series_id=None, sonarr_series_id=5,
            sonarr_episode_id=55, arr_instance_id=owner, provider="provider-a",
            subs_id="shared-sub", language="en",
            timestamp=datetime(2026, 6, 12, 12, 0, bid)))
    db.flush()


def test_episode_blacklist_delete_scoped_removes_only_target(schema_session, monkeypatch):
    from sonarr import blacklist

    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist, "event_stream", lambda *a, **k: None)
    _seed_episode_blacklist(schema_session)

    blacklist.blacklist_delete(provider="provider-a", subs_id="shared-sub",
                               arr_instance_id=8)

    remaining = schema_session.execute(
        TableBlacklist.__table__.select()).fetchall()
    owners = sorted(r.arr_instance_id for r in remaining)
    assert owners == [9], f"only instance 8's row should be gone; left {owners}"


def test_episode_blacklist_delete_unscoped_refuses_fanout(schema_session, monkeypatch):
    from sonarr import blacklist

    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist, "event_stream", lambda *a, **k: None)
    _seed_episode_blacklist(schema_session)

    blacklist.blacklist_delete(provider="provider-a", subs_id="shared-sub",
                               arr_instance_id=None)

    remaining = schema_session.execute(
        TableBlacklist.__table__.select()).fetchall()
    owners = sorted(r.arr_instance_id for r in remaining)
    assert owners == [8, 9], (
        f"unscoped episode delete must refuse the fan-out; left {owners}")


def test_episode_blacklist_delete_single_instance_legacy_unaffected(schema_session, monkeypatch):
    from sonarr import blacklist

    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist, "event_stream", lambda *a, **k: None)
    schema_session.add(TableBlacklist(
        id=1, episode_id=None, series_id=None, sonarr_series_id=5,
        sonarr_episode_id=55, arr_instance_id=None, provider="provider-a",
        subs_id="shared-sub", language="en",
        timestamp=datetime(2026, 6, 12, 12, 0, 1)))
    schema_session.flush()

    blacklist.blacklist_delete(provider="provider-a", subs_id="shared-sub",
                               arr_instance_id=None)

    remaining = schema_session.execute(
        TableBlacklist.__table__.select()).fetchall()
    assert remaining == [], "legacy single-instance delete must still work"


def test_movie_blacklist_delete_unscoped_refuses_mixed_null_owner(schema_session, monkeypatch):
    # A legacy NULL-owner row AND an instance-owned row share the key. SQL
    # COUNT(DISTINCT) ignores NULLs, so without a COALESCE the guard sees one
    # owner and fans out the delete across both. The fix must treat NULL as its
    # own distinct owner and refuse.
    from radarr import blacklist

    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist, "event_stream", lambda *a, **k: None)
    schema_session.add(TableBlacklistMovie(
        id=1, radarr_id=99, arr_instance_id=None, provider="p", subs_id="s",
        language="en", timestamp=datetime(2026, 6, 12, 12, 0, 1)))
    schema_session.add(TableBlacklistMovie(
        id=2, radarr_id=99, arr_instance_id=5, provider="p", subs_id="s",
        language="en", timestamp=datetime(2026, 6, 12, 12, 0, 2)))
    schema_session.flush()

    blacklist.blacklist_delete_movie(provider="p", subs_id="s", arr_instance_id=None)

    remaining = schema_session.execute(TableBlacklistMovie.__table__.select()).fetchall()
    assert len(remaining) == 2, "mixed NULL+owner duplicate must not be fanned-out deleted"
