# coding=utf-8
"""Deleting a language profile must not disable a default that was replaced.

Deleting a profile clears every stored reference to it, because the editor
reuses ids and a stale reference would silently adopt an unrelated profile.

That cleanup reads the CURRENT configuration, and the settings endpoint applies
the submitted form at the very end. Run it first and it sees the profile the
user is replacing rather than the one they chose. The frontend stages only the
fields that changed, so an unchanged "enabled" checkbox is not in the request:
the cleanup switches the default off, the form puts the new profile id back, and
the user ends up with a default profile selected and defaults disabled.
"""
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import insert

from app.database import TableLanguagesProfiles


def _profile(session, profile_id, name):
    session.execute(insert(TableLanguagesProfiles).values(
        profileId=profile_id, name=name, items="[]", cutoff=None))


def _profile_payload(profile_id, name):
    return {"profileId": profile_id, "name": name, "items": [], "cutoff": None,
            "mustContain": [], "mustNotContain": [], "originalFormat": False,
            "tag": None}


def _settings_endpoint(session, monkeypatch):
    """Drive the real endpoint with a form, the way the frontend submits one."""
    import importlib.util
    from pathlib import Path
    import sys
    from types import ModuleType, SimpleNamespace
    from app import database as db_module
    from subtitles.indexer import sports

    # Import this HTTP boundary without starting unrelated API startup jobs.
    root = Path(__file__).resolve().parents[2] / 'bazarr' / 'api'
    for name in ('api', 'api.system'):
        package = ModuleType(name)
        package.__path__ = [str(root if name == 'api' else root / 'system')]
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, 'app.scheduler', SimpleNamespace(scheduler=None))
    for name, path in [('api.utils', root / 'utils.py'), ('api.system.settings', root / 'system/settings.py')]:
        spec = importlib.util.spec_from_file_location(name, path)
        endpoint = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, endpoint)
        spec.loader.exec_module(endpoint)
    monkeypatch.setattr(db_module, 'database', session)
    monkeypatch.setattr(sports, 'database', session)
    monkeypatch.setattr(sports, 'notify', lambda *a: None)

    monkeypatch.setattr(endpoint, "database", session)
    monkeypatch.setattr(endpoint, "event_stream", lambda *a, **kw: None)
    monkeypatch.setattr(endpoint, "queue_missing_subtitles_recalculation", lambda *a, **kw: None)
    monkeypatch.setattr(endpoint.TableLanguagesProfiles, "__table__",
                        endpoint.TableLanguagesProfiles.__table__)

    from arr_instances import resolution
    monkeypatch.setattr(resolution, "database", session, raising=False)

    def _call(form):
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context("/", method="POST", data=form):
            return endpoint.SystemSettings.post.__wrapped__(object())

    return _call


@pytest.fixture
def post_settings(schema_session, monkeypatch):
    return _settings_endpoint(schema_session, monkeypatch)


@pytest.fixture(params=["sqlite", "postgresql"])
def app_session(request, tmp_path):
    """A database on each engine the app runs on, opened in AUTOCOMMIT as the app opens it.

    Every statement commits by itself there, so rows written before a failure
    stay written, which is what the cases using this look at.
    """
    from sqlalchemy.orm import scoped_session, sessionmaker
    from sqlalchemy.pool import NullPool
    from app.database import Base

    admin = schema = None
    if request.param == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.skip("Set BAZARR_PG_TEST_URL to exercise PostgreSQL")
        schema = "settings_rows_" + uuid.uuid4().hex
        admin = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True,
                                  connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'bazarr.db'}", poolclass=NullPool,
                                  isolation_level="AUTOCOMMIT")

        @sa.event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    session = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))
    try:
        Base.metadata.create_all(engine)
        yield session
    finally:
        session.remove()
        engine.dispose()
        if schema:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


def test_replacing_the_default_profile_while_deleting_the_old_one(
        schema_session, post_settings):
    from app.config import settings

    _profile(schema_session, 3, "Old")
    _profile(schema_session, 4, "New")
    schema_session.commit()

    settings.general.serie_default_enabled = True
    settings.general.serie_default_profile = 3
    try:
        # Exactly what the frontend sends: profile 3 is gone from the list, the
        # new id is staged, and the untouched enable checkbox is not submitted.
        post_settings({
            "languages-profiles": json.dumps([_profile_payload(4, "New")]),
            "settings-general-serie_default_profile": "4",
        })

        assert settings.general.serie_default_profile == 4
        assert settings.general.serie_default_enabled is True, (
            "the replacement profile was kept but defaults were switched off, so "
            "newly synced series silently stop receiving a profile")
    finally:
        settings.general.serie_default_enabled = False
        settings.general.serie_default_profile = ''


def test_deleting_the_default_without_replacing_it_still_turns_it_off(
        schema_session, post_settings):
    """The case the cleanup exists for has to keep working."""
    from app.config import settings

    _profile(schema_session, 3, "Old")
    schema_session.commit()

    settings.general.serie_default_enabled = True
    settings.general.serie_default_profile = 3
    try:
        post_settings({"languages-profiles": json.dumps([])})

        assert settings.general.serie_default_enabled is False
        assert settings.general.serie_default_profile in ('', None)
    finally:
        settings.general.serie_default_enabled = False
        settings.general.serie_default_profile = ''


# ---------------------------------------------------------------------------
# A save refused for its configuration keeps none of its database rows
#
# The languages, profiles and notifiers used to be written first, on an
# AUTOCOMMIT engine the configuration rollback cannot reach, so a save answered
# with 503 or 406 still kept them.
# ---------------------------------------------------------------------------

def _seed_rows(session):
    from app.database import TableSettingsLanguages, TableSettingsNotifier

    _profile(session, 3, "Old")
    session.execute(insert(TableSettingsLanguages).values(
        code3="eng", code2="en", enabled=0, name="English"))
    session.execute(insert(TableSettingsNotifier).values(
        name="Discord", enabled=0, url=None))
    session.commit()


def _rows(session):
    from sqlalchemy import select
    from app.database import TableSettingsLanguages, TableSettingsNotifier

    return {
        "profiles": sorted(session.execute(
            select(TableLanguagesProfiles.profileId, TableLanguagesProfiles.name)).all()),
        "languages": session.execute(select(TableSettingsLanguages.enabled)).scalars().all(),
        "notifiers": session.execute(
            select(TableSettingsNotifier.enabled, TableSettingsNotifier.url)).all(),
    }


_ROW_FORM = {
    "languages-enabled": "en",
    "languages-profiles": json.dumps([_profile_payload(4, "New")]),
    "notifications-providers": json.dumps({"name": "Discord", "enabled": True,
                                           "url": "discord://token"}),
}


@pytest.mark.parametrize("failure", ["persistence", "validation"])
def test_a_refused_save_writes_no_database_rows(schema_session, post_settings, monkeypatch, failure):
    import sys
    from dynaconf.validator import ValidationError
    from app.config import MetadataPersistenceError

    endpoint = sys.modules["api.system.settings"]
    _seed_rows(schema_session)
    before = _rows(schema_session)

    def refuse(_items):
        if failure == "persistence":
            raise MetadataPersistenceError("synthetic")
        raise ValidationError("Unable to save settings to disk")

    monkeypatch.setattr(endpoint, "save_settings", refuse)

    _body, status = post_settings(dict(_ROW_FORM))

    assert status == (503 if failure == "persistence" else 406)
    assert _rows(schema_session) == before, "a refused save kept part of what it submitted"


@pytest.mark.parametrize("error_name, code", [
    ("MetadataFollowupError", "discover_settings_refresh_failed"),
    ("SettingsFollowupError", "settings_refresh_failed"),
])
def test_a_save_whose_refresh_fails_still_writes_its_rows(schema_session, post_settings, monkeypatch,
                                                          error_name, code):
    """The configuration reached the disk there, so the rows go with it.

    That holds for any save, not only one that changed the metadata settings.
    """
    import sys
    from app import config

    endpoint = sys.modules["api.system.settings"]
    _seed_rows(schema_session)

    def saved_then_failed(_items):
        raise getattr(config, error_name)("synthetic")

    monkeypatch.setattr(endpoint, "save_settings", saved_then_failed)
    events = []
    monkeypatch.setattr(endpoint, "event_stream", lambda *args, **_kwargs: events.append(args))

    body, status = post_settings(dict(_ROW_FORM))

    assert status == 503
    assert body["code"] == code
    assert "synthetic" not in body["message"]
    # Other open pages reload the settings, as they do after any written save.
    assert events[-1] == ("settings",)
    assert _rows(schema_session) == {"profiles": [(4, "New")], "languages": [1],
                                     "notifiers": [(1, "discord://token")]}


def test_a_saved_failure_is_reported_as_saved_when_its_broadcast_fails(schema_session, post_settings,
                                                                     monkeypatch):
    """The settings event after a written save is best effort.

    Raised through, it took the place of the saved-with-a-failed-refresh answer, and
    the page reported a save that had reached the disk as failed.
    """
    import sys
    from app.config import SettingsFollowupError

    endpoint = sys.modules["api.system.settings"]
    _seed_rows(schema_session)

    def saved_then_failed(_items):
        raise SettingsFollowupError("synthetic")

    def broadcast(kind=None, *_args, **_kwargs):
        if kind == "settings":
            raise RuntimeError("synthetic broadcast failure")

    monkeypatch.setattr(endpoint, "save_settings", saved_then_failed)
    monkeypatch.setattr(endpoint, "event_stream", broadcast)

    body, status = post_settings(dict(_ROW_FORM))

    assert status == 503
    assert body["code"] == "settings_refresh_failed"
    assert _rows(schema_session) == {"profiles": [(4, "New")], "languages": [1],
                                     "notifiers": [(1, "discord://token")]}


def test_a_saved_request_writes_its_rows(schema_session, post_settings, monkeypatch):
    import sys

    endpoint = sys.modules["api.system.settings"]
    _seed_rows(schema_session)
    monkeypatch.setattr(endpoint, "save_settings", lambda _items: None)

    assert post_settings(dict(_ROW_FORM)) == ('', 204)
    assert _rows(schema_session) == {"profiles": [(4, "New")], "languages": [1],
                                     "notifiers": [(1, "discord://token")]}


# ---------------------------------------------------------------------------
# A notification that fails once the rows are being written
#
# Socket.IO refuses every event while its transport is down. The languages
# event used to go out between the language and the profile writes, so it
# stopped the rest of the rows there and took the place of the answer that says
# the save was kept.
# ---------------------------------------------------------------------------

_ALL_ROWS = {"profiles": [(4, "New")], "languages": [1], "notifiers": [(1, "discord://token")]}


def _refusing_events(monkeypatch, endpoint, refused):
    """Refuse the named events, or every one for None, and record the rest."""
    sent = []

    def emit(kind=None, *_args, **_kwargs):
        if refused is None or kind in refused:
            raise RuntimeError("synthetic transport failure")
        sent.append(kind)

    monkeypatch.setattr(endpoint, "event_stream", emit)
    return sent


def _recording_queue(monkeypatch, endpoint, fails=False):
    queued = []

    def queue(*_args, **_kwargs):
        queued.append(True)
        if fails:
            raise RuntimeError("synthetic queue failure")

    monkeypatch.setattr(endpoint, "queue_missing_subtitles_recalculation", queue)
    return queued


@pytest.mark.parametrize("error_name, code", [
    ("MetadataFollowupError", "discover_settings_refresh_failed"),
    ("SettingsFollowupError", "settings_refresh_failed"),
])
def test_a_saved_failure_keeps_its_answer_and_rows_while_events_fail(app_session, monkeypatch,
                                                                     error_name, code):
    import sys
    from app import config

    post = _settings_endpoint(app_session, monkeypatch)
    endpoint = sys.modules["api.system.settings"]
    _seed_rows(app_session)

    def saved_then_failed(_items):
        raise getattr(config, error_name)("synthetic")

    monkeypatch.setattr(endpoint, "save_settings", saved_then_failed)
    _refusing_events(monkeypatch, endpoint, None)
    queued = _recording_queue(monkeypatch, endpoint)

    body, status = post(dict(_ROW_FORM))

    assert status == 503
    assert body["code"] == code
    assert _rows(app_session) == _ALL_ROWS, "a failed event stopped the rows after it"
    assert queued == [True], "the recalculation for the saved profiles was never queued"


@pytest.mark.parametrize("refused, queue_fails", [
    (None, False),
    ({"languages"}, False),
    ({"settings"}, False),
    (set(), True),
], ids=["every-event", "languages-event", "settings-event", "recalculation-queue"])
def test_a_save_whose_announcement_fails_is_reported_as_saved(app_session, monkeypatch,
                                                             refused, queue_fails):
    """The configuration and every row are written, so the answer says so.

    Each step after the rows runs whichever other one fails, as the refresh after
    the configuration does.
    """
    import sys

    post = _settings_endpoint(app_session, monkeypatch)
    endpoint = sys.modules["api.system.settings"]
    _seed_rows(app_session)
    monkeypatch.setattr(endpoint, "save_settings", lambda _items: None)
    sent = _refusing_events(monkeypatch, endpoint, refused)
    queued = _recording_queue(monkeypatch, endpoint, fails=queue_fails)

    body, status = post(dict(_ROW_FORM))

    assert status == 503
    assert body["code"] == "settings_refresh_failed"
    assert "synthetic" not in body["message"]
    assert _rows(app_session) == _ALL_ROWS
    assert queued == [True]
    assert sent == [kind for kind in ("languages", "settings") if refused is not None and kind not in refused]


def test_a_refused_save_keeps_its_answer_while_events_fail(schema_session, post_settings, monkeypatch,
                                                          caplog):
    import logging
    import sys
    from dynaconf.validator import ValidationError

    endpoint = sys.modules["api.system.settings"]
    _seed_rows(schema_session)
    before = _rows(schema_session)

    def refuse(_items):
        raise ValidationError("Unable to save settings to disk")

    monkeypatch.setattr(endpoint, "save_settings", refuse)
    _refusing_events(monkeypatch, endpoint, None)

    with caplog.at_level(logging.ERROR):
        assert post_settings(dict(_ROW_FORM)) == ("Unable to save settings to disk", 406)
    assert _rows(schema_session) == before
    # The failed event is logged, and the log does not contradict the answer.
    logged = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert logged, "the failed settings event was not logged"
    assert not any("were saved" in message for message in logged), logged
