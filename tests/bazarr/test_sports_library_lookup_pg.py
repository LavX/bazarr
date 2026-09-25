"""The Hub's owned sports-file lookup on the application engine's isolation level.

The application engine runs in AUTOCOMMIT on both backends. A streamed read
(``yield_per`` or ``stream_results``) declares a named server-side cursor on
PostgreSQL, which the server only accepts inside a transaction, so the lookup
raised there on every request. The error was caught and logged at debug level,
which turned every sports hash or filename match into a silent miss. SQLite has
no named cursors, so the SQLite contract suite could not see it.
"""
import json
import logging
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

HASH = "0000000000020000"  # 128 KiB of zero bytes, OpenSubtitles size-plus-words.
RECORDING = "Alpha.Final.2026.mkv"


@pytest.fixture(params=["sqlite", "postgresql"])
def sports_database(request, tmp_path, monkeypatch):
    from app import database as db
    schema = None
    # Built as app/database.py builds the real engine: AUTOCOMMIT on both.
    if request.param == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.fail("Dedicated PostgreSQL test URL is required")
        schema = "sports_lookup_" + uuid.uuid4().hex
        admin = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True,
                                  connect_args={"options": f"-csearch_path={schema}"})
    else:
        admin = None
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'bazarr.db'}", poolclass=NullPool,
                                  isolation_level="AUTOCOMMIT")
    db.Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(db, "database", session)
    try:
        yield session
    finally:
        session.remove()
        engine.dispose()
        if admin is not None:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


@pytest.fixture
def indexed_recording(sports_database, tmp_path, monkeypatch):
    from app.config import settings
    from app.database import TableArrInstances, TableSportsEvents, TableSportsLeagues
    from sportarr.hash_index import refresh_recording_index

    monkeypatch.setattr(settings.general, "subfolder", "current")
    monkeypatch.setattr(settings.general, "subfolder_custom", "")
    directory = tmp_path / "recordings"
    directory.mkdir()
    (directory / RECORDING).write_bytes(b"\0" * 131072)
    (directory / RECORDING.replace(".mkv", ".en.srt")).write_bytes(
        b"1\n00:00:00,000 --> 00:00:01,000\nFinal\n")
    database = sports_database
    database.execute(sa.insert(TableArrInstances).values(
        id=101, kind="sportarr", name="alpha", stable_key="alpha", port=1867, enabled=1,
        is_default=1, path_mappings=json.dumps([["/recordings", str(directory)]])))
    database.execute(sa.insert(TableSportsLeagues).values(
        id=101, arr_instance_id=101, sportarrLeagueId=31, title="League"))
    database.execute(sa.insert(TableSportsEvents).values(
        id=1011, arr_instance_id=101, league_id=101, sportarrLeagueId=31, sportarrEventId=51,
        file_id=77, path="/recordings/" + RECORDING, title="Final",
        sceneName=RECORDING.replace(".mkv", ".Release.mkv"), eventDate="2026-09-08",
        subtitles=repr([["en", "/recordings/" + RECORDING.replace(".mkv", ".en.srt")]])))
    refresh_recording_index(101, session=database)
    return database


def _unavailable(caplog):
    return [record for record in caplog.records
            if record.getMessage() == "Sports library resolution unavailable"]


@pytest.mark.parametrize("query, moviehash, moviehash_match, hashed", [
    (None, HASH, "only", True),
    (RECORDING, None, None, False),
], ids=["hash", "filename"])
def test_an_indexed_recording_resolves_under_autocommit(indexed_recording, caplog, query,
                                                        moviehash, moviehash_match, hashed):
    from compat import sports

    with caplog.at_level(logging.DEBUG, logger="compat.sports"):
        match = sports.resolve_for_request(None, None, None, "movie", query, moviehash,
                                           moviehash_match)

    failures = _unavailable(caplog)
    assert not failures, repr(failures[0].exc_info[1])
    assert match is not None
    assert match.context.event_id == 1011
    assert match.context.arr_instance_id == 101
    assert match.hash_matched is hashed
    assert [candidate["lang"] for candidate in match.candidates] == ["en"]
