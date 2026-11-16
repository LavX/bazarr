"""The connection test and the model list are wired into the running app.

``get_service`` is the real one here, on a real database file, so what the page's state advertises and what the
test route does are what a running install does: the test goes to the bound worker port, and says so plainly when
none is bound.
"""
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
        settings={"fields": [], "backend_types": {
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


def test_the_real_service_advertises_the_connection_test(app_service):
    assert "test" in app_service.service.capabilities()
    assert app_service.service.snapshot()["capabilities"] == ["summary", "limits", "test", "queue"]


def test_with_no_worker_bound_the_test_says_so_and_sends_nothing(app_service):
    workers.unbind()
    result = app_service.service.test_connection(app_service.connection_id)
    assert result["ok"] is False and result["message"] == OPEN_TEST


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


def test_the_pool_port_sends_a_short_request_to_the_plugins_worker_and_cancels_one_that_times_out():
    class Handle:
        def __init__(self, fail):
            self.fail, self.cancelled = fail, []

        def wait(self, timeout=None):
            if self.fail:
                raise TimeoutError()
            return "answer"

        def cancel(self, reason="cancelled"):
            self.cancelled.append(reason)

    class Pool:
        def __init__(self):
            self.handle = Handle(False)
            self.sent = []

        def definition(self, key):
            return object()

        def submit(self, key, op, params, **kwargs):
            self.sent.append((key, op, params, kwargs))
            return self.handle

    pool = Pool()
    port = workers.PoolWorkers(pool, lambda plugin_id: None)
    assert port.call("engine", "test", {"config": {}}, {"api_key": "x"}, timeout=5) == "answer"
    assert pool.sent == [("engine", "test", {"config": {}}, {"secrets": {"api_key": "x"}, "timeout": 5})]
    pool.handle = Handle(True)
    with pytest.raises(TimeoutError):
        port.call("engine", "test", {}, {}, timeout=5)
    assert pool.handle.cancelled == ["timeout"]
    assert pool.sent[-1][3]["secrets"] is None
