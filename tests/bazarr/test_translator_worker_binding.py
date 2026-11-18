"""The dispatcher's port bound to real workers: what a worker is started with, one-call workers for short requests, and a
worker that is retired only after its jobs finish."""
import os
import subprocess
import threading
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from provider_hub.runtime import errors
from provider_hub.runtime.pool import PoolLimits, WorkerDefinition, WorkerPool
from provider_hub.runtime.session import SessionLimits
from provider_hub.runtime.testing.fake_translator import fake_translator_command, fake_translator_env
from provider_hub.runtime.testing.fake_worker import fake_worker_command, fake_worker_env
from subtitles.tools.translate.runtime import worker_binding, workers

PLUGIN = "sample"
CALL = "0123456789ab"
ENGINE = {"openai_compatible": {"label": "OpenAI compatible", "endpoints": [], "fields": [
    {"key": "base_url", "type": "text", "destination": True, "default": "http://lan.example.test:1234/v1"},
    {"key": "api_key", "type": "secret"}]},
    "openrouter": {"label": "OpenRouter", "endpoints": ["https://openrouter.ai:443"],
                   "fields": [{"key": "api_key", "type": "secret"}]}}


def _manifest(plugin_id=PLUGIN, **extra):
    from plugin_platform_helpers import service_manifest

    return service_manifest(plugin_id, settings_version=2, kind="translator", kind_api="bazarr.translator.v1",
                            endpoints=["https://updates.example.test:8443"], settings={"fields": [
                                {"key": "mirror", "type": "text", "destination": True,
                                 "default": "https://mirror.example.test:9443"}], "backend_types": ENGINE},
                            entry_module="sample_mod", entry_class="Sample", **extra)


def _fast_session(**overrides):
    values = dict(handshake_timeout=10.0, heartbeat_interval=0.1, heartbeat_timeout=3.0, cancel_grace=0.5,
                  kill_grace=3.0, tick=0.02)
    values.update(overrides)
    return SessionLimits(**values)


def _pool():
    return WorkerPool(PoolLimits(tick=0.05, heartbeat_interval=0.1, acquire_timeout=2.0, close_grace=1.0,
                                 budget_interval=0.0, backoff_base=0.05, backoff_cap=0.2), autostart=True)


# --------------------------------------------------------------------------------------------- the pure parts
def test_ports_come_from_the_origins_with_the_scheme_default():
    assert worker_binding.ports_of(["https://a.test", "http://b.test", "http://c.test:1234/v1",
                                    "https://[::1]:8443/x"]) == {443, 80, 1234, 8443}
    # What is not a plain web address names no port: the stand-in of a value that could not be read, a scheme
    # that is not http, a port out of range.
    assert worker_binding.ports_of(["invalid:0123", "ftp://a.test:21", "http://a.test:99999", "", "http://"]) == set()


# ----------------------------------------------------------------------------------- definitions, on a fake hub
class Hub:
    """A Plugin Hub folder with one translator installed, an engine instance and two connections."""

    def __init__(self, tmp_path, session_factory=None):
        self.root = tmp_path / "hub"
        self.config = tmp_path / "config"
        (self.config / "config").mkdir(parents=True)
        env_bin = self.root / "shared_envs" / "env1" / "bin"
        env_bin.mkdir(parents=True)
        os.symlink(os.path.realpath(sys.executable), env_bin / "python")
        self.bundle = self.root / "bundles" / PLUGIN / "1.0"
        self.bundle.mkdir(parents=True)
        (self.bundle / "sample_mod.py").write_text(
            "import asyncio\n"
            "from bazarr_plugin_sdk.translator.v1 import TranslatorPlugin\n\n\n"
            "class Sample(TranslatorPlugin):\n"
            "    max_in_flight = 2\n\n"
            "    def describe(self):\n"
            "        return {'name': 'sample'}\n\n"
            "    async def test(self, config, secrets, ctx=None):\n"
            "        return {'ok': True, 'message': 'hello', 'ports': sorted((config.get('settings') or {}))}\n",
            encoding="utf-8")
        self.manifest = _manifest()
        self.installation = SimpleNamespace(provider_id=PLUGIN, active_path=str(self.bundle),
                                            python_path=str(env_bin / "python"), manifest=self.manifest)
        self.installed = {PLUGIN: self.installation}
        self.extra_known = set()  # inventory rows that are not running: held back, switched off, staged
        self.rows = []  # (instance id, plugin id, enabled, settings, [(connection plugin, type, enabled, settings)])

    def store(self):
        rows = self.rows

        class Store:
            def list_instances(self_inner, plugin_id=None):
                return [SimpleNamespace(id=index, plugin_id=plugin, enabled=enabled, settings=settings,
                                        connections=connections)
                        for index, (plugin, enabled, settings, connections) in enumerate(rows, 1)]

            def list_connections(self_inner, instance_id):
                return [SimpleNamespace(plugin_id=plugin, backend_type=kind, enabled=enabled, settings=settings)
                        for plugin, kind, enabled, settings in rows[instance_id - 1][3]]

            def settings_of(self_inner, row):
                return dict(row.settings)

        return Store()

    def definitions(self, **kwargs):
        return worker_binding.TranslatorWorkers(
            installation_for=lambda plugin_id: self.installed.get(plugin_id), store_factory=self.store,
            known_ids=lambda: set(self.installed) | self.extra_known,
            hub_dir=lambda: self.root, config_dir=lambda: str(self.config), reserved_ports=lambda: {6767}, **kwargs)


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path)


def test_the_long_lived_worker_is_admitted_to_the_ports_its_connections_and_manifest_need(hub):
    hub.rows = [(PLUGIN, 1, {}, [("", "openai_compatible", 1, {}), ("", "openrouter", 1, {}),
                                  ("", "openai_compatible", 0, {"base_url": "http://off.example.test:7777"})]),
                (PLUGIN, 0, {"mirror": "https://disabled.example.test:4444"},
                 [("", "openai_compatible", 1, {"base_url": "http://other.example.test:5555"})]),
                ("other_plugin", 1, {}, [("", "openai_compatible", 1, {"base_url": "http://x.example.test:3333"})])]
    ports = hub.definitions().ports_for(PLUGIN)
    # DNS and web ports, the manifest's own origin, the engine's mirror, the enabled connections of the enabled engine.
    # The default LAN address of the connection that is on, and not the disabled connection, the disabled engine or the
    # connections of another plugin's engine.
    assert ports == {80, 443, 8443, 9443, 1234}


def test_a_connection_an_extension_serves_adds_its_port_to_the_extension_and_not_the_engine(hub):
    hub.installed["extension"] = SimpleNamespace(provider_id="extension", active_path=str(hub.bundle),
                                                 python_path=hub.installation.python_path,
                                                 manifest=_manifest("extension"))
    hub.rows = [(PLUGIN, 1, {}, [("extension", "openai_compatible", 1, {"base_url": "http://lan.example.test:4321"})])]
    definitions = hub.definitions()
    assert 4321 in definitions.ports_for("extension") and 4321 not in definitions.ports_for(PLUGIN)


def test_bazarrs_own_port_is_never_in_a_set(hub):
    hub.rows = [(PLUGIN, 1, {}, [("", "openai_compatible", 1, {"base_url": "http://lan.example.test:6767"})])]
    assert 6767 not in hub.definitions().ports_for(PLUGIN)
    assert 6767 not in hub.definitions().draft_ports(
        PLUGIN, {"config": {"connection": {"type": "openai_compatible",
                                           "settings": {"base_url": "http://lan.example.test:6767"}}}})


def test_a_draft_admits_the_port_it_names_and_nothing_a_saved_connection_has(hub):
    hub.rows = [(PLUGIN, 1, {}, [("", "openai_compatible", 1, {"base_url": "http://saved.example.test:5555"})])]
    definitions = hub.definitions()
    draft = {"config": {"settings": {}, "connection": {
        "type": "openai_compatible", "settings": {"base_url": "http://draft.example.test:2468/v1"}}}}
    assert definitions.draft_ports(PLUGIN, draft) == {80, 443, 8443, 9443, 2468}
    # A draft that names no address takes the back end's default, a request with no config only the web ports.
    assert definitions.draft_ports(PLUGIN, {"config": {"connection": {"type": "openai_compatible"}}}) == \
        {80, 443, 8443, 1234}
    assert definitions.draft_ports(PLUGIN, {}) == {80, 443}


def test_a_plugin_that_is_not_installed_or_has_nothing_to_run_has_no_definition(hub):
    definitions = hub.definitions()
    assert definitions.definition_for("missing") is None
    assert definitions.one_call_for("missing", {}, CALL) is None
    for broken in ({"entry_module": None}, {"entry_class": "not a class"}, {"entry_module": "../escape"}):
        hub.installation.manifest = {**hub.manifest, **broken}
        assert definitions.definition_for(PLUGIN) is None
    hub.installation.manifest = hub.manifest
    hub.installation.python_path = str(hub.root.parent / "elsewhere" / "bin" / "python")  # not one of the hub's
    assert definitions.definition_for(PLUGIN) is None
    hub.installation.python_path = None
    assert definitions.definition_for(PLUGIN) is None


def test_the_definition_names_its_pool_entry_and_the_one_call_worker_has_its_own(hub):
    definitions = hub.definitions()
    resident = definitions.definition_for(PLUGIN)
    one_call = definitions.one_call_for(PLUGIN, {}, CALL)
    assert (resident.key, resident.kind) == (PLUGIN, "translator")
    assert one_call.key == f"{PLUGIN}{workers.ONE_CALL_SUFFIX}:{CALL}" != resident.key
    assert definitions.one_call_for(PLUGIN, {}, "not a call id") is None
    assert set(resident.host_calls) == {"reserve", "usage", "checkpoint"}
    assert [budget.path for budget in resident.budgets] == [str(hub.root / "run" / PLUGIN / "data"),
                                                            str(hub.root / "run" / PLUGIN / "tmp")]


def test_the_launch_spec_confines_the_worker_to_its_bundle_and_two_folders_of_its_own(hub):
    hub.rows = [(PLUGIN, 1, {}, [("", "openai_compatible", 1, {})])]
    definitions = hub.definitions()
    spec = definitions._spec(PLUGIN, definitions.ports_for(PLUGIN))
    data, temp = hub.root / "run" / PLUGIN / "data", hub.root / "run" / PLUGIN / "tmp"
    assert data.is_dir() and temp.is_dir()
    assert spec.args[:2] == ("-I", "-B") and spec.args[2].endswith("bazarr_plugin_sdk/_bootstrap.py")
    assert spec.args[3:] == ("--entry", "sample_mod:Sample", "--path", str(hub.bundle))
    assert spec.cwd == str(hub.bundle)
    assert set(spec.policy.read_write) == {str(data), str(temp)}
    assert str(hub.bundle) in spec.policy.read_only
    granted = (*spec.policy.read_only, *spec.policy.read_write, *spec.policy.devices, *spec.policy.resolve_unix)
    # No /dev/shm, and nothing that holds the configuration or the database.
    assert not [path for path in granted if path.startswith("/dev/shm") or "shm" in path.split("/")]
    for protected in (hub.config / "config", hub.config / "db"):
        assert not [path for path in granted if str(protected).startswith(path.rstrip("/") + "/")]
    assert spec.policy.connect_ports == {53, 80, 443, 8443, 9443, 1234}
    assert spec.env["HOME"] == str(data) and spec.env["TMPDIR"] == str(temp)
    assert spec.env["BAZARR_PLUGIN_DATA_DIR"] == str(data)
    assert not [name for name in spec.env if "proxy" in name.lower()]


def test_every_start_reads_the_release_and_the_ports_that_are_current_then(hub):
    hub.rows = [(PLUGIN, 1, {}, [])]
    definitions = hub.definitions()
    ports = {"now": frozenset({443})}
    first = definitions._spec(PLUGIN, ports["now"])
    other = hub.root / "bundles" / PLUGIN / "2.0"
    other.mkdir(parents=True)
    hub.installation.active_path = str(other)
    second = definitions._spec(PLUGIN, frozenset({443, 9999}))
    assert first.cwd == str(hub.bundle) and second.cwd == str(other)
    assert first.policy.connect_ports == {53, 443} and second.policy.connect_ports == {53, 443, 9999}
    hub.installed.pop(PLUGIN)
    from provider_hub.sandbox.policy import PolicyError

    with pytest.raises(PolicyError):
        definitions._spec(PLUGIN, frozenset({443}))


def test_a_real_sdk_worker_starts_through_the_launcher_and_answers_a_short_request(hub):
    """The definition is good enough to start a real plugin worker: the SDK runner in isolated mode, from the
    bundle, in the sandbox where the host has one, and a one-call worker that is gone afterwards."""
    pool = WorkerPool(autostart=True)
    try:
        definitions = hub.definitions()
        port = workers.PoolWorkers(pool, definitions.definition_for, one_call_for=definitions.one_call_for,
                                   ports_for=definitions.ports_for)
        answer = port.call(PLUGIN, workers.TEST_OP, {"config": {"settings": {"mirror": "https://m.example.test"}}},
                           {}, timeout=30)
        assert answer.result["ok"] is True and answer.result["message"] == "hello"
    finally:
        pool.close()


# -------------------------------------------------- short requests never touch the worker that holds translations
def _fake_definition(key, command, env, **kwargs):
    return WorkerDefinition(key, kind="translator", command=command, env=env, session_limits=_fast_session(),
                            **kwargs)


def _translate(port, plugin=PLUGIN, job="job-1"):
    request = {"job_id": job, "target": {"code": "fr"}, "lines": [{"position": 0, "text": "One"}],
               "config": {"connection": {"id": "1", "type": "openrouter", "settings": {}}}}
    return port.submit(plugin, request, {}, context=None, on_progress=None)


def test_a_probe_that_ignores_its_cancel_never_ends_the_worker_that_holds_a_translation():
    pool = _pool()
    try:
        script = {"*": [{"wait_cancel": True}]}
        resident = _fake_definition(PLUGIN, fake_translator_command(), fake_translator_env(script))

        def one_call(plugin_id, params, call_id):
            # The one-call worker is a worker that sleeps through its cancel: only the grace period ends it.
            return _fake_definition(f"{plugin_id}{workers.ONE_CALL_SUFFIX}:{call_id}", fake_worker_command(),
                                    fake_worker_env())

        port = workers.PoolWorkers(pool, lambda plugin_id: resident, one_call_for=one_call)
        held = _translate(port)
        time.sleep(0.3)  # the translation is running
        started = time.monotonic()
        with pytest.raises((errors.RequestTimeout, errors.RequestCancelled, errors.SessionError)):
            port.call(PLUGIN, "sleep", {"seconds": 60, "ignore_cancel": True}, {}, timeout=0.3)
        assert time.monotonic() - started < 15
        # The probe overran its deadline and its worker was ended after the cancel grace. The worker that holds the
        # translation was not touched: its request is still running, and answers its cancel like any other.
        assert not held.done()
        assert pool.state(PLUGIN).value in ("ready", "busy")
        held.cancel("done")
        with pytest.raises((errors.RequestCancelled, errors.RequestFailed)):
            held.wait(10)
    finally:
        pool.close()


# ------------------------------------------------------------------------------- a port change waits for the jobs
class CountingStarts:
    """A ``spawn`` that counts the workers it starts, each one a fake translator."""

    def __init__(self, script):
        self.starts = []
        self.script = script

    def __call__(self):
        env = {**os.environ, **fake_translator_env(self.script)}
        process = subprocess.Popen(fake_translator_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, start_new_session=True)
        self.starts.append(process)
        return process


def test_a_changed_port_set_restarts_the_worker_only_after_its_jobs_finish():
    pool = _pool()
    try:
        starts = CountingStarts({"*": [{"wait_cancel": True}]})
        definition = WorkerDefinition(PLUGIN, kind="translator", spawn=starts, session_limits=_fast_session())
        wanted = {"ports": frozenset({443})}
        port = workers.PoolWorkers(pool, lambda plugin_id: definition, ports_for=lambda plugin_id: wanted["ports"])
        first = _translate(port, job="job-1")
        time.sleep(0.3)
        assert len(starts.starts) == 1
        # A connection now needs another port. The job that is running is not interrupted ...
        wanted["ports"] = frozenset({443, 1234})
        second = _translate(port, job="job-2")
        time.sleep(0.3)
        assert len(starts.starts) == 2  # a new worker serves the new job
        assert not first.done() and starts.starts[0].poll() is None  # ... and the old worker is still up for it
        # The old worker goes only when its job is done.
        first.cancel("done")
        with pytest.raises((errors.RequestCancelled, errors.RequestFailed)):
            first.wait(10)
        deadline = time.monotonic() + 10
        while starts.starts[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert starts.starts[0].poll() is not None
        assert not second.done() and starts.starts[1].poll() is None
        # Nothing changed since: the next job reuses the worker it has.
        third = _translate(port, job="job-3")
        time.sleep(0.2)
        assert len(starts.starts) == 2
        for handle in (second, third):
            handle.cancel("done")
    finally:
        pool.close()


def test_a_port_set_that_cannot_be_read_changes_nothing():
    pool = _pool()
    try:
        starts = CountingStarts({"*": [{"wait_cancel": True}]})
        definition = WorkerDefinition(PLUGIN, kind="translator", spawn=starts, session_limits=_fast_session())
        answers = iter([frozenset({443}), RuntimeError("the database is busy"), frozenset({443})])

        def ports_for(plugin_id):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        port = workers.PoolWorkers(pool, lambda plugin_id: definition, ports_for=ports_for)
        handles = [_translate(port, job=f"job-{index}") for index in range(3)]
        time.sleep(0.3)
        assert len(starts.starts) == 1
        for handle in handles:
            handle.cancel("done")
    finally:
        pool.close()


def test_retiring_a_plugin_that_never_had_a_worker_is_a_no_op_and_one_that_has_is_retired_after_its_jobs():
    pool = _pool()
    try:
        starts = CountingStarts({"*": [{"wait_cancel": True}]})
        definition = WorkerDefinition(PLUGIN, kind="translator", spawn=starts, session_limits=_fast_session())
        port = workers.PoolWorkers(pool, lambda plugin_id: definition)
        port.retire("nobody")
        held = _translate(port)
        time.sleep(0.3)
        port.retire(PLUGIN)
        time.sleep(0.2)
        assert not held.done() and starts.starts[0].poll() is None
        held.cancel("done")
        with pytest.raises((errors.RequestCancelled, errors.RequestFailed)):
            held.wait(10)
    finally:
        pool.close()


# ------------------------------------------------------------------------------------------ a draft's one-call worker
def test_a_draft_test_runs_in_a_one_call_worker_that_admits_the_drafts_port(hub):
    hub.rows = [(PLUGIN, 1, {}, [])]
    seen = []

    class Pool:
        def definition(self, key):
            raise AssertionError("a test never reaches the long-lived worker")

        def run_once(self, definition, op, params, **kwargs):
            seen.append((definition, op, kwargs))
            return SimpleNamespace(result={"ok": True}, data=b"")

    definitions = hub.definitions()
    port = workers.PoolWorkers(Pool(), definitions.definition_for, one_call_for=definitions.one_call_for,
                               ports_for=definitions.ports_for)
    draft = {"config": {"connection": {"type": "openai_compatible",
                                       "settings": {"base_url": "http://draft.example.test:2468/v1"}}}}
    port.call(PLUGIN, workers.TEST_OP, draft, {"api_key": "k" * 12}, timeout=20)
    ((definition, op, kwargs),) = seen
    assert definition.key.startswith(PLUGIN + workers.ONE_CALL_SUFFIX + ":")
    assert (op, kwargs["timeout"]) == ("test", 20)
    assert kwargs["secrets"] == {"api_key": "k" * 12}
    assert 2468 in definitions.draft_ports(PLUGIN, draft)


# ------------------------------------------------------------------------------------------------------ binding
@pytest.fixture
def unbound():
    workers.unbind()
    worker_binding.shutdown()
    yield
    worker_binding.shutdown()
    workers.unbind()


def test_with_the_switch_off_nothing_is_bound(unbound, monkeypatch):
    monkeypatch.setattr("subtitles.tools.translate.runtime.runtime_v2_flag", lambda: False)
    assert worker_binding.bind_when_enabled() is False
    assert not workers.is_bound()
    worker_binding.plugin_changed(PLUGIN)  # nothing to retire
    worker_binding.shutdown()


def test_with_the_switch_on_the_dispatcher_is_bound_to_the_pool_once_and_shutdown_unbinds(unbound, monkeypatch, tmp_path):
    monkeypatch.setattr("subtitles.tools.translate.runtime.runtime_v2_flag", lambda: True)
    monkeypatch.setattr(worker_binding, "_config_dir", lambda: str(tmp_path))
    assert worker_binding.bind_when_enabled() is True
    port = workers.bound_port()
    assert workers.is_bound() and isinstance(port, workers.PoolWorkers)
    assert worker_binding.bind_when_enabled() is True and workers.bound_port() is port  # once
    worker_binding.shutdown()
    assert not workers.is_bound()
    worker_binding.shutdown()  # twice is fine


def test_the_kind_retires_the_worker_when_a_release_activates_or_the_plugin_is_turned_off_or_removed(unbound, monkeypatch):
    from provider_hub.kinds.translator import TranslatorKind

    retired = []

    class Port(workers.PoolWorkers):
        def __init__(self):
            pass

        def retire(self, plugin_id):
            retired.append(plugin_id)

    monkeypatch.setattr(worker_binding, "_port", Port())

    class Store:
        def release_plugin(self, plugin_id):
            return SimpleNamespace(notices=[])

        def plugin_disabled(self, plugin_id):
            return SimpleNamespace(notices=[])

    Store.delete_plugin_data = lambda self, plugin_id: None
    removed = []
    monkeypatch.setattr(worker_binding, "plugin_removed", removed.append)
    monkeypatch.setattr("provider_hub.kinds.translator._store", lambda: Store())
    monkeypatch.setattr("subtitles.tools.translate.runtime.plugin_update.bring_up_to_date", lambda *a: None)
    kind = TranslatorKind()
    assert kind.on_activated(PLUGIN, None, {}) is None
    assert kind.on_removed(PLUGIN) is True
    assert kind.set_enabled({"provider_id": PLUGIN}, False) is True
    assert kind.set_enabled({"provider_id": PLUGIN}, True) is True  # turning it on retires nothing
    assert retired == [PLUGIN, PLUGIN, PLUGIN]
    kind.after_removed(PLUGIN)  # the removal is saved: the plugin's folders go too
    assert removed == [PLUGIN]


@pytest.mark.parametrize("switch,bound", [(False, False), (True, True)])
def test_startup_binds_the_workers_only_with_the_switch_on(tmp_path, switch, bound):
    """``init`` is what a start imports first. With the switch off it never loads the binding."""
    import yaml

    root = Path(__file__).resolve().parents[2]
    config_dir = tmp_path / "data"
    (config_dir / "config").mkdir(parents=True)
    (config_dir / "config" / "config.yaml").write_text(yaml.safe_dump({"translator": {"runtime_v2": switch}}),
                                                       encoding="utf-8")
    start = ("import sys, time; sys.path[:0] = [sys.argv[1], sys.argv[2]]; "
             "wait = time.sleep; time.sleep = lambda seconds: wait(min(seconds, 0.01)); "
             "import init; from subtitles.tools.translate.runtime import workers; "
             "print('state', 'subtitles.tools.translate.runtime.worker_binding' in sys.modules, workers.is_bound()); "
             "from subtitles.tools.translate.runtime import worker_binding as b; b.shutdown()")
    environment = {**os.environ, "BAZARR_CONFIG_DIR": str(config_dir), "NO_CLI": "true", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-c", start, str(root / "bazarr"), str(root / "custom_libs")],
                          cwd=root / "bazarr", env=environment, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == f"state {bound} {bound}"


def test_the_first_start_after_an_upgrade_runs_its_startup_tasks_as_soon_as_the_migration_made_the_tables(
        tmp_path, monkeypatch):
    """init runs with no runtime tables, and says nothing about it. The call after the migrations does the work at
    once: it does not wait for the remembered "tables missing" answer to run out."""
    import importlib.util

    import sqlalchemy as sa

    import app.database as app_database
    import secret_store.scrub as scrub
    from subtitles.tools.translate.runtime import dispatch
    import subtitles.tools.translate.runtime as runtime

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    monkeypatch.setattr(app_database, "engine", engine)
    monkeypatch.setattr(runtime, "_tables_present", False)
    monkeypatch.setattr(runtime, "_absent_until", 0.0)
    monkeypatch.setattr(runtime, "_startup_done", False)
    monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: True)
    calls = []
    monkeypatch.setattr(scrub, "refresh_secret_values", lambda settings: calls.append("scrub"))
    monkeypatch.setattr(dispatch, "recover_after_restart", lambda: calls.append("recover"))

    # init: the tables are not there. Nothing runs, nothing is raised.
    assert runtime.run_startup_tasks() is False and calls == []
    assert runtime.runtime_v2_enabled() is False  # and that answer is now remembered for the next 30 seconds

    # The migrations run.
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "a7c2e9f4b183_translator_runtime_tables.py"
    spec = importlib.util.spec_from_file_location("runtime_tables_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with engine.begin() as connection:
        revision.create_translator_runtime_tables(connection)

    # Right after them, inside the remembered window: the tasks run, once, and the runtime reads as enabled.
    assert runtime.run_startup_tasks() is True
    assert calls == ["scrub", "recover"]
    assert runtime.runtime_v2_enabled() is True
    assert runtime.run_startup_tasks() is True and calls == ["scrub", "recover"]
    engine.dispose()


def test_a_start_where_the_tables_exist_at_init_runs_its_startup_tasks_once(tmp_path, monkeypatch):
    import importlib.util

    import sqlalchemy as sa

    import app.database as app_database
    import secret_store.scrub as scrub
    import subtitles.tools.translate.runtime as runtime
    from subtitles.tools.translate.runtime import dispatch

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ready.db'}")
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "a7c2e9f4b183_translator_runtime_tables.py"
    spec = importlib.util.spec_from_file_location("runtime_tables_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with engine.begin() as connection:
        revision.create_translator_runtime_tables(connection)
    monkeypatch.setattr(app_database, "engine", engine)
    monkeypatch.setattr(runtime, "_tables_present", False)
    monkeypatch.setattr(runtime, "_absent_until", 0.0)
    monkeypatch.setattr(runtime, "_startup_done", False)
    monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: True)
    calls = []
    monkeypatch.setattr(scrub, "refresh_secret_values", lambda settings: calls.append("scrub"))
    monkeypatch.setattr(dispatch, "recover_after_restart", lambda: calls.append("recover"))
    assert runtime.run_startup_tasks() is True and runtime.run_startup_tasks() is True  # init, then main
    assert calls == ["scrub", "recover"]
    engine.dispose()


def test_the_startup_tasks_do_nothing_with_the_switch_off(monkeypatch):
    import subtitles.tools.translate.runtime as runtime

    monkeypatch.setattr(runtime, "_startup_done", False)
    monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: False)
    assert runtime.run_startup_tasks() is False


# ------------------------------------------------------------------ a probe has folders and a budget of its own
def test_a_short_calls_worker_writes_in_folders_of_its_own_with_their_own_budgets(hub):
    hub.rows = [(PLUGIN, 1, {}, [])]
    definitions = hub.definitions()
    resident = definitions.definition_for(PLUGIN)
    one_call = definitions.one_call_for(PLUGIN, {}, CALL)
    base = hub.root / "run" / PLUGIN
    assert [budget.path for budget in resident.budgets] == [str(base / "data"), str(base / "tmp")]
    assert [budget.path for budget in one_call.budgets] == [str(base / "call" / CALL / "data"),
                                                            str(base / "call" / CALL / "tmp")]
    # The two sets of folders do not hold one another, so one's disk use is never counted for the other.
    for call_path in (budget.path for budget in one_call.budgets):
        for resident_path in (budget.path for budget in resident.budgets):
            assert not call_path.startswith(resident_path + "/") and not resident_path.startswith(call_path + "/")
    spec = definitions._spec(PLUGIN, {443}, CALL)
    assert set(spec.policy.read_write) == {str(base / "call" / CALL / "data"), str(base / "call" / CALL / "tmp")}
    assert spec.env["HOME"] == str(base / "call" / CALL / "data")
    long_lived = definitions._spec(PLUGIN, {443})
    assert not set(long_lived.policy.read_write) & set(spec.policy.read_write)


def test_a_short_calls_folders_go_when_the_call_ends_and_nothing_else_does(hub):
    hub.rows = [(PLUGIN, 1, {}, [])]
    definitions = hub.definitions()
    base = hub.root / "run" / PLUGIN
    definitions._spec(PLUGIN, {443})
    definitions._spec(PLUGIN, {443}, CALL)
    other = "ba9876543210"
    definitions._spec(PLUGIN, {443}, other)
    (base / "call" / CALL / "data" / "cache.bin").write_bytes(b"x")
    outside = hub.root / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    os.symlink(outside, base / "call" / CALL / "tmp" / "link")  # a plugin can leave a link in its own folder
    definitions.one_call_done(PLUGIN, CALL)
    assert not (base / "call" / CALL).exists()
    assert (outside / "keep.txt").read_text() == "keep"  # the link was removed, not followed
    assert (base / "call" / other).is_dir() and (base / "data").is_dir() and (base / "tmp").is_dir()
    definitions.one_call_done(PLUGIN, "../../data")  # not a call id: nothing happens
    definitions.one_call_done("../x", CALL)
    assert (base / "data").is_dir()


def test_removing_the_plugin_clears_its_folders_when_no_worker_of_it_is_left(hub):
    hub.rows = [(PLUGIN, 1, {}, [])]
    definitions = hub.definitions()
    base = hub.root / "run" / PLUGIN
    definitions._spec(PLUGIN, {443})
    definitions.plugin_removed(PLUGIN)
    assert not base.exists()
    definitions.plugin_removed("../escape")
    assert hub.root.is_dir()


def test_a_probe_that_fills_its_folders_is_stopped_alone_and_the_translation_worker_goes_on(tmp_path):
    """A worker is stopped for a folder over its budget. The probe's budget is its own folder's, so a probe that
    writes too much cannot take down the worker that holds translations."""
    from provider_hub.runtime.pool import FolderBudget

    resident_folder = tmp_path / "resident"
    call_folder = tmp_path / "call"
    resident_folder.mkdir()
    call_folder.mkdir()
    (call_folder / "big.bin").write_bytes(b"x" * 4096)  # what the probe wrote: over its budget of 1 KiB
    pool = WorkerPool(PoolLimits(tick=0.05, heartbeat_interval=0.1, acquire_timeout=2.0, close_grace=1.0,
                                 budget_interval=0.0, backoff_base=0.05, backoff_cap=0.2), autostart=True)
    try:
        script = {"*": [{"wait_cancel": True}]}
        resident = _fake_definition(PLUGIN, fake_translator_command(), fake_translator_env(script),
                                    budgets=(FolderBudget(str(resident_folder), 1024, "resident"),))

        def one_call(plugin_id, params, call_id):
            return _fake_definition(f"{plugin_id}{workers.ONE_CALL_SUFFIX}:{call_id}", fake_worker_command(),
                                    fake_worker_env(), budgets=(FolderBudget(str(call_folder), 1024, "call"),))

        port = workers.PoolWorkers(pool, lambda plugin_id: resident, one_call_for=one_call)
        held = _translate(port)
        time.sleep(0.3)
        with pytest.raises(workers.NotSent):  # refused at its start: its folder is over its own budget
            port.call(PLUGIN, "echo", {}, {}, timeout=5)
        time.sleep(0.4)
        assert not held.done() and pool.state(PLUGIN).value in ("ready", "busy")
        held.cancel("done")
        with pytest.raises((errors.RequestCancelled, errors.RequestFailed)):
            held.wait(10)
    finally:
        pool.close()


# --------------------------------------------------------------------- port changes are atomic with the drain
def test_a_submission_never_sees_the_new_port_set_before_the_worker_has_been_retired():
    """The set is recorded after the retirement, under the lock a submission takes first: while the retirement runs,
    another submission waits for it and is then served by the worker that replaces the old one."""
    restarting = threading.Event()
    release = threading.Event()
    order = []

    class Pool:
        def definition(self, key):
            return object()

        def restart_after_drain(self, key):
            order.append("retire-start")
            restarting.set()
            release.wait(10)
            order.append("retire-done")

        def submit(self, key, op, params, **kwargs):
            order.append("submit")
            return object()

    wanted = {"ports": frozenset({443})}
    port = workers.PoolWorkers(Pool(), lambda plugin_id: None, ports_for=lambda plugin_id: wanted["ports"])
    port._ports[PLUGIN] = frozenset({443})
    wanted["ports"] = frozenset({443, 1234})
    first = threading.Thread(target=lambda: port.submit(PLUGIN, {}, {}, context=None, on_progress=None))
    first.start()
    assert restarting.wait(5)
    second = threading.Thread(target=lambda: port.submit(PLUGIN, {}, {}, context=None, on_progress=None))
    second.start()
    time.sleep(0.3)
    assert order == ["retire-start"]  # the second waits for the retirement, it does not run beside it
    release.set()
    first.join(10)
    second.join(10)
    assert order[:2] == ["retire-start", "retire-done"] and order.count("submit") == 2
    assert order.index("submit") > order.index("retire-done")


def test_a_retirement_that_failed_is_tried_again_by_the_next_submission():
    attempts = []

    class Pool:
        def definition(self, key):
            return object()

        def restart_after_drain(self, key):
            attempts.append(key)
            if len(attempts) == 1:
                raise RuntimeError("busy")

        def submit(self, key, op, params, **kwargs):
            return object()

    port = workers.PoolWorkers(Pool(), lambda plugin_id: None, ports_for=lambda plugin_id: frozenset({443, 1234}))
    port._ports[PLUGIN] = frozenset({443})
    port.submit(PLUGIN, {}, {}, context=None, on_progress=None)
    port.submit(PLUGIN, {}, {}, context=None, on_progress=None)
    port.submit(PLUGIN, {}, {}, context=None, on_progress=None)
    assert attempts == [PLUGIN, PLUGIN]  # the failed one, the one that worked, then nothing more


# ------------------------------------------------------------------ overlapping short calls do not refuse each other
def _fake_calls(pool, work=0.3):
    return workers.PoolWorkers(pool, lambda plugin_id: None, one_call_for=lambda plugin_id, params, call_id: _fake_definition(
        f"{plugin_id}{workers.ONE_CALL_SUFFIX}:{call_id}", fake_worker_command(), fake_worker_env()))


def test_three_overlapping_short_calls_for_one_plugin_all_run():
    pool = _pool()
    try:
        port = _fake_calls(pool)
        outcomes = []

        def call():
            try:
                outcomes.append(port.call(PLUGIN, "sleep", {"seconds": 0.3}, {}, timeout=20).result)
            except Exception as error:
                outcomes.append(error)

        for _round in range(3):
            threads = [threading.Thread(target=call) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        refused = [item for item in outcomes if isinstance(item, Exception)]
        assert not refused, refused
        assert len(outcomes) == 9
    finally:
        pool.close()


def test_calls_that_start_as_another_ends_are_not_refused_for_its_teardown():
    pool = _pool()
    try:
        port = _fake_calls(pool)
        errors_seen = []

        def call():
            try:
                port.call(PLUGIN, "sleep", {"seconds": 0.3}, {}, timeout=20)
            except Exception as error:
                errors_seen.append(error)

        threads = []
        for _index in range(10):
            thread = threading.Thread(target=call)
            thread.start()
            threads.append(thread)
            time.sleep(0.35)  # each starts about when the one before it ends
        for thread in threads:
            thread.join(30)
        assert not errors_seen, errors_seen
    finally:
        pool.close()


# ------------------------------------------------- routine short calls are not a quota on hosts without the launcher
def test_routine_short_calls_are_never_counted_toward_the_unverified_cap_but_ones_that_are_killed_are():
    """A plain process (any host without the launcher) is ended without proof that its whole tree went. A one-off run
    whose worker left by itself is the routine end of a short request and is not counted; one the pool had to kill
    still is, so the cap still stops a plugin that leaks."""
    from provider_hub.runtime.pool import PoolUnavailable

    pool = WorkerPool(PoolLimits(tick=0.05, heartbeat_interval=0.1, acquire_timeout=2.0, close_grace=1.0,
                                 budget_interval=0.0, unverified_limit=3), autostart=True)
    try:
        key = "sample:call:aaaaaaaaaaaa"
        clean = WorkerDefinition(key, kind="translator", command=fake_worker_command(), env=fake_worker_env(),
                                 session_limits=_fast_session())
        for _index in range(8):  # more than the cap, one after the other
            assert pool.run_once(clean, "echo", {"n": 1}).result["echo"] == {"n": 1}
        assert key not in pool.status()["unverified_teardowns"]
        for _index in range(3):
            with pytest.raises((errors.RequestTimeout, errors.RequestCancelled, errors.SessionError)):
                pool.run_once(clean, "sleep", {"seconds": 60, "ignore_cancel": True}, timeout=0.2)
        assert pool.status()["unverified_teardowns"][key]["count"] == 3
        with pytest.raises(PoolUnavailable) as capped:
            pool.run_once(clean, "echo", {})
        assert capped.value.code == "unverified_limit"
    finally:
        pool.close()


def test_short_calls_with_a_key_each_are_capped_together_as_one_plugin():
    """Each call has its own pool key, and the cap on workers ended without whole-tree proof is the plugin's: killed
    probes under different keys count toward one limit, and routine ones still count for nothing."""
    from provider_hub.runtime.pool import PoolUnavailable

    pool = WorkerPool(PoolLimits(tick=0.05, heartbeat_interval=0.1, acquire_timeout=2.0, close_grace=1.0,
                                 budget_interval=0.0, unverified_limit=3), autostart=True)
    try:
        def definition(index):
            return WorkerDefinition(f"sample:call:{index:012x}", kind="translator", command=fake_worker_command(),
                                    env=fake_worker_env(), session_limits=_fast_session(), cap_key="sample:call")

        for index in range(8):  # routine: a worker that leaves by itself is never counted
            assert pool.run_once(definition(index), "echo", {"n": 1}).result["echo"] == {"n": 1}
        assert "sample:call" not in pool.status()["unverified_teardowns"]
        for index in range(100, 103):  # three that have to be killed, each under a key of its own
            with pytest.raises((errors.RequestTimeout, errors.RequestCancelled, errors.SessionError)):
                pool.run_once(definition(index), "sleep", {"seconds": 60, "ignore_cancel": True}, timeout=0.2)
        assert pool.status()["unverified_teardowns"]["sample:call"]["count"] == 3
        with pytest.raises(PoolUnavailable) as capped:  # a key never seen before is refused all the same
            pool.run_once(definition(200), "echo", {})
        assert capped.value.code == "unverified_limit"
        # A plugin of another name is not affected.
        other = WorkerDefinition("other:call:000000000001", kind="translator", command=fake_worker_command(),
                                 env=fake_worker_env(), session_limits=_fast_session(), cap_key="other:call")
        assert pool.run_once(other, "echo", {"n": 2}).result["echo"] == {"n": 2}
    finally:
        pool.close()


def test_the_one_call_definition_is_counted_under_its_plugin_apart_from_the_long_lived_worker(hub):
    definitions = hub.definitions()
    first = definitions.one_call_for(PLUGIN, {}, CALL)
    second = definitions.one_call_for(PLUGIN, {}, "ba9876543210")
    assert first.key != second.key and first.count_key == second.count_key == PLUGIN + workers.ONE_CALL_SUFFIX
    assert definitions.definition_for(PLUGIN).count_key == PLUGIN


# ---------------------------------------------------------- a refusal before the request is given to a worker
def test_a_worker_over_its_memory_limit_as_it_starts_is_a_refusal_and_one_killed_later_is_not():
    from provider_hub.runtime.pool import WorkerKilled

    early = WorkerKilled("over its limit", code="memory_limit")
    early.before_send = True
    late = WorkerKilled("over its limit", code="memory_limit")
    for killed, refused in ((early, True), (late, False)):
        class Pool:
            def run_once(self, definition, op, params, **kwargs):
                raise killed

        port = workers.PoolWorkers(Pool(), _definition_of)
        if refused:
            with pytest.raises(workers.NotSent):
                port.call(PLUGIN, "test", {}, {}, timeout=5)
        else:
            with pytest.raises(WorkerKilled):
                port.call(PLUGIN, "test", {}, {}, timeout=5)


def _definition_of(plugin_id):
    return WorkerDefinition(plugin_id, kind="translator", command=["unused"])


def test_the_real_pool_refuses_a_worker_that_starts_over_its_memory_limit_before_sending_anything():
    pool = _pool()
    try:
        def one_call(plugin_id, params, call_id):
            return _fake_definition(f"{plugin_id}{workers.ONE_CALL_SUFFIX}:{call_id}", fake_worker_command(),
                                    fake_worker_env(), memory_limit_bytes=1)

        port = workers.PoolWorkers(pool, lambda plugin_id: None, one_call_for=one_call)
        with pytest.raises(workers.NotSent):
            port.call(PLUGIN, "echo", {"n": 1}, {}, timeout=10)
    finally:
        pool.close()


def test_the_pre_send_check_is_the_frame_the_session_writes():
    """A request a few bytes under the frame cap without its deadline is refused here, not after a worker started."""
    from provider_hub.runtime import frames

    def fits(size, with_deadline):
        message = {"t": frames.REQ, "id": 2 ** 53, "op": "test", "params": {"text": "x" * size},
                   "max_result_bytes": frames.DEFAULT_MAX_RESULT_BYTES, "secrets": {}}
        if with_deadline:
            message["deadline_ms"] = 20000
        try:
            frames.pack_control(message, sender="host")
            return True
        except frames.FrameError:
            return False

    low, high = 0, frames.MAX_CONTROL_BYTES
    while low < high:  # the largest text that fits without a deadline
        middle = (low + high + 1) // 2
        low, high = (middle, high) if fits(middle, False) else (low, middle - 1)
    assert fits(low, False) and not fits(low, True)  # the deadline is what tips it over
    with pytest.raises((ValueError, frames.FrameError)):
        workers.PoolWorkers._check_encodable("test", {"text": "x" * low}, {}, 20)
    workers.PoolWorkers._check_encodable("test", {"text": "x" * (low - 100)}, {}, 20)


@pytest.mark.parametrize("error", [OSError("disk"), KeyError("manifest"), ValueError("policy")])
def test_a_failure_building_the_definition_is_a_refusal_but_a_bug_is_raised(error):
    def broken(plugin_id, params, call_id):
        raise error

    port = workers.PoolWorkers(object(), lambda plugin_id: None, one_call_for=broken)
    with pytest.raises(workers.NotSent):
        port.call(PLUGIN, "test", {}, {}, timeout=5)

    def bug(plugin_id, params, call_id):
        raise TypeError("a bug of ours")

    with pytest.raises(TypeError):
        workers.PoolWorkers(object(), lambda plugin_id: None, one_call_for=bug).call(PLUGIN, "test", {}, {}, timeout=5)


# ----------------------------------------------------------------------------- removal clears the run folders
@pytest.fixture
def bound(hub, monkeypatch):
    """A real pool bound with the fake hub's definitions, for the removal tests."""
    monkeypatch.setattr(worker_binding, "REMOVAL_RECHECK_SECONDS", 0.1)
    hub.rows = [(PLUGIN, 1, {}, [])]
    pool = WorkerPool(autostart=True)
    definitions = hub.definitions()
    port = worker_binding.bind(pool, definitions)
    yield SimpleNamespace(hub=hub, pool=pool, port=port, definitions=definitions)
    worker_binding.shutdown()
    pool.close()
    workers.unbind()


def _wait(condition, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_removal_clears_the_run_folders_once_a_running_worker_has_stopped(bound):
    hub, base = bound.hub, bound.hub.root / "run" / PLUGIN
    bound.port._ensure(PLUGIN)
    bound.pool.warm(PLUGIN)  # an idle worker of the plugin is up
    assert (base / "data").is_dir()
    hub.installed.pop(PLUGIN)  # the removal has gone through
    worker_binding.plugin_removed(PLUGIN)
    # The worker is only retired, so it is still on its way out when the removal is saved.
    assert _wait(lambda: not base.exists()), "the folders were left behind"
    assert bound.port.idle(PLUGIN)
    assert PLUGIN not in worker_binding._pending_removal


def test_removal_waits_for_a_short_call_that_has_a_worker_and_keeps_the_folders_meanwhile(bound):
    hub, base = bound.hub, bound.hub.root / "run" / PLUGIN
    (base / "call" / CALL).mkdir(parents=True)
    gate, started = threading.Event(), threading.Event()

    class Pool:
        def state(self, key):
            from provider_hub.runtime.pool import UnknownWorker

            raise UnknownWorker("never started")

        def teardown_confirmed(self, key):
            return True

        def run_once(self, definition, op, params, **kwargs):
            started.set()
            gate.wait(20)
            return SimpleNamespace(result={}, data=b"")

    port = workers.PoolWorkers(Pool(), lambda plugin_id: None,
                               one_call_for=lambda plugin_id, params, call_id: _definition_of(plugin_id))
    with worker_binding._lock:
        worker_binding._port = port  # the removal looks at this port
    caller = threading.Thread(target=lambda: port.call(PLUGIN, "test", {}, {}, timeout=5))
    caller.start()
    assert started.wait(5)
    hub.installed.pop(PLUGIN)
    worker_binding.plugin_removed(PLUGIN)
    time.sleep(0.5)
    assert base.exists() and not port.idle(PLUGIN)  # a probe is running in a folder of it
    gate.set()
    caller.join(10)
    assert _wait(lambda: not base.exists())


def test_a_removal_that_is_only_staged_arms_no_timer_and_the_sweep_after_the_next_start_finishes_it(bound):
    hub, base = bound.hub, bound.hub.root / "run" / PLUGIN
    bound.definitions._spec(PLUGIN, {443})
    worker_binding.plugin_removed(PLUGIN)  # still installed: the removal waits for a restart
    assert base.exists() and not worker_binding._pending_removal and worker_binding._removal_timer is None
    assert worker_binding.sweep_removed_plugins() == []  # the same process: nothing changed
    assert base.exists()
    hub.installed.pop(PLUGIN)  # the next start: the staged removal was applied
    assert worker_binding.sweep_removed_plugins() == [PLUGIN]
    assert not base.exists()


def test_a_start_clears_the_folders_of_plugins_that_are_not_installed(hub):
    for plugin in (PLUGIN, "ghost"):
        (hub.root / "run" / plugin / "data").mkdir(parents=True)
    (hub.root / "run" / "not a plugin id").mkdir()
    (hub.root / "run" / "stray-file").write_text("x")
    assert hub.definitions().sweep_orphans() == ["ghost"]
    assert (hub.root / "run" / PLUGIN / "data").is_dir() and (hub.root / "run" / "not a plugin id").is_dir()
    assert hub.definitions().sweep_orphans() == []


def test_a_start_leaves_the_folders_of_a_removed_plugin_whose_data_was_kept(hub, monkeypatch):
    for plugin in ("kept_one", "ghost"):
        (hub.root / "run" / plugin / "data").mkdir(parents=True)
        (hub.root / "run" / plugin / "data" / "state.db").write_text("data")
    definitions = hub.definitions()
    monkeypatch.setattr(worker_binding, "_definitions", definitions)
    monkeypatch.setattr(worker_binding, "_port", SimpleNamespace(retire=lambda plugin_id: None))

    worker_binding.plugin_kept("kept_one")

    assert (hub.root / "run" / "kept_one" / worker_binding.KEPT_MARKER).exists()
    assert definitions.sweep_orphans() == ["ghost"]
    assert (hub.root / "run" / "kept_one" / "data" / "state.db").read_text() == "data"
    # Removing it again with the data deleted takes the marker and the folder with it.
    definitions.plugin_removed("kept_one")
    assert not (hub.root / "run" / "kept_one").exists()


def test_a_later_removal_that_deletes_the_data_wins_over_an_earlier_keep_even_when_it_waits_for_a_restart(
        hub, monkeypatch):
    """Keep, reinstall, then remove again with the data deleted: the removal of a plugin that is running is staged
    until the next start, and the sweep after that start must take the folder, not honour the old mark."""
    folder = hub.root / "run" / PLUGIN
    (folder / "data").mkdir(parents=True)
    (folder / "data" / "state.db").write_text("data")
    definitions = hub.definitions()
    monkeypatch.setattr(worker_binding, "_definitions", definitions)
    monkeypatch.setattr(worker_binding, "_port", SimpleNamespace(retire=lambda plugin_id: None, idle=lambda p: True))

    worker_binding.plugin_kept(PLUGIN)
    assert (folder / worker_binding.KEPT_MARKER).exists()
    # Still installed in this process, as a staged removal is: nothing is cleared yet, but the mark is gone.
    worker_binding.plugin_removed(PLUGIN)
    assert folder.exists() and not (folder / worker_binding.KEPT_MARKER).exists()
    hub.installed.pop(PLUGIN)  # the next start: the staged removal was applied
    assert worker_binding.sweep_removed_plugins() == [PLUGIN]
    assert not folder.exists()


def test_installing_a_plugin_again_ends_what_an_earlier_keep_said_about_its_folders(hub, monkeypatch):
    folder = hub.root / "run" / PLUGIN
    (folder / "data").mkdir(parents=True)
    monkeypatch.setattr(worker_binding, "_definitions", hub.definitions())
    monkeypatch.setattr(worker_binding, "_port", SimpleNamespace(retire=lambda plugin_id: None))
    worker_binding.plugin_kept(PLUGIN)
    assert (folder / worker_binding.KEPT_MARKER).exists()

    worker_binding.plugin_installed(PLUGIN)

    assert not (folder / worker_binding.KEPT_MARKER).exists() and (folder / "data").is_dir()


def test_the_mark_is_written_and_cleared_while_no_binding_is_in_place(hub, monkeypatch):
    """A pool that did not start must not turn a kept folder into a leftover for the next start's sweep."""
    folder = hub.root / "run" / PLUGIN
    (folder / "data").mkdir(parents=True)
    monkeypatch.setattr(worker_binding, "_definitions", None)
    monkeypatch.setattr(worker_binding, "_port", None)
    monkeypatch.setattr(worker_binding, "_hub_dir", lambda: hub.root)

    worker_binding.plugin_kept(PLUGIN)
    assert (folder / worker_binding.KEPT_MARKER).exists()
    worker_binding.plugin_removed(PLUGIN)
    assert not (folder / worker_binding.KEPT_MARKER).exists()


def test_discarding_kept_folders_removes_them_with_their_mark(hub, monkeypatch):
    folder = hub.root / "run" / PLUGIN
    (folder / "data").mkdir(parents=True)
    monkeypatch.setattr(worker_binding, "_definitions", None)
    monkeypatch.setattr(worker_binding, "_port", None)
    monkeypatch.setattr(worker_binding, "_hub_dir", lambda: hub.root)
    worker_binding.plugin_kept(PLUGIN)
    assert (folder / worker_binding.KEPT_MARKER).exists()

    worker_binding.discard_folders(PLUGIN)

    assert not folder.exists()
    worker_binding.discard_folders("not a plugin id")  # nothing to do, and no error


def test_discarding_kept_folders_raises_when_they_cannot_be_removed_and_not_when_they_are_gone(hub, monkeypatch):
    import shutil

    folder = hub.root / "run" / PLUGIN
    (folder / "data").mkdir(parents=True)
    monkeypatch.setattr(worker_binding, "_definitions", None)
    monkeypatch.setattr(worker_binding, "_hub_dir", lambda: hub.root)

    def refused(path, *args, **kwargs):
        raise PermissionError("not allowed")

    monkeypatch.setattr(shutil, "rmtree", refused)
    with pytest.raises(PermissionError):
        worker_binding.discard_folders(PLUGIN)
    assert folder.exists()

    monkeypatch.undo()
    monkeypatch.setattr(worker_binding, "_definitions", None)
    monkeypatch.setattr(worker_binding, "_hub_dir", lambda: hub.root)
    worker_binding.discard_folders(PLUGIN)
    worker_binding.discard_folders(PLUGIN)  # already gone: nothing to do
    assert not folder.exists()


def test_keeping_the_data_of_a_plugin_that_never_ran_marks_nothing(hub, monkeypatch):
    definitions = hub.definitions()
    monkeypatch.setattr(worker_binding, "_definitions", definitions)
    monkeypatch.setattr(worker_binding, "_port", SimpleNamespace(retire=lambda plugin_id: None))
    worker_binding.plugin_kept("never_ran")
    assert not (hub.root / "run" / "never_ran").exists()


# ------------------------------------------------------------------ the sweep only removes what is not installed at all
def _write_inventory(path, rows):
    import json

    path.write_text(json.dumps({"catalog_sources": {}, "catalog_entries": {}, "jobs": [], "installations": rows}),
                    encoding="utf-8")


def _row(**overrides):
    row = {"provider_id": PLUGIN, "name": "Sample", "kind": "translator", "state": "active", "active_version": "1.0",
           "pending_restart": False, "manifest": _manifest(), "enabled": True}
    row.update(overrides)
    return row


def test_the_sweep_keeps_the_folders_of_every_translator_the_inventory_has_a_row_for(hub, monkeypatch, tmp_path):
    """A translator that is held back at boot, switched off or only staged is installed: its data stays. Only a
    folder with no row at all goes."""
    state = tmp_path / "state.json"
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state))
    names = {"running": "runner", "blocked": "blocker", "disabled": "sleeper", "staged": "newcomer", "ghost": "ghost"}
    for plugin in names.values():
        (hub.root / "run" / plugin / "data").mkdir(parents=True)
        (hub.root / "run" / plugin / "data" / "keep.db").write_text("data")
    _write_inventory(state, {
        "runner": _row(provider_id="runner"),
        "blocker": _row(provider_id="blocker", blocked=True),
        "sleeper": _row(provider_id="sleeper", enabled=False),
        "newcomer": _row(provider_id="newcomer", state="staged", active_version=None, staged_version="2.0",
                         pending_restart=True)})
    definitions = worker_binding.TranslatorWorkers(hub_dir=lambda: hub.root, store_factory=hub.store,
                                                   config_dir=lambda: str(hub.config))  # the real inventory reader
    assert definitions.sweep_orphans() == ["ghost"]
    for plugin in ("runner", "blocker", "sleeper", "newcomer"):
        assert (hub.root / "run" / plugin / "data" / "keep.db").read_text() == "data", plugin
    assert not (hub.root / "run" / "ghost").exists()
    # A removal that is staged is not a removal yet.
    assert definitions.installed("blocker") and definitions.installed("newcomer") and not definitions.installed("ghost")


@pytest.mark.parametrize("content", ["not json at all", '["a", "list"]', '{"installations": []}'])
def test_the_sweep_removes_nothing_when_the_inventory_cannot_be_read(hub, monkeypatch, tmp_path, content):
    state = tmp_path / "state.json"
    state.write_text(content, encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state))
    (hub.root / "run" / "keeper" / "data").mkdir(parents=True)
    definitions = worker_binding.TranslatorWorkers(hub_dir=lambda: hub.root, store_factory=hub.store,
                                                   config_dir=lambda: str(hub.config))
    assert definitions.sweep_orphans() == []
    assert (hub.root / "run" / "keeper" / "data").is_dir()


# ------------------------------------------------------------- idle means nothing of the plugin is left running
@pytest.mark.parametrize("retiring,confirmed,idle", [(0, True, True), (1, True, False), (0, False, False)])
def test_a_plugin_with_a_draining_worker_or_an_unconfirmed_teardown_is_not_idle(retiring, confirmed, idle):
    from provider_hub.runtime.pool import WorkerState

    class Pool:
        def state(self, key):
            return WorkerState.STOPPED  # a failed restart can leave the entry stopped with a worker still draining

        def teardown_confirmed(self, key):
            return confirmed

        def status(self):
            return {"workers": {PLUGIN: {"retiring": retiring}}}

    assert workers.PoolWorkers(Pool(), _definition_of).idle(PLUGIN) is idle


# ------------------------------------------------------- a paused plugin says so instead of "in a moment"
def test_a_refusal_for_the_per_plugin_cap_carries_its_wait_and_the_page_says_how_long():
    from provider_hub.runtime.pool import PoolCodes, PoolUnavailable
    from subtitles.tools.translate.runtime import probe
    from subtitles.tools.translate.runtime.settings_service import ProbeRequest  # noqa: F401

    class Pool:
        def run_once(self, definition, op, params, **kwargs):
            raise PoolUnavailable("paused", code=PoolCodes.UNVERIFIED_LIMIT, retry_after=3000.0)

    port = workers.PoolWorkers(Pool(), _definition_of)
    with pytest.raises(workers.NotSent) as refused:
        port.call(PLUGIN, "test", {}, {}, timeout=5)
    assert refused.value.code == "unverified_limit" and refused.value.retry_after == 3000.0
    assert "50 minutes" in probe.PAUSED_TEST.format(minutes=round(refused.value.retry_after / 60))
