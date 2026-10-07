"""
Tests for the request-host check and its approval code.

While no login mode is configured, Bazarr+ answers every request that reaches
it, whatever hostname it arrived for. This suite pins the gate that refuses
hostnames the install does not recognize, the default set of hostnames that are
always answered, the way an operator allows one more, and the same check in the
Docker supervisor, which is the only layer that sees the client-facing hostname
when general.trusted_proxy names an outer proxy.
"""
import importlib.util
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from flask import Flask


SUPERVISOR_PATH = Path(__file__).resolve().parents[2] / "docker" / "supervisor.py"
_SUPERVISOR_SPEC = importlib.util.spec_from_file_location("bazarr_docker_supervisor_host", SUPERVISOR_PATH)
supervisor = importlib.util.module_from_spec(_SUPERVISOR_SPEC)
_SUPERVISOR_SPEC.loader.exec_module(supervisor)

API_KEY = "testapikeytestapikeytestapikey12"

FOREIGN_HOST = "attacker.example.com"
FOREIGN_HOST_HEADER = "attacker.example.com:6767"


def _config(auth_type=None, allowed_hosts=None, base_url=""):
    return SimpleNamespace(
        auth=SimpleNamespace(type=auth_type, apikey=API_KEY),
        general=SimpleNamespace(base_url=base_url, allowed_hosts=list(allowed_hosts or [])),
    )


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def console_log():
    """Everything the approval-code logger writes, which is the console."""
    from app import host_allowlist

    capture = _Capture()
    logger = logging.getLogger(host_allowlist.CODE_LOGGER_NAME)
    logger.addHandler(capture)
    yield capture
    logger.removeHandler(capture)


@pytest.fixture
def root_log():
    """Everything the root logger writes, which is what bazarr.log receives."""
    capture = _Capture()
    root = logging.getLogger()
    root.addHandler(capture)
    yield capture
    root.removeHandler(capture)


@pytest.fixture
def fresh_state(monkeypatch):
    """A fresh refusal record and approval code for one test."""
    from app import host_allowlist

    monkeypatch.setattr(host_allowlist, "_refusals", host_allowlist.BoundedRefusalRecorder())
    monkeypatch.setattr(host_allowlist, "_approval_code", host_allowlist._ApprovalCode())
    return host_allowlist


def _app_with_check(config):
    from app.host_allowlist import HostAllowlistMiddleware

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"

    @flask_app.route("/probe")
    def probe():  # pragma: no cover - only reached when the gate passes
        return "reached"

    @flask_app.route(config.general.base_url.rstrip("/") + "/host-approval", methods=["POST"])
    def host_approval():  # pragma: no cover - only reached when the gate passes
        return "approval-route", 200

    flask_app.wsgi_app = HostAllowlistMiddleware(flask_app.wsgi_app, config)
    return flask_app


def _announced_code(console_log):
    """The 16 hex characters of the approval code, as announced on the console."""
    for message in console_log.messages:
        found = re.search(r"\b[0-9a-f]{16}\b", message)
        if found:
            return found.group(0)
    return None


# ---------------------------------------------------------------------------
# The policy: which hostnames are recognized
# ---------------------------------------------------------------------------


def test_effective_host_strips_case_ports_brackets_and_a_trailing_dot():
    from app.host_policy import effective_host

    assert effective_host("192.168.1.10:6767") == "192.168.1.10"
    assert effective_host("[::1]:6767") == "::1"
    assert effective_host("::1") == "::1"
    assert effective_host("fe80::1") == "fe80::1"
    assert effective_host("Bazarr.Local:6767") == "bazarr.local"
    assert effective_host("evil.example.com:80") == "evil.example.com"
    assert effective_host("bazarr.") == "bazarr"
    assert effective_host("  ") == ""
    assert effective_host(None) == ""


@pytest.mark.parametrize("host", [
    "192.168.1.10",
    "127.0.0.1",
    "::1",
    "fe80::1",
    "localhost",
    "bazarr",
    "nas",
    "my.nas.local",
    "x.y.home.arpa",
    "a.internal",
    "b.localhost",
    "media.lan",
])
def test_the_default_set_answers_local_names(host):
    from app.host_policy import effective_host, in_default_set

    assert in_default_set(effective_host(host)) is True


@pytest.mark.parametrize("host", [
    "evil.example.com",
    "bazarr.example.com",
    "example.com",
    "host.publicdns.net",
])
def test_the_default_set_refuses_names_from_public_dns(host):
    from app.host_policy import effective_host, in_default_set

    assert in_default_set(effective_host(host)) is False


def test_configured_entries_answer_exact_names_and_subdomains():
    from app.host_policy import host_is_allowed

    assert host_is_allowed("bazarr.example.com:6767", ["bazarr.example.com"], {}) is True
    assert host_is_allowed("BAZARR.Example.com", ["bazarr.example.com"], {}) is True
    # An entry may carry the port an operator copied from an address bar.
    assert host_is_allowed("bazarr.example.com", ["bazarr.example.com:6767"], {}) is True
    assert host_is_allowed("a.proxy.example", ["*.proxy.example"], {}) is True
    assert host_is_allowed("a.b.proxy.example", ["*.proxy.example"], {}) is True
    # The bare domain is not one of the wildcard's labels.
    assert host_is_allowed("proxy.example", ["*.proxy.example"], {}) is False
    assert host_is_allowed("other.example", ["*.proxy.example"], {}) is False
    assert host_is_allowed("another.example.com", ["bazarr.example.com"], {}) is False


def test_environment_entries_merge_into_the_allowlist(monkeypatch):
    from app.host_policy import ALLOWED_HOSTS_ENV, host_is_allowed

    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "one.example.com, TWO.example.com")

    assert host_is_allowed("one.example.com:6767", [], None) is True
    assert host_is_allowed("two.example.com", [], None) is True
    assert host_is_allowed("three.example.com", [], None) is False


def test_a_lone_star_answers_every_hostname():
    from app.host_policy import allows_all_hosts, host_is_allowed, normalize_entries

    assert host_is_allowed("anything.example", ["*"], {}) is True
    assert allows_all_hosts(normalize_entries(["*"])) is True
    # A wildcard entry is not the switch.
    assert allows_all_hosts(normalize_entries(["*.example.com"])) is False


# ---------------------------------------------------------------------------
# The middleware in front of Flask
# ---------------------------------------------------------------------------


def test_the_check_runs_only_without_a_configured_login():
    from app.host_allowlist import check_active

    assert check_active(_config()) is True
    assert check_active(_config(auth_type="form")) is False
    assert check_active(_config(auth_type="basic")) is False


def test_a_foreign_host_is_refused_on_the_page_with_neither_key_nor_shell(fresh_state):
    client = _app_with_check(_config()).test_client()

    response = client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})
    body = response.get_data(as_text=True)

    assert response.status_code == 421
    assert "attacker.example.com" in body
    assert "general.allowed_hosts" in body
    assert "BAZARR_ALLOWED_HOSTS" in body
    assert API_KEY not in body
    # The shell is never handed over: the refusal is a static page.
    assert "reached" not in body
    assert "BAZARR_SERVER_INJECT" not in body


def test_the_api_answers_json_with_a_reason_code(fresh_state):
    client = _app_with_check(_config()).test_client()

    response = client.get("/api/system/status", headers={"Host": FOREIGN_HOST_HEADER})
    payload = json.loads(response.get_data(as_text=True))

    assert response.status_code == 421
    assert payload["code"] == "host_not_allowed"
    assert payload["host"] == FOREIGN_HOST
    assert API_KEY not in payload["message"]


def test_the_socket_transport_is_refused_too(fresh_state):
    client = _app_with_check(_config()).test_client()

    response = client.get("/api/socket.io/?EIO=4&transport=polling",
                          headers={"Host": FOREIGN_HOST_HEADER})
    payload = json.loads(response.get_data(as_text=True))

    assert response.status_code == 421
    assert payload["code"] == "host_not_allowed"


@pytest.mark.parametrize("host_header", [
    "192.168.1.10:6767",
    "127.0.0.1",
    "[::1]:6767",
    "localhost",
    "bazarr:6767",
    "my.nas.local",
    "x.home.arpa",
])
def test_a_recognized_host_is_admitted(fresh_state, host_header):
    client = _app_with_check(_config()).test_client()

    response = client.get("/probe", headers={"Host": host_header})

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "reached"


def test_a_listed_host_is_admitted_with_or_without_its_port(fresh_state):
    client = _app_with_check(_config(allowed_hosts=["bazarr.example.com"])).test_client()

    for host_header in ("bazarr.example.com", "bazarr.example.com:6767", "BAZARR.example.com:80"):
        assert client.get("/probe", headers={"Host": host_header}).status_code == 200


def test_the_middleware_parses_no_forwarded_header_itself(fresh_state):
    """Waitress replaces the host from X-Forwarded-Host for a trusted peer; the
    middleware reads only what it is handed and ignores anything else."""
    client = _app_with_check(_config()).test_client()

    response = client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER,
                                             "X-Forwarded-Host": "localhost"})

    assert response.status_code == 421


@pytest.mark.parametrize("auth_type", ["form", "basic"])
def test_a_configured_login_switches_the_check_off(fresh_state, auth_type):
    client = _app_with_check(_config(auth_type=auth_type)).test_client()

    response = client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})

    assert response.status_code == 200


def test_a_lone_star_switches_the_check_off(fresh_state):
    client = _app_with_check(_config(allowed_hosts=["*"])).test_client()

    response = client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})

    assert response.status_code == 200


def test_the_approval_path_is_exempt_from_the_check(fresh_state):
    client = _app_with_check(_config()).test_client()

    response = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER})

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "approval-route"


def test_base_url_moves_the_exempt_paths(fresh_state):
    config = _config(base_url="/bazarr")
    client = _app_with_check(config).test_client()

    assert client.post("/bazarr/host-approval",
                      headers={"Host": FOREIGN_HOST_HEADER}).get_data(as_text=True) == "approval-route"
    assert client.get("/host-approval",
                     headers={"Host": FOREIGN_HOST_HEADER}).status_code == 421
    payload = json.loads(client.get("/bazarr/api/system/status",
                                    headers={"Host": FOREIGN_HOST_HEADER}).get_data(as_text=True))
    assert payload["code"] == "host_not_allowed"


# ---------------------------------------------------------------------------
# The bounded refusal record
# ---------------------------------------------------------------------------


def test_the_refusal_record_and_log_are_bounded(fresh_state):
    """A page can cycle hostnames at the gate, so the record holds the 50 most
    recent refused hosts and the log holds at most 20 refusal lines and one
    counting line per hour."""
    recorder = fresh_state.BoundedRefusalRecorder()
    lines = 0

    for index in range(1000):
        if recorder.note(f"h{index}.example.com"):
            lines += 1
        if recorder.take_summary():
            lines += 1

    assert len(recorder.recent(86400)) == 50
    assert lines <= 21


def test_a_refusal_is_recorded_for_the_health_page(fresh_state):
    from app.host_allowlist import health_issues

    fresh_state._refusals.note(FOREIGN_HOST)

    issues = health_issues(_config())
    reported = [issue for issue in issues if "attacker.example.com" in issue["issue"]]

    assert len(reported) == 1
    assert "Allowed Hosts" in reported[0]["issue"]


def test_health_warns_when_the_check_is_switched_off(fresh_state):
    from app.host_allowlist import health_issues

    issues = health_issues(_config(allowed_hosts=["*"]))

    assert any("every hostname" in issue["issue"].lower() for issue in issues)


def test_health_is_quiet_when_a_login_is_configured(fresh_state):
    from app.host_allowlist import health_issues

    fresh_state._refusals.note(FOREIGN_HOST)

    assert health_issues(_config(auth_type="form")) == []


# ---------------------------------------------------------------------------
# The approval code and the approval endpoint
# ---------------------------------------------------------------------------


def test_the_code_reaches_the_console_and_neither_the_page_nor_the_log(fresh_state, console_log, root_log):
    client = _app_with_check(_config()).test_client()

    response = client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})
    code = _announced_code(console_log)

    assert response.status_code == 421
    assert code is not None
    assert code not in response.get_data(as_text=True)
    # The root logger feeds bazarr.log; the code must never appear there.
    assert code not in " ".join(root_log.messages)


def test_the_code_is_repeated_on_the_console_at_most_once_an_hour(fresh_state, console_log):
    client = _app_with_check(_config()).test_client()

    client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})
    client.get("/probe", headers={"Host": "second.example.com"})

    announcements = [message for message in console_log.messages if "approval code" in message]
    assert len(announcements) == 1
    assert _announced_code(console_log) is not None


@pytest.fixture
def approval_app(monkeypatch):
    from api.system.account import _login_attempts
    from app import host_allowlist, ui

    config = _config()
    state = {"saved": None}

    def fake_save_settings(items):
        for key, value in items:
            if key == "settings-general-allowed_hosts":
                config.general.allowed_hosts = list(value)
        state["saved"] = list(items)
        return True

    monkeypatch.setattr(ui, "settings", config)
    monkeypatch.setattr(ui, "save_settings", fake_save_settings)
    monkeypatch.setattr(ui, "base_url", "")
    # A fresh code and record: the code is announced once an hour, so one left
    # over from an earlier test would swallow this test's announcement.
    monkeypatch.setattr(host_allowlist, "_refusals", host_allowlist.BoundedRefusalRecorder())
    monkeypatch.setattr(host_allowlist, "_approval_code", host_allowlist._ApprovalCode())

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"

    @flask_app.route("/probe")
    def probe():  # pragma: no cover - only reached when the gate passes
        return "reached"

    flask_app.register_blueprint(ui.ui_bp)
    flask_app.wsgi_app = host_allowlist.HostAllowlistMiddleware(flask_app.wsgi_app, config)

    _login_attempts.clear()
    yield flask_app, config, state
    _login_attempts.clear()


def test_the_console_code_allows_a_refused_host(approval_app, console_log):
    flask_app, config, _ = approval_app
    client = flask_app.test_client()

    assert client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER}).status_code == 421
    code = _announced_code(console_log)
    assert code is not None

    approval = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                           data={"host": FOREIGN_HOST_HEADER, "code": code})

    assert approval.status_code == 302
    assert config.general.allowed_hosts == [FOREIGN_HOST]
    # The gate admits the hostname on the next request, with no restart.
    assert client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER}).status_code == 200


def test_a_used_code_is_replaced(approval_app, console_log):
    flask_app, config, _ = approval_app
    client = flask_app.test_client()

    client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})
    code = _announced_code(console_log)
    first = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                        data={"host": FOREIGN_HOST_HEADER, "code": code})
    assert first.status_code == 302

    second = client.post("/host-approval", headers={"Host": "second.example.com"},
                         data={"host": "second.example.com", "code": code})

    assert second.status_code == 403
    assert "second.example.com" not in config.general.allowed_hosts


def test_a_wrong_code_counts_against_the_login_throttle(approval_app, console_log):
    flask_app, config, _ = approval_app
    client = flask_app.test_client()

    client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})
    code = _announced_code(console_log)

    for _ in range(5):
        wrong = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                            data={"host": FOREIGN_HOST_HEADER, "code": "wrong" * 3})
        assert wrong.status_code == 403

    throttled = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                            data={"host": FOREIGN_HOST_HEADER, "code": code})

    assert throttled.status_code == 429
    assert config.general.allowed_hosts == []


def test_a_star_cannot_be_allowed_through_the_form(approval_app, console_log):
    flask_app, config, _ = approval_app
    client = flask_app.test_client()

    client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})
    code = _announced_code(console_log)

    response = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                           data={"host": "*", "code": code})

    assert response.status_code == 403
    assert config.general.allowed_hosts == []


# ---------------------------------------------------------------------------
# The supervisor's share of the check
# ---------------------------------------------------------------------------


def test_the_supervisor_decision_matrix():
    decision = supervisor.host_check_decision

    # A direct client is judged by the hostname it sent.
    assert decision(request_host="attacker.example.com:6767", remote="192.168.1.10",
                    forwarded_host="", auth_type=None, trusted_proxy=None,
                    allowed_hosts=[]) == (False, "attacker.example.com")
    assert decision(request_host="192.168.1.10:6767", remote="192.168.1.10",
                    forwarded_host="", auth_type=None, trusted_proxy=None,
                    allowed_hosts=[]) == (True, "192.168.1.10")
    # The forwarded host is believed only from the configured proxy address.
    assert decision(request_host="upstream.name:6767", remote="172.18.0.5",
                    forwarded_host="attacker.example.com:443", auth_type=None,
                    trusted_proxy="172.18.0.5", allowed_hosts=[]) == (False, "attacker.example.com")
    assert decision(request_host="upstream.name:6767", remote="172.18.0.5",
                    forwarded_host="bazarr.local", auth_type=None,
                    trusted_proxy="172.18.0.5", allowed_hosts=[]) == (True, "bazarr.local")
    # From any other address the forwarded header is not believed.
    assert decision(request_host="attacker.example.com:6767", remote="10.0.0.9",
                    forwarded_host="localhost", auth_type=None,
                    trusted_proxy="172.18.0.5", allowed_hosts=[]) == (False, "attacker.example.com")
    # A configured login switches the check off, as it does in the backend.
    assert decision(request_host="attacker.example.com", remote="192.168.1.10",
                    forwarded_host="", auth_type="form", trusted_proxy=None,
                    allowed_hosts=[]) == (True, "attacker.example.com")
    # A listed hostname or the switch answers it.
    assert decision(request_host="proxy.example.com", remote="192.168.1.10",
                    forwarded_host="", auth_type=None, trusted_proxy=None,
                    allowed_hosts=["proxy.example.com"]) == (True, "proxy.example.com")
    assert decision(request_host="anything.example", remote="192.168.1.10",
                    forwarded_host="", auth_type=None, trusted_proxy=None,
                    allowed_hosts=["*"]) == (True, "anything.example")


class _Backend:
    def __init__(self, state="running"):
        self.state = state

    def get_status(self):
        return {"state": self.state, "stage_index": 0}


async def _proxy_marker(request):
    return web.Response(text="proxied")


async def _supervisor_client(tmp_path, monkeypatch):
    """A started front listener over a throwaway config, proxying to a marker.

    The TestClient must be built inside a running loop, so this is a coroutine
    each test starts and closes rather than a fixture.
    """
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("general: {}\n", encoding="utf-8")
    monkeypatch.setattr(supervisor, "proxy_handler", _proxy_marker)

    app = supervisor.create_app(str(tmp_path), _Backend())
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_the_supervisor_refuses_a_foreign_host(tmp_path, monkeypatch):
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        response = await client.get("/", headers={"Host": FOREIGN_HOST_HEADER})
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 421
    assert "attacker.example.com" in body
    assert "general.allowed_hosts" in body
    assert "BAZARR_ALLOWED_HOSTS" in body
    assert "host-approval" in body


@pytest.mark.asyncio
async def test_the_supervisor_answers_json_under_the_api_prefix(tmp_path, monkeypatch):
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        response = await client.get("/api/system/status", headers={"Host": FOREIGN_HOST_HEADER})
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 421
    assert payload["code"] == "host_not_allowed"
    assert payload["host"] == FOREIGN_HOST


@pytest.mark.asyncio
async def test_the_supervisor_proxies_a_recognized_host(tmp_path, monkeypatch):
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        response = await client.get("/", headers={"Host": "127.0.0.1:6767"})
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert body == "proxied"


@pytest.mark.asyncio
async def test_the_supervisor_exempts_the_approval_path(tmp_path, monkeypatch):
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        response = await client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER})
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert body == "proxied"


@pytest.mark.asyncio
async def test_the_supervisor_picks_up_an_allowed_host_saved_by_the_backend(tmp_path, monkeypatch):
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        refused = await client.get("/", headers={"Host": "proxy.example.com:6767"})
        assert refused.status == 421

        (tmp_path / "config" / "config.yaml").write_text(
            "general:\n  allowed_hosts:\n  - proxy.example.com\n", encoding="utf-8")

        response = await client.get("/", headers={"Host": "proxy.example.com:6767"})
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert body == "proxied"


@pytest.mark.asyncio
async def test_the_supervisor_bounds_its_refusal_lines(tmp_path, monkeypatch):
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        for index in range(30):
            response = await client.get("/", headers={"Host": f"h{index}.example.com"})
            assert response.status == 421
    finally:
        await client.close()

    assert len(lines) <= 21
