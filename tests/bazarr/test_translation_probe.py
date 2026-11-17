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
from subtitles.tools.translate.runtime.settings_service import (InstalledPlugin, TranslatorSettingsService)
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
    # The plugin asked the back end: this is a failure of the back end, not a test that never ran.
    assert result["outcome"] == "failed"


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
    assert result["ok"] is False and "Other tests are running" in result["message"]
    # Through the real probe: the busy worker never took the request, and the answer says so.
    assert result["outcome"] == "not_ready"
    rows = service._store.session.query(TableTranslationUsage).all()
    assert [(row.state, row.actual_micros) for row in rows] == [("settled", 0)]


def test_a_worker_that_would_not_start_took_nothing_and_the_answer_says_try_again_not_failed(app_service):
    from subtitles.tools.translate.runtime import probe

    class Unstarted(FakePort):
        def call(self, plugin_id, op, params, secrets, *, timeout):
            raise workers.NotSent("the worker would not start", busy=False)

    workers.bind(Unstarted({}))
    result = app_service.service.test_connection(app_service.connection_id)
    assert result == {"ok": False, "message": probe.UNSTARTED_TEST, "latency_ms": None, "outcome": "not_ready"}


class _Pool:
    """A pool whose one-call runs are scripted: ``outcome`` is an answer, or an exception to raise."""

    def __init__(self, outcome="answer"):
        self.outcome = outcome
        self.runs = []

    def definition(self, key):
        raise AssertionError("a short request never touches the long-lived worker")

    def submit(self, *args, **kwargs):
        raise AssertionError("a short request never touches the long-lived worker")

    def run_once(self, definition, op, params, **kwargs):
        self.runs.append((definition.key, op, params, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _definition(plugin_id):
    from provider_hub.runtime.pool import WorkerDefinition

    return WorkerDefinition(plugin_id, kind="translator", command=["unused"])


def test_the_pool_port_sends_a_short_request_to_a_worker_of_its_own():
    pool = _Pool()
    port = workers.PoolWorkers(pool, _definition)
    assert port.call("engine", "test", {"config": {}}, {"api_key": "x"}, timeout=5) == "answer"
    ((key, op, params, kwargs),) = pool.runs
    assert key.startswith("engine" + workers.ONE_CALL_SUFFIX + ":") and len(key.split(":")[-1]) == 12
    assert (op, params, kwargs) == ("test", {"config": {}}, {"secrets": {"api_key": "x"}, "timeout": 5})
    # Every call is a worker of its own: a second call does not share the first one's key.
    port.call("engine", "test", {}, {}, timeout=5)
    assert pool.runs[0][0] != pool.runs[1][0]
    pool.runs.clear()
    port.call("engine", "test", {}, {}, timeout=5)
    assert pool.runs[-1][3]["secrets"] is None


def test_a_plugin_with_no_worker_to_start_is_not_sent():
    port = workers.PoolWorkers(_Pool(), lambda plugin_id: None)
    with pytest.raises(workers.NotSent):
        port.call("engine", "test", {}, {}, timeout=5)


def _refusals():
    from provider_hub.runtime import errors, pool as pool_module

    return [
        pytest.param(errors.SessionBusy("busy"), True, id="session busy"),
        pytest.param(pool_module.PoolUnavailable("full", code=pool_module.PoolCodes.POOL_FULL), True, id="pool full"),
        pytest.param(pool_module.PoolUnavailable("no disk", code=pool_module.PoolCodes.DISK_RESERVE), False,
                     id="pool unavailable"),
        pytest.param(pool_module.UnknownWorker("who"), False, id="unknown worker"),
        pytest.param(pool_module.PoolClosed("closed"), False, id="pool closed"),
    ]


@pytest.mark.parametrize("error,busy", _refusals())
def test_a_refusal_before_the_request_is_written_is_not_sent(error, busy):
    port = workers.PoolWorkers(_Pool(error), _definition)
    with pytest.raises(workers.NotSent) as raised:
        port.call("engine", "test", {}, {}, timeout=5)
    assert raised.value.busy is busy


@pytest.mark.parametrize("call,params,secrets,timeout", [
    pytest.param("test", {}, {}, -1, id="negative timeout"),
    pytest.param("test", {}, {}, float("nan"), id="timeout that is not a number"),
    pytest.param("test", {}, {"api_key": ""}, 5, id="empty secret"),
    pytest.param("test", {"config": {"price": float("nan")}}, {}, 5, id="params that are not JSON"),
    pytest.param("test", {"config": {"text": "x" * (40 * 1024 * 1024)}}, {}, 5, id="a frame over its cap"),
])
def test_what_the_wire_would_refuse_is_not_sent_and_no_worker_is_started_for_it(call, params, secrets, timeout):
    pool = _Pool()
    port = workers.PoolWorkers(pool, _definition)
    with pytest.raises(workers.NotSent):
        port.call("engine", call, params, secrets, timeout=timeout)
    assert pool.runs == []


@pytest.mark.parametrize("raised", [ValueError("late"), "unencodable", "frame"])
def test_an_error_after_a_worker_has_the_request_is_not_taken_for_a_refusal(raised):
    """Only what is settled before the worker exists is a refusal. This one may have been sent, so it is raised."""
    from provider_hub.runtime import frames

    error = {"unencodable": frames.UnencodableFrame("late"), "frame": frames.FrameError("late")}.get(raised, raised)
    port = workers.PoolWorkers(_Pool(error), _definition)
    with pytest.raises(type(error)):
        port.call("engine", "test", {}, {}, timeout=5)


@pytest.mark.parametrize("error", ["worker killed", "timeout", "failed", "exited"])
def test_a_failure_after_the_request_may_have_gone_out_is_not_a_refusal(error):
    """It may have been acted on, so the test stays charged at its worst case."""
    from provider_hub.runtime import errors, pool as pool_module

    raised = {"worker killed": pool_module.WorkerKilled("over its memory limit", code="memory_limit"),
              "timeout": errors.RequestTimeout("request exceeded its deadline"),
              "failed": errors.RequestFailed("it failed", code="internal"),
              "exited": errors.WorkerExited("the worker exited")}[error]
    port = workers.PoolWorkers(_Pool(raised), _definition)
    with pytest.raises(type(raised)):
        port.call("engine", "test", {}, {}, timeout=5)


@pytest.mark.parametrize("kind", ["negative timeout", "empty secret", "params that are not JSON"])
def test_a_refusal_the_wire_made_is_not_charged_through_the_real_probe(app_service, kind, monkeypatch):
    """The probe tells the service nothing was sent, so a priced test is settled at nothing."""
    from app.database import TableTranslationUsage
    from subtitles.tools.translate.runtime import probe, settings_service

    if kind == "negative timeout":
        monkeypatch.setattr(settings_service, "TEST_TIMEOUT_SECONDS", -1.0)
    elif kind == "empty secret":
        monkeypatch.setattr(workers.PoolWorkers, "_check_encodable", staticmethod(
            lambda op, params, secrets, timeout: (_ for _ in ()).throw(ValueError("secrets must be flat"))))
    else:
        from provider_hub.runtime import frames

        monkeypatch.setattr(workers.PoolWorkers, "_check_encodable", staticmethod(
            lambda op, params, secrets, timeout: (_ for _ in ()).throw(frames.UnencodableFrame("not JSON"))))
    service = app_service.service
    service.update_connection(app_service.connection_id, {
        "expected_revision": service._store.get_connection(app_service.connection_id, fresh=True).revision,
        "values": {"price_input_per_million": 1.0, "price_output_per_million": 2.0}})
    pool = _Pool()
    workers.bind(workers.PoolWorkers(pool, _definition))
    result = service.test_connection(app_service.connection_id)
    assert result["ok"] is False and result["outcome"] == "not_ready" and probe.UNSTARTED_TEST in result["message"]
    rows = service._store.session.query(TableTranslationUsage).all()
    assert [(row.state, row.actual_micros) for row in rows] == [("settled", 0)]
    assert pool.runs == []  # no worker was started for it


def test_a_second_test_cannot_start_while_the_first_ones_worker_is_still_stopping(app_service, monkeypatch):
    """The claim on a connection is released when the probe thread ends, and the probe thread ends only when the
    port returns, which it does once the one-call worker is gone."""
    from subtitles.tools.translate.runtime import settings_service

    service = app_service.service
    release = threading.Event()

    class Slow(_Pool):
        def run_once(self, definition, op, params, **kwargs):
            release.wait(10)
            raise TimeoutError("request exceeded its deadline")

    workers.bind(workers.PoolWorkers(Slow(), _definition))
    monkeypatch.setattr(settings_service, "TEST_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(settings_service, "TEST_TIMEOUT_GRACE_SECONDS", 0.2)
    first = service.test_connection(app_service.connection_id)
    assert first["ok"] is False  # the first stopped waiting, its worker is still being stopped
    assert app_service.connection_id in settings_service._RUNNING_TESTS
    second = service.test_connection(app_service.connection_id)
    assert second["ok"] is False and "already running" in second["message"]
    release.set()
    deadline = time.monotonic() + 10
    while app_service.connection_id in settings_service._RUNNING_TESTS and time.monotonic() < deadline:
        time.sleep(0.05)
    assert app_service.connection_id not in settings_service._RUNNING_TESTS
