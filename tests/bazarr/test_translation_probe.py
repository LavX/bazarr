"""The connection test and the model list are wired into the running app.

``get_service`` is the real one here, on a real database file, so what the page's state advertises and what the
test route does are what a running install does: the test goes to the bound worker port, and says so plainly when
none is bound.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from api.translation.translation import get_service as real_get_service
from plugin_platform_helpers import service_manifest
from subtitles.tools.translate.runtime import workers
from subtitles.tools.translate.runtime.settings_service import (InstalledPlugin, OPEN_TEST,
                                                                TranslatorSettingsService)
from subtitles.tools.translate.runtime.store import OFFICIAL_ENGINE_PLUGIN_ID, TranslatorStore

PLUGIN = OFFICIAL_ENGINE_PLUGIN_ID
ADDRESS = "http://192.0.2.20:1234/v1"
KEY = "sk-live-0123456789abcdef-WXYZ"  # pragma: allowlist secret


def _manifest():
    return service_manifest(
        PLUGIN, settings_version=2, kind="translator", kind_api="bazarr.translator.v1",
        settings={"fields": [{"key": "service_token", "type": "secret"}], "backend_types": {
            "openai_compatible": {"label": "OpenAI compatible", "fields": [
                {"key": "base_url", "type": "text", "destination": True, "default": "https://api.openai.com/v1"},
                {"key": "api_key", "type": "secret"},
                {"key": "model_list", "type": "options"}]}}})


class MemoryPointers:
    def __init__(self):
        self.engine = self.fallback = None

    def get(self):
        return self.engine, self.fallback

    def set(self, engine, fallback):
        self.engine, self.fallback = engine, fallback
        return True


class FakePort:
    """A bound worker port that records what it is asked and answers ``answer``."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def submit(self, *args, **kwargs):
        raise AssertionError("a test never translates")

    def abandon(self, plugin_id):
        return None

    def call(self, plugin_id, op, params, secrets, *, timeout):
        self.calls.append((plugin_id, op, params, dict(secrets or {}), timeout))
        return SimpleNamespace(result=self.answer, data=b"")


@pytest.fixture
def app_service(monkeypatch, tmp_path):
    """The real ``get_service`` on a database file, with the one plugin installed."""
    import api.translation.translation as module
    import app.database as app_database
    import sqlalchemy as sa
    from sqlalchemy.orm import scoped_session, sessionmaker

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'translation.db'}", connect_args={"timeout": 30})
    app_database.Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(app_database, "database", session)
    plugins = {PLUGIN: InstalledPlugin(PLUGIN, "AI Subtitle Translator", "2.2.0", True, _manifest())}

    class Wired(TranslatorSettingsService):
        def __init__(self, store, **kwargs):
            super().__init__(TranslatorStore(session, pointers=MemoryPointers(), plugin_active=lambda pid: True),
                             plugin_lookup=plugins.get, catalog_engines=lambda: [], update_lookup=lambda pid: None,
                             **kwargs)

    monkeypatch.setattr(module, "TranslatorSettingsService", Wired)
    service = real_get_service()
    store = service._store
    instance = store.create_instance(PLUGIN, name="AI", settings_version=2, plugin_version="2.2.0")
    created = service.create_connection(instance.id, {
        "backend_type": "openai_compatible", "accept_destinations": [ADDRESS],
        "values": {"base_url": ADDRESS, "api_key": KEY, "model": "qwen"}})
    yield SimpleNamespace(service=service, instance=instance, connection_id=created["connection"]["id"])
    workers.unbind()
    session.remove()
    engine.dispose()


def test_the_real_service_advertises_the_test_only_while_a_worker_is_bound(app_service):
    workers.unbind()
    assert "test" not in app_service.service.capabilities()
    assert app_service.service.snapshot()["capabilities"] == ["summary", "limits", "queue"]
    workers.bind(FakePort({"ok": True}))
    assert app_service.service.snapshot()["capabilities"] == ["summary", "limits", "test", "queue"]


def test_with_no_worker_bound_the_test_is_refused_as_not_running_and_sends_nothing(app_service):
    from subtitles.tools.translate.runtime.settings_service import PluginUnavailableError

    workers.unbind()
    with pytest.raises(PluginUnavailableError, match="not running"):
        app_service.service.test_connection(app_service.connection_id)


def test_a_bound_worker_gets_the_plugins_test_with_the_stored_key_for_the_saved_address(app_service):
    port = FakePort({"ok": True, "input_tokens": 5, "output_tokens": 2})
    workers.bind(port)
    result = app_service.service.test_connection(app_service.connection_id)
    assert result["ok"] is True and KEY not in str(result)
    ((plugin_id, op, params, secrets, timeout),) = port.calls
    assert (plugin_id, op) == (PLUGIN, "test")
    assert params["config"]["connection"]["type"] == "openai_compatible"
    assert params["config"]["connection"]["settings"]["base_url"] == ADDRESS
    assert secrets == {"api_key": KEY} and KEY not in str(params)
    assert timeout > 0


def test_a_failed_test_reports_the_plugins_message_with_the_key_masked(app_service):
    workers.bind(FakePort({"ok": False, "message": f"rejected {KEY}"}))
    result = app_service.service.test_connection(app_service.connection_id)
    assert result["ok"] is False and KEY not in str(result)


def test_a_model_list_asks_the_worker_with_no_stored_secret(app_service):
    port = FakePort([{"id": "qwen", "label": "Qwen"}])
    workers.bind(port)
    answer = app_service.service.options(app_service.instance.id, "openai_compatible", "model_list",
                                         {"base_url": ADDRESS})
    assert answer["available"] is True and answer["items"] == [{"id": "qwen", "label": "Qwen"}]
    ((plugin_id, op, params, secrets, _timeout),) = port.calls
    assert (plugin_id, op, secrets) == (PLUGIN, "options", {})
    assert params["field"] == "model_list" and KEY not in str(params)


def test_a_model_list_with_no_worker_bound_is_unavailable_so_the_page_offers_text(app_service):
    workers.unbind()
    answer = app_service.service.options(app_service.instance.id, "openai_compatible", "model_list",
                                         {"base_url": ADDRESS})
    assert answer["available"] is False


def test_the_test_sends_the_snapshot_it_cut_and_never_reads_the_connection_again(app_service, monkeypatch):
    """An address saved between the cut of the secrets and the send must not pair the new address with the key."""
    from subtitles.tools.translate.runtime.settings_service import TranslatorSettingsService

    service = app_service.service
    port = FakePort({"ok": True})
    workers.bind(port)
    other = "http://198.51.100.7:4321/v1"
    saved = []
    original = service._store.secrets_for_request

    def cut_then_save(owner, destinations):
        granted = original(owner, destinations)
        if owner[0] == "connection" and not saved:
            saved.append(True)
            current = service._store.get_connection(app_service.connection_id, fresh=True)
            service.update_connection(app_service.connection_id, {
                "expected_revision": current.revision, "values": {"base_url": other}, "accept_destinations": [other]})
        return granted

    monkeypatch.setattr(service._store, "secrets_for_request", cut_then_save)
    assert service.test_connection(app_service.connection_id)["ok"] is True
    ((_plugin, _op, params, secrets, _timeout),) = port.calls
    assert saved and params["config"]["connection"]["settings"]["base_url"] == ADDRESS
    assert secrets == {"api_key": KEY}


def test_a_test_sends_the_engines_secrets_as_a_translation_would(app_service):
    service = app_service.service
    saved = service.save_engine(app_service.instance.id, {
        "expected_revision": app_service.instance.revision, "values": {"service_token": "engine-token-0123456789"}})
    assert saved
    port = FakePort({"ok": True})
    workers.bind(port)
    assert service.test_connection(app_service.connection_id)["ok"] is True
    ((_plugin, _op, params, secrets, _timeout),) = port.calls
    assert secrets == {"service_token": "engine-token-0123456789", "api_key": KEY}
    assert "engine-token-0123456789" not in str(params)


def test_a_worker_busy_with_a_translation_is_not_charged_and_the_person_is_told(app_service):
    from subtitles.tools.translate.runtime.usage import UsageLedger
    from app.database import TableTranslationUsage

    class Busy(FakePort):
        def call(self, plugin_id, op, params, secrets, *, timeout):
            raise workers.NotSent("the worker is busy", busy=True)

    service = app_service.service
    service.update_connection(app_service.connection_id, {
        "expected_revision": service._store.get_connection(app_service.connection_id, fresh=True).revision,
        "values": {"price_input_per_million": 1.0, "price_output_per_million": 2.0}})
    workers.bind(Busy({}))
    result = service.test_connection(app_service.connection_id)
    assert result["ok"] is False and "busy translating" in result["message"]
    rows = service._store.session.query(TableTranslationUsage).all()
    assert [(row.state, row.actual_micros) for row in rows] == [("settled", 0)]


def _pool(handle_factory):
    class Pool:
        def __init__(self):
            self.sent = []

        def definition(self, key):
            return object()

        def submit(self, key, op, params, **kwargs):
            self.sent.append((key, op, params, kwargs))
            return handle_factory()

    return Pool()


class _Handle:
    """A request that answers, or runs on until it is cancelled and then takes ``cancel_takes`` seconds to stop."""

    def __init__(self, answer=None, runs=False, cancel_takes=0.0):
        self.answer, self.runs, self.cancel_takes = answer, runs, cancel_takes
        self.cancelled = []
        self._ended = threading.Event()
        if not runs:
            self._ended.set()

    def done(self):
        return self._ended.is_set()

    def cancel(self, reason="cancelled"):
        self.cancelled.append(reason)
        threading.Timer(self.cancel_takes, self._ended.set).start()

    def wait(self, timeout=None):
        if not self._ended.wait(timeout):
            raise TimeoutError("still running")
        if self.runs:
            raise RuntimeError("cancelled")
        return self.answer


def test_the_pool_port_sends_a_short_request_to_the_plugins_worker():
    pool = _pool(lambda: _Handle("answer"))
    port = workers.PoolWorkers(pool, lambda plugin_id: None)
    assert port.call("engine", "test", {"config": {}}, {"api_key": "x"}, timeout=5) == "answer"
    assert pool.sent == [("engine", "test", {"config": {}}, {"secrets": {"api_key": "x"}, "timeout": 5})]
    pool.sent.clear()
    port.call("engine", "test", {}, {}, timeout=5)
    assert pool.sent[-1][3]["secrets"] is None


@pytest.mark.parametrize("error_name,busy", [("SessionBusy", True), ("PoolUnavailable", False),
                                             ("UnknownWorker", False)])
def test_a_refusal_before_the_request_is_written_is_not_sent(error_name, busy):
    from provider_hub.runtime import errors, pool as pool_module

    error_class = getattr(errors, error_name, None) or getattr(pool_module, error_name)

    class Refusing:
        def definition(self, key):
            return object()

        def submit(self, *args, **kwargs):
            raise error_class("refused")

    port = workers.PoolWorkers(Refusing(), lambda plugin_id: None)
    with pytest.raises(workers.NotSent) as raised:
        port.call("engine", "test", {}, {}, timeout=5)
    assert raised.value.busy is busy


def test_a_request_that_runs_out_of_time_is_waited_for_until_the_session_says_it_is_gone(monkeypatch):
    handle = _Handle(runs=True, cancel_takes=1.5)
    port = workers.PoolWorkers(_pool(lambda: handle), lambda plugin_id: None)
    monkeypatch.setattr(workers, "STOP_SETTLE_SECONDS", 30.0)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        port.call("engine", "test", {}, {}, timeout=0.2)
    assert handle.cancelled == ["timeout"]
    assert handle.done() and time.monotonic() - started >= 1.5


def test_a_second_test_cannot_start_while_a_slow_cancel_of_the_first_is_still_running(app_service, monkeypatch):
    """The claim on a connection is released when the probe thread ends, and the probe thread ends only when the
    port returns, which it does after the session confirms the stopped request."""
    from subtitles.tools.translate.runtime import settings_service

    service = app_service.service
    handle = _Handle(runs=True, cancel_takes=2.0)
    workers.bind(workers.PoolWorkers(_pool(lambda: handle), lambda plugin_id: None))
    monkeypatch.setattr(settings_service, "TEST_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(settings_service, "TEST_TIMEOUT_GRACE_SECONDS", 0.2)
    first = service.test_connection(app_service.connection_id)
    assert first["ok"] is False  # the first stopped waiting, its worker request is still being stopped
    assert app_service.connection_id in settings_service._RUNNING_TESTS
    second = service.test_connection(app_service.connection_id)
    assert second["ok"] is False and "already running" in second["message"]
    deadline = time.monotonic() + 10
    while app_service.connection_id in settings_service._RUNNING_TESTS and time.monotonic() < deadline:
        time.sleep(0.05)
    assert handle.done() and app_service.connection_id not in settings_service._RUNNING_TESTS
