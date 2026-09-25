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

import pytest
from sqlalchemy import insert

from app.database import TableLanguagesProfiles


def _profile(session, profile_id, name):
    session.execute(insert(TableLanguagesProfiles).values(
        profileId=profile_id, name=name, items="[]", cutoff=None))


def _profile_payload(profile_id, name):
    return {"profileId": profile_id, "name": name, "items": [], "cutoff": None,
            "mustContain": [], "mustNotContain": [], "originalFormat": False,
            "tag": None}


@pytest.fixture
def post_settings(schema_session, monkeypatch):
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
    monkeypatch.setattr(db_module, 'database', schema_session)
    monkeypatch.setattr(sports, 'database', schema_session)
    monkeypatch.setattr(sports, 'notify', lambda *a: None)

    monkeypatch.setattr(endpoint, "database", schema_session)
    monkeypatch.setattr(endpoint, "event_stream", lambda *a, **kw: None)
    monkeypatch.setattr(endpoint, "queue_missing_subtitles_recalculation", lambda *a, **kw: None)
    monkeypatch.setattr(endpoint.TableLanguagesProfiles, "__table__",
                        endpoint.TableLanguagesProfiles.__table__)

    from arr_instances import resolution
    monkeypatch.setattr(resolution, "database", schema_session, raising=False)

    def _call(form):
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context("/", method="POST", data=form):
            return endpoint.SystemSettings.post.__wrapped__(object())

    return _call


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


def test_a_save_whose_refresh_fails_still_writes_its_rows(schema_session, post_settings, monkeypatch):
    """The configuration reached the disk there, so the rows go with it."""
    import sys
    from app.config import MetadataFollowupError

    endpoint = sys.modules["api.system.settings"]
    _seed_rows(schema_session)

    def saved_then_failed(_items):
        raise MetadataFollowupError("synthetic")

    monkeypatch.setattr(endpoint, "save_settings", saved_then_failed)

    _body, status = post_settings(dict(_ROW_FORM))

    assert status == 503
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
