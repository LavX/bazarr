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
import http.client
import importlib.util
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import ClientTimeout, TCPConnector, web
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


def _build_real_app(monkeypatch):
    """The application as the server builds it: create_app plus both blueprints.

    create_app puts the request-host gate, the forwarded-header middleware and
    the supervisor token wrapper in place; the two blueprints are what
    bazarr/app/server.py registers around it. The Socket.IO state is saved and
    restored by the caller's monkeypatch, as test_logging_filters does,
    because the module's one SocketIO object is shared with the rest of the
    suite.
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
    return flask_app, _login_attempts.clear, shared_database.close


@pytest.fixture
def real_factory(monkeypatch):
    """The application as the server builds it, for one test."""
    flask_app, _reset_login_attempts, _close_database = _build_real_app(monkeypatch)
    yield flask_app
    _reset_login_attempts()
    _close_database()


@pytest.fixture
def shell_template():
    """A build output shaped like the real frontend/index.html.

    The real page parses the injected payload with the page's own script
    (window.Bazarr = JSON.parse(...)), so the shell tests assert on that
    content and not on the template marker, which the real build renders
    away. In CI the UI artifact is downloaded into frontend/build; a bare
    checkout has no build, so this writes one index.html and removes it
    again.
    """
    from app import ui

    template = Path(ui.frontend_build_path) / "index.html"
    existed = template.exists()
    if not existed:
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(
            '<!DOCTYPE html>\n<html lang="en">\n  <head>\n    <title>Bazarr+</title>\n  </head>\n'
            '  <body>\n    <noscript>You need to enable JavaScript to run this app.</noscript>\n'
            '    <div id="root"></div>\n    <script>\n      try {\n'
            '        window.Bazarr = JSON.parse(`{{BAZARR_SERVER_INJECT | tojson | safe}}`);\n'
            '      } catch (error) {}\n    </script>\n  </body>\n</html>\n', encoding="utf-8")
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
    # The configured address, and the star.
    ("172.18.0.5", "172.18.0.5", True),
    ("172.18.0.9", "172.18.0.5", False),
    # A mapped IPv6 form is a different peer at the backend, whose
    # forwarded-header middleware compares the peer against the setting
    # exactly as written, so it is one here too: one spelling of the setting
    # works at both layers, or neither layer trusts it.
    ("::ffff:172.18.0.5", "172.18.0.5", False),
    ("10.0.0.9", "*", True),
    ("proxy.local", "proxy.local", True),
    ("proxy.local", "proxy.other", False),
    (None, "*", False),
    ("127.0.0.1", "localhost", False),
    # Only the forms the backend accepts: a network or a list matches nobody
    # here, exactly as it does not at the backend, where waitress compares
    # the peer against the value.
    ("10.0.0.9", "10.0.0.0/8", False),
    ("10.0.0.9", "192.168.0.0/16", False),
    ("10.0.0.9", "10.0.0.8, 10.0.0.9", False),
    ("10.0.0.9", "10.0.0.8, 10.0.0.7", False),
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
    assert "window.Bazarr" not in body


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
    """The forwarded-header middleware in create_app replaces the host from
    X-Forwarded-Host for a trusted peer; the gate reads only what it is
    handed and ignores anything else."""
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
    full window once its hour has run, not the first suppression on its own."""
    clock = [0]
    recorder = fresh_state.BoundedRefusalRecorder(now=lambda: clock[0])

    for index in range(40):
        recorder.note(f"h{index}.example.com")
        # The first suppressed refusal opens the window; nothing is due yet.
        assert recorder.take_summary() is None

    # Due: the count, with the window's two ends for the line to name and
    # its stable ID for whoever carries it next.
    clock[0] = 3600
    count, opened, closed, window_id = recorder.take_summary()
    assert (count, opened, closed) == (20, 0, 3600)
    assert window_id

    # The count is reset, and the next suppression opens a fresh window.
    assert recorder.take_summary() is None
    for index in range(40, 62):
        recorder.note(f"h{index}.example.com")
    assert recorder.take_summary() is None
    clock[0] = 7300
    count, opened, closed, second_id = recorder.take_summary()
    assert (count, opened, closed) == (2, 3600, 7300)
    assert second_id and second_id != window_id


def test_a_due_summary_is_logged_even_when_the_refusals_stop(fresh_state, root_log, caplog,
                                                             monkeypatch):
    """A stream of refusals that stops does not take its summary with it: the
    next refusal delivers it, and so does a visit to the health list, worded
    by the window the count ran in."""
    from app import host_allowlist

    # The summary line is INFO, as production logging is: configure_logging
    # has the root at INFO, while this suite leaves it at the WARNING default.
    caplog.set_level(logging.INFO)
    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()

    for index in range(25):
        host_allowlist.record_refusal(f"h{index}.example.com", config=config)
    assert recorder.recent(86400) == [f"h{index}.example.com" for index in reversed(range(25))]

    # No further refusal: the health list alone delivers the summary. Five
    # of the 25 went past the hour's ceiling of 20 lines.
    clock[0] = 3601
    host_allowlist.health_issues(config)

    opened = time.strftime("%H:%M", time.localtime(0))
    closed = time.strftime("%H:%M", time.localtime(3601))
    summaries = [message for message in root_log.messages if "host-refusal log lines" in message]
    assert summaries == [f"BAZARR suppressed 5 host-refusal log lines between {opened} and {closed}."]


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
        body = wrong.get_data(as_text=True)
        assert wrong.status_code == 403
        # The error paragraph names the problem; it is never rendered as a
        # second, forwarded hostname.
        assert "Wrong or missing approval code." in body
        assert '<p class="error">' in body
        assert "forwarded hostname" not in body

    throttled = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                            data={"host": FOREIGN_HOST_HEADER, "code": code})
    body = throttled.get_data(as_text=True)

    assert throttled.status_code == 429
    assert "Too many attempts. Try again in 5 minutes." in body
    assert '<p class="error">' in body
    assert "forwarded hostname" not in body
    assert config.general.allowed_hosts == []


def test_a_star_cannot_be_allowed_through_the_form(approval_app, console_log):
    flask_app, config, _ = approval_app
    client = flask_app.test_client()

    client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})
    code = _announced_code(console_log)

    for host in ("*", "*.example.com", "bad name.example.com", "<script>alert(1)</script>"):
        response = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                               data={"host": host, "code": code})
        body = response.get_data(as_text=True)
        assert response.status_code == 403, host
        assert "That hostname cannot be allowed." in body, host
        assert "forwarded hostname" not in body, host
        assert config.general.allowed_hosts == [], host


def test_a_failed_save_names_the_problem_on_the_page(approval_app, console_log, monkeypatch):
    """The recovery surface's fourth refusal: a save that fails tells the
    operator so on the page, in the error paragraph, instead of rendering
    the message as a second, forwarded hostname."""
    from app import ui

    flask_app, config, _ = approval_app
    client = flask_app.test_client()
    client.get("/probe", headers={"Host": FOREIGN_HOST_HEADER})
    code = _announced_code(console_log)

    def broken_save(items):
        raise ValueError("the save is refused")

    monkeypatch.setattr(ui, "save_settings", broken_save)
    response = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                           data={"host": FOREIGN_HOST, "code": code})
    body = response.get_data(as_text=True)

    assert response.status_code == 503
    assert "The hostname could not be saved. Try again." in body
    assert '<p class="error">' in body
    assert "forwarded hostname" not in body


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
            assert "window.Bazarr" not in body, (path, method)
            # The answer names the hostname and the ways in.
            if method == "get":
                assert FOREIGN_HOST in body, path
                assert "host_not_allowed" in body or "general.allowed_hosts" in body, path


def _shell_payload(body):
    """The settings the shell was injected with, parsed out of the page."""
    found = re.search(r"JSON\.parse\(`(.*)`\)", body, re.DOTALL)
    assert found, "the page carries no injected payload"
    return json.loads(found.group(1))


def test_the_real_application_answers_a_recognized_host_with_the_shell(real_factory,
                                                                       shell_template):
    """The page parses the injected payload with its own script, so the
    settings the shell needs are in the body as content, not as the template
    marker the build renders away."""
    client = real_factory.test_client()
    apikey = _real_apikey()

    response = client.get("/", headers={"Host": "bazarr:6767"})
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "window.Bazarr" in body
    assert _shell_payload(body)["apiKey"] == apikey


def test_a_login_mode_leaves_the_unauthenticated_shell_without_the_key(real_factory,
                                                                       shell_template):
    from app.config import settings

    client = real_factory.test_client()
    original = settings.auth.type
    try:
        settings.set("auth.type", "form")
        response = client.get("/", headers={"Host": FOREIGN_HOST_HEADER})
        body = response.get_data(as_text=True)
    finally:
        settings.set("auth.type", original)

    assert response.status_code == 200
    assert "window.Bazarr" in body
    assert "apiKey" not in _shell_payload(body)


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
                assert "window.Bazarr" not in body
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
    """The ceiling the bounds set: at most 21 refusal-path lines in the log
    that bazarr.log is fed from, the hour's 20 per-host lines plus the one
    summary line, and at most one code line on the console."""
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


def test_a_report_of_a_refusal_the_console_already_named_writes_no_second_line(
        real_factory, root_log, monkeypatch):
    """The report of a refusal the front listener named on its own console
    while the backend could not be told carries the time of that line: the
    hostname is recorded and the code is announced all the same, but no
    second line is written for it within that hour. A carried time past the
    hour suppresses nothing."""
    from app import host_allowlist
    from app.config import settings

    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "supervisor-token")
    client = real_factory.test_client()
    headers = {"Host": "127.0.0.1:6767", "X-Bazarr-Supervisor-Token": "supervisor-token"}

    report = client.post("/_supervisor/host_refusal", headers=headers,
                         data={"host": "front.example.com", "logged": repr(time.time() - 60)})
    assert report.status_code == 204

    issues = host_allowlist.health_issues(settings)
    assert any("front.example.com" in issue["issue"] for issue in issues)
    refused = [message for message in root_log.messages if "refused a request" in message]
    assert refused == []

    # Past the carried hour, the backend's own line is written.
    report = client.post("/_supervisor/host_refusal", headers=headers,
                         data={"host": "late.example.com", "logged": repr(time.time() - 3700)})
    assert report.status_code == 204
    refused = [message for message in root_log.messages if "refused a request" in message]
    assert len(refused) == 1
    assert "late.example.com" in refused[0]


def test_a_refusal_names_the_hostname_that_failed_and_recovers_from_it(
        real_factory, shell_template, console_log, monkeypatch):
    """Raw Host refused while the forwarded host would pass: the page, the
    form's hidden field and the record all name the failing one, so the code
    the operator enters approves a hostname that actually opens the door. The
    passing one approves nothing and the request stays refused."""
    from app import ui
    from app.config import settings

    original = list(settings.general.allowed_hosts)

    def fake_save(items):
        for key, value in items:
            if key == "settings-general-allowed_hosts":
                settings.set("general.allowed_hosts", list(value))
        return True

    monkeypatch.setattr(ui, "save_settings", fake_save)
    client = real_factory.test_client()
    try:
        # The test client's peer is loopback, which the default
        # trusted_proxy names, so the forwarded host is believed.
        response = client.get("/", headers={"Host": FOREIGN_HOST_HEADER,
                                            "X-Forwarded-Host": "bazarr.local:6767"})
        body = response.get_data(as_text=True)

        assert response.status_code == 421
        assert FOREIGN_HOST in body
        assert "bazarr.local" not in body
        assert f'value="{FOREIGN_HOST}"' in body
        code = _announced_code(console_log)
        assert code is not None

        approval = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                               data={"host": FOREIGN_HOST, "code": code})
        assert approval.status_code == 302

        # The gate admits the request now, with no restart.
        admitted = client.get("/", headers={"Host": FOREIGN_HOST_HEADER,
                                            "X-Forwarded-Host": "bazarr.local:6767"})
        assert admitted.status_code == 200
    finally:
        settings.set("general.allowed_hosts", original)


def test_a_request_with_two_refused_hostnames_names_both(real_factory):
    from app import host_allowlist

    client = real_factory.test_client()

    page = client.get("/", headers={"Host": "first.example.com:6767",
                                    "X-Forwarded-Host": "second.example.com"})
    body = page.get_data(as_text=True)
    assert page.status_code == 421
    assert "first.example.com" in body
    assert "second.example.com" in body
    assert "forwarded hostname" in body
    # The form names the first one, which is the one to allow.
    assert 'value="first.example.com"' in body

    payload = json.loads(client.get("/api/system/status",
                                    headers={"Host": "first.example.com:6767",
                                             "X-Forwarded-Host": "second.example.com"}
                                    ).get_data(as_text=True))
    assert payload["host"] == "first.example.com"
    assert payload["also_refused"] == "second.example.com"

    # The health record holds both: each hostname the request carried is one
    # the operator may have to allow, each within its own bounds.
    recorded = host_allowlist._refusals.recent(86400)
    assert "first.example.com" in recorded
    assert "second.example.com" in recorded


def test_a_request_that_carries_one_name_twice_names_it_once(real_factory):
    """Host and forwarded hostname that are the same name, modulo a port or
    case, are one refusal: the page names it once, the JSON carries no
    second hostname, and the record holds one entry."""
    from app import host_allowlist

    client = real_factory.test_client()

    page = client.get("/", headers={"Host": "same.example.com:6767",
                                    "X-Forwarded-Host": "SAME.example.com:443"})
    body = page.get_data(as_text=True)
    assert page.status_code == 421
    assert "same.example.com" in body
    assert "forwarded hostname" not in body

    payload = json.loads(client.get("/api/system/status",
                                    headers={"Host": "same.example.com:6767",
                                             "X-Forwarded-Host": "SAME.example.com:443"}
                                    ).get_data(as_text=True))
    assert "also_refused" not in payload

    assert host_allowlist._refusals.recent(86400) == ["same.example.com"]


def test_a_request_with_no_host_header_is_refused(real_factory, shell_template):
    """A request that carries no Host header is refused at this layer too,
    whatever a trusted proxy's forwarded header would have put in its place:
    the raw host is read by presence, so the header the forwarded-header
    middleware may have replaced never stands in for the one the request
    never carried."""
    from werkzeug.test import EnvironBuilder

    def answer(headers=None):
        environ = EnvironBuilder(path="/", headers=headers or {}).get_environ()
        environ["REMOTE_ADDR"] = "127.0.0.1"
        environ.pop("HTTP_HOST", None)
        captured = []
        body = b"".join(real_factory(environ, lambda status, response_headers:
                                     captured.append((status, dict(response_headers)))))
        return captured[0][0], body.decode("utf-8", "replace")

    status, body = answer()
    assert status.startswith("421")
    assert "carried no hostname" in body

    # The trusted peer's forwarded hostname is judged with the request, but
    # it cannot stand in for the Host header the request never carried.
    status, body = answer({"X-Forwarded-Host": "bazarr.local"})
    assert status.startswith("421")
    assert "carried no hostname" in body


def test_the_approval_submission_is_served_at_the_root_under_a_base_url(monkeypatch, console_log):
    """A proxy that strips the install's base_url prefix reaches the approval
    form at the root; create_app serves the same submission there, so the
    recovery works through the stripped path as well, at both layers."""
    from app import app as app_module
    from app import config as config_module
    from app import ui
    from app.config import settings

    saved = []
    monkeypatch.setattr(ui, "save_settings", lambda items: saved.extend(items) or True)
    original_base = settings.general.base_url
    original_hosts = list(settings.general.allowed_hosts)
    settings.set("general.base_url", "/bazarr")
    monkeypatch.setattr(config_module, "base_url", "/bazarr")
    monkeypatch.setattr(app_module, "base_url", "/bazarr")
    flask_app, reset_logins, close_database = _build_real_app(monkeypatch)
    client = flask_app.test_client()
    try:
        refused = client.get("/bazarr/", headers={"Host": FOREIGN_HOST_HEADER})
        body = refused.get_data(as_text=True)
        assert refused.status_code == 421
        assert 'action="/bazarr/host-approval"' in body

        code = _announced_code(console_log)
        assert code is not None
        at_root = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                              data={"host": FOREIGN_HOST, "code": code})
        assert at_root.status_code == 302
        assert saved == [("settings-general-allowed_hosts", [FOREIGN_HOST])]
    finally:
        settings.set("general.base_url", original_base)
        settings.set("general.allowed_hosts", original_hosts)
        reset_logins()
        close_database()


@pytest.mark.asyncio
async def test_an_approval_goes_through_the_real_save_and_reaches_a_fresh_listener(
        real_factory, console_log, monkeypatch):
    """The approval's save is the real settings save: config.yaml on disk
    carries the hostname, and a front listener started over that same file,
    with no other way of knowing, admits the hostname on the next request."""
    from app.config import settings

    config_dir = os.environ["BAZARR_CONFIG_DIR"].strip()
    config_file = Path(config_dir) / "config" / "config.yaml"
    before = config_file.read_bytes() if config_file.exists() else None
    original = list(settings.general.allowed_hosts)
    client = real_factory.test_client()
    try:
        assert client.get("/", headers={"Host": FOREIGN_HOST_HEADER}).status_code == 421
        code = _announced_code(console_log)
        assert code is not None

        approval = client.post("/host-approval", headers={"Host": FOREIGN_HOST_HEADER},
                               data={"host": FOREIGN_HOST, "code": code})
        assert approval.status_code == 302
        assert settings.general.allowed_hosts == [FOREIGN_HOST]

        # The disk carries it, in the shape the Docker supervisor reads.
        saved = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        assert FOREIGN_HOST in (saved.get("general", {}) or {}).get("allowed_hosts", [])

        # A front listener over that config file, nothing else told anything.
        monkeypatch.setattr(supervisor, "proxy_handler", _proxy_marker)
        front = TestClient(TestServer(supervisor.create_app(str(config_dir), _Backend())))
        await front.start_server()
        try:
            response = await front.get("/", headers={"Host": FOREIGN_HOST_HEADER})
            body = await response.text()
        finally:
            await front.close()

        assert response.status == 200
        assert body == "proxied"
    finally:
        settings.set("general.allowed_hosts", original)
        if before is not None:
            config_file.write_bytes(before)
        else:
            config_file.unlink(missing_ok=True)


def test_the_configured_server_honours_the_trusted_proxy_over_a_real_socket(
        real_factory, shell_template):
    """The wiring server.py builds, serving requests over a real waitress
    socket. The loopback peer is the configured trusted_proxy, so the
    forwarded host, scheme and address arrive at the app, and both of the
    request's hostnames are judged. With the server's own default
    clear_untrusted_proxy_headers, waitress would strip the forwarded headers
    in front of the app and none of that would be true."""
    from waitress.server import create_server

    from app.app import waitress_server_options

    # The options the server passes are the wiring under test.
    options = waitress_server_options()
    assert options["clear_untrusted_proxy_headers"] is False
    assert "trusted_proxy" not in options

    seen = []

    @real_factory.route("/_waitress_probe")
    def probe():  # pragma: no cover - only reached when the gate passes
        from flask import request

        seen.append((request.host, request.scheme, request.remote_addr))
        return "ok"

    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    server = create_server(real_factory, host="127.0.0.1", port=port,
                           threads=4, **options)

    def serve():
        try:
            server.run()
        except OSError:
            # The production loop closes on the same Errno 9 this shutdown
            # races in; here it only means the test is done with the server.
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        def get(path, host, **forwarded):
            headers = {"Host": host}
            headers.update(forwarded)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            body = response.read().decode("utf-8", "replace")
            connection.close()
            return response.status, body

        # The loopback peer's forwarded host, scheme and address arrive.
        status, _ = get("/_waitress_probe", "localhost",
                        **{"X-Forwarded-Host": "bazarr.local",
                           "X-Forwarded-Proto": "https",
                           "X-Forwarded-For": "10.1.2.3"})
        assert status == 200
        assert seen[-1] == ("bazarr.local", "https", "10.1.2.3")

        # Both hostnames are judged, over the real socket as over the test
        # client, and a refusal names the failing one.
        status, body = get("/", "attacker.example.com", **{"X-Forwarded-Host": "bazarr.local"})
        assert status == 421
        assert "attacker.example.com" in body
        status, body = get("/", "localhost", **{"X-Forwarded-Host": "attacker.example.com"})
        assert status == 421
        assert "attacker.example.com" in body
        assert "window.Bazarr" not in body

        status, body = get("/_waitress_probe", "localhost",
                           **{"X-Forwarded-Host": "bazarr.local"})
        assert status == 200
    finally:
        server.close()
        thread.join(timeout=10)


def test_the_boot_sequence_announces_the_code_to_the_console_only(tmp_path):
    """The wiring bazarr/main.py runs at boot: logging configured, then
    app.server imported, which builds the app and announces the code. The
    console the process runs in carries the code, and the bazarr.log the
    same boot wrote never does."""
    repo = Path(__file__).resolve().parents[2]
    bazarr_dir = repo / "bazarr"
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    for subdirectory in ("backup", "cache", "config", "db", "log", "restore"):
        (tmp_path / subdirectory).mkdir()

    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.pathsep.join((str(bazarr_dir), str(repo / "custom_libs"))),
        "BAZARR_CONFIG_DIR": str(tmp_path),
        # init.py sets this before app.server is imported in the real boot,
        # and the log paths under it are read while the module loads.
        "BAZARR_ROOT": str(tmp_path),
        "BAZARR_VERSION": "test",
        "SZ_USER_AGENT": "test",
        "NO_CLI": "false",
    })
    env.pop("PYTEST_CURRENT_TEST", None)

    boot = subprocess.run(
        [sys.executable, "-c",
         "from app.logger import configure_logging\n"
         "configure_logging()\n"
         "import app.server\n"
         # The real server's own adjustment, from the options it is built
         # with: the clearing waitress would do in front of the app's own
         # forwarded-header parsing has to be off on the object it built.
         "print('server clear_untrusted_proxy_headers:',\n"
         "      app.server.webserver.server.adj.clear_untrusted_proxy_headers)\n",
         "--port", str(port)],
        cwd=str(repo), env=env, capture_output=True, timeout=300)

    console = (boot.stdout + boot.stderr).decode("utf-8", "replace")
    assert boot.returncode == 0, console[-4000:]
    assert "server clear_untrusted_proxy_headers: False" in console

    # The code is announced in a line of its own shape, and that shape is
    # where the code is read from, not the first hex-looking token around.
    code_lines = [line for line in console.splitlines()
                  if "approval code for refused hostnames: " in line]
    assert code_lines, console[-4000:]
    code = re.search(r"approval code for refused hostnames: ([0-9a-f]{16})",
                     code_lines[-1]).group(1)

    log_file = tmp_path / "log" / "bazarr.log"
    assert log_file.exists()
    assert code not in log_file.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# The supervisor's share of the check
# ---------------------------------------------------------------------------


def test_the_supervisor_decision_matrix():
    decision = supervisor.host_check_decision

    # A direct client is judged by the hostname it sent, and the refusal
    # names that hostname.
    assert decision(request_host="attacker.example.com:6767", remote="192.168.1.10",
                    forwarded_host="", auth_type=None, trusted_proxy=None,
                    allowed_hosts=[]) == (False, ["attacker.example.com"])
    assert decision(request_host="192.168.1.10:6767", remote="192.168.1.10",
                    forwarded_host="", auth_type=None, trusted_proxy=None,
                    allowed_hosts=[]) == (True, [])
    # The forwarded host is believed only from the configured proxy address.
    assert decision(request_host="upstream:6767", remote="172.18.0.5",
                    forwarded_host="attacker.example.com:443", auth_type=None,
                    trusted_proxy="172.18.0.5", allowed_hosts=[]) == (False, ["attacker.example.com"])
    assert decision(request_host="upstream:6767", remote="172.18.0.5",
                    forwarded_host="bazarr.local", auth_type=None,
                    trusted_proxy="172.18.0.5", allowed_hosts=[]) == (True, [])
    # From any other address the forwarded header is not believed.
    assert decision(request_host="attacker.example.com:6767", remote="10.0.0.9",
                    forwarded_host="localhost", auth_type=None,
                    trusted_proxy="172.18.0.5", allowed_hosts=[]) == (False, ["attacker.example.com"])
    # A configured login switches the check off, as it does in the backend.
    assert decision(request_host="attacker.example.com", remote="192.168.1.10",
                    forwarded_host="", auth_type="form", trusted_proxy=None,
                    allowed_hosts=[]) == (True, [])
    # A listed hostname or the switch answers it.
    assert decision(request_host="proxy.example.com", remote="192.168.1.10",
                    forwarded_host="", auth_type=None, trusted_proxy=None,
                    allowed_hosts=["proxy.example.com"]) == (True, [])
    assert decision(request_host="anything.example", remote="192.168.1.10",
                    forwarded_host="", auth_type=None, trusted_proxy=None,
                    allowed_hosts=["*"]) == (True, [])
    # An absent trusted_proxy keeps the backend's loopback default; an
    # explicitly empty one trusts nobody, so the forwarded host that would
    # pass is never believed.
    assert decision(request_host="upstream:6767", remote="127.0.0.1",
                    forwarded_host="bazarr.local", auth_type=None, trusted_proxy=None,
                    allowed_hosts=[]) == (True, [])
    assert decision(request_host="attacker.example.com:6767", remote="127.0.0.1",
                    forwarded_host="localhost", auth_type=None, trusted_proxy="",
                    allowed_hosts=[]) == (False, ["attacker.example.com"])
    # When a forwarded host is believed, both hostnames are judged, and a
    # refusal names the one that failed, not the one that passed.
    assert decision(request_host="localhost:6767", remote="172.18.0.5",
                    forwarded_host="attacker.example.com", auth_type=None,
                    trusted_proxy="172.18.0.5", allowed_hosts=[]) == (False, ["attacker.example.com"])
    assert decision(request_host="attacker.example.com:6767", remote="172.18.0.5",
                    forwarded_host="second.example.com", auth_type=None, trusted_proxy="172.18.0.5",
                    allowed_hosts=[]) == (False, ["attacker.example.com", "second.example.com"])
    assert decision(request_host="localhost:6767", remote="127.0.0.1",
                    forwarded_host="bazarr.local", auth_type=None, trusted_proxy=None,
                    allowed_hosts=[]) == (True, [])
    # A request that carried no hostname is refused, at this layer too.
    assert decision(request_host="", remote="192.168.1.10",
                    forwarded_host="", auth_type=None, trusted_proxy=None,
                    allowed_hosts=[]) == (False, [""])


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
async def test_the_root_api_path_answers_json_under_a_base_url_too(tmp_path, monkeypatch,
                                                                   fresh_state):
    """A proxy that strips the install's base_url prefix leaves the API at
    the root, and the refusal a root API request gets is the API's own JSON
    with the reason code, at the front listener and at the backend, exactly
    as under the prefix."""
    client = await _supervisor_client(
        tmp_path, monkeypatch, "general:\n  base_url: /bazarr\nauth: {}\n")
    try:
        at_root = await client.get("/api/system/status", headers={"Host": FOREIGN_HOST_HEADER})
        root_body = await at_root.text()
        under_base = await client.get("/bazarr/api/system/status",
                                      headers={"Host": FOREIGN_HOST_HEADER})
        base_body = await under_base.text()
    finally:
        await client.close()

    assert at_root.status == 421
    assert at_root.content_type == "application/json"
    assert json.loads(root_body)["code"] == "host_not_allowed"
    assert under_base.status == 421
    assert under_base.content_type == "application/json"
    assert json.loads(base_body)["code"] == "host_not_allowed"

    # The backend's own gate refuses the same root path the same way, because
    # the prefix-stripping proxy sits in front of it too.
    backend_client = _app_with_check(_config(base_url="/bazarr")).test_client()
    root = backend_client.get("/api/system/status", headers={"Host": FOREIGN_HOST_HEADER})
    prefixed = backend_client.get("/bazarr/api/system/status",
                                  headers={"Host": FOREIGN_HOST_HEADER})

    assert root.status_code == 421
    assert root.content_type == "application/json"
    assert json.loads(root.get_data(as_text=True))["code"] == "host_not_allowed"
    assert prefixed.status_code == 421
    assert prefixed.content_type == "application/json"
    assert json.loads(prefixed.get_data(as_text=True))["code"] == "host_not_allowed"


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
async def test_the_front_listener_refuses_a_request_with_no_host_header(tmp_path, monkeypatch):
    """The Host header itself is what the front listener judges: the web
    library's fallback to the socket address would admit a request that
    carried no hostname, and the rule refuses it at this layer too."""
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", client.server.port)
        writer.write(b"GET / HTTP/1.0\r\n\r\n")
        await writer.drain()
        raw = await reader.read()
        writer.close()
    finally:
        await client.close()

    status_line = raw.split(b"\r\n", 1)[0]
    body = raw.decode("utf-8", "replace")
    assert b"421" in status_line
    assert "carried no hostname" in body
    assert "proxied" not in body


@pytest.mark.asyncio
async def test_the_supervisor_reports_its_refusals_to_the_backend(tmp_path, monkeypatch):
    """A refusal the front listener answers reaches the backend's record and
    its console code: at most one report per hostname per hour, carrying this
    supervisor's token, and only its own acknowledgement counts."""
    reports = []

    async def notice(request):
        reports.append((request.headers.get("X-Bazarr-Supervisor-Token"),
                         await request.post()))
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token"})

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
            assert len(reports) == 1
            assert reports[0][0] == "the-supervisor-token"
            assert reports[0][1]["host"] == FOREIGN_HOST
            # Every report also carries the newest refused names with the
            # console's recency stamps, this one the only so far.
            assert reports[0][1]["recent"] == FOREIGN_HOST

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


@pytest.mark.asyncio
async def test_every_refused_host_is_reported_even_past_the_line_ceiling(tmp_path, monkeypatch):
    """The report pace is not the line pace: past the hour's ceiling of 20
    refusal lines, every hostname still reaches the backend, which then owns
    the record, the health page and the console code. This console writes no
    refusal line at all while the backend answers."""
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)

    async def notice(request):
        reports.append((await request.post())["host"])
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token"})

    reports = []
    backend_app = web.Application()
    backend_app.router.add_route("POST", "/_supervisor/host_refusal", notice)
    backend = TestClient(TestServer(backend_app))
    await backend.start_server()
    try:
        monkeypatch.setattr(supervisor, "BACKEND_PORT", backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        client = await _supervisor_client(tmp_path, monkeypatch)
        try:
            for index in range(30):
                response = await client.get("/", headers={"Host": f"h{index}.example.com"})
                assert response.status == 421
            for _ in range(500):
                if len(reports) >= 30:
                    break
                await asyncio.sleep(0.01)
            assert sorted(reports) == sorted(f"h{index}.example.com" for index in range(30))
            assert lines == []
        finally:
            await client.close()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_the_backends_record_reflects_the_newest_refusals_of_a_burst(tmp_path, monkeypatch):
    """A burst of more distinct hosts than the record's bound, against a live
    backend, spends the hour's reporting bound: the newest names wait out the
    hour while the backend's record holds older ones, and a repeated refusal
    of a host the console already told is skipped by its reports, so its
    recency never refreshes there. Every report carries the newest bounded
    refused set with the console's own recency stamps, and the record takes
    the newer stamp per host: once the hour rolls and the waiting reports go
    out, the record holds the newest names, with the repeat refusal's
    refreshed recency."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        answer = deliver(form)
        headers = answer[2] if len(answer) > 2 else {}
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    live_backend = TestClient(TestServer(backend))
    await live_backend.start_server()
    try:
        monkeypatch.setattr(supervisor, "BACKEND_PORT", live_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        client = await _supervisor_client(tmp_path, monkeypatch)
        try:
            reporter = supervisor._active_reporter
            monkeypatch.setattr(reporter, "_now", lambda: clock[0])
            monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

            # The burst: each report is let to land before the next refusal,
            # so the hour's reporting bound is spent in order and the newest
            # names are the ones left waiting.
            for index in range(60):
                response = await client.get("/", headers={"Host": f"h{index}.example.com"})
                assert response.status == 421
                if index < supervisor.REPORTS_PER_HOUR:
                    for _ in range(2000):
                        if f"h{index}.example.com" not in reporter.pending_reports():
                            break
                        clock[0] += 0.01
                        await asyncio.sleep(0.005)
            waiting = [f"h{index}.example.com" for index in range(50, 60)]
            assert reporter.pending_reports() == waiting

            # A repeated refusal of a host the backend was told within the
            # hour is skipped by its reports; its recency refreshes only in
            # the set the waiting reports carry.
            clock[0] += 10
            response = await client.get("/", headers={"Host": "h1.example.com"})
            assert response.status == 421
            assert reporter.pending_reports() == waiting

            # The hour rolls, and a later refusal wakes the delivery worker:
            # the waiting reports go out before its own, and each carries the
            # newest refused names with the console's stamps.
            clock[0] += 3601
            response = await client.get("/", headers={"Host": "after.example.com"})
            assert response.status == 421
            for _ in range(6000):
                if not reporter.pending_reports():
                    break
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            assert reporter.pending_reports() == []
        finally:
            await client.close()
    finally:
        await live_backend.close()

    recorded = recorder.recent(86400)
    assert "h1.example.com" in recorded
    assert sorted(recorded) == sorted([f"h{index}.example.com" for index in range(12, 60)]
                                      + ["h1.example.com", "after.example.com"])
    # The repeat's refreshed recency: the record ranks it ahead of a name
    # the burst reported long after its own first report.
    assert recorded.index("h1.example.com") < recorded.index("h49.example.com")


@pytest.mark.asyncio
async def test_refusals_while_the_backend_is_down_are_named_here_and_reported_when_it_answers(
        tmp_path, monkeypatch):
    """While the backend cannot be told, this console is the only place a
    refused hostname is named; the report waits for the backend to answer and
    arrives then, so the health page and the console code catch up. Nothing
    schedules the delivery by hand: the worker's own timer finds the backend
    answering again."""
    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        await client.get("/", headers={"Host": FOREIGN_HOST_HEADER})
        assert lines == [f"Refused a request for an unrecognized host: {FOREIGN_HOST}. Allow it under "
                         f"general.allowed_hosts in config.yaml, or set "
                         f"{supervisor.host_policy.ALLOWED_HOSTS_ENV}."]
        reporter = supervisor._active_reporter
        assert reporter.pending_reports() == [FOREIGN_HOST]

        async def notice(request):
            reports.append((request.headers.get("X-Bazarr-Supervisor-Token"),
                            await request.post()))
            return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                    "the-supervisor-token"})

        reports = []
        backend_app = web.Application()
        backend_app.router.add_route("POST", "/_supervisor/host_refusal", notice)
        backend = TestClient(TestServer(backend_app))
        await backend.start_server()
        try:
            monkeypatch.setattr(supervisor, "BACKEND_PORT", backend.server.port)
            monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
            for _ in range(100):
                if reports:
                    break
                await asyncio.sleep(0.01)
            assert len(reports) == 1
            assert reports[0][0] == "the-supervisor-token"
            assert reports[0][1]["host"] == FOREIGN_HOST
            # The console named the hostname while the backend was down, so
            # the report carries the time of that line and the backend writes
            # no second one for it.
            assert float(reports[0][1]["logged"]) > 0
            # The worker clears the pending set once it has read the answer,
            # not when the handler records it, so wait for its own clock.
            for _ in range(200):
                if not reporter.pending_reports():
                    break
                await asyncio.sleep(0.01)
            assert reporter.pending_reports() == []
            # The delivery worked, so this console needs no second line.
            assert len(lines) == 1
        finally:
            await backend.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_report_the_backend_did_not_take_is_held_and_retried(tmp_path, monkeypatch):
    """A backend that answers 500 has not taken the report: it stays pending
    and the worker tries it again on its own timer, with no further request
    and no call from anywhere, instead of waiting an hour for the next
    refusal. Until it lands, this console names the hostname."""
    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    attempts = {"count": 0}

    async def notice(request):
        await request.post()
        attempts["count"] += 1
        if attempts["count"] == 1:
            return web.Response(status=500)
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token"})

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
                if attempts["count"]:
                    break
                await asyncio.sleep(0.01)
            reporter = supervisor._active_reporter
            assert reporter.pending_reports() == [FOREIGN_HOST]
            # The failed delivery is named on this console, within its
            # bounds, once the worker has read the answer that failed it.
            for _ in range(200):
                if lines:
                    break
                await asyncio.sleep(0.01)
            assert lines == [f"Refused a request for an unrecognized host: {FOREIGN_HOST}. Allow it "
                             f"under general.allowed_hosts in config.yaml, or set "
                             f"{supervisor.host_policy.ALLOWED_HOSTS_ENV}."]

            # The retry is the worker's own: nothing was asked of the
            # backend in between, and no manual call schedules it.
            for _ in range(200):
                if attempts["count"] >= 2:
                    break
                await asyncio.sleep(0.01)
            assert attempts["count"] == 2
            # The acknowledged retry is recorded once the worker has read
            # the answer, so the pending set empties on the worker's clock.
            for _ in range(200):
                if not reporter.pending_reports():
                    break
                await asyncio.sleep(0.01)
            assert reporter.pending_reports() == []
            # And within the hour, a repeat refusal asks for nothing more.
            await client.get("/", headers={"Host": FOREIGN_HOST_HEADER})
            await asyncio.sleep(0.05)
            assert attempts["count"] == 2
        finally:
            await client.close()
    finally:
        await backend.close()


def test_the_report_cooldown_survives_a_burst_of_other_names():
    """However many hostnames are acknowledged around it, a hostname told
    within the hour is not told again: the cooldown is kept by time alone,
    never by a count, so no roll of other names makes it due again."""
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])

    assert reporter.report_due("first.example.com") is True
    reporter.report_sent("first.example.com")

    for index in range(120):
        assert reporter.report_due(f"h{index}.example.com") is True
        reporter.report_sent(f"h{index}.example.com")

    assert reporter.report_due("first.example.com") is False

    clock[0] = 3601
    assert reporter.report_due("first.example.com") is True


def test_the_report_cooldown_does_not_outlive_the_backend():
    """The backend's refusal record lives in its memory, so a backend that
    was restarted knows none of the hostnames the old one acknowledged:
    readiness clears the cooldown stamps with the rest, and the next refusal
    of such a hostname is reported to the new backend, whose status page
    would otherwise name none of them for up to an hour."""
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])

    assert reporter.report_due("restarted.example.com") is True
    assert reporter.take_due_report() == "restarted.example.com"
    reporter.report_sent("restarted.example.com")
    # The same backend has the hostname, so a repeat reports nothing.
    assert reporter.report_due("restarted.example.com") is False

    # The backend restarts and answers again.
    reporter.ready_to_deliver()
    assert reporter.report_due("restarted.example.com") is True
    assert reporter.take_due_report() == "restarted.example.com"


def test_a_host_refused_again_while_waiting_is_not_evicted():
    """A hostname refused again while its report is still waiting is the
    newest news about it, so it moves to the end of the pending queue: the
    eviction drops the oldest waiting, and a frequently refused hostname is
    not pushed out by names that arrived after its last refusal."""
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])

    assert reporter.report_due("often.example.com") is True
    for index in range(supervisor.host_policy.REFUSED_HOSTS_LIMIT - 1):
        assert reporter.report_due(f"h{index}.example.com") is True
    assert len(reporter.pending_reports()) == supervisor.host_policy.REFUSED_HOSTS_LIMIT

    # Refused again while still waiting, and one more name arrives after it.
    assert reporter.report_due("often.example.com") is False
    clock[0] = 10
    assert reporter.report_due("newest.example.com") is True

    pending = reporter.pending_reports()
    assert len(pending) == supervisor.host_policy.REFUSED_HOSTS_LIMIT
    assert "h0.example.com" not in pending
    assert pending[-2:] == ["often.example.com", "newest.example.com"]


def test_the_report_pace_is_bounded_globally_too():
    """The once-an-hour pace of each hostname has a global ceiling as well:
    at most the refused-hosts limit of acknowledged reports an hour in
    total, the same bound the backend's refused-host record holds, so a
    long roll of distinct hostnames cannot become a long roll of requests.
    A report the backend does not acknowledge does not count against it, so
    a failing backend cannot spend the hour instead of the recovery. The
    rest stay pending and go out as the hour rolls over."""
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])

    # Ten hostnames are told inside the hour, and fifty more queue behind
    # them for the same hour.
    for index in range(10):
        assert reporter.report_due(f"told{index}.example.com") is True
        assert reporter.take_due_report() == f"told{index}.example.com"
        reporter.report_sent(f"told{index}.example.com")
    for index in range(50):
        assert reporter.report_due(f"held{index}.example.com") is True

    delivered = []
    while True:
        host = reporter.take_due_report()
        if host is None:
            break
        reporter.report_sent(host)
        delivered.append(host)
    # The ten already told leave room for forty of the fifty waiting.
    assert len(delivered) == supervisor.REPORTS_PER_HOUR - 10
    # The rest are held, not dropped, and go out once the hour rolls over.
    clock[0] = 3601
    assert reporter.take_due_report() is not None


def test_a_crash_window_of_refused_connections_does_not_spend_the_delivery_bound(monkeypatch):
    """A backend that refuses every connection answers on the spot, so an
    hour's bound that counts attempts is spent by the crash window alone and
    the backend that recovers is told none of the fifty for up to an hour.
    The bound counts acknowledged reports only, and a failed delivery holds
    the next attempt back for a pace instead, so after readiness all fifty
    are delivered."""
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])

    hosts = [f"h{index}.example.com" for index in range(supervisor.REPORTS_PER_HOUR)]
    for host in hosts:
        assert reporter.report_due(host) is True

    # The crash window: every connection is refused, one report at a time.
    attempted = []
    while True:
        host = reporter.take_due_report()
        if host is None:
            break
        attempted.append(host)
        reporter.report_failed(host)
    # A failed delivery holds the next attempt back for a pace, so the
    # window asks about one hostname and not fifty.
    assert attempted == hosts[:1]

    # The readiness poll finds the backend answering again.
    reporter.ready_to_deliver()

    delivered = []
    while True:
        host = reporter.take_due_report()
        if host is None:
            break
        delivered.append(host)
        reporter.report_sent(host)
    assert sorted(delivered) == sorted(hosts)


def test_failing_retries_do_not_count_as_suppressed_lines(monkeypatch):
    """One refusal is one line of news, and an hour of failing retries of
    its report is still one: the failure path asks the line pace without
    counting a held-back line as suppressed, so the summary never prints
    sixty suppressed lines for the one refusal."""
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])

    assert reporter.note_line("held.example.com") is True
    assert reporter.report_due("held.example.com") is True
    assert reporter.take_due_report() == "held.example.com"
    reporter.report_failed("held.example.com")
    # The delivery is retried once a minute for the hour.
    for _ in range(59):
        clock[0] += 60
        reporter.report_failed("held.example.com")
    assert lines == []

    # Nothing was suppressed, so no summary is ever due.
    clock[0] = 3600
    reporter.take_summary()
    clock[0] = 7200
    assert reporter.take_summary() is None


@pytest.mark.asyncio
async def test_only_an_acknowledged_report_counts_as_delivered(tmp_path, monkeypatch):
    """The report counts as delivered only on the answer this backend gives:
    the success status carrying this supervisor's token. A refusal and a
    redirect alike leave the hostname pending, and the delivery call does not
    follow the redirect, so the page it points at is never asked."""
    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    answers = {"status": 403, "headers": {}}
    visits = []

    async def notice(request):
        await request.post()
        visits.append("notice")
        return web.Response(status=answers["status"], headers=answers["headers"])

    async def elsewhere(request):
        visits.append("elsewhere")
        return web.Response(status=204)

    backend_app = web.Application()
    backend_app.router.add_route("POST", "/_supervisor/host_refusal", notice)
    backend_app.router.add_route("GET", "/shell", elsewhere)
    backend = TestClient(TestServer(backend_app))
    await backend.start_server()
    try:
        monkeypatch.setattr(supervisor, "BACKEND_PORT", backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        client = await _supervisor_client(tmp_path, monkeypatch)
        try:
            await client.get("/", headers={"Host": FOREIGN_HOST_HEADER})
            reporter = supervisor._active_reporter

            # A 403 is not an acknowledgement: the hostname stays pending and
            # is tried again.
            for _ in range(200):
                if visits.count("notice") >= 2:
                    break
                await asyncio.sleep(0.01)
            assert reporter.pending_reports() == [FOREIGN_HOST]

            # Nor is a redirect one, and it is not followed.
            answers.update(status=302, headers={"Location": "/shell"})
            for _ in range(200):
                if visits.count("notice") >= 3:
                    break
                await asyncio.sleep(0.01)
            assert "elsewhere" not in visits
            assert reporter.pending_reports() == [FOREIGN_HOST]

            # The one answer that counts: the success status carrying this
            # supervisor's token.
            answers.update(status=204,
                           headers={supervisor.TOKEN_HEADER: "the-supervisor-token"})
            for _ in range(200):
                if not reporter.pending_reports():
                    break
                await asyncio.sleep(0.01)
            assert reporter.pending_reports() == []
        finally:
            await client.close()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_a_request_with_two_refused_hostnames_reports_both(tmp_path, monkeypatch):
    """Both hostnames a forwarded request carried are told to the backend,
    each within its own pace, so the health page and the console code can
    name either one."""
    reports = []

    async def notice(request):
        reports.append((await request.post())["host"])
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token"})

    backend_app = web.Application()
    backend_app.router.add_route("POST", "/_supervisor/host_refusal", notice)
    backend = TestClient(TestServer(backend_app))
    await backend.start_server()
    try:
        monkeypatch.setattr(supervisor, "BACKEND_PORT", backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        client = await _supervisor_client(tmp_path, monkeypatch)
        try:
            # The test client's peer is loopback, which the absent setting's
            # default names, so the forwarded host is believed.
            await client.get("/", headers={"Host": "first.example.com:6767",
                                           "X-Forwarded-Host": "second.example.com"})
            for _ in range(200):
                if len(reports) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert sorted(reports) == ["first.example.com", "second.example.com"]
        finally:
            await client.close()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_the_front_listener_names_one_hostname_once_when_host_and_forwarded_match(
        tmp_path, monkeypatch):
    """Host and forwarded hostname that are the same name, modulo a port or
    case, are one refusal at this layer too: the page names it once and one
    report is queued."""
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        response = await client.get("/", headers={"Host": "same.example.com:6767",
                                                  "X-Forwarded-Host": "SAME.example.com:443"})
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 421
    assert "same.example.com" in body
    assert "forwarded hostname" not in body
    assert supervisor._active_reporter.pending_reports() == ["same.example.com"]


@pytest.mark.asyncio
async def test_a_thousand_refusals_while_the_backend_is_down_stay_bounded(
        tmp_path, monkeypatch):
    """The pending reports and the delivery are bounded: a long roll of
    distinct hostnames against a backend that cannot be told leaves at most
    the refused-hosts limit of them waiting, the worker sends one report at
    a time, and once the backend answers it is told about all of them within
    the pace, with no request and no manual call in between."""
    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        for index in range(1000):
            response = await client.get("/", headers={"Host": f"h{index}.example.com"})
            assert response.status == 421
        reporter = supervisor._active_reporter
        pending = reporter.pending_reports()
        assert len(pending) == supervisor.host_policy.REFUSED_HOSTS_LIMIT
        # The eviction drops the oldest waiting entries and keeps the newest:
        # of the thousand refused names the last fifty survive, and the first
        # of them is the nine-hundred-fiftieth.
        assert pending[0] == "h950.example.com"

        in_flight = {"now": 0, "peak": 0}
        reports = []

        async def notice(request):
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            try:
                reports.append((await request.post())["host"])
                return web.Response(status=204,
                                    headers={supervisor.TOKEN_HEADER: "the-supervisor-token"})
            finally:
                in_flight["now"] -= 1

        backend_app = web.Application()
        backend_app.router.add_route("POST", "/_supervisor/host_refusal", notice)
        backend = TestClient(TestServer(backend_app))
        await backend.start_server()
        try:
            monkeypatch.setattr(supervisor, "BACKEND_PORT", backend.server.port)
            monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
            for _ in range(4000):
                if len(reports) >= supervisor.host_policy.REFUSED_HOSTS_LIMIT:
                    break
                await asyncio.sleep(0.01)
            for _ in range(500):
                if not reporter.pending_reports():
                    break
                await asyncio.sleep(0.01)
        finally:
            await backend.close()
        assert len(reports) == supervisor.host_policy.REFUSED_HOSTS_LIMIT
        assert in_flight["peak"] <= 1
        assert reporter.pending_reports() == []
        assert len(lines) <= 21
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_console_and_the_backend_share_the_refusal_log_budget(
        tmp_path, monkeypatch, fresh_state, root_log):
    """An outage is named on this console under the hour's line cap, and once
    the backend answers, the reports arrive and the backend owns the lines: a
    hostname this console already spent a line on, or held back under its
    cap, is not named a second time, so the two paths together stay within
    the hour's cap and the one line past it."""
    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    config = _config()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        # The outage: this console is the only place a refusal is named.
        for index in range(25):
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        assert len(lines) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        async def notice(request):
            form = await request.post()
            logged = form.get("logged")
            fresh_state.record_refusal(form["host"], config=config,
                                       line_at=float(logged) if logged else None)
            return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                     "the-supervisor-token"})

        backend_app = web.Application()
        backend_app.router.add_route("POST", "/_supervisor/host_refusal", notice)
        backend = TestClient(TestServer(backend_app))
        await backend.start_server()
        try:
            monkeypatch.setattr(supervisor, "BACKEND_PORT", backend.server.port)
            monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
            reporter = supervisor._active_reporter
            for _ in range(4000):
                if not reporter.pending_reports():
                    break
                await asyncio.sleep(0.01)
            assert reporter.pending_reports() == []
        finally:
            await backend.close()

        backend_lines = [message for message in root_log.messages
                         if "refused a request" in message]
        assert len(lines) + len(backend_lines) <= (
            supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR + 1)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_fresh_refusals_after_recovery_draw_on_the_merged_hour_budget(
        tmp_path, monkeypatch, fresh_state, root_log):
    """The outage spends this console's hour line cap, and the recovery is
    told so: each report carries the times of the refusal lines the console
    spent within the hour, the backend folds the ones its hour still holds
    into its own, and a hostname refused for the first time after the
    recovery draws on the one merged budget rather than a fresh cap, so the
    lines of the two processes together stay within the hour's cap."""
    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    config = _config()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        # The outage: this console is the only place a refusal is named.
        for index in range(25):
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        assert len(lines) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        async def notice(request):
            form = await request.post()
            logged = form.get("logged")
            hour_lines = form.get("hour_lines")
            carried = {"line_at": float(logged)} if logged else {}
            if hour_lines:
                carried["hour_lines"] = [float(stamp) for stamp in hour_lines.split(",")]
            fresh_state.record_refusal(form["host"], config=config, **carried)
            return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                     "the-supervisor-token"})

        backend_app = web.Application()
        backend_app.router.add_route("POST", "/_supervisor/host_refusal", notice)
        backend = TestClient(TestServer(backend_app))
        await backend.start_server()
        try:
            monkeypatch.setattr(supervisor, "BACKEND_PORT", backend.server.port)
            monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
            reporter = supervisor._active_reporter
            for _ in range(4000):
                if not reporter.pending_reports():
                    break
                await asyncio.sleep(0.01)
            assert reporter.pending_reports() == []

            # Fresh hostnames, refused for the first time while the backend
            # answers and within the same hour, name no console line of their
            # own: the backend owns their lines, out of the merged hour.
            for index in range(10):
                response = await client.get("/", headers={"Host": f"post{index}.example.com"})
                assert response.status == 421
            for _ in range(4000):
                if not reporter.pending_reports():
                    break
                await asyncio.sleep(0.01)
            assert reporter.pending_reports() == []
        finally:
            await backend.close()

        backend_lines = [message for message in root_log.messages
                         if "refused a request" in message]
        assert len(lines) + len(backend_lines) <= (
            supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR + 1)
    finally:
        await client.close()


def test_a_repeated_recovery_rolls_the_merged_budget_at_the_lines_true_times(
        real_factory, root_log, monkeypatch):
    """The report carries the times of the lines the console spent, not a
    count of them, so the backend's hour budget is the one the console's
    lines actually cover: a later outage replaces the console's lines as its
    hour rolls the old ones out, and the recovery within that rollover
    carries the fresh times, so the fresh hostnames of both recoveries name
    no line of their own while the console's lines hold, and the budget
    reopens only once those lines really run out."""
    from app import host_allowlist, host_policy
    from app.config import settings

    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "supervisor-token")
    clock = [0]
    monkeypatch.setattr(host_allowlist, "_refusals",
                        host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0]))
    client = real_factory.test_client()
    headers = {"Host": "127.0.0.1:6767", "X-Bazarr-Supervisor-Token": "supervisor-token"}

    def report(host, logged=None, spent_lines=()):
        data = {"host": host}
        if logged is not None:
            data["logged"] = repr(logged)
        if spent_lines:
            data["hour_lines"] = ",".join(repr(stamp) for stamp in spent_lines)
        return client.post("/_supervisor/host_refusal", headers=headers, data=data)

    cap = host_policy.REFUSAL_LOG_LINES_PER_HOUR

    # The first outage spent the hour's cap of lines within its hour, and
    # the first recovery lands inside that hour.
    first_lines = [60.0 + 30.0 * index for index in range(cap)]
    clock[0] = 1000
    assert report("down0.example.com", logged=first_lines[0],
                  spent_lines=first_lines).status_code == 204
    assert report("post0.example.com").status_code == 204

    # A later outage, past the first hour: the console's lines have rolled
    # out of its hour, and it spent the cap again at fresh times, so the
    # second recovery carries those and not the first hour's count.
    second_lines = [4030.0 + 30.0 * index for index in range(cap)]
    clock[0] = 4600
    assert report("down20.example.com", logged=second_lines[0],
                  spent_lines=second_lines).status_code == 204
    assert report("post1.example.com").status_code == 204

    # Both recoveries' fresh hostnames name no line: the budget the console
    # spent each hour covers them, wherever they were refused.
    refused = [message for message in root_log.messages if "refused a request" in message]
    assert refused == []
    issues = host_allowlist.health_issues(settings)
    assert any("post0.example.com" in issue["issue"] for issue in issues)
    assert any("post1.example.com" in issue["issue"] for issue in issues)

    # Once the second hour's lines have really run out, the budget reopens
    # and a fresh hostname names its line after all.
    clock[0] = 8201
    assert report("post2.example.com").status_code == 204
    refused = [message for message in root_log.messages if "refused a request" in message]
    assert len(refused) == 1
    assert "post2.example.com" in refused[0]


@pytest.mark.asyncio
async def test_the_console_and_the_backend_share_the_one_summary_window(
        tmp_path, monkeypatch, real_factory, root_log, caplog):
    """An outage opens this console's suppressed window, and the first report
    the recovered backend acknowledges carries it over: the lines the
    backend holds back after the recovery join the same window, and the one
    summary the window owes is logged by the backend alone, so a rolling hour
    never carries a summary from each of the two layers for the one stream
    of refusals."""
    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    console_lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", console_lines.append)
    caplog.set_level(logging.INFO)
    from waitress.server import create_server

    from app import host_allowlist
    from app.app import waitress_server_options

    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    clock = [0]
    monkeypatch.setattr(host_allowlist, "_refusals",
                        host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0]))

    # The backend answers the reports over a real socket, the way the
    # supervisor sends them, with the real notice route.
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    server = create_server(real_factory, host="127.0.0.1", port=port,
                           threads=4, **waitress_server_options())

    def serve():
        try:
            server.run()
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        # Wait for the delivery worker to hand every report over, letting a
        # little time pass on the shared clock as the round trips run.
        async def settle():
            for _ in range(6000):
                if not reporter.pending_reports():
                    return
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            raise AssertionError("the reports never drained")

        # The outage: this console names the hour's cap of lines and holds
        # the rest back, which opens its suppressed window.
        for index in range(30):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in console_lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        # The recovery: the reports arrive at the real notice route, and the
        # first one the backend acknowledges carries the console's window.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        await settle()
        clock[0] = 3000

        # Fresh hostnames, refused while the backend answers: the merged
        # budget is spent, so the backend holds their lines back into the
        # window it now holds.
        for index in range(5):
            response = await client.get("/", headers={"Host": f"fresh{index}.example.com"})
            assert response.status == 421
        await settle()

        # The window's hour runs: the console no longer holds one, so it
        # prints no summary, and the backend logs the one the combined
        # window owes.
        clock[0] = 3700
        response = await client.get("/", headers={"Host": "ripe.example.com"})
        assert response.status == 421
        await settle()

        # Past the whole recovery, the next hour's refusals are named or
        # held back within the budget the recovery left, and no second
        # summary of the first stream arrives.
        clock[0] = 7000
        response = await client.get("/", headers={"Host": "late.example.com"})
        assert response.status == 421
        await settle()

        console_summaries = [line for line in console_lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert len(console_summaries) + len(backend_summaries) <= 1
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 15 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        server.close()
        thread.join(timeout=10)


@pytest.mark.asyncio
async def test_a_retried_report_counts_its_carried_window_once(tmp_path, monkeypatch, root_log,
                                                               caplog):
    """A report the backend processes but whose answer is lost stays
    pending and is retried, and the retry carries the console's suppressed
    window again: the backend folds a window it has already counted
    nowhere, so the one summary the window owes when its hour runs counts
    the lines it holds once, and not once per delivery attempt."""
    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)
    from app import host_allowlist

    clock = [0]
    monkeypatch.setattr(host_allowlist, "_refusals",
                        host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0]))
    config = _config()
    client = await _supervisor_client(tmp_path, monkeypatch)
    reporter = supervisor._active_reporter
    monkeypatch.setattr(reporter, "_now", lambda: clock[0])
    monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])
    try:
        # The outage: this console spends the hour's cap of lines and holds
        # the rest back, which opens its suppressed window.
        for index in range(25):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        answers = {"lost": True}

        async def notice(request):
            form = await request.post()
            carried = {}
            logged = form.get("logged")
            if logged:
                carried["line_at"] = float(logged)
            hour_lines = form.get("hour_lines")
            if hour_lines:
                carried["hour_lines"] = [float(stamp) for stamp in hour_lines.split(",")]
            for field, parse in (("suppressed", int), ("suppressed_since", float),
                                 ("mirrored", int), ("mirrored_since", float)):
                if form.get(field):
                    carried[field] = parse(form[field])
            for field in ("suppressed_id", "mirrored_id"):
                if form.get(field):
                    carried[field] = form[field]
            # The backend takes the report in, and then its answer is lost.
            host_allowlist.record_refusal(form["host"], config=config, **carried)
            if answers["lost"]:
                answers["lost"] = False
                return web.Response(status=503)
            return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                     "the-supervisor-token"})

        backend_app = web.Application()
        backend_app.router.add_route("POST", "/_supervisor/host_refusal", notice)
        backend = TestClient(TestServer(backend_app))
        await backend.start_server()
        try:
            monkeypatch.setattr(supervisor, "BACKEND_PORT", backend.server.port)
            monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
            for _ in range(6000):
                if not reporter.pending_reports():
                    break
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            assert reporter.pending_reports() == []
        finally:
            await backend.close()

        # The window's hour runs: the backend holds it, folded once, so it
        # alone logs the one summary the window owes, counting it once
        # however many times the report that carried it was delivered.
        clock[0] = 3700
        host_allowlist.health_issues(config)
        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 5 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_lost_acknowledgement_leaves_the_one_summary_with_the_backend(tmp_path, monkeypatch,
                                                                             root_log, caplog):
    """An already-due window travels with a report the backend processes and
    summarizes before its answer is lost. The console keeps its copy in
    flight instead of folding it back into its own recorder, so a refusal
    before the retry succeeds emits nothing here, and the retry's
    acknowledgement names the window among the IDs the backend has
    completed, so the console discards its copy: the pair has logged the one
    summary the window owes, from the backend alone."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    answer = {"lost": True}
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        notice_route = deliver(form)
        if notice_route[1] != 204:
            return web.Response(status=503)
        if answer["lost"]:
            # The backend has taken the report in and logged the summary the
            # carried window owed; its answer is lost all the same.
            answer["lost"] = False
            return web.Response(status=503)
        delivered.append(form)
        _body, _status, headers = notice_route
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    losing_backend = TestClient(TestServer(backend))
    await losing_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        # The outage spends the hour's cap of lines and holds the rest back,
        # which opens the console's suppressed window.
        for index in range(25):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        # The window is already due when the first report carries it, and the
        # backend that takes the report in logs its summary before an answer
        # that never arrives.
        clock[0] = 3700
        monkeypatch.setattr(supervisor, "BACKEND_PORT", losing_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        for _ in range(2000):
            if not answer["lost"]:
                break
            await asyncio.sleep(0.01)
        assert not answer["lost"]

        # A refusal before the retry succeeds names no summary here: the copy
        # the console kept never folds back into its own recorder, and the
        # sustained-outage case has not run, with the carry just begun.
        response = await client.get("/", headers={"Host": "between.example.com"})
        assert response.status == 421

        # The retry lands, and the pending refusal's report after it. The
        # retry's acknowledgement carries the IDs of the windows the backend
        # has completed, the one this report carried among them.
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        # A later report carries no copy of the window: the console discarded
        # its copy when the acknowledgement named it completed.
        delivered.clear()
        clock[0] = 3701
        response = await client.get("/", headers={"Host": "witness.example.com"})
        assert response.status == 421
        for _ in range(6000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert "suppressed" not in delivered[0]
        assert "suppressed_id" not in delivered[0]

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 5 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        await losing_backend.close()


@pytest.mark.asyncio
async def test_a_prolonged_outage_after_a_lost_answer_keeps_the_one_summary_with_the_backend(
        tmp_path, monkeypatch, root_log, caplog):
    """An already-due window travels with a report the backend processes and
    summarizes before its answer is lost, and the backend then stays
    unreachable past the window's hour. The console never emits the copy it
    kept, because the report reached the backend at the transport level and
    the backend may have logged the summary before its answer was lost, so
    a refusal arriving in the prolonged outage writes no summary here. The
    pair leaves the window to the acknowledgement protocol instead: the
    retry that reaches the recovered backend carries the copy, the backend
    folds a window it still counts nowhere, and the acknowledgement names
    the window among the IDs it has completed, so the console discards its
    copy: the pair has logged the one summary the window owes, from the
    backend alone."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    answer = {"lost": True}
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        if answer["lost"]:
            # The backend takes a moment of the shared clock to process the
            # report, after this console's carry began, and logs the
            # summary the carried window owed.
            clock[0] += 1
        notice_route = deliver(form)
        if notice_route[1] != 204:
            return web.Response(status=503)
        if answer["lost"]:
            # The backend has taken the report in and logged the summary the
            # carried window owed; its answer is lost all the same.
            answer["lost"] = False
            return web.Response(status=503)
        delivered.append(form)
        _body, _status, headers = notice_route
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    losing_backend = TestClient(TestServer(backend))
    await losing_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    recovered_backend = None
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        # The outage spends the hour's cap of lines and holds the rest back,
        # which opens the console's suppressed window.
        for index in range(25):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        # The window is already due when the first report carries it, and the
        # backend that takes the report in logs its summary before an answer
        # that never arrives.
        clock[0] = 3700
        monkeypatch.setattr(supervisor, "BACKEND_PORT", losing_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        for _ in range(2000):
            if not answer["lost"]:
                break
            await asyncio.sleep(0.01)
        assert not answer["lost"]

        # The backend becomes unreachable, and the outage outlasts the
        # window's hour with no acknowledgement of any kind in between.
        await losing_backend.close()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        clock[0] = 3700 + int(supervisor.host_policy.HOUR_SECONDS)

        # A refusal arrives in the prolonged outage: the console emits no
        # second summary for a window the report already reached the backend
        # with, whatever became of its answer, so the copy waits in flight
        # for the backend to answer again.
        response = await client.get("/", headers={"Host": "late.example.com"})
        assert response.status == 421
        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        assert console_summaries == []

        # The backend recovers, and the retry reaches it: the report carries
        # the window again, the backend folds a window it still counts
        # nowhere, and the acknowledgement names its ID among the windows
        # the backend has completed, so the console discards its copy.
        recovered_backend = TestClient(TestServer(backend))
        await recovered_backend.start_server()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", recovered_backend.server.port)
        for _ in range(2000):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert delivered
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        # A later report carries no copy of the window: the console discarded
        # its copy when the acknowledgement named it completed.
        delivered.clear()
        clock[0] += 1
        response = await client.get("/", headers={"Host": "witness.example.com"})
        assert response.status == 421
        for _ in range(6000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert "suppressed" not in delivered[0]
        assert "suppressed_id" not in delivered[0]

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 5 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        if recovered_backend is not None:
            await recovered_backend.close()


@pytest.mark.asyncio
async def test_a_prolonged_outage_after_a_dropped_exchange_keeps_the_one_summary_with_the_backend(
        tmp_path, monkeypatch, root_log, caplog):
    """An already-due window travels with a report the backend processes
    and summarizes before the connection drops without any response byte.
    The delivery counts as possibly delivered, because the exchange had
    its connection open, and the backend then stays unreachable past the
    window's hour with no acknowledgement: the console never emits the
    copy it kept, so a refusal arriving in the prolonged outage writes no
    summary here. The pair leaves the window to the acknowledgement
    protocol instead: the retry that reaches the recovered backend
    carries the copy, the backend folds a window it still counts nowhere,
    and the acknowledgement names the window among the IDs it has
    completed, so the console discards its copy: the pair has logged the
    one summary the window owes, from the backend alone."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    answer = {"dropped": True}
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        if answer["dropped"]:
            # The backend takes a moment of the shared clock to process the
            # report, after this console's carry began, and logs the
            # summary the carried window owed.
            clock[0] += 1
        notice_route = deliver(form)
        if notice_route[1] != 204:
            return web.Response(status=503)
        if answer["dropped"]:
            # The backend has taken the report in and logged the summary the
            # carried window owed, and then the connection drops before any
            # response byte leaves: the acknowledgement this route built
            # never reaches the wire, so no answer arrives at all.
            answer["dropped"] = False
            request.transport.close()
            return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                     "the-supervisor-token",
                                                     **notice_route[2]})
        delivered.append(form)
        _body, _status, headers = notice_route
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    dropping_backend = TestClient(TestServer(backend))
    await dropping_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    recovered_backend = None
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        # The outage spends the hour's cap of lines and holds the rest back,
        # which opens the console's suppressed window.
        for index in range(25):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        # The window is already due when the first report carries it, and the
        # backend that takes the report in logs its summary before dropping
        # the connection without any response byte. The failure path names
        # the report's hostname once the console has seen the drop, so the
        # wait ends only after the exchange has failed here.
        clock[0] = 3700
        monkeypatch.setattr(supervisor, "BACKEND_PORT", dropping_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        failed = [line for line in lines if line.startswith("Refused")]
        for _ in range(2000):
            if len([line for line in lines if line.startswith("Refused")]) > len(failed):
                break
            await asyncio.sleep(0.01)
        assert not answer["dropped"]
        assert len([line for line in lines if line.startswith("Refused")]) > len(failed)

        # The backend becomes unreachable, and the outage outlasts the
        # window's hour with no acknowledgement of any kind in between.
        await dropping_backend.close()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        clock[0] = 3700 + int(supervisor.host_policy.HOUR_SECONDS)

        # A refusal arrives in the prolonged outage: the console emits no
        # second summary for a window the report's exchange carried on an
        # open connection, whatever became of the answer, so the copy waits
        # in flight for the backend to answer again.
        response = await client.get("/", headers={"Host": "late.example.com"})
        assert response.status == 421
        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        assert console_summaries == []

        # The backend recovers, and the retry reaches it: the report carries
        # the window again, the backend folds a window it still counts
        # nowhere, and the acknowledgement names its ID among the windows
        # the backend has completed, so the console discards its copy.
        recovered_backend = TestClient(TestServer(backend))
        await recovered_backend.start_server()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", recovered_backend.server.port)
        for _ in range(2000):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert delivered
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        # A later report carries no copy of the window: the console discarded
        # its copy when the acknowledgement named it completed.
        delivered.clear()
        clock[0] += 1
        response = await client.get("/", headers={"Host": "witness.example.com"})
        assert response.status == 421
        for _ in range(6000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert "suppressed" not in delivered[0]
        assert "suppressed_id" not in delivered[0]

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 5 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        if recovered_backend is not None:
            await recovered_backend.close()


@pytest.mark.asyncio
async def test_a_connect_phase_stall_leaves_the_carried_window_unsent(tmp_path, monkeypatch,
                                                                      root_log, caplog):
    """An already-due window travels with a report whose connection stalls
    before it ever opens: the session's one pool slot is held by another
    long-lived request, so the report waits for the slot inside its
    connect phase and no request byte of it ever leaves for any backend.
    The stall resolves as the connect timeout, the connector's own
    failure to open a connection, so the window stays unsent and the
    sustained-outage emission stays available: once the backend has been
    unreachable past the window's hour with no acknowledgement of any
    kind, a refusal arriving in that outage emits the window here, once,
    because nothing could have summarized it anywhere."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        delivered.append(form)
        _body, _status, headers = deliver(form)
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token", **headers})

    # A request this route holds open occupies the session's one pool slot:
    # the pool wait runs inside a report's connect phase, so a report sent
    # on the same session waits there for the slot, where the connect
    # timeout is what ends the wait, and no request byte of it leaves.
    slot_entered = asyncio.Event()
    slot_release = asyncio.Event()

    async def hold(request):
        slot_entered.set()
        await slot_release.wait()
        return web.Response(text="held")

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    backend.router.add_route("GET", "/hold", hold)
    stall_backend = TestClient(TestServer(backend))
    await stall_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    hold_task = None
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        # The delivery worker's session is built over a connector of one
        # slot, with the timeout the worker itself asks for, so a long-lived
        # request on it can hold a report's connect phase closed.
        real_client_session = supervisor.ClientSession
        made = []

        def one_slot_session(*args, **kwargs):
            kwargs["connector"] = TCPConnector(limit=1)
            session = real_client_session(*args, **kwargs)
            made.append(session)
            return session

        monkeypatch.setattr(supervisor, "ClientSession", one_slot_session)

        # The unresolved copy's return from a delivery is how this test
        # sees the stalled exchange end.
        restored = []
        real_restore = reporter.restore_carried_summary

        def recording_restore(carried, reached=False):
            restored.append(reached)
            real_restore(carried, reached)

        monkeypatch.setattr(reporter, "restore_carried_summary", recording_restore)

        # A port nothing listens on, so the first report of the outage is
        # refused at the connect.
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            refused_port = reserve.getsockname()[1]

        # The outage spends the hour's cap of lines and holds the rest back,
        # which opens the console's suppressed window.
        for index in range(25):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        # The first report reaches for a port nothing answers and is
        # refused at the connect, so no request byte of it ever leaves: the
        # window stays unsent in the slot, and the attempt builds the
        # session the stall needs.
        clock[0] = 3700
        monkeypatch.setattr(supervisor, "BACKEND_PORT", refused_port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        for _ in range(2000):
            if len([line for line in lines if line.startswith("Refused")]) > len(named):
                break
            await asyncio.sleep(0.01)
        assert len([line for line in lines if line.startswith("Refused")]) > len(named)
        restored.clear()

        # The held request takes the one pool slot, and the report the
        # console then retries carries the window again on the same session,
        # against a backend that would answer it: the connect phase waits
        # for the slot, and the timeout that ends the wait is the connect
        # one, before any connection for the report ever opened.
        async def hold_the_slot():
            async with made[0].get(f"http://127.0.0.1:{stall_backend.server.port}/hold",
                                  timeout=ClientTimeout(total=None)) as response:
                await response.read()

        hold_task = asyncio.create_task(hold_the_slot())
        await slot_entered.wait()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", stall_backend.server.port)
        clock[0] += 0.06
        for _ in range(1500):
            if restored:
                break
            await asyncio.sleep(0.01)
        assert restored
        slot_release.set()
        await hold_task
        hold_task = None

        # The backend becomes unreachable, and the outage outlasts the
        # window's hour with no acknowledgement of any kind in between.
        await stall_backend.close()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        clock[0] = 3700 + int(supervisor.host_policy.HOUR_SECONDS)

        # A refusal arrives in the prolonged outage: the stalled exchange
        # never opened a connection, so no request byte of the window ever
        # left for any backend, and the console emits it here, once, the
        # sustained-outage case.
        response = await client.get("/", headers={"Host": "late.example.com"})
        assert response.status == 421
        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        assert len(console_summaries) == 1
        assert console_summaries[0].startswith("Suppressed 5 host-refusal lines between")

        # The emission is the window's one summary: a later refusal in the
        # same outage writes no second line here.
        response = await client.get("/", headers={"Host": "again.example.com"})
        assert response.status == 421
        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        assert len(console_summaries) == 1

        # No report ever reached the backend, so it holds and logs nothing
        # of the window.
        assert delivered == []
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert backend_summaries == []
    finally:
        if hold_task is not None:
            hold_task.cancel()
            try:
                await hold_task
            except asyncio.CancelledError:
                pass
        await client.close()


@pytest.mark.asyncio
async def test_a_retry_reaching_the_backend_more_than_an_hour_later_folds_the_window_nowhere(
        tmp_path, monkeypatch, root_log, caplog):
    """An already-due window travels with a report the backend processes
    and summarizes before the connection drops without any response byte,
    and the backend then stays unreachable more than an hour past the
    moment it processed that report, while the console's copy is still
    within the span it may keep re-carrying a sent window no answer
    resolved. The retry that reaches the recovered backend meets one that
    still remembers the window's ID, so the re-carried copy folds nowhere
    and the backend logs no second summary: the pair keeps the one summary
    the window owes, the backend's, and the console discards its copy when
    the acknowledgement names the window completed."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    answer = {"dropped": True}
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        if answer["dropped"]:
            # The backend takes a moment of the shared clock to process the
            # report, after this console's carry began, and logs the
            # summary the carried window owed.
            clock[0] += 1
        notice_route = deliver(form)
        if notice_route[1] != 204:
            return web.Response(status=503)
        if answer["dropped"]:
            # The backend has taken the report in and logged the summary the
            # carried window owed, and then the connection drops before any
            # response byte leaves: the acknowledgement this route built
            # never reaches the wire, so no answer arrives at all.
            answer["dropped"] = False
            request.transport.close()
            return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                     "the-supervisor-token",
                                                     **notice_route[2]})
        delivered.append(form)
        _body, _status, headers = notice_route
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    dropping_backend = TestClient(TestServer(backend))
    await dropping_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    recovered_backend = None
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        # The outage spends the hour's cap of lines and holds the rest back,
        # which opens the console's suppressed window.
        for index in range(25):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        # The window is already due when the first report carries it, and the
        # backend that takes the report in logs its summary before dropping
        # the connection without any response byte. The failure path names
        # the report's hostname once the console has seen the drop, so the
        # wait ends only after the exchange has failed here.
        clock[0] = 3700
        monkeypatch.setattr(supervisor, "BACKEND_PORT", dropping_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        failed = [line for line in lines if line.startswith("Refused")]
        for _ in range(2000):
            if len([line for line in lines if line.startswith("Refused")]) > len(failed):
                break
            await asyncio.sleep(0.01)
        assert not answer["dropped"]
        assert len([line for line in lines if line.startswith("Refused")]) > len(failed)

        # The backend becomes unreachable, and the outage outlasts the
        # window's hour, running more than an hour past the moment the
        # backend processed the report, with no acknowledgement of any kind
        # in between, while the carry is still within the span the console
        # may keep re-carrying its unresolved sent copy.
        await dropping_backend.close()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        clock[0] = 3700 + int(supervisor.host_policy.HOUR_SECONDS) + 60

        # A refusal arrives in the prolonged outage: the console emits no
        # second summary for a window the report's exchange carried on an
        # open connection, so the copy waits in flight for the backend to
        # answer again.
        response = await client.get("/", headers={"Host": "late.example.com"})
        assert response.status == 421
        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        assert console_summaries == []

        # The backend recovers, and the retry reaches it more than an hour
        # after it processed the report: the backend still remembers the
        # window's ID, so the re-carried copy folds nowhere and no second
        # summary is logged, and the acknowledgement names the window among
        # the IDs the backend has completed, so the console discards its
        # copy.
        recovered_backend = TestClient(TestServer(backend))
        await recovered_backend.start_server()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", recovered_backend.server.port)
        for _ in range(2000):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert delivered
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        # A later report carries no copy of the window: the console discarded
        # its copy when the acknowledgement named it completed.
        delivered.clear()
        clock[0] += 1
        response = await client.get("/", headers={"Host": "witness.example.com"})
        assert response.status == 421
        for _ in range(6000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert "suppressed" not in delivered[0]
        assert "suppressed_id" not in delivered[0]

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 5 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        if recovered_backend is not None:
            await recovered_backend.close()


@pytest.mark.asyncio
async def test_a_carry_past_the_retention_horizon_drops_the_unresolved_sent_copy(
        tmp_path, monkeypatch, root_log, caplog):
    """An already-due window travels with a report the backend processes
    and summarizes before the connection drops without any response byte,
    and the backend then stays unreachable past the whole span the console
    may keep re-carrying its unresolved sent copy. The console drops the
    copy outright rather than re-carry a window whose ID no backend
    remembers anymore: nothing about the window reaches the recovered
    backend, no report carries a copy of it, and no second summary is
    logged anywhere, so the pair is left with the one summary the backend
    already logged, losing the copy's claim rather than logging the window
    twice."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    answer = {"dropped": True}
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        if answer["dropped"]:
            # The backend takes a moment of the shared clock to process the
            # report, after this console's carry began, and logs the
            # summary the carried window owed.
            clock[0] += 1
        notice_route = deliver(form)
        if notice_route[1] != 204:
            return web.Response(status=503)
        if answer["dropped"]:
            # The backend has taken the report in and logged the summary the
            # carried window owed, and then the connection drops before any
            # response byte leaves: the acknowledgement this route built
            # never reaches the wire, so no answer arrives at all.
            answer["dropped"] = False
            request.transport.close()
            return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                     "the-supervisor-token",
                                                     **notice_route[2]})
        delivered.append(form)
        _body, _status, headers = notice_route
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    dropping_backend = TestClient(TestServer(backend))
    await dropping_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    recovered_backend = None
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        # The outage spends the hour's cap of lines and holds the rest back,
        # which opens the console's suppressed window.
        for index in range(25):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        # The window is already due when the first report carries it, and the
        # backend that takes the report in logs its summary before dropping
        # the connection without any response byte. The failure path names
        # the report's hostname once the console has seen the drop, so the
        # wait ends only after the exchange has failed here.
        clock[0] = 3700
        monkeypatch.setattr(supervisor, "BACKEND_PORT", dropping_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        failed = [line for line in lines if line.startswith("Refused")]
        for _ in range(2000):
            if len([line for line in lines if line.startswith("Refused")]) > len(failed):
                break
            await asyncio.sleep(0.01)
        assert not answer["dropped"]
        assert len([line for line in lines if line.startswith("Refused")]) > len(failed)

        # The backend becomes unreachable, and the outage outlasts the
        # whole span the console may keep re-carrying its unresolved sent
        # copy, twice the window's hour, with no acknowledgement of any
        # kind in between.
        await dropping_backend.close()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        clock[0] = 3700 + 2 * int(supervisor.host_policy.HOUR_SECONDS) + 60

        # A refusal arrives past that span: the console drops its unresolved
        # sent copy outright, emitting nothing here, because re-carrying it
        # again would meet a backend that no longer remembers the window's
        # ID and would log a second summary, so the pair loses the one
        # summary the backend may never have taken rather than risk the
        # second line.
        response = await client.get("/", headers={"Host": "late.example.com"})
        assert response.status == 421
        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        assert console_summaries == []

        # The backend recovers, and the reports that reach it carry no copy
        # of the window: the console dropped the one it held, and its own
        # recorder's window was carried out long ago, so no form names the
        # window and the backend folds nothing in and logs nothing.
        recovered_backend = TestClient(TestServer(backend))
        await recovered_backend.start_server()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", recovered_backend.server.port)
        for _ in range(2000):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert delivered
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []
        for form in delivered:
            assert "suppressed" not in form
            assert "suppressed_id" not in form

        # A later witness report carries no copy of the window either: the
        # slot is empty, so the console has nothing left to re-carry.
        delivered.clear()
        clock[0] += 1
        response = await client.get("/", headers={"Host": "witness.example.com"})
        assert response.status == 421
        for _ in range(6000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert "suppressed" not in delivered[0]
        assert "suppressed_id" not in delivered[0]

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 5 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        if recovered_backend is not None:
            await recovered_backend.close()


@pytest.mark.asyncio
async def test_a_recovery_past_the_retention_horizon_with_no_refusal_drops_the_stale_carry(
        tmp_path, monkeypatch, root_log, caplog):
    """An already-due window travels with a report the backend processes
    and summarizes before the connection drops without any response byte,
    and the backend then stays unreachable past the whole span the console
    may keep re-carrying its unresolved sent copy, with no refusal
    arriving at the console in between: the local emission never runs, so
    the slot is the only place the stale copy lives, and the worker's own
    retry of the report the drop left pending is what reaches the
    recovered backend. The slot ages out on the same span the backend
    remembers the window's ID, so no report carries the copy to a backend
    that has pruned the ID and would fold it into a fresh window and count
    its lines a second time: the pair keeps the one summary the backend
    already logged, and the take falls through to the recorder's own
    window, which the outage long ago carried out, so it carries nothing."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    answer = {"dropped": True}
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        if answer["dropped"]:
            # The backend takes a moment of the shared clock to process the
            # report, after this console's carry began, and logs the
            # summary the carried window owed.
            clock[0] += 1
        notice_route = deliver(form)
        if notice_route[1] != 204:
            return web.Response(status=503)
        if answer["dropped"]:
            # The backend has taken the report in and logged the summary the
            # carried window owed, and then the connection drops before any
            # response byte leaves: the acknowledgement this route built
            # never reaches the wire, so no answer arrives at all.
            answer["dropped"] = False
            request.transport.close()
            return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                     "the-supervisor-token",
                                                     **notice_route[2]})
        delivered.append(form)
        _body, _status, headers = notice_route
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    dropping_backend = TestClient(TestServer(backend))
    await dropping_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    recovered_backend = None
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        # The outage spends the hour's cap of lines and holds the rest back,
        # which opens the console's suppressed window.
        for index in range(25):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        # The window is already due when the first report carries it, and the
        # backend that takes the report in logs its summary before dropping
        # the connection without any response byte. The failure path names
        # the report's hostname once the console has seen the drop, so the
        # wait ends only after the exchange has failed here.
        clock[0] = 3700
        monkeypatch.setattr(supervisor, "BACKEND_PORT", dropping_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        failed = [line for line in lines if line.startswith("Refused")]
        for _ in range(2000):
            if len([line for line in lines if line.startswith("Refused")]) > len(failed):
                break
            await asyncio.sleep(0.01)
        assert not answer["dropped"]
        assert len([line for line in lines if line.startswith("Refused")]) > len(failed)

        # The backend becomes unreachable, and the outage outlasts the whole
        # span the console may keep re-carrying its unresolved sent copy,
        # with no acknowledgement of any kind in between and no refusal
        # arriving at the console: the local emission never runs, so the
        # stale copy waits in the slot alone.
        await dropping_backend.close()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        clock[0] = 3700 + 2 * int(supervisor.host_policy.HOUR_SECONDS) + 60

        # The backend recovers past that span, and the worker's own retry of
        # the report the drop left pending reaches it: the slot drops the
        # stale sent copy before any report can carry it, on the same
        # retention span the backend pruned the window's ID on, so no
        # report carries a copy of the window, the backend folds nothing in
        # and logs no second summary of the lines it already paid, and the
        # take that follows the drop falls through to the recorder's own
        # window, which the outage long ago carried out.
        recovered_backend = TestClient(TestServer(backend))
        await recovered_backend.start_server()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", recovered_backend.server.port)
        for _ in range(2000):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert delivered
        for form in delivered:
            assert "suppressed" not in form
            assert "suppressed_id" not in form
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        # A later report carries no copy of the window either: the slot is
        # empty, and the recorder holds no window of its own.
        delivered.clear()
        clock[0] += 1
        response = await client.get("/", headers={"Host": "witness.example.com"})
        assert response.status == 421
        for _ in range(6000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert "suppressed" not in delivered[0]
        assert "suppressed_id" not in delivered[0]

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 5 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        if recovered_backend is not None:
            await recovered_backend.close()


@pytest.mark.asyncio
async def test_a_merged_window_completes_the_folded_id_and_a_restart_replay_folds_it_nowhere(
        tmp_path, monkeypatch, root_log, caplog):
    """The backend holds a suppressed window of its own, opened before the
    one the console's outage opens, and the recovery's first acknowledged
    report carries the console's window over: the backend folds it into
    its own, the merged window's hour has already run, and the same report
    pays the one summary the combined window owes. The summary completes
    every ID the window counts, its own minted one and the folded-in one,
    so the acknowledgement names them both and the console drops the copy
    it holds of the folded window on the answer. A backend restart before
    the horizon empties the counted windows cannot make the pair count the
    folded lines again: the replay's report carries no copy of the window,
    the restarted backend folds nothing in, and no second summary of lines
    the first one already paid is logged anywhere."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        delivered.append(form)
        _body, _status, headers = deliver(form)
        if _status != 204:
            return web.Response(status=503)
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    answering_backend = TestClient(TestServer(backend))
    await answering_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        async def settle():
            for _ in range(6000):
                if not reporter.pending_reports():
                    return
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            raise AssertionError("the reports never drained")

        # The backend answers while the console refuses: the reported
        # hostnames spend the backend's hour's cap of lines and the last one
        # is held back, which opens the backend's own suppressed window and
        # mints its ID at its first held-back line.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        for index in range(21):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"up{index}.example.com"})
            assert response.status == 421
        await settle()
        held = recorder.hour_suppressed_window()
        assert held is not None
        assert held[0] == 1

        # The backend becomes unreachable, and this console spends its own
        # hour's cap of lines while it cannot be told, holding the rest back
        # into a window of its own, opened later than the backend's: the
        # line times the acknowledgements folded in have rolled out of the
        # hour by then, so the merged budget the answering backend left is
        # spent and rolled over.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        for index in range(25):
            clock[0] = 3700 + index
            response = await client.get("/", headers={"Host": f"down{index}.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR

        # The recovery lands past the backend window's hour: the first
        # acknowledged report carries the console's window, the backend
        # folds it into its own earlier one, and the merged window's hour
        # has already run, so the same report pays the one summary the
        # combined window owes, completing every ID the window counts.
        clock[0] = 4000
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        await settle()

        # The backend restarts, its memory of the window IDs gone, well
        # inside the span the counted windows were kept for: a refusal at
        # the console queues the replay's report, and the acknowledgement
        # after the merge has named the folded ID, so the console holds no
        # copy of the window and the report carries nothing of it. The
        # replay lands past the folded window's hour but inside the hour
        # the carry began in, so the restarted backend folds nothing in
        # and logs no second summary of the lines the first one paid.
        restarted = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
        monkeypatch.setattr(host_allowlist, "_refusals", restarted)
        delivered.clear()
        clock[0] = 7400
        response = await client.get("/", headers={"Host": "replay.example.com"})
        assert response.status == 421
        for _ in range(2000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert delivered
        assert "suppressed" not in delivered[0]
        assert "suppressed_id" not in delivered[0]
        assert "mirrored" not in delivered[0]
        assert "mirrored_id" not in delivered[0]
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 6 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        await answering_backend.close()


@pytest.mark.asyncio
async def test_an_undue_aggregate_owns_its_folded_window_across_a_backend_restart(
        tmp_path, monkeypatch, root_log, caplog):
    """The backend holds a suppressed window of its own, opened before the
    one the console's outage opens, and the recovery's first acknowledged
    report carries the console's window over: the backend folds it into
    its own, and the merged window's hour has not run yet, so the same
    report pays nothing and the answer carries the aggregate back, which
    the console mirrors. The answer resolves the carry as well: the
    aggregate it carries back already counts the folded window's lines,
    folded in before the answer was built, so the folded window belongs
    to the aggregate from the answer on and the console keeps no copy of
    its own beside the mirror. A backend restart before the aggregate's
    hour runs therefore cannot count the folded lines twice: the
    replay's report carries the mirror alone, the restarted backend
    folds the aggregate in counting the folded lines the one time the
    aggregate counts them, and the summary the hour finally runs to pays
    the six lines the pair held back, not the eleven a copy re-delivered
    beside the aggregate would have counted."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        delivered.append(form)
        _body, _status, headers = deliver(form)
        if _status != 204:
            return web.Response(status=503)
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    answering_backend = TestClient(TestServer(backend))
    await answering_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        async def settle():
            for _ in range(6000):
                if not reporter.pending_reports():
                    return
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            raise AssertionError("the reports never drained")

        # The backend answers while the console refuses: the reported
        # hostnames spend the backend's hour's cap of lines, one line each,
        # and the console writes none of its own.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        for index in range(supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"up{index}.example.com"})
            assert response.status == 421
        await settle()

        # The cap's line times are still live, so one more refusal is held
        # back into the backend's own window, and the acknowledgement of
        # its report carries that window back to the console: the window's
        # hour has not begun to run yet.
        clock[0] = 3000
        response = await client.get("/", headers={"Host": "upward.example.com"})
        assert response.status == 421
        await settle()
        held = recorder.hour_suppressed_window()
        assert held is not None
        assert held[0] == 1
        backend_window = held[2]

        # The backend becomes unreachable, and this console spends one line
        # of its own, because the line times the acknowledgements folded in
        # have rolled out of the hour by then, and holds the refusals that
        # follow it back into a window of its own.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        for index in range(6):
            clock[0] = 3700 + index
            response = await client.get("/", headers={"Host": "down.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == 1

        # The recovery lands inside the aggregate's hour: the first
        # acknowledged report carries the console's window, the backend
        # folds it into its own earlier one, and the merged window is not
        # due yet, so nothing is paid and the answer carries the aggregate
        # back. The answer resolves the carry, and the aggregate it names
        # owns the folded window from then on: the console holds the mirror
        # of the aggregate and no copy of the folded window beside it.
        clock[0] = 5000
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        await settle()
        aggregated = recorder.hour_suppressed_window()
        assert aggregated is not None
        assert aggregated[0] == 6
        assert aggregated[2] == backend_window
        mirrored = reporter.mirrored_summary()
        assert mirrored is not None
        assert mirrored[0] == 6
        assert mirrored[2] == backend_window
        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert backend_summaries == []

        # The backend restarts, its memory of the window IDs gone, well
        # inside the hour the aggregate still has to run: a refusal at the
        # console queues the replay's report, which carries the mirror
        # alone, because the aggregate owns the folded window and no copy
        # of it waits in the slot to be counted beside the aggregate that
        # already counts it.
        restarted = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
        monkeypatch.setattr(host_allowlist, "_refusals", restarted)
        delivered.clear()
        clock[0] = 6100
        response = await client.get("/", headers={"Host": "replay.example.com"})
        assert response.status == 421
        for _ in range(2000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert delivered
        assert "suppressed" not in delivered[0]
        assert "suppressed_id" not in delivered[0]
        assert int(delivered[0]["mirrored"]) == 6
        assert delivered[0]["mirrored_id"] == backend_window
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        # The restarted backend folded the mirror in, so its window counts
        # the folded lines the one time the aggregate counts them: the six
        # the pair held back, and not the eleven a copy re-delivered beside
        # the aggregate would have counted.
        merged = restarted.hour_suppressed_window()
        assert merged is not None
        assert merged[0] == 6
        assert merged[2] == backend_window

        # The aggregate's hour runs, and the restarted backend pays the one
        # summary the window owes, once, without the console ever naming a
        # summary of its own.
        clock[0] = 6700
        response = await client.get("/", headers={"Host": "witness.example.com"})
        assert response.status == 421
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 6 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        await answering_backend.close()


@pytest.mark.asyncio
async def test_a_live_window_never_counts_its_own_mirror_twice(
        tmp_path, monkeypatch, root_log, caplog):
    """The console mirrors the backend's live unpaid window, and the
    mirror is replayed once the counted record of the window's ID has aged
    out of the backend's retention while the window itself remains held:
    the fold refuses, because the ID belongs to the window the backend is
    still holding, so a live window can never count its own lines twice.
    The normal notice-route ordering holds as well: the mirror a report
    hands back to the backend that still holds its window, with the
    counted record young, folds nowhere, and the one summary the window
    owes counts the two lines it held back and not the four its own
    mirror re-delivered beside it would have counted."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        delivered.append(form)
        _body, _status, headers = deliver(form)
        if _status != 204:
            return web.Response(status=503)
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    answering_backend = TestClient(TestServer(backend))
    await answering_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        async def wait_for_delivery(host):
            for _ in range(2000):
                if any(form.get("host") == host for form in delivered):
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("the report never landed")

        async def settle():
            for _ in range(6000):
                if not reporter.pending_reports():
                    return
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            raise AssertionError("the reports never drained")

        # The backend cannot be told, and the console refuses one hostname
        # three times: it spends one line of its own and holds the other
        # two back into a window of its own.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        for _ in range(3):
            response = await client.get("/", headers={"Host": "down.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == 1

        # The backend answers again: the first acknowledged report carries
        # the console's window, the backend folds it in under its ID, and
        # the answer carries the window back, which the console mirrors.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        await wait_for_delivery("down.example.com")
        await settle()
        held = recorder.hour_suppressed_window()
        assert held is not None
        assert held[0] == 2
        backend_window = held[2]
        mirrored = reporter.mirrored_summary()
        assert mirrored is not None
        assert mirrored[0] == 2
        assert mirrored[2] == backend_window

        # A report lands while the window's hour is still running, and its
        # answer carries the window back again: the normal notice-route
        # ordering, in which the mirror handed back to the backend that
        # still holds its window folds nowhere, and the window still
        # counts the two lines it held back.
        clock[0] = 3599
        delivered.clear()
        response = await client.get("/", headers={"Host": "upward.example.com"})
        assert response.status == 421
        await wait_for_delivery("upward.example.com")
        await settle()
        refreshed = [form for form in delivered
                     if form.get("host") == "upward.example.com"]
        assert int(refreshed[0]["mirrored"]) == 2
        assert refreshed[0]["mirrored_id"] == backend_window
        held = recorder.hour_suppressed_window()
        assert held is not None
        assert held[0] == 2
        mirrored = reporter.mirrored_summary()
        assert mirrored is not None
        assert mirrored[0] == 2
        assert mirrored[2] == backend_window

        # The backend cannot be told again, and the console spends a fresh
        # line of its own in a new hour: the next report carries that
        # line's time, and the route merges it before it folds the
        # mirrored window in, so the merge is the roll that prunes the
        # window's counted record out, expired by then, while the window
        # itself remains held.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        clock[0] = 7100
        response = await client.get("/", headers={"Host": "replay.example.com"})
        assert response.status == 421
        assert reporter.pending_reports() == ["replay.example.com"]

        # The recovery lands past the whole retention span the backend
        # kept the counted record for: the report carries the mirror, and
        # the fold of a window the backend still holds refuses, so the
        # one summary the window owes counts the two lines it held back
        # and not the four its own mirror would have counted beside them.
        clock[0] = 7201
        delivered.clear()
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        await wait_for_delivery("replay.example.com")
        await settle()
        replayed = [form for form in delivered
                    if form.get("host") == "replay.example.com"]
        assert int(replayed[0]["mirrored"]) == 2
        assert replayed[0]["mirrored_id"] == backend_window
        assert recorder.hour_suppressed_window() is None
        assert reporter.mirrored_summary() is None

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 2 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        await answering_backend.close()


@pytest.mark.asyncio
async def test_a_backend_first_outage_keeps_the_pair_within_the_one_line_budget(
        tmp_path, monkeypatch, real_factory, root_log, caplog):
    """The backend can be the layer that spends the hour: it answers the
    reports and writes the refusal lines itself, and then becomes
    unreachable. Every acknowledgement carries its spent hour back to the
    console, which folds the line times into its recorder as its own, so
    the console that keeps refusing names no further line and counts every
    one it holds back into the one window that comes due here: the pair
    stays on the one budget whichever layer spent it."""
    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    console_lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", console_lines.append)
    caplog.set_level(logging.INFO)
    from waitress.server import create_server

    from app import host_allowlist
    from app.app import waitress_server_options

    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    clock = [0]
    monkeypatch.setattr(host_allowlist, "_refusals",
                        host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0]))

    # The backend answers the reports over a real socket, the way the
    # supervisor sends them, with the real notice route.
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    server = create_server(real_factory, host="127.0.0.1", port=port,
                           threads=4, **waitress_server_options())

    def serve():
        try:
            server.run()
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        async def settle():
            for _ in range(6000):
                if not reporter.pending_reports():
                    return
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            raise AssertionError("the reports never drained")

        # The backend owns the lines while it answers, and spends the
        # hour's whole allowance on one per host. Each report is let to
        # land before the next refusal, with the clock moving a step for
        # it, so every line the backend spends has a time of its own the
        # way a real hour's do.
        for index in range(supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR):
            response = await client.get("/", headers={"Host": f"spent{index}.example.com"})
            assert response.status == 421
            for _ in range(2000):
                if not reporter.pending_reports():
                    break
                clock[0] += 1
                await asyncio.sleep(0.005)
        await settle()
        backend_lines = [message for message in root_log.messages
                         if "refused a request" in message]
        assert len(backend_lines) == supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR
        assert [line for line in console_lines if line.startswith("Refused")] == []

        # The backend becomes unreachable, and the console keeps refusing
        # hosts: the budget the acknowledgements folded in is spent, so it
        # holds every further line back and opens its window here.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        for index in range(5):
            clock[0] = 3000 + index
            response = await client.get("/", headers={"Host": f"late{index}.example.com"})
            assert response.status == 421
        named = [line for line in console_lines if line.startswith("Refused")]
        assert named == []

        # The window's hour runs: the console, the only layer that can be
        # reached, reports the lines the pair held back, once.
        clock[0] = 6700
        response = await client.get("/", headers={"Host": "after.example.com"})
        assert response.status == 421
        console_summaries = [line for line in console_lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert len(backend_lines) + len(named) == (
            supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR)
        assert console_summaries
        assert "Suppressed 5 host-refusal lines" in console_summaries[0]
        assert len(console_summaries) + len(backend_summaries) == 1
    finally:
        await client.close()
        server.close()
        thread.join(timeout=10)


def test_first_failed_notices_of_distinct_hosts_count_once_past_the_ceiling(monkeypatch):
    """A report that fails delivery leaves its hostname named nowhere else:
    the first failed notice of each distinct host the hour's cap holds back
    counts toward the summary window, the way the host's own held-back line
    would, and its later delivery retries do not count it again."""
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])

    # An outage spends the hour's cap on per-host lines.
    for index in range(supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR):
        assert reporter.note_line(f"early{index}.example.com") is True

    # The backend answers, and a longer roll of distinct hosts is refused
    # while it does: every one of their first notices fails its delivery.
    late = [f"late{index}.example.com" for index in range(25)]
    for host in late:
        assert reporter.report_due(host) is True
        reporter.report_failed(host)
    # A retry of an earlier host's notice fails too, and counts nothing.
    reporter.report_failed(late[0])

    assert reporter.take_summary() is None
    clock[0] += int(supervisor.host_policy.HOUR_SECONDS) + 1
    summary = reporter.take_summary()
    assert summary is not None
    assert summary[0] == len(late)
    assert lines == []


def test_a_pending_notices_first_failure_lives_as_long_as_the_notice(monkeypatch):
    """Both hours' line budgets are spent and one unchanged pending notice
    is retried in each of them: the first failure counts one suppressed
    line in the hour it failed and none in the next, because its
    accounting lives exactly as long as the notice and not as long as an
    hour. The accounting resolves with the notice, at the acknowledgement
    that lands and at the pending queue's eviction alike, so a fresh
    notice of the same host in a later hour counts its own first failure
    again: the window holds one line per notice, however many hours the
    notice waits and however many times it is retried."""
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])

    def spend_the_hours_cap(prefix):
        for index in range(supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR):
            assert reporter.note_line(f"{prefix}{index}.example.com") is True

    def the_window_pays(count):
        assert reporter.take_summary() is None
        clock[0] += int(supervisor.host_policy.HOUR_SECONDS) + 1
        summary = reporter.take_summary()
        assert summary is not None
        assert summary[0] == count

    # The first hour's budget is spent, and one notice fails its first
    # delivery with the cap holding its line back: the first failure
    # counts one suppressed line.
    spend_the_hours_cap("early")
    assert reporter.report_due("down.example.com") is True
    reporter.report_failed("down.example.com")

    # The hour rolls, the second hour's budget is spent the same way, and
    # the same unchanged notice is retried: no new refusal has arrived, so
    # its first failure counts nothing in the new hour, and the window it
    # opened pays the one line it counts.
    clock[0] = int(supervisor.host_policy.HOUR_SECONDS) + 1
    spend_the_hours_cap("late")
    reporter.report_failed("down.example.com")
    the_window_pays(1)
    assert lines == []

    # The backend acknowledges the notice: the accounting resolves with it,
    # and a fresh notice of the same host in a later hour counts its own
    # first failure again, its own retries counting nothing.
    clock[0] += 1
    reporter.report_sent("down.example.com")
    clock[0] = 3 * int(supervisor.host_policy.HOUR_SECONDS) + 3
    spend_the_hours_cap("third")
    assert reporter.report_due("down.example.com") is True
    reporter.report_failed("down.example.com")
    reporter.report_failed("down.example.com")
    the_window_pays(1)

    # The pending queue's eviction resolves the notice it drops the same
    # way: a roll of newer names pushes the waiting notice out, and a
    # fresh notice of the same host in a later hour counts its own first
    # failure again.
    for index in range(supervisor.host_policy.REFUSED_HOSTS_LIMIT):
        assert reporter.report_due(f"newer{index}.example.com") is True
    assert "down.example.com" not in reporter.pending_reports()
    clock[0] = 4 * int(supervisor.host_policy.HOUR_SECONDS) + 4
    spend_the_hours_cap("fourth")
    assert reporter.report_due("down.example.com") is True
    reporter.report_failed("down.example.com")
    the_window_pays(1)
    assert lines == []


class _CountingWake:
    """The reporter's wake event, counting the delivery worker's turns: the
    worker clears the event once per turn, so its clear count is the turn
    count."""

    def __init__(self, inner):
        self._inner = inner
        self.turns = 0

    def clear(self):
        self.turns += 1
        self._inner.clear()

    def set(self):
        self._inner.set()

    async def wait(self):
        await self._inner.wait()


def _count_worker_turns(reporter):
    """Swap the reporter's wake event for one that counts the worker's turns."""
    counting = _CountingWake(reporter._wake)
    reporter._wake = counting
    return counting


async def _run_the_worker_for_a_second(reporter):
    """Run the delivery worker for a second and return how many turns it
    took, counted on its wake event."""
    counting = _count_worker_turns(reporter)
    task = asyncio.create_task(supervisor._drain_refusal_notices(reporter))
    try:
        await asyncio.sleep(1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return counting.turns


@pytest.mark.asyncio
async def test_the_delivery_worker_does_not_spin_while_the_backend_is_down(monkeypatch):
    """One way the worker's wait computed to zero: the backend went down
    after a failed delivery, and the pending report is past its retry pace,
    so the paces alone read as no wait at all. While the port is unset the
    worker waits on its wake event, which readiness sets, so it takes a
    handful of turns over a second and not a spin at a full core for as long
    as the backend stays down."""
    monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", None)
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])
    assert reporter.report_due("down.example.com") is True
    assert reporter.take_due_report() == "down.example.com"
    reporter.report_failed("down.example.com")
    clock[0] = 120  # past the retry pace, with the port unset

    turns = await _run_the_worker_for_a_second(reporter)
    assert 1 <= turns <= 5


@pytest.mark.asyncio
async def test_the_delivery_worker_does_not_spin_while_the_hours_bound_is_spent(monkeypatch):
    """The other way: the hour's reporting bound is spent and a pending
    report is past its retry pace, so its overdue pace shortened the wait to
    nothing while the bound held every delivery back for up to the rest of
    the hour. The worker waits for the oldest acknowledged report to roll
    out of the hour instead, whatever the paces say."""
    # A port nothing answers, and nothing is ever sent to: the spent bound
    # holds every delivery back.
    monkeypatch.setattr(supervisor, "BACKEND_PORT", 1)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    clock = [0]
    reporter = supervisor._RefusalReporter("/_supervisor/host_refusal", now=lambda: clock[0])
    # The bound is spent by acknowledged deliveries, one report each.
    for index in range(supervisor.REPORTS_PER_HOUR):
        assert reporter.report_due(f"h{index}.example.com") is True
        assert reporter.take_due_report() == f"h{index}.example.com"
        reporter.report_sent(f"h{index}.example.com")
    # One more hostname was attempted and not acknowledged, and its retry
    # pace has since run out.
    assert reporter.report_due("late.example.com") is True
    reporter.report_failed("late.example.com")
    clock[0] = 120

    turns = await _run_the_worker_for_a_second(reporter)
    assert 1 <= turns <= 5


def test_a_notice_delivered_late_displaces_no_genuinely_newer_refusal(real_factory,
                                                                      monkeypatch):
    """A refusal whose report arrives late carries the refusal's own time,
    in the line the console spent for it and in the set of the newest
    refused names: the record must hold that time and not the delivery's,
    and its bound must drop the oldest refusal by its own stamp. A notice
    however late is therefore the oldest news its report carries: it
    displaces no genuinely newer refusal, and the record keeps reflecting
    the newest refusals whichever layer saw them and however late their
    reports landed."""
    from app import host_allowlist

    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "supervisor-token")
    clock = [200]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    client = real_factory.test_client()
    headers = {"Host": "127.0.0.1:6767", "X-Bazarr-Supervisor-Token": "supervisor-token"}

    limit = supervisor.host_policy.REFUSED_HOSTS_LIMIT
    newer = [f"h{index}.example.com" for index in range(limit)]
    stamps = [str(100.0 + index) for index in range(limit)]

    # The report of one of the fifty genuinely newer refusals, refused at
    # 100 through 149 and named in the set the report carries the way every
    # report of the burst carries the newest names.
    report = client.post("/_supervisor/host_refusal", headers=headers,
                         data={"host": newer[0], "recent": ",".join(newer),
                               "recent_at": ",".join(stamps)})
    assert report.status_code == 204
    assert sorted(recorder.recent(supervisor.host_policy.DAY_SECONDS)) == sorted(newer)

    # The delayed notice: a refusal at 50 whose report lands at 200, within
    # the hour of the line the console spent for it, so it carries the
    # refusal's own time and the newest names, which the fifty newer
    # refusals have evicted it from.
    report = client.post("/_supervisor/host_refusal", headers=headers,
                         data={"host": "late.example.com", "logged": "50",
                               "recent": ",".join(newer), "recent_at": ",".join(stamps)})
    assert report.status_code == 204

    # The record keeps the fifty genuinely newer refusals and ranks them by
    # their own stamps, the newest first: the delayed notice is the oldest
    # news its report carried, so the bound drops it and not one of the
    # names a genuinely newer refusal named.
    recorded = recorder.recent(supervisor.host_policy.DAY_SECONDS)
    assert "late.example.com" not in recorded
    assert recorded == list(reversed(newer))


@pytest.mark.asyncio
async def test_a_restarted_backend_is_told_the_unpaid_aggregate_after_its_hour(
        tmp_path, monkeypatch, root_log, caplog):
    """The backend holds an aggregate window the answer carried back, which
    the console mirrors, and the aggregate's hour runs while the backend
    cannot be told: the mirror is the only record the six held-back lines
    have left, so it is kept until an answer reconciles their payment and
    not dropped by the hour it was waiting on, nor by the whole retention
    span since the answer that carried it back. A backend that restarts
    before the summary is paid is told again however late the next report
    arrives, past that whole span: the replay folds the aggregate in, the
    restarted backend pays the one summary the window owes, and the
    acknowledgement completes the window's ID, so the pair counts the six
    lines exactly once and the console drops the mirror on the answer."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        delivered.append(form)
        _body, _status, headers = deliver(form)
        if _status != 204:
            return web.Response(status=503)
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    answering_backend = TestClient(TestServer(backend))
    await answering_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        async def settle():
            for _ in range(6000):
                if not reporter.pending_reports():
                    return
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            raise AssertionError("the reports never drained")

        # The backend answers while the console refuses: the reported
        # hostnames spend the backend's hour's cap of lines, one line each,
        # and the console writes none of its own.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        for index in range(supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"up{index}.example.com"})
            assert response.status == 421
        await settle()

        # One more refusal is held back into the backend's own window, and
        # the acknowledgement of its report carries that window back: the
        # window's hour has not begun to run yet.
        clock[0] = 3000
        response = await client.get("/", headers={"Host": "upward.example.com"})
        assert response.status == 421
        await settle()
        held = recorder.hour_suppressed_window()
        assert held is not None
        assert held[0] == 1
        backend_window = held[2]

        # The backend becomes unreachable, and this console spends one line
        # of its own and holds the refusals that follow it back into a
        # window of its own.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        for index in range(6):
            clock[0] = 3700 + index
            response = await client.get("/", headers={"Host": "down.example.com"})
            assert response.status == 421
        named = [line for line in lines if line.startswith("Refused")]
        assert len(named) == 1

        # The recovery lands inside the aggregate's hour: the first
        # acknowledged report carries the console's window, the backend
        # folds it into its own earlier one, and the answer carries the
        # aggregate back, which the console mirrors.
        clock[0] = 5000
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        await settle()
        aggregated = recorder.hour_suppressed_window()
        assert aggregated is not None
        assert aggregated[0] == 6
        assert aggregated[2] == backend_window
        mirrored = reporter.mirrored_summary()
        assert mirrored is not None
        assert mirrored[0] == 6
        assert mirrored[2] == backend_window

        # The backend restarts, its memory of the window IDs gone, and the
        # aggregate's hour has already run: the replay's report carries the
        # mirror past the hour it had left and past the whole retention span
        # since the answer that carried the aggregate back, because the
        # mirror is the only record the six held-back lines have left and
        # only an answer that reconciles their payment may drop it.
        restarted = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
        monkeypatch.setattr(host_allowlist, "_refusals", restarted)
        delivered.clear()
        clock[0] = 12300
        response = await client.get("/", headers={"Host": "replay.example.com"})
        assert response.status == 421
        for _ in range(2000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert delivered
        assert "mirrored" in delivered[0]
        assert int(delivered[0]["mirrored"]) == 6
        assert delivered[0]["mirrored_id"] == backend_window
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        # The restarted backend folded the aggregate in and its hour has
        # run, so the same report pays the one summary the window owes and
        # the acknowledgement completes the window's ID: the console drops
        # the mirror on the answer, and the aggregate is counted once.
        assert restarted.hour_suppressed_window() is None
        assert reporter.mirrored_summary() is None

        # A later refusal reports again, and its report carries no copy of
        # the paid window: no second summary of the six lines is logged
        # anywhere.
        delivered.clear()
        clock[0] = 12350
        response = await client.get("/", headers={"Host": "witness.example.com"})
        assert response.status == 421
        for _ in range(2000):
            if delivered:
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert delivered
        assert "mirrored" not in delivered[0]
        for _ in range(6000):
            if not reporter.pending_reports():
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert reporter.pending_reports() == []

        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        backend_summaries = [message for message in root_log.messages
                             if "host-refusal log lines" in message]
        assert console_summaries == []
        assert len(backend_summaries) == 1
        assert "suppressed 6 host-refusal log lines" in backend_summaries[0]
    finally:
        await client.close()
        await answering_backend.close()


@pytest.mark.asyncio
async def test_a_pending_notice_keeps_its_refusal_time_when_the_newest_names_and_the_line_expire(
        tmp_path, monkeypatch):
    """A report no backend acknowledged waits with the time of the refusal
    it reports on, beside its retry bookkeeping and independently of every
    other record of the refusal: the newest-refused set is bounded, and a
    roll of fifty newer names rolls a waiting hostname out of it, while the
    line the console spent for that hostname is kept for its hour alone.
    The report that finally lands carries that retained time, so the
    backend's record holds when the refusal happened and not when its
    notice was delivered: the delayed notice is the oldest news its report
    carries, displaces no genuinely newer refusal, and the names the newer
    refusals named stay in the record."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    delivered = []
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        delivered.append((clock[0], form))
        _body, _status, headers = deliver(form)
        if _status != 204:
            return web.Response(status=503)
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    answering_backend = TestClient(TestServer(backend))
    await answering_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        async def settle():
            for _ in range(6000):
                if not reporter.pending_reports():
                    return
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            raise AssertionError("the reports never drained")

        # Fifty hostnames are refused while the backend answers, so each is
        # reported and acknowledged: they spend the hour's reporting bound
        # and hold their places in the newest-refused set.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")
        for index in range(supervisor.host_policy.REFUSED_HOSTS_LIMIT):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"seen{index}.example.com"})
            assert response.status == 421
        await settle()

        # The backend cannot be reached, and one hostname is refused while
        # it cannot: its notice waits, and the console spends a line for it
        # that is kept for its hour alone.
        monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
        clock[0] = 10
        response = await client.get("/", headers={"Host": "late.example.com"})
        assert response.status == 421
        assert reporter.pending_reports() == ["late.example.com"]

        # The fifty acknowledged names are refused again while the backend
        # stays unreachable: their reports are skipped, because they were
        # acknowledged within the hour, but every refusal refreshes the
        # newest-refused set, and the fiftieth rolls the waiting hostname
        # out of it.
        for index in range(supervisor.host_policy.REFUSED_HOSTS_LIMIT):
            clock[0] = 11 + index
            response = await client.get("/", headers={"Host": f"seen{index}.example.com"})
            assert response.status == 421
        assert reporter.pending_reports() == ["late.example.com"]
        assert [name for name, _stamp in reporter.recent_refusals()
                if name == "late.example.com"] == []

        # The backend answers again past every hour the refusal knew: a new
        # refusal wakes the worker, the waiting notice is delivered first,
        # and its report is past the recent set that rolled it out, past
        # the line the console spent for it and past the reporting bound
        # the fifty acknowledged names spent.
        clock[0] = 3700
        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        response = await client.get("/", headers={"Host": "witness.example.com"})
        assert response.status == 421
        await settle()

        notices = [form for _at, form in delivered
                   if form.get("host") == "late.example.com"]
        assert notices
        # The notice carried the refusal's own time, independently of the
        # recent names and the line history.
        assert float(notices[0]["refused_at"]) == 10
        assert "logged" not in notices[0]

        # The record holds that time and not the delivery's: the delayed
        # notice is the oldest news its report carried, so the bound drops
        # it and keeps every genuinely newer name.
        recorded = recorder.recent(supervisor.host_policy.DAY_SECONDS)
        assert "late.example.com" not in recorded
        assert "witness.example.com" in recorded
        assert len([name for name in recorded if name.startswith("seen")]) == (
            supervisor.host_policy.REFUSED_HOSTS_LIMIT - 1)
    finally:
        await client.close()
        await answering_backend.close()


@pytest.mark.asyncio
async def test_a_mirror_whose_completion_acknowledgement_was_lost_produces_no_second_summary(
        tmp_path, monkeypatch, root_log, caplog):
    """The console keeps the window an acknowledgement carried back until
    an answer reconciles its payment, past the whole retention span of
    silence since the answer that carried it, and the backend keeps the
    completion record of a paid window, renewing its lease on every replay
    that reaches it: a copy the console can still carry meets a backend
    that still remembers paying it. A payment whose acknowledgement is
    lost is therefore refused by the completion record however long the
    silence runs, and the answer that finally lands carries no window
    back and names the window completed, so the console drops the mirror
    on it: the six refusals the window counts are summarized once, by the
    one line that paid them."""
    from app import host_allowlist

    monkeypatch.setattr(supervisor, "RETRY_PACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "MIN_RETRY_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("BAZARR_SUPERVISOR_TOKEN", "the-supervisor-token")
    lines = []
    monkeypatch.setattr(supervisor, "_print_refusal_line", lines.append)
    caplog.set_level(logging.INFO)

    clock = [0]
    recorder = host_allowlist.BoundedRefusalRecorder(now=lambda: clock[0])
    monkeypatch.setattr(host_allowlist, "_refusals", recorder)
    config = _config()
    delivered = []
    acknowledged = [True]
    backend_app = Flask(__name__)

    def deliver(form):
        """The backend's own notice route, over the form the report carried."""
        with backend_app.test_request_context(
                "/_supervisor/host_refusal", method="POST",
                headers={supervisor.TOKEN_HEADER: "the-supervisor-token"}, data=form):
            return host_allowlist.front_refusal_notice(config=config)

    async def notice(request):
        form = dict(await request.post())
        delivered.append((clock[0], form))
        _body, _status, headers = deliver(form)
        if _status != 204 or not acknowledged[0]:
            return web.Response(status=503)
        return web.Response(status=204, headers={supervisor.TOKEN_HEADER:
                                                 "the-supervisor-token", **headers})

    backend = web.Application()
    backend.router.add_route("POST", "/_supervisor/host_refusal", notice)
    answering_backend = TestClient(TestServer(backend))
    await answering_backend.start_server()
    client = await _supervisor_client(tmp_path, monkeypatch)
    try:
        reporter = supervisor._active_reporter
        monkeypatch.setattr(reporter, "_now", lambda: clock[0])
        monkeypatch.setattr(reporter._lines, "_now", lambda: clock[0])

        async def settle():
            for _ in range(6000):
                if not reporter.pending_reports():
                    return
                clock[0] += 0.06
                await asyncio.sleep(0.01)
            raise AssertionError("the reports never drained")

        monkeypatch.setattr(supervisor, "BACKEND_PORT", answering_backend.server.port)
        monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "the-supervisor-token")

        # The backend spends its hour's cap of lines while it answers.
        for index in range(supervisor.host_policy.REFUSAL_LOG_LINES_PER_HOUR):
            clock[0] = index
            response = await client.get("/", headers={"Host": f"up{index}.example.com"})
            assert response.status == 421
        await settle()

        # Six more refusals are held back into the backend's own window, and
        # every acknowledgement of their reports carries that window back:
        # the console holds its mirror of it.
        for index in range(6):
            clock[0] = 20 + index
            response = await client.get("/", headers={"Host": f"held{index}.example.com"})
            assert response.status == 421
        await settle()
        held = recorder.hour_suppressed_window()
        assert held is not None
        assert held[0] == 6
        backend_window = held[2]
        mirrored = reporter.mirrored_summary()
        assert mirrored is not None
        assert mirrored[0] == 6
        assert mirrored[2] == backend_window

        # The window's hour runs, and the next report that lands pays the
        # one summary it owes: the answer that would have told the console
        # the payment is lost, and the report that paid stays pending.
        clock[0] = 3621
        acknowledged[0] = False
        response = await client.get("/", headers={"Host": "payer.example.com"})
        assert response.status == 421
        for _ in range(2000):
            if any(form.get("host") == "payer.example.com" for _at, form in delivered):
                break
            await asyncio.sleep(0.01)
        assert any(form.get("host") == "payer.example.com" for _at, form in delivered)
        summaries = [message for message in root_log.messages
                     if "host-refusal log lines" in message]
        assert len(summaries) == 1
        assert "suppressed 6 host-refusal log lines" in summaries[0]

        # A replay lands past the span the backend remembers counting the
        # window in, while the console still holds its mirror: the
        # window's ID is still on the backend's lists, so the fold refuses,
        # the pair still owes the window nothing, and the lost answer keeps
        # the mirror held for however long the silence runs.
        clock[0] = 7222
        for _ in range(2000):
            if any(at >= 7222 for at, form in delivered
                   if form.get("host") == "payer.example.com"):
                break
            clock[0] += 0.06
            await asyncio.sleep(0.01)
        assert any(at >= 7222 for at, form in delivered
                   if form.get("host") == "payer.example.com")
        summaries = [message for message in root_log.messages
                     if "host-refusal log lines" in message]
        assert len(summaries) == 1

        # The last replay lands past the whole retention span both sides
        # share, measured from the answer that carried the window back,
        # while the console still holds its mirror: the copy is kept until
        # an answer reconciles its payment, and the completion record the
        # payment left, reached once the counted record's own lease ran
        # out, refuses the fold and renews its lease on the arrival. The
        # acknowledged answer carries no window back and names the window
        # completed, so the console drops the mirror on it, and the six
        # refusals are still summarized once.
        clock[0] = 9000
        acknowledged[0] = True
        delivered.clear()
        await settle()
        replayed = [form for _at, form in delivered
                    if form.get("host") == "payer.example.com"]
        assert replayed
        assert int(replayed[0]["mirrored"]) == 6
        assert replayed[0]["mirrored_id"] == backend_window
        assert reporter.mirrored_summary() is None
        summaries = [message for message in root_log.messages
                     if "host-refusal log lines" in message]
        assert len(summaries) == 1
        console_summaries = [line for line in lines if line.startswith("Suppressed")]
        assert console_summaries == []
    finally:
        await client.close()
        await answering_backend.close()
