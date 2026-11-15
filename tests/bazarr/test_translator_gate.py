"""The v2 gate: which engine answers, which causes close it, Lingarr as a legacy engine, and the switch.

Runs on SQLite, and on PostgreSQL when BAZARR_PG_TEST_URL is set.
"""
import sys

import pytest
from sqlalchemy.orm import scoped_session, sessionmaker

import translator_dispatch_helpers as helpers
from translator_dispatch_helpers import PLUGIN, MemoryPointers
from subtitles.tools.translate.runtime import gate, legacy_engines
from subtitles.tools.translate.runtime.gate import GateHolds
from subtitles.tools.translate.runtime.store import TranslatorStore


class Env:
    def __init__(self, session):
        self.pointers = MemoryPointers()
        self.active = {PLUGIN}
        self.store = TranslatorStore(session, pointers=self.pointers, plugin_active=lambda pid: pid in self.active)
        self.holds = GateHolds()

    def evaluate(self):
        return gate.evaluate(self.store, self.holds)


@pytest.fixture(params=["sqlite", "postgres"])
def env(request, tmp_path):
    import sqlalchemy as sa

    engine, pg = helpers.make_engine(request.param, tmp_path)
    scoped = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    try:
        yield Env(scoped)
    finally:
        scoped.remove()
        engine.dispose()
        if pg:
            admin, schema = pg
            with admin.begin() as connection:
                connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
            admin.dispose()


@pytest.fixture
def lingarr_url(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.translator, "lingarr_url", "http://lingarr.example.test:9876")


def test_no_engine_selected_is_a_closed_gate_with_its_cause(env):
    result = env.evaluate()
    assert (result.available, result.cause) == (False, gate.NO_ENGINE)
    assert result.reason == "No translator is configured"


def test_a_selected_engine_that_no_longer_exists_closes_the_gate(env):
    env.pointers.engine = 99
    assert env.evaluate().cause == gate.ENGINE_MISSING


def test_the_official_engine_needs_a_usable_connection(env):
    instance = env.store.create_instance(PLUGIN)
    env.pointers.engine = instance.id
    closed = env.evaluate()
    assert (closed.available, closed.cause) == (False, gate.NOT_CONFIGURED)
    assert "back ends" in closed.reason
    env.store.add_connection(instance.id, "openrouter", model="m")
    assert env.evaluate().available is True


def test_the_cause_is_named_for_each_way_an_engine_is_not_configured(env):
    instance = env.store.create_instance(PLUGIN)
    env.store.add_connection(instance.id, "openrouter", model="m")
    env.pointers.engine = instance.id
    env.active.clear()
    assert "plugin is not installed" in env.evaluate().reason
    env.active.add(PLUGIN)
    env.store.set_instance_enabled(instance.id, False)
    env.pointers.engine = instance.id  # turning an engine off lets the pointer go; put it back for the check
    assert env.evaluate().reason == "The selected translator is turned off"


def test_a_migrated_engine_awaiting_install_closes_the_gate_until_it_is_released(env):
    instance = env.store.create_instance(PLUGIN)
    env.store.add_connection(instance.id, "openrouter", model="m")
    env.pointers.engine = instance.id
    env.holds.hold_for_install(instance.id)
    closed = env.evaluate()
    assert (closed.available, closed.cause) == (False, gate.AWAITING_INSTALL)
    env.holds.release_install(instance.id)
    assert env.evaluate().available is True


def test_the_drain_pause_closes_the_gate_only_for_the_paused_selected_engine(env):
    selected = env.store.create_instance(PLUGIN)
    env.store.add_connection(selected.id, "openrouter", model="m")
    other = env.store.create_instance(PLUGIN, name="other")
    env.pointers.engine = selected.id
    env.holds.pause_for_drain(other.id)
    assert env.evaluate().available is True, "a paused engine that is not the selected one leaves the gate open"
    env.holds.pause_for_drain(selected.id)
    paused = env.evaluate()
    assert (paused.available, paused.cause) == (False, gate.SIDECAR_DRAIN)
    env.holds.resume_after_drain(selected.id)
    assert env.evaluate().available is True


def test_lingarr_is_configured_once_its_address_is_set_and_needs_no_plugin(env, lingarr_url):
    instance = env.store.create_instance(legacy_engines.LINGARR_PLUGIN_ID, settings={legacy_engines.SAVED_FLAG: True})
    env.pointers.engine = instance.id
    assert env.active == {PLUGIN} and env.evaluate().available is True


def test_lingarr_with_no_address_closes_the_gate_naming_it(env, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.translator, "lingarr_url", "")
    instance = env.store.create_instance(legacy_engines.LINGARR_PLUGIN_ID)
    env.pointers.engine = instance.id
    closed = env.evaluate()
    assert (closed.available, closed.reason) == (False, "The Lingarr engine has no URL configured")


def test_a_gate_that_cannot_read_the_configuration_is_closed_and_says_why(env):
    class Broken:
        def pointers(self):
            raise RuntimeError("the database is gone")

    result = gate.evaluate(Broken(), env.holds)
    assert (result.available, result.cause) == (False, gate.UNREADABLE) and "database is gone" in result.reason


# ------------------------------------------------------------------------------------------------- the switch


def test_with_the_runtime_on_translation_available_is_the_gates_answer(env, monkeypatch):
    from subtitles.tools.translate import availability

    monkeypatch.setattr(availability, "runtime_active", lambda: True)
    monkeypatch.setattr(gate, "evaluate", lambda store=None, holds=None: gate.GateResult(False, "closed here", "x"))
    answer = availability.translation_available()
    assert tuple(answer) == (False, "closed here")
    # A named type is one of the old engines, which the runtime does not have: the selected engine answers.
    assert tuple(availability.translation_available("openrouter")) == (False, "closed here")


def test_with_the_runtime_off_the_old_gate_answers_and_the_runtime_is_never_imported(monkeypatch):
    from app.config import settings
    from subtitles.tools.translate import availability

    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(settings.translator, "translator_type", "google_translate")
    for name in [name for name in sys.modules if name.startswith("subtitles.tools.translate.runtime")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "subtitles.tools.translate.runtime", None)  # an import now raises
    assert tuple(availability.translation_available()) == (True, "")
    monkeypatch.setattr(settings.translator, "translator_type", "none")
    assert tuple(availability.translation_available()) == (False, "No translator is configured")


def test_with_the_switch_on_but_no_tables_the_old_gate_answers(monkeypatch):
    from app.config import settings
    from subtitles.tools.translate import availability, runtime

    monkeypatch.setattr(settings.translator, "runtime_v2", True)
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: False)
    monkeypatch.setattr(settings.translator, "translator_type", "google_translate")
    assert availability.runtime_active() is False
    assert tuple(availability.translation_available()) == (True, "")
