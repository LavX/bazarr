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
    monkeypatch.setattr(endpoint, "list_missing_subtitles", lambda *a, **kw: None)
    monkeypatch.setattr(endpoint, "list_missing_subtitles_movies", lambda *a, **kw: None)
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
