import asyncio
import errno
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]


def _load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "bazarr_supervisor", ROOT / "docker" / "supervisor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installing_providers_stage_present_and_ordered():
    sup = _load_supervisor()
    stages = sup.BackendManager.STAGES
    markers = sup.BackendManager._STAGE_MARKERS

    assert "Installing providers" in stages
    install_idx = stages.index("Installing providers")
    # It runs after launch, before checking for updates.
    assert stages.index("Launching process") < install_idx < stages.index("Checking for updates")

    # Marker indices stay within range and increase in declaration order (forward-only).
    idxs = [idx for _, idx in markers]
    assert idxs == sorted(idxs)
    assert all(0 <= idx < len(stages) for _, idx in markers)
    # The auto-install marker maps to the new stage.
    assert any(marker == "Provider Hub startup auto-install" and idx == install_idx
               for marker, idx in markers)


def test_wait_for_ready_returns_when_process_not_alive():
    # The readiness poll waits as long as the backend process is alive (so a long
    # first-boot migration can't leave the startup screen stuck), but must end
    # promptly when there is no live process rather than looping forever.
    sup = _load_supervisor()
    mgr = sup.BackendManager([])
    mgr.state = mgr.STATE_STARTING
    mgr.process = None  # no live backend process

    asyncio.run(asyncio.wait_for(mgr._wait_for_ready(), timeout=5))

    # It never falsely marks RUNNING without a responding backend.
    assert mgr.state == mgr.STATE_STARTING


# --- listen port, backend port and backend identity -------------------------
#
# Two containers that share one network namespace (network_mode: host, or both
# behind one VPN container) used to collide twice: every supervisor listened on
# 6767 whatever general.port said, and every backend sat on 127.0.0.1:6768, so
# a second supervisor that did get its own listener polled and proxied the
# first instance's backend and called it ready.


def _write_general(config_root, text):
    config = config_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("config_text, expected", [
    (None, (6767, "default")),
    ("general:\n  port: 6868\n", (6868, "general.port")),
    ("general:\n  port: 6767\n", (6767, "general.port")),
    ("general:\n  base_url: /bazarr\n", (6767, "default")),
    ("general:\n  port: abc\n", (6767, "default")),
    ("general:\n  port: '6868'\n", (6767, "default")),
    ("general:\n  port: 0\n", (6767, "default")),
    ("general:\n  port: 70000\n", (6767, "default")),
    ("general:\n  port: true\n", (6767, "default")),
    ("general: [not, a, mapping]\n", (6767, "default")),
    (": not yaml [\n", (6767, "default")),
])
def test_the_configured_listen_port_mirrors_what_the_backend_accepts(tmp_path, config_text, expected):
    # The backend's validator takes an int in 1..65535 and resets anything
    # else to 6767, so the supervisor must reach the same answer before the
    # backend has had a chance to rewrite the file. The second value says
    # where the port came from, for the log line and the bind error.
    sup = _load_supervisor()
    if config_text is not None:
        _write_general(tmp_path, config_text)

    assert sup._configured_listen_port(str(tmp_path)) == expected


def _run_main(sup, monkeypatch, argv, port_file, bind_error=None):
    """Run the real main() with a fake listener and backend.

    The fake listener records the address instead of binding it, or raises
    `bind_error` the way a taken port does. The fake backend waits for main()
    to install its SIGTERM handler and then sends the signal, which is how a
    container stop reaches main() in production. If the handler never appears
    the signal is not sent and the outer timeout fails the test instead of
    killing the test process.
    """
    bound = {}

    class _Site:
        def __init__(self, runner, host, port):
            bound["address"] = (host, port)
            bound["port_file_before_start"] = port_file.exists()

        async def start(self):
            if bind_error is not None:
                raise bind_error
            bound["started"] = True

    launched = []
    default_term = signal.getsignal(signal.SIGTERM)

    async def _fake_run(self):
        launched.append(bound.get("started", False))
        bound["port_file_while_running"] = (
            port_file.read_text(encoding="utf-8") if port_file.exists() else None)
        for _ in range(100):
            if signal.getsignal(signal.SIGTERM) is not default_term:
                os.kill(os.getpid(), signal.SIGTERM)
                return
            await asyncio.sleep(0.05)

    monkeypatch.setattr(sup.web, "TCPSite", _Site)
    monkeypatch.setattr(sup.BackendManager, "run", _fake_run)
    monkeypatch.setattr(sup, "PORT_FILE", port_file, raising=False)
    monkeypatch.setattr(sys, "argv", ["supervisor.py"] + argv)

    result = asyncio.run(asyncio.wait_for(sup.main(), timeout=10))
    return bound, launched, result


@pytest.mark.parametrize("cli, config_text, expected, origin", [
    # The reporter's second instance: general.port set, no --port.
    ([], "general:\n  port: 6868\n", 6868, "(from general.port)"),
    # --port still wins over config.yaml.
    (["--port", "7001"], "general:\n  port: 6868\n", 7001, "(from --port)"),
    # A default install is unchanged, and the log does not claim a setting
    # the operator never made.
    ([], None, 6767, "(default)"),
    ([], "general:\n  port: 6767\n", 6767, "(from general.port)"),
])
def test_main_listens_on_the_port_the_operator_chose(monkeypatch, tmp_path, capsys, cli, config_text,
                                                     expected, origin):
    sup = _load_supervisor()
    if config_text is not None:
        _write_general(tmp_path, config_text)
    port_file = tmp_path / "supervisor.port"

    bound, launched, result = _run_main(
        sup, monkeypatch, ["--no-update", "--config", str(tmp_path)] + cli, port_file)

    assert bound["address"] == ("0.0.0.0", expected)
    # The health check reads this file, so it names the port actually bound.
    assert bound["port_file_while_running"] == f"{expected}\n"
    assert bound["port_file_before_start"] is False
    # The backend is launched only once the listener is up, so a listener
    # that cannot bind leaves no half-started backend behind.
    assert launched == [True]
    assert not result
    assert f"Frontend serving on http://0.0.0.0:{expected} {origin}" in capsys.readouterr().out


@pytest.mark.parametrize("cli, config_text, port, names", [
    # Port set in the UI while it had no effect, and now taken on the host
    # network or below the unprivileged port floor.
    ([], "general:\n  port: 6868\n", 6868, ["general.port", "config.yaml"]),
    (["--port", "7001"], None, 7001, ["--port"]),
    ([], None, 6767, ["general.port", "config.yaml"]),
])
def test_a_port_that_cannot_be_bound_stops_the_supervisor_with_a_clear_message(
        monkeypatch, tmp_path, capsys, cli, config_text, port, names):
    # The UI is unreachable at this point, so the log line is the only place
    # an operator learns which setting to change. Falling back to 6767 would
    # bring back the collision this listener exists to avoid, so the
    # supervisor still stops, with a non-zero status.
    sup = _load_supervisor()
    if config_text is not None:
        _write_general(tmp_path, config_text)
    port_file = tmp_path / "supervisor.port"
    error = OSError(errno.EADDRINUSE,
                    f"error while attempting to bind on address ('0.0.0.0', {port}): address already in use")

    bound, launched, result = _run_main(
        sup, monkeypatch, ["--no-update", "--config", str(tmp_path)] + cli, port_file, bind_error=error)

    assert result == 1
    assert launched == []
    assert not port_file.exists()
    out = capsys.readouterr().out
    message = [line for line in out.splitlines() if "Cannot listen on port" in line]
    assert len(message) == 1, out
    assert f"Cannot listen on port {port}" in message[0]
    assert "address already in use" in message[0]
    for name in names:
        assert name in message[0]
    assert "Traceback" not in out


# The listen port comes from the config.yaml the backend reads, so the
# supervisor has to reach the backend's configuration directory from the same
# arguments and environment. It used to know only "--config DIR" and fell
# back to /config, so "-c DIR" or BAZARR_CONFIG_DIR left it on 6767 whatever
# general.port said.

_CONFIG_DIR_FORMS = [
    ["--no-update", "--config", "/srv/a"],
    ["--no-update", "--config=/srv/a"],
    ["--conf", "/srv/a"],
    ["--co=/srv/a"],
    ["-c", "/srv/a", "--no-update"],
    ["-c/srv/a"],
    ["-c=/srv/a"],
    ["-c", "/srv/a", "--config", "/srv/b"],
    ["--config", "/srv/a", "-c", "/srv/b"],
    ["--no-update"],
    [],
]

_BACKEND_CONFIG_DIRS = r'''
import json
import os
import sys

sys.path.insert(0, os.path.join(sys.argv[1], "bazarr"))
from app.get_args import parser  # noqa: E402

forms = json.loads(sys.argv[2])
print("RESULTS " + json.dumps([parser.parse_args(form).config_dir for form in forms]), flush=True)
'''


@pytest.mark.parametrize("env_dir", [None, "/srv/env"])
def test_the_supervisor_uses_the_config_directory_the_backend_parses(env_dir):
    # Ask the backend's own parser, so a form it accepts cannot drift from
    # what the supervisor reads.
    sup = _load_supervisor()
    env = {k: v for k, v in os.environ.items() if k not in ("BAZARR_CONFIG_DIR", "NO_UPDATE")}
    env["NO_CLI"] = "true"
    environ = {}
    if env_dir is not None:
        env["BAZARR_CONFIG_DIR"] = environ["BAZARR_CONFIG_DIR"] = env_dir
    result = subprocess.run(
        [sys.executable, "-c", _BACKEND_CONFIG_DIRS, str(ROOT), json.dumps(_CONFIG_DIR_FORMS)],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(ROOT))
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULTS ")]
    assert lines, f"the backend parser did not answer (rc {result.returncode}):\n{result.stderr[-4000:]}"
    backend = json.loads(lines[-1][len("RESULTS "):])

    supervisor = [sup._backend_config_dir(form, environ) for form in _CONFIG_DIR_FORMS]

    assert [os.path.realpath(p) for p in supervisor] == [os.path.realpath(p) for p in backend]
    # The forms with a flag really did name a directory, and the flag won.
    assert supervisor[:9] == ["/srv/a"] * 7 + ["/srv/b"] * 2
    if env_dir is not None:
        assert supervisor[9:] == [env_dir, env_dir]


@pytest.mark.parametrize("how", ["-c", "--config=", "BAZARR_CONFIG_DIR"])
def test_main_listens_on_general_port_from_the_backends_config_directory(monkeypatch, tmp_path, capsys, how):
    sup = _load_supervisor()
    _write_general(tmp_path, "general:\n  port: 6868\n")
    port_file = tmp_path / "supervisor.port"
    monkeypatch.delenv("BAZARR_CONFIG_DIR", raising=False)
    if how == "-c":
        argv = ["--no-update", "-c", str(tmp_path)]
    elif how == "--config=":
        argv = ["--no-update", f"--config={tmp_path}"]
    else:
        monkeypatch.setenv("BAZARR_CONFIG_DIR", str(tmp_path))
        argv = ["--no-update"]
    forwarded = []
    original_init = sup.BackendManager.__init__

    def _recording_init(self, bazarr_args):
        forwarded.append(list(bazarr_args))
        original_init(self, bazarr_args)

    monkeypatch.setattr(sup.BackendManager, "__init__", _recording_init)

    bound, launched, result = _run_main(sup, monkeypatch, argv, port_file)

    assert bound["address"] == ("0.0.0.0", 6868)
    assert "(from general.port)" in capsys.readouterr().out
    # The backend still gets the flag exactly as given.
    assert forwarded == [argv]
    assert launched == [True]
    assert not result


def test_the_port_file_is_replaced_atomically(tmp_path):
    sup = _load_supervisor()
    port_file = tmp_path / "supervisor.port"
    port_file.write_text("6767\n", encoding="utf-8")

    sup._write_port_file(6868, port_file)

    assert port_file.read_text(encoding="utf-8") == "6868\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["supervisor.port"]


def test_an_unwritable_port_file_does_not_stop_the_supervisor(tmp_path, capsys):
    sup = _load_supervisor()

    # A directory that does not exist: best effort, no exception.
    sup._write_port_file(6868, tmp_path / "missing" / "supervisor.port")

    # But say what it costs: without the file the health check probes 6767,
    # which on a shared network can be another instance.
    out = capsys.readouterr().out
    assert "health check" in out
    assert "6767" in out and "6868" in out
    assert "tmpfs" in out


def test_the_picked_backend_port_is_a_free_loopback_port():
    sup = _load_supervisor()

    port = sup._pick_backend_port()

    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


class _FakeStdout:
    async def readline(self):
        return b""


class _FakeProcess:
    def __init__(self, manager, sup):
        self._manager = manager
        self._sup = sup
        self.returncode = None
        self.pid = 4242
        self.stdout = _FakeStdout()

    async def wait(self):
        # As if the readiness poll had verified this launch before it exited.
        self._sup.BACKEND_PORT = self._manager.port
        self._manager._should_run = False
        self.returncode = 0
        return 0


def test_nothing_is_proxied_before_the_first_launch_has_proved_itself():
    # Between the listener coming up and the first launch, any port the proxy
    # named would be a guess, and 6768 is where an older neighbour's backend
    # listens.
    sup = _load_supervisor()

    assert sup.BACKEND_PORT is None


def test_each_launch_gets_its_own_loopback_port_and_identity(monkeypatch):
    sup = _load_supervisor()
    mgr = sup.BackendManager(["--no-update", "--config", "/config"])
    picked = iter([41001, 41002])
    monkeypatch.setattr(sup, "_pick_backend_port", lambda: next(picked), raising=False)
    monkeypatch.delenv("BAZARR_SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("BAZARR_BACKEND_HOST", raising=False)

    launches = []

    async def _fake_exec(*cmd, env=None, **kwargs):
        launches.append((list(cmd), dict(env), sup.BACKEND_PORT, mgr.port))
        return _FakeProcess(mgr, sup)

    monkeypatch.setattr(sup.asyncio, "create_subprocess_exec", _fake_exec)

    asyncio.run(asyncio.wait_for(mgr.run(), timeout=10))

    assert len(launches) == 1
    cmd, env, port_seen_by_proxy, port_polled = launches[0]
    assert cmd[cmd.index("--port") + 1] == "41001"
    # The readiness poll follows the launch. The proxy does not, until the
    # poll has seen this supervisor's token there: two supervisors can pick
    # the same free port before either backend binds it.
    assert port_polled == 41001
    assert port_seen_by_proxy is None
    # A backend that exited no longer vouches for its port.
    assert sup.BACKEND_PORT is None
    assert env["BAZARR_BACKEND_HOST"] == "127.0.0.1"
    assert env["BAZARR_SUPERVISOR_TOKEN"] == mgr.token
    assert len(mgr.token) >= 32
    assert "--no-update" in cmd and "/config" in cmd


async def _start_backend_stub(headers):
    async def status(request):
        return web.json_response({"data": {"bazarr_version": "neighbour"}}, headers=headers)

    app = web.Application()
    app.router.add_get("/api/system/status", status)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


class _AliveProcess:
    returncode = None
    pid = 4243


@pytest.mark.parametrize("headers", [
    {},
    {"X-Bazarr-Supervisor-Token": "somebody-elses-token"},
])
def test_the_readiness_poll_rejects_a_neighbours_backend(monkeypatch, headers):
    # Another instance's backend answering on the port this supervisor polls
    # is exactly what a shared network namespace produced. It must not be
    # mistaken for this instance's backend, nor proxied to.
    sup = _load_supervisor()
    mgr = sup.BackendManager([])
    mgr.process = _AliveProcess()

    async def scenario():
        runner, port = await _start_backend_stub(headers)
        mgr.port = port
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(mgr._wait_for_ready(), timeout=2.5)
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

    assert mgr.state == mgr.STATE_STARTING
    assert sup.BACKEND_PORT is None
    assert sup.BACKEND_TOKEN is None


def test_the_readiness_poll_accepts_its_own_backend(monkeypatch):
    sup = _load_supervisor()
    mgr = sup.BackendManager([])
    mgr.process = _AliveProcess()
    seen = {}

    async def scenario():
        runner, port = await _start_backend_stub({"X-Bazarr-Supervisor-Token": mgr.token})
        mgr.port = port
        seen["port"] = port
        try:
            await asyncio.wait_for(mgr._wait_for_ready(), timeout=5)
        finally:
            await runner.cleanup()

    asyncio.run(scenario())

    assert mgr.state == mgr.STATE_RUNNING
    assert sup.BACKEND_PORT == seen["port"]
    # The proxy checks every answer it relays against the same token.
    assert sup.BACKEND_TOKEN == mgr.token


# --- an in-app restart ------------------------------------------------------
#
# bazarr.py restarts bazarr/main.py on the same --port while the supervisor's
# process keeps running. The port is free from the moment the old listener
# closes until the new one binds, which on a shared network namespace is long
# enough for another supervisor's free-port pick to land on it.

class _LineStdout:
    def __init__(self):
        self.lines = asyncio.Queue()

    async def readline(self):
        return await self.lines.get()


class _RestartingProcess:
    returncode = None
    pid = 4244

    def __init__(self):
        self.stdout = _LineStdout()


async def _say(process, line):
    await process.stdout.lines.put(line.encode() + b"\n")
    # The reader handles a line synchronously once readline returns.
    for _ in range(5):
        await asyncio.sleep(0)


async def _eventually(check, seconds):
    for _ in range(int(seconds * 20)):
        if check():
            return True
        await asyncio.sleep(0.05)
    return check()


@pytest.mark.parametrize("new_child_headers, reverified", [
    # The new child of this instance answers with the same token.
    ("own", True),
    # Something else took the port during the restart.
    ({}, False),
    ({"X-Bazarr-Supervisor-Token": "somebody-elses-token"}, False),
])
def test_an_in_app_restart_closes_the_proxy_until_the_new_child_proves_itself(
        monkeypatch, new_child_headers, reverified):
    sup = _load_supervisor()
    mgr = sup.BackendManager([])
    process = _RestartingProcess()
    mgr.process = process
    seen = {}

    async def scenario():
        headers = ({"X-Bazarr-Supervisor-Token": mgr.token} if new_child_headers == "own"
                   else new_child_headers)
        runner, port = await _start_backend_stub(headers)
        mgr.port = port
        reader = asyncio.create_task(mgr._read_stdout())
        try:
            # The first child of a launch is the one the startup poll checks.
            await _say(process, "Bazarr starting child process with PID 100...")
            mgr.state = mgr.STATE_RUNNING
            sup.BACKEND_PORT = port
            await _say(process, "Closing webserver...")
            seen["after_close"] = sup.BACKEND_PORT
            # Nothing polls the listener that is closing: an answer from it
            # in its last moment would reopen the proxy to a port about to
            # be free.
            await asyncio.sleep(0.5)
            seen["held_until_child_start"] = sup.BACKEND_PORT
            await _say(process, "Bazarr is restarting...")
            await _say(process, "Bazarr starting child process with PID 101...")
            seen["reverified"] = await _eventually(lambda: sup.BACKEND_PORT == port, 2.5)
            seen["state"] = mgr.state
            seen["port"] = port
        finally:
            reader.cancel()
            mgr._should_run = False
            await asyncio.sleep(0)
            await runner.cleanup()

    monkeypatch.setattr(sup, "BACKEND_PORT", None)
    asyncio.run(scenario())

    assert seen["after_close"] is None
    assert seen["held_until_child_start"] is None
    assert seen["reverified"] is reverified
    # The container stays healthy through a restart from the UI; only the
    # proxy waits.
    assert seen["state"] == mgr.STATE_RUNNING


def test_the_first_child_of_a_launch_does_not_reopen_the_check(monkeypatch):
    # The startup poll owns the first child. Seeing its start line must not
    # withdraw a port that poll has already verified.
    sup = _load_supervisor()
    mgr = sup.BackendManager([])
    process = _RestartingProcess()
    mgr.process = process
    mgr.port = 41003
    seen = {}

    async def scenario():
        reader = asyncio.create_task(mgr._read_stdout())
        try:
            sup.BACKEND_PORT = 41003
            await _say(process, "Bazarr starting child process with PID 100...")
            seen["port"] = sup.BACKEND_PORT
        finally:
            reader.cancel()

    monkeypatch.setattr(sup, "BACKEND_PORT", None)
    asyncio.run(scenario())

    assert seen["port"] == 41003


def test_the_restart_markers_are_what_the_backend_prints():
    sup = _load_supervisor()

    assert sup.BackendManager._CHILD_START_MARKER in (ROOT / "bazarr.py").read_text(encoding="utf-8")
    server = (ROOT / "bazarr" / "app" / "server.py").read_text(encoding="utf-8")
    assert f'print("{sup.BackendManager._LISTENER_CLOSE_LINE}")' in server


# --- the backend's side: bind address, port fallback, identity header -------
#
# Importing app.server builds the whole web stack and binds the listener at
# import time, so the scenarios run in one child process with waitress's
# create_server and the process exit replaced by recorders.

_SERVER_SCENARIOS = r'''
import errno
import json
import os
import sys

root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "custom_libs"))
sys.path.insert(0, os.path.join(root, "bazarr"))

import waitress.server  # noqa: E402

in_use = set()
calls = []


def fake_create_server(app, host, port, **kwargs):
    calls.append([host, port])
    if "*" in in_use or port in in_use:
        raise OSError(errno.EADDRINUSE, "Address already in use")
    return type("FakeServer", (), {"close": lambda self: None})()


waitress.server.create_server = fake_create_server


class Stopped(Exception):
    pass


import utilities.central  # noqa: E402


def fake_stop(status_code=0):
    raise Stopped(status_code)


utilities.central.stop_bazarr = fake_stop

from app.get_args import args  # noqa: E402
from app import server  # noqa: E402
from literals import EXIT_PORT_ALREADY_IN_USE_ERROR  # noqa: E402


def scenario(env, port, busy):
    for key in ("BAZARR_SUPERVISOR_TOKEN", "BAZARR_BACKEND_HOST"):
        os.environ.pop(key, None)
    os.environ.update(env)
    args.port = port
    in_use.clear()
    in_use.update(busy)
    del calls[:]
    try:
        instance = server.Server()
    except Stopped as stop:
        return {"calls": list(calls), "exit": stop.args[0]}
    return {"calls": list(calls), "exit": None, "port": instance.port, "address": instance.address}


results = {"exit_in_use": EXIT_PORT_ALREADY_IN_USE_ERROR}

# Requests first: a scenario that ends in shutdown closes the database.
client = server.app.test_client()
os.environ.pop("BAZARR_SUPERVISOR_TOKEN", None)
plain = client.get("/api/system/status")
os.environ["BAZARR_SUPERVISOR_TOKEN"] = "t0k3n"
stamped = client.get("/api/system/status")
results["unsupervised_header"] = plain.headers.get("X-Bazarr-Supervisor-Token")
results["supervised_header"] = stamped.headers.get("X-Bazarr-Supervisor-Token")
results["supervised_status"] = stamped.status_code
# The event stream: Socket.IO long-polling, answered by Flask-SocketIO's
# middleware in front of Flask.
event_stream = client.get("/api/socket.io/?EIO=4&transport=polling")
results["event_stream_header"] = event_stream.headers.get("X-Bazarr-Supervisor-Token")
results["event_stream_status"] = event_stream.status_code

supervised = {"BAZARR_SUPERVISOR_TOKEN": "t0k3n", "BAZARR_BACKEND_HOST": "127.0.0.1"}
results["supervised_free"] = scenario(supervised, 41007, set())
results["custom_port_falls_back"] = scenario({}, 8080, {8080})
results["supervised_busy"] = scenario(supervised, 41007, {"*"})
results["default_port_busy"] = scenario({}, None, {"*"})
results["custom_port_busy"] = scenario({}, 8080, {"*"})
print("RESULTS " + json.dumps(results), flush=True)
os._exit(0)
'''


@pytest.fixture(scope="module")
def server_scenarios(tmp_path_factory):
    config_dir = tmp_path_factory.mktemp("server-scenarios")
    for sub in ("backup", "cache", "config", "db", "log", "restore"):
        (config_dir / sub).mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("BAZARR_SUPERVISOR_TOKEN", "BAZARR_BACKEND_HOST", "BAZARR_PG_TEST_URL")}
    env.update({"BAZARR_CONFIG_DIR": str(config_dir), "NO_CLI": "true",
                "SZ_USER_AGENT": "test", "BAZARR_VERSION": "test"})
    result = subprocess.run([sys.executable, "-c", _SERVER_SCENARIOS, str(ROOT)],
                            capture_output=True, text=True, timeout=240, env=env, cwd=str(ROOT))
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULTS ")]
    assert lines, f"the scenarios did not finish (rc {result.returncode}):\n{result.stderr[-4000:]}"
    return json.loads(lines[-1][len("RESULTS "):])


def test_a_supervised_backend_binds_loopback_and_never_retries_on_6767(server_scenarios):
    # Under the supervisor 6767 belongs to the front listener, and on a shared
    # network it may belong to another instance: retrying there can only
    # collide or, worse, succeed. Exit instead; the supervisor restarts the
    # backend on a fresh port.
    busy = server_scenarios["supervised_busy"]
    assert busy["calls"] == [["127.0.0.1", 41007]]
    assert busy["exit"] == server_scenarios["exit_in_use"]

    free = server_scenarios["supervised_free"]
    assert free["calls"] == [["127.0.0.1", 41007]]
    assert free["address"] == "127.0.0.1"
    assert free["port"] == 41007


def test_an_unsupervised_backend_on_6767_exits_without_retrying_the_same_port(server_scenarios):
    busy = server_scenarios["default_port_busy"]
    assert busy["calls"] == [["*", 6767]]
    assert busy["exit"] == server_scenarios["exit_in_use"]


def test_an_unsupervised_backend_on_another_port_falls_back_to_6767_once(server_scenarios):
    busy = server_scenarios["custom_port_busy"]
    assert busy["calls"] == [["*", 8080], ["*", 6767]]
    assert busy["exit"] == server_scenarios["exit_in_use"]

    fallback = server_scenarios["custom_port_falls_back"]
    assert fallback["calls"] == [["*", 8080], ["*", 6767]]
    assert fallback["port"] == 6767


def test_the_backend_names_itself_to_its_supervisor_only_when_supervised(server_scenarios):
    # An unauthenticated status call answers 401, and the readiness poll
    # accepts any answer under 500, so the header must ride on that too.
    assert server_scenarios["unsupervised_header"] is None
    assert server_scenarios["supervised_header"] == "t0k3n"
    assert server_scenarios["supervised_status"] < 500


def test_the_event_stream_names_the_backend_to_its_supervisor_too(server_scenarios):
    # The proxy checks the token on every answer it relays, not only at
    # startup. Socket.IO is answered by middleware in front of Flask, where
    # Flask's own response hooks never run, so the token has to be added
    # outside Flask or the event stream would be refused.
    assert server_scenarios["event_stream_status"] < 500
    assert server_scenarios["event_stream_header"] == "t0k3n"
