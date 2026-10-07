"""
Tests for the request-host check and its approval code.

While no login mode is configured, Bazarr+ answers every request that reaches
it, whatever hostname it arrived for. This suite pins the gate that refuses
hostnames the install does not recognize, the default set of hostnames that are
always answered, the way an operator allows one more, and the same check in the
Docker supervisor, which is the only layer that sees the client-facing hostname
when general.trusted_proxy names an outer proxy.

The middleware tests run against the real application factory wherever the
answer depends on the wiring: a stub app cannot produce the settings shell or
the Socket.IO transport, so its body assertions say nothing about them.
"""
import asyncio
import importlib.util
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
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
    """The 16 hex characters of the current approval code, as announced last
    on the console (a code that was used is announced in its place)."""
    for message in reversed(console_log.messages):
        found = re.search(r"\b[0-9a-f]{16}\b", message)
        if found:
            return found.group(0)
    return None


@pytest.fixture
def real_factory(monkeypatch):
    """The application as the server builds it: create_app plus both blueprints.

    create_app puts the request-host gate, the forwarded-header middleware and
    the supervisor token wrapper in place; the two blueprints are what
    bazarr/app/server.py registers around it. The Socket.IO state is put back
    afterwards, as test_logging_filters does, because the module's one SocketIO
    object is shared with the rest of the suite.
    """
    from api import api_bp
    from app import app as app_module
    from app import config as config_module
    from app import ui as ui_module
    from app import host_allowlist

    socketio = app_module.socketio
    for name in ("server", "sockio_mw", "async_mode"):
        monkeypatch.setattr(socketio, name, getattr(socketio, name, None), raising=False)
    monkeypatch.setattr(socketio, "server_options", dict(socketio.server_options))
    monkeypatch.setattr(host_allowlist, "_refusals", host_allowlist.BoundedRefusalRecorder())
    monkeypatch.setattr(host_allowlist, "_approval_code", host_allowlist._ApprovalCode())

    flask_app = app_module.create_app()
    flask_app.register_blueprint(api_bp, url_prefix=config_module.base_url.rstrip('/') + '/api')
    flask_app.register_blueprint(ui_module.ui_bp, url_prefix=config_module.base_url.rstrip('/'))

    # The api package's import leaves a transaction open on this thread's
    # session, and every request's own begin() would refuse it otherwise.
    from app.database import database as shared_database
    shared_database.close()

    from api.system.account import _login_attempts
    _login_attempts.clear()
    yield flask_app
    _login_attempts.clear()
    shared_database.close()


@pytest.fixture
def shell_template():
    """A minimal build output, so the settings shell render is the real one.

    In CI the UI artifact is downloaded into frontend/build; a bare checkout
    has no build, so this writes one index.html and removes it again.
    """
    from app import ui

    template = Path(ui.frontend_build_path) / "index.html"
    existed = template.exists()
    if not existed:
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(
            '<!DOCTYPE html><html><body>'
            '<div id="shell">BAZARR_SERVER_INJECT = {{ BAZARR_SERVER_INJECT | tojson }}</div>'
            '</body></html>', encoding="utf-8")
    yield template
    if not existed:
        template.unlink()


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
    # The port goes first, then one trailing dot.
    assert effective_host("bazarr.example.com.:8443") == "bazarr.example.com"
    assert effective_host("host..") == "host."
    assert effective_host("  ") == ""
    assert effective_host(None) == ""


def test_a_scalar_entry_string_is_read_as_a_list():
    from app.host_policy import normalize_entries

    assert normalize_entries("hand.example.com") == ["hand.example.com"]
    assert normalize_entries("one.example.com, TWO.example.com") == ["one.example.com",
                                                                     "two.example.com"]
    assert normalize_entries(" , , ") == []


def test_the_approval_form_accepts_only_names_the_allowlist_can_match():
    from app.host_policy import is_approvable_host

    assert is_approvable_host("bazarr.example.com") is True
    assert is_approvable_host("bazarr") is True
    assert is_approvable_host("192.168.1.5") is True
    assert is_approvable_host("fe80::1") is True
    # A wildcard cannot be added through the form, in any spelling.
    assert is_approvable_host("*") is False
    assert is_approvable_host("*.example.com") is False
    assert is_approvable_host("* .example.com") is False
    # Nor can anything that is not a hostname.
    assert is_approvable_host("") is False
    assert is_approvable_host("bad name.example.com") is False
    assert is_approvable_host("<script>alert(1)</script>") is False
    assert is_approvable_host("example.com/path") is False
    assert is_approvable_host(None) is False
    assert is_approvable_host("a" * 254) is False


@pytest.mark.parametrize("remote, trusted_proxy, expected", [
    # The setting's default applies when it is absent: loopback may name the
    # hostname, as in the backend.
    ("127.0.0.1", None, True),
    ("192.168.1.5", None, False),
    # An explicitly empty one trusts nobody.
    ("127.0.0.1", "", False),
    # The configured address, its mapped form, a network, a list, the star.
    ("172.18.0.5", "172.18.0.5", True),
    ("172.18.0.9", "172.18.0.5", False),
    ("::ffff:172.18.0.5", "172.18.0.5", True),
    ("10.0.0.9", "10.0.0.0/8", True),
    ("10.0.0.9", "192.168.0.0/16", False),
    ("10.0.0.9", "10.0.0.8, 10.0.0.9", True),
    ("10.0.0.9", "10.0.0.8, 10.0.0.7", False),
    ("10.0.0.9", "*", True),
    ("proxy.local", "proxy.local", True),
    ("proxy.local", "proxy.other", False),
    (None, "*", False),
    ("127.0.0.1", "localhost", False),
])
def test_a_peer_may_name_the_client_hostname_only_as_configured(remote, trusted_proxy, expected):
    from app.host_policy import peer_is_trusted_proxy

    assert peer_is_trusted_proxy(remote, trusted_proxy) is expected


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


def test_only_the_approval_submission_is_exempt_from_the_check(fresh_state):
    """Exactly the one POST the refusal page's form submits is left open; a
    GET or HEAD for the path is judged by its hostname like everything else."""
    client = _app_with_check(_config()).test_client()

    assert client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER}).status_code == 200
    for method in ("get", "head"):
        response = getattr(client, method)("/host-approval", headers={"Host": FOREIGN_HOST_HEADER})
        body = response.get_data(as_text=True)
        assert response.status_code == 421
        assert "approval-route" not in body
        assert API_KEY not in body


def test_base_url_moves_the_exempt_paths(fresh_state):
    config = _config(base_url="/bazarr")
    client = _app_with_check(config).test_client()

    assert client.post("/bazarr/host-approval",
                      headers={"Host": FOREIGN_HOST_HEADER}).get_data(as_text=True) == "approval-route"
    # Every other method and every other spelling is judged like any request.
    assert client.get("/host-approval",
                     headers={"Host": FOREIGN_HOST_HEADER}).status_code == 421
    assert client.get("/bazarr/host-approval",
                     headers={"Host": FOREIGN_HOST_HEADER}).status_code == 421
    payload = json.loads(client.get("/bazarr/api/system/status",
                                    headers={"Host": FOREIGN_HOST_HEADER}).get_data(as_text=True))
    assert payload["code"] == "host_not_allowed"


# ---------------------------------------------------------------------------
# The bounded refusal record
# ---------------------------------------------------------------------------


def test_the_refusal_record_and_log_are_bounded(fresh_state):
    """Many distinct hostnames may be refused in quick succession, so the
    record holds the 50 most recently refused ones and the log holds at most
    20 refusal lines and one counting line per hour."""
    recorder = fresh_state.BoundedRefusalRecorder()
    lines = 0

    for index in range(1000):
        if recorder.note(f"h{index}.example.com"):
            lines += 1
        if recorder.take_summary():
            lines += 1

    assert len(recorder.recent(86400)) == 50
    assert lines <= 21


def test_a_repeated_host_moves_to_the_end_of_the_record(fresh_state):
    """The record is the most recently refused, not the first sighted: an
    operator's real proxy hostname, refused again and again, is not pushed out
    by 50 one-off names."""
    recorder = fresh_state.BoundedRefusalRecorder()

    for index in range(50):
        recorder.note(f"h{index}.example.com")
    recorder.note("h0.example.com")
    recorder.note("new.example.com")

    recent = recorder.recent(86400)
    assert recent[:3] == ["new.example.com", "h0.example.com", "h49.example.com"]
    assert "h1.example.com" not in recent


def test_the_per_host_log_pace_survives_record_eviction(fresh_state):
    """Evicting a hostname from the record must not reset its once-per-hour
    log pace."""
    clock = [0]
    recorder = fresh_state.BoundedRefusalRecorder(now=lambda: clock[0])

    assert recorder.note("h0.example.com") is True
    for index in range(1, 120):
        recorder.note(f"h{index}.example.com")
    assert "h0.example.com" not in recorder.recent(86400)

    # Still inside the hour of its last line, so no second one.
    clock[0] = 1800
    assert recorder.note("h0.example.com") is False

    # Past the hour, and the line cap has rolled over with it.
    clock[0] = 3700
    assert recorder.note("h0.example.com") is True


def test_the_summary_counts_the_whole_window(fresh_state):
    """The summary line opens at the first suppressed refusal and reports the
    full count once its hour has run, not the first suppression on its own."""
    clock = [0]
    recorder = fresh_state.BoundedRefusalRecorder(now=lambda: clock[0])

    for index in range(40):
        recorder.note(f"h{index}.example.com")
        # The first suppressed refusal opens the window; nothing is due yet.
        assert recorder.take_summary() == 0

    clock[0] = 3600
    assert recorder.take_summary() == 20

    # The count is reset, and the next suppression opens a fresh window.
    assert recorder.take_summary() == 0
    for index in range(40, 62):
        recorder.note(f"h{index}.example.com")
    assert recorder.take_summary() == 0
    clock[0] = 7300
    assert recorder.take_summary() == 2


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


def test_a_used_code_is_replaced_and_the_replacement_is_announced(approval_app, console_log):
    flask_app, config, _ = approval_app
    client = flask_app.test_client()

    client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})
    code = _announced_code(console_log)
    first = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                        data={"host": FOREIGN_HOST_HEADER, "code": code})
    assert first.status_code == 302

    # The code just used is gone, and its replacement is on the console
    # straight away: no restart and no further refusal is needed before the
    # next hostname can be allowed.
    second = client.post("/host-approval", headers={"Host": "second.example.com"},
                         data={"host": "second.example.com", "code": code})
    assert second.status_code == 403
    replacement = _announced_code(console_log)
    assert replacement is not None and replacement != code

    third = client.post("/host-approval", headers={"Host": "second.example.com"},
                        data={"host": "second.example.com", "code": replacement})
    assert third.status_code == 302
    assert config.general.allowed_hosts == [FOREIGN_HOST, "second.example.com"]


def test_concurrent_submissions_cannot_reuse_the_code(fresh_state, console_log):
    """The comparison, the consumption and the replacement share one lock: a
    code submitted many times in the same moment is accepted exactly once."""
    code = fresh_state._approval_code
    code.announce()
    supplied = _announced_code(console_log)
    assert supplied is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(code.verify, [supplied] * 8))

    assert answers.count(True) == 1
    # The replacement is announced, and it is the only code accepted next.
    replacement = _announced_code(console_log)
    assert replacement is not None and replacement != supplied
    assert code.verify(replacement) is True
    assert code.verify("wrong" * 4) is False


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

    for host in ("*", "*.example.com", "bad name.example.com", "<script>alert(1)</script>"):
        response = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                               data={"host": host, "code": code})
        assert response.status_code == 403, host
        assert config.general.allowed_hosts == []


def test_the_approval_endpoint_answers_404_once_a_login_is_configured(monkeypatch):
    from app import ui

    config = _config(auth_type="form")
    monkeypatch.setattr(ui, "settings", config)
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    flask_app.register_blueprint(ui.ui_bp)

    response = flask_app.test_client().post("/host-approval", data={"host": "x.example.com"})

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The settings and health surfaces
# ---------------------------------------------------------------------------


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


@pytest.fixture
def saveable(monkeypatch):
    """save_settings with the disk write and the database stubbed out, as the
    other settings round-trip suites do. The validators stay real, because the
    point here is what they accept."""
    from app import config

    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: None),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    return config


def test_a_one_entry_allowed_hosts_list_round_trips_through_the_settings_save(saveable):
    """A list saved through the settings editor stays a list: without the
    array-keys entry, one hostname would collapse into a bare string."""
    original = saveable.settings.general.allowed_hosts
    try:
        saveable.save_settings([("settings-general-allowed_hosts", ["one.example.com"])])
        assert saveable.settings.general.allowed_hosts == ["one.example.com"]
        saveable.settings.validators.validate()
    finally:
        saveable.settings.general.allowed_hosts = original


def test_a_scalar_allowed_hosts_hand_edit_stays_a_list():
    """config.yaml names one hostname most naturally as a scalar; the
    validator reads it as a one-entry list instead of resetting it to none."""
    from app.config import settings

    original = settings.general.allowed_hosts
    try:
        settings.set("general.allowed_hosts", "hand.example.com")
        settings.validators.validate()
        assert settings.general.allowed_hosts == ["hand.example.com"]
    finally:
        settings.set("general.allowed_hosts", original)


def test_get_health_issues_reports_a_refused_host(schema_session, monkeypatch, fresh_state):
    """The System > Status list carries the hostnames the gate refused."""
    from utilities import health as health_module

    monkeypatch.setattr(health_module, "database", schema_session)
    fresh_state._refusals.note(FOREIGN_HOST)

    issues = health_module.get_health_issues()
    reported = [issue for issue in issues if FOREIGN_HOST in issue["issue"]]

    assert len(reported) == 1
    assert "Allowed Hosts" in reported[0]["issue"]


# ---------------------------------------------------------------------------
# The gate through the real application factory
# ---------------------------------------------------------------------------


REAL_FOREIGN_TARGETS = [
    "/",
    "/settings",
    "/host-approval",
    "/api/system/status",
    "/assets/app.js",
    "/api/socket.io/?EIO=4&transport=polling",
]


def _real_apikey():
    from app.config import settings

    return settings.auth.apikey


def test_the_real_application_refuses_a_foreign_host_everywhere(real_factory, shell_template):
    client = real_factory.test_client()
    apikey = _real_apikey()

    for path in REAL_FOREIGN_TARGETS:
        for method in ("get", "head"):
            response = getattr(client, method)(path, headers={"Host": FOREIGN_HOST_HEADER})
            body = response.get_data(as_text=True)
            assert response.status_code == 421, (path, method)
            assert apikey not in body, (path, method)
            assert "BAZARR_SERVER_INJECT" not in body, (path, method)
            # The answer names the hostname and the ways in.
            if method == "get":
                assert FOREIGN_HOST in body, path
                assert "host_not_allowed" in body or "general.allowed_hosts" in body, path


def test_the_real_application_answers_a_recognized_host_with_the_shell(real_factory,
                                                                       shell_template):
    client = real_factory.test_client()
    apikey = _real_apikey()

    response = client.get("/", headers={"Host": "bazarr:6767"})
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "BAZARR_SERVER_INJECT" in body
    assert apikey in body


def test_a_login_mode_leaves_the_unauthenticated_shell_without_the_key(real_factory,
                                                                       shell_template):
    from app.config import settings

    client = real_factory.test_client()
    apikey = _real_apikey()
    original = settings.auth.type
    try:
        settings.set("auth.type", "form")
        response = client.get("/", headers={"Host": FOREIGN_HOST_HEADER})
        body = response.get_data(as_text=True)
    finally:
        settings.set("auth.type", original)

    assert response.status_code == 200
    assert "BAZARR_SERVER_INJECT" in body
    assert apikey not in body


def test_the_forwarded_host_is_believed_only_from_the_trusted_proxy(real_factory, shell_template):
    """Through the real forwarded-header middleware: a trusted peer may name
    the hostname, and then both the name it carried and the name it was given
    have to pass. Any other peer's forwarded headers never arrive."""
    client = real_factory.test_client()

    # The test client's peer is loopback, which the default trusted_proxy
    # names: the forwarded host is believed, and both are judged.
    assert client.get("/", headers={"Host": FOREIGN_HOST_HEADER,
                                    "X-Forwarded-Host": "localhost:6767"}).status_code == 421
    assert client.get("/", headers={"Host": "localhost:6767",
                                    "X-Forwarded-Host": FOREIGN_HOST_HEADER}).status_code == 421
    assert client.get("/", headers={"Host": "localhost:6767",
                                    "X-Forwarded-Host": "nas.local:6767"}).status_code == 200

    # Any other peer: the forwarded headers are stripped before the gate,
    # and only the hostname it sent decides.
    client.environ_base["REMOTE_ADDR"] = "192.168.1.50"
    assert client.get("/", headers={"Host": FOREIGN_HOST_HEADER,
                                    "X-Forwarded-Host": "localhost:6767"}).status_code == 421
    assert client.get("/", headers={"Host": "localhost:6767",
                                    "X-Forwarded-Host": FOREIGN_HOST_HEADER}).status_code == 200


def test_the_approval_path_get_is_refused_under_a_base_url_too(real_factory, shell_template):
    """With a base_url configured the exemption moves with it, and still
    covers only the one POST: a GET for the path, at the base or at the root,
    carries no shell."""
    from app.config import settings

    client = real_factory.test_client()
    original = settings.general.base_url
    try:
        settings.set("general.base_url", "/bazarr")
        for method in ("get", "head"):
            for path in ("/bazarr/host-approval", "/host-approval"):
                response = getattr(client, method)(path, headers={"Host": FOREIGN_HOST_HEADER})
                body = response.get_data(as_text=True)
                assert response.status_code == 421, (method, path)
                assert _real_apikey() not in body
                assert "BAZARR_SERVER_INJECT" not in body
    finally:
        settings.set("general.base_url", original)


def test_the_boot_announcement_writes_to_the_console_only(fresh_state, console_log, root_log):
    """server.py calls this once at boot; the console is the only place the
    code appears, and the logger that carries it never reaches the root."""
    from app import host_allowlist

    assert host_allowlist.announce_code() is True
    code = _announced_code(console_log)

    assert code is not None
    assert code not in " ".join(root_log.messages)
    assert logging.getLogger(host_allowlist.CODE_LOGGER_NAME).propagate is False


def test_a_thousand_refused_hosts_leave_the_log_bounded(real_factory, console_log, root_log):
    """The PRD's own ceiling: at most 21 refusal-path lines in the log that
    bazarr.log is fed from, and at most one code line on the console."""
    client = real_factory.test_client()

    for index in range(1000):
        assert client.get("/", headers={"Host": f"h{index}.example.com"}).status_code == 421

    refusal_lines = [message for message in root_log.messages
                     if "refused a request for an unrecognized host" in message
                     or "suppressed" in message]
    code_lines = [message for message in console_log.messages if "approval code" in message]

    assert len(refusal_lines) <= 21
    assert len(code_lines) == 1


def test_the_code_is_absent_from_the_log_endpoints(real_factory, console_log, monkeypatch):
    """The two ways an operator reads the log over HTTP: neither carries the
    approval code, while both carry the refusal line that belongs there."""
    from app.logger import FileHandlerFormatter
    from utilities.central import get_log_file_path

    client = real_factory.test_client()
    handler = logging.FileHandler(get_log_file_path(), encoding="utf-8")
    handler.setFormatter(FileHandlerFormatter('%(asctime)s|%(levelname)-8s|%(name)-32s|%(message)s|',
                                              '%Y-%m-%d %H:%M:%S'))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        assert client.get("/", headers={"Host": FOREIGN_HOST_HEADER}).status_code == 421
    finally:
        root.removeHandler(handler)
        handler.close()

    code = _announced_code(console_log)
    assert code is not None

    download = client.get("/bazarr.log", headers={"Host": "bazarr:6767"})
    assert download.status_code == 200
    assert FOREIGN_HOST in download.get_data(as_text=True)
    assert code not in download.get_data(as_text=True)

    from app.config import settings

    page = client.get("/api/system/logs?limit=500", headers={"Host": "bazarr:6767",
                                                             "X-API-KEY": settings.auth.apikey})
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert FOREIGN_HOST in body
    assert code not in body


def test_the_front_listener_report_records_the_host_and_announces(real_factory, console_log,
                                                                  monkeypatch):
    """The report the Docker supervisor's refusals arrive through: with the
    supervisor token it records the hostname and announces the code, and
    without the right token it changes nothing."""
    from app import host_allowlist
    from app.config import settings

    client = real_factory.test_client()
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "supervisor-token")

    wrong = client.post("/_supervisor/host_refusal", headers={"Host": "127.0.0.1:6767",
                                                              "X-Bazarr-Supervisor-Token": "wrong"},
                        data={"host": "front.example.com"})
    assert wrong.status_code == 403
    assert host_allowlist.health_issues(settings) == []

    report = client.post("/_supervisor/host_refusal", headers={"Host": "127.0.0.1:6767",
                                                               "X-Bazarr-Supervisor-Token":
                                                                   "supervisor-token"},
                         data={"host": "front.example.com"})
    assert report.status_code == 204

    issues = host_allowlist.health_issues(settings)
    assert any("front.example.com" in issue["issue"] for issue in issues)
    assert _announced_code(console_log) is not None


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
    assert decision(request_host="upstream:6767", remote="172.18.0.5",
                    forwarded_host="attacker.example.com:443", auth_type=None,
                    trusted_proxy="172.18.0.5", allowed_hosts=[]) == (False, "attacker.example.com")
    assert decision(request_host="upstream:6767", remote="172.18.0.5",
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
    # An absent trusted_proxy keeps the backend's loopback default; an
    # explicitly empty one trusts nobody.
    assert decision(request_host="upstream:6767", remote="127.0.0.1",
                    forwarded_host="bazarr.local", auth_type=None, trusted_proxy=None,
                    allowed_hosts=[]) == (True, "bazarr.local")
    assert decision(request_host="upstream:6767", remote="127.0.0.1",
                    forwarded_host="bazarr.local", auth_type=None, trusted_proxy="",
                    allowed_hosts=[]) == (True, "upstream")
    # When a forwarded host is believed, both hostnames have to pass.
    assert decision(request_host="localhost:6767", remote="172.18.0.5",
                    forwarded_host="attacker.example.com", auth_type=None,
                    trusted_proxy="172.18.0.5", allowed_hosts=[]) == (False, "attacker.example.com")
    assert decision(request_host="attacker.example.com:6767", remote="172.18.0.5",
                    forwarded_host="bazarr.local", auth_type=None, trusted_proxy="172.18.0.5",
                    allowed_hosts=[]) == (False, "bazarr.local")
    assert decision(request_host="localhost:6767", remote="127.0.0.1",
                    forwarded_host="bazarr.local", auth_type=None, trusted_proxy=None,
                    allowed_hosts=[]) == (True, "bazarr.local")


class _Backend:
    def __init__(self, state="running"):
        self.state = state

    def get_status(self):
        return {"state": self.state, "stage_index": 0}


async def _proxy_marker(request):
    return web.Response(text="proxied")


def _write_supervisor_config(tmp_path, text=None):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "config.yaml").write_text(
        text if text is not None else "general: {}\nauth: {}\n", encoding="utf-8")


async def _supervisor_client(tmp_path, monkeypatch, text=None):
    """A started front listener over a throwaway config, proxying to a marker.

    The TestClient must be built inside a running loop, so this is a coroutine
    each test starts and closes rather than a fixture.
    """
    _write_supervisor_config(tmp_path, text)
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
async def test_only_the_approval_submission_is_exempt_at_the_front_listener(tmp_path, monkeypatch):
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        posted = await client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER})
        assert posted.status == 200
        assert await posted.text() == "proxied"

        for method in ("get", "head"):
            response = await getattr(client, method)("/host-approval",
                                                      headers={"Host": FOREIGN_HOST_HEADER})
            body = await response.text()
            assert response.status == 421
            assert "proxied" not in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_supervisor_picks_up_an_allowed_host_saved_by_the_backend(tmp_path, monkeypatch):
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        refused = await client.get("/", headers={"Host": "proxy.example.com:6767"})
        assert refused.status == 421

        _write_supervisor_config(tmp_path,
                                 "general:\n  allowed_hosts:\n  - proxy.example.com\nauth: {}\n")

        response = await client.get("/", headers={"Host": "proxy.example.com:6767"})
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert body == "proxied"


@pytest.mark.asyncio
async def test_the_supervisor_reads_a_scalar_allowed_host_hand_edit(tmp_path, monkeypatch):
    """The same hand edit the backend accepts: one hostname without the list
    syntax still answers that hostname."""
    client = await _supervisor_client(
        tmp_path, monkeypatch, "general:\n  allowed_hosts: proxy.example.com\nauth: {}\n")
    try:
        refused = await client.get("/", headers={"Host": "other.example.com:6767"})
        admitted = await client.get("/", headers={"Host": "proxy.example.com:6767"})
        body = await admitted.text()
    finally:
        await client.close()

    assert refused.status == 421
    assert admitted.status == 200
    assert body == "proxied"


@pytest.mark.asyncio
async def test_the_supervisor_judges_both_hostnames_of_a_forwarded_request(tmp_path, monkeypatch):
    """Under a trusted-proxy peer (host networking, the loopback default), a
    forwarded hostname is believed and both hostnames have to pass."""
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        # The test client's peer is loopback, which the absent setting's
        # default names, so the forwarded host is believed.
        refused = await client.get("/", headers={"Host": "localhost:6767",
                                                 "X-Forwarded-Host": "attacker.example.com"})
        refused_other = await client.get("/", headers={"Host": "attacker.example.com:6767",
                                                       "X-Forwarded-Host": "bazarr.local"})
        admitted = await client.get("/", headers={"Host": "localhost:6767",
                                                  "X-Forwarded-Host": "bazarr.local:6767"})
        body = await admitted.text()
    finally:
        await client.close()

    assert refused.status == 421
    assert refused_other.status == 421
    assert admitted.status == 200
    assert body == "proxied"


@pytest.mark.asyncio
async def test_a_base_url_without_a_leading_slash_still_starts_the_front_listener(tmp_path,
                                                                                  monkeypatch):
    """The base is normalized the way the route code does it, so a hand-typed
    baseUrl does not take the whole listener down at startup."""
    client = await _supervisor_client(
        tmp_path, monkeypatch, "general:\n  base_url: bazarr\nauth: {}\n")
    try:
        refused = await client.get("/", headers={"Host": FOREIGN_HOST_HEADER})
        at_base = await client.post("/bazarr/host-approval", headers={"Host": FOREIGN_HOST_HEADER})
        at_root = await client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER})
    finally:
        await client.close()

    assert refused.status == 421
    assert at_base.status == 200
    assert at_root.status == 200


@pytest.mark.asyncio
async def test_the_supervisor_reports_its_refusals_to_the_backend(tmp_path, monkeypatch):
    """A refusal the front listener answers reaches the backend's record and
    its console code: at most one report per hostname per hour, carrying this
    supervisor's token."""
    reports = []

    async def notice(request):
        reports.append((request.headers.get("X-Bazarr-Supervisor-Token"),
                         await request.post()))
        return web.Response(status=204)

    backend_app = web.Application()
    backend_app.router.add_route("POST", "/_supervisor/host_refusal", notice)
    backend = TestClient(TestServer(backend_app))
    await backend.start_server()
    try:
        monkeypatch.setattr(supervisor, "BACKEND_PORT", backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        client = await _supervisor_client(tmp_path, monkeypatch)
        try:
            await client.get("/", headers={"Host": FOREIGN_HOST_HEADER})
            for _ in range(100):
                if reports:
                    break
                await asyncio.sleep(0.01)
            assert reports == [("the-supervisor-token", {"host": FOREIGN_HOST})]

            # The same hostname within the hour reports nothing more.
            reports.clear()
            await client.get("/", headers={"Host": FOREIGN_HOST_HEADER})
            for _ in range(20):
                if reports:
                    raise AssertionError("the same hostname reported twice")
                await asyncio.sleep(0.01)
        finally:
            await client.close()
    finally:
        await backend.close()


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
