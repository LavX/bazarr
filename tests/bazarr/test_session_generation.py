# coding=utf-8
"""
Tests for session revocation by generation.

A Flask session cookie is signed, not stored, so deleting it in the browser on
logout revoked nothing: a copy stayed valid for the whole lifetime window, and
a read that was already in flight when the cookie was deleted came back
carrying a freshly signed copy of the session it had just ended. The server now
owns a generation number that every signed-in cookie carries; logout, a
password change and an auth-type change advance it, and a cookie from an
earlier generation is refused.

These run against the application as the server builds it (create_app plus the
api and ui blueprints), so the cookie in each test is a real signed one.
"""
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import request, session

API_KEY = "generationtestapikey000000000001"
USERNAME = "owner"
PASSWORD = "correct horse battery"
ACCOUNT_URL = "/api/system/account"
STATUS_URL = "/api/system/status"


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def shell_template():
    """A build output shaped like the real frontend/index.html (see test_host_allowlist)."""
    from app import ui

    template = Path(ui.frontend_build_path) / "index.html"
    existed = template.exists()
    if not existed:
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(
            '<!DOCTYPE html>\n<html lang="en">\n  <head>\n    <title>Bazarr+</title>\n  </head>\n'
            '  <body>\n    <div id="root"></div>\n    <script>\n      try {\n'
            '        window.Bazarr = JSON.parse(`{{BAZARR_SERVER_INJECT | tojson | safe}}`);\n'
            '      } catch (error) {}\n    </script>\n  </body>\n</html>\n', encoding="utf-8")
    yield template
    if not existed:
        template.unlink()


_AUTH_FIELDS = ("type", "username", "password", "apikey", "session_generation")


def _live_auth():
    """The auth section as it is now. A settings save replaces it, so never hold on to one."""
    from app.config import settings

    return settings.auth


def _set_auth(**values):
    auth = _live_auth()
    for name, value in values.items():
        auth[name] = value


@pytest.fixture
def login_settings(monkeypatch):
    """Form login for one known account, generation 0, with every change undone afterwards.

    Written by hand rather than with monkeypatch: a settings save swaps the whole
    auth section for a new object, and monkeypatch would put the old values back
    on the one it patched, leaving the live section changed for the next test.
    """
    from app import config
    from utilities import helper

    monkeypatch.setattr(helper, "PBKDF2_ITERATIONS", 1000)
    saved = {name: _live_auth().get(name) for name in _AUTH_FIELDS}
    _set_auth(type="form", username=USERNAME, password=helper.hash_password(PASSWORD),
              apikey=API_KEY, session_generation=0)
    yield config.settings
    _set_auth(**saved)


@pytest.fixture
def writes(monkeypatch):
    """Stand-in for the disk write, counting how often settings were persisted."""
    from app import config

    calls = []

    def fake_write_config(*_args, **_kwargs):
        calls.append(config.settings.auth.session_generation)
        return True

    monkeypatch.setattr(config, "write_config", fake_write_config)
    return calls


@pytest.fixture
def server(monkeypatch, shell_template, login_settings, writes):
    """The real application with the api and ui blueprints, and a probe route."""
    from api import api_bp
    from app import app as app_module
    from app import config as config_module
    from app import host_allowlist
    from app import ui as ui_module
    from app.database import database as shared_database

    socketio = app_module.socketio
    for name in ("server", "sockio_mw", "async_mode"):
        monkeypatch.setattr(socketio, name, getattr(socketio, name, None), raising=False)
    monkeypatch.setattr(socketio, "server_options", dict(socketio.server_options))
    monkeypatch.setattr(host_allowlist, "_refusals", host_allowlist.BoundedRefusalRecorder())
    monkeypatch.setattr(host_allowlist, "_approval_code", host_allowlist._ApprovalCode())

    flask_app = app_module.create_app()
    flask_app.register_blueprint(api_bp, url_prefix=config_module.base_url.rstrip("/") + "/api")
    flask_app.register_blueprint(ui_module.ui_bp, url_prefix=config_module.base_url.rstrip("/"))

    started = threading.Event()
    proceed = threading.Event()

    @flask_app.route("/_slow-read")
    def slow_read():
        # A request that has read its session and is still being answered.
        signed_in = session.get("logged_in") is True
        started.set()
        proceed.wait(10)
        return "late" if signed_in else "anonymous"

    @flask_app.route("/_save-settings", methods=["POST"])
    def save_settings_probe():
        config_module.save_settings([(key, [value]) for key, value in request.form.items()])
        return "saved"

    @flask_app.route("/_revoked-then-save", methods=["POST"])
    def revoked_then_save():
        # A browser whose request is still running when another one logs out:
        # the revocation lands mid-request, then this request saves a new password.
        from app.auth.generation import revoke_all_sessions

        revoke_all_sessions()
        config_module.save_settings([(key, [value]) for key, value in request.form.items()])
        return "saved"

    @flask_app.route("/_gate")
    def gate():
        from app.auth import is_session_authenticated

        return "open" if is_session_authenticated() else "closed"

    @flask_app.route("/_keys")
    def keys():
        return json.dumps(sorted(session.keys()))

    @flask_app.route("/_confirm")
    def confirm():
        session["password_confirmed_until"] = time.time() + 600
        return "ok"

    @flask_app.route("/_slow-confirm")
    def slow_confirm():
        # A password confirmation that is still being checked: the session it will
        # write into was valid when the request started.
        started.set()
        proceed.wait(10)
        session["password_confirmed_until"] = time.time() + 600
        return "ok"

    @flask_app.route("/_confirmed")
    def confirmed():
        return "yes" if session.get("password_confirmed_until") else "no"

    @flask_app.route("/_confirmation-gate")
    def confirmation_gate():
        from api.password_confirm import password_confirmation_live

        return ("open", 200) if password_confirmation_live() else ("closed", 403)

    flask_app.slow_started = started
    flask_app.slow_proceed = proceed

    shared_database.close()
    from api.system.account import _login_attempts

    _login_attempts.clear()
    yield flask_app
    proceed.set()
    _login_attempts.clear()
    shared_database.close()


@pytest.fixture
def saveable(monkeypatch):
    """save_settings with the validators' log check and the database stubbed out."""
    from app import config

    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: None, scalar=lambda *a, **k: "0",
                                     close=lambda: None),
            update=lambda _model: _FakeUpdate(),
            System=SimpleNamespace(updated="updated", configured="configured"),
        ),
    )
    return config


@pytest.fixture
def settled_disk(login_settings):
    """The config file holds exactly the login settings now in memory, and is put back afterwards.

    A refused save reloads the settings from the file, and what it does next
    depends on what the file holds, so a test of that has to say what is on
    disk. The file's own bytes are restored at the end.
    """
    from app import config

    path = Path(config.config_yaml_file)
    original = path.read_bytes() if path.exists() else None
    assert config._write_config() is True
    yield path
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(original)


def _login(client, password=PASSWORD):
    return client.post(ACCOUNT_URL, data={"action": "login", "username": USERNAME, "password": password})


def _logout(client):
    return client.post(ACCOUNT_URL, data={"action": "logout"})


def _cookie_value(client):
    cookie = client.get_cookie("session")
    return cookie.value if cookie else None


def _signed_in_cookie(app):
    """The session cookie of one real login, as a plain string a copy could carry."""
    client = app.test_client()
    assert _login(client).status_code == 204
    return _cookie_value(client)


def _bare_client(app):
    """A client with no jar of its own: every request carries only what it is handed."""
    return app.test_client(use_cookies=False)


def _with_cookie(value):
    return {"Cookie": f"session={value}"}


def _document_embeds_key(response):
    return API_KEY in response.get_data(as_text=True)


def _document_for(app, cookie):
    return _bare_client(app).get("/", headers=_with_cookie(cookie))


def _renewed_cookie(response):
    """The session cookie value a response sets, or None when it sets none or deletes it."""
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith("session="):
            value = header.split(";", 1)[0][len("session="):]
            return value or None
    return None


def _session_cookie_headers(response):
    """Every Set-Cookie a response sends for the session, a deletion included."""
    return [header for header in response.headers.getlist("Set-Cookie") if header.startswith("session=")]


def _generation():
    from app.config import settings

    return settings.auth.session_generation


# ---------------------------------------------------------------------------
# The defect: logout revoked nothing
# ---------------------------------------------------------------------------


def test_a_signed_in_cookie_opens_the_document_with_the_key(server):
    """Control for the tests below: this is what a live session gets."""
    cookie = _signed_in_cookie(server)

    assert _document_embeds_key(_document_for(server, cookie))


def test_the_old_cookie_is_refused_after_logout(server):
    """A copy of the cookie, taken before logout, must be dead afterwards."""
    client = server.test_client()
    assert _login(client).status_code == 204
    copy = _cookie_value(client)

    assert _logout(client).status_code == 204

    assert not _document_embeds_key(_document_for(server, copy))


def test_the_old_cookie_fails_the_session_gate_after_logout(server):
    """The gate itself, not only the document: this is what every form-auth route asks."""
    client = server.test_client()
    _login(client)
    copy = _cookie_value(client)
    _logout(client)

    assert _bare_client(server).get("/_gate", headers=_with_cookie(copy)).get_data(as_text=True) == "closed"


def test_a_read_carrying_the_old_cookie_after_logout_does_not_reissue_a_session(server):
    """
    The reported defect. A read that left while the session was still signed in
    answers after the logout deleted the cookie. It must not hand the browser a
    signed-in session back: it may delete the cookie, never renew it.
    """
    client = server.test_client()
    _login(client)
    copy = _cookie_value(client)
    _logout(client)

    late_read = _bare_client(server).get(STATUS_URL, headers={**_with_cookie(copy), "X-API-KEY": API_KEY})
    assert late_read.status_code == 200

    reissued = _renewed_cookie(late_read)
    assert reissued is None


def test_a_request_in_flight_during_logout_does_not_renew_the_cookie(server):
    """The same defect with a request that is genuinely open while the logout runs."""
    client = server.test_client()
    _login(client)
    copy = _cookie_value(client)

    answered = {}

    def late_request():
        answered["response"] = _bare_client(server).get("/_slow-read", headers=_with_cookie(copy))

    worker = threading.Thread(target=late_request)
    worker.start()
    assert server.slow_started.wait(10)

    assert _logout(client).status_code == 204
    server.slow_proceed.set()
    worker.join(10)

    response = answered["response"]
    assert response.get_data(as_text=True) == "late"
    reissued = _renewed_cookie(response)
    assert reissued is None


def _slow_read_in_a_thread(app, cookie):
    answered = {}

    def late_request():
        answered["response"] = _bare_client(app).get("/_slow-read", headers=_with_cookie(cookie))

    worker = threading.Thread(target=late_request)
    worker.start()
    assert app.slow_started.wait(10)
    return worker, answered


def test_a_request_in_flight_during_a_password_change_does_not_delete_the_new_cookie(server, saveable):
    """The browser that changes its password gets a new cookie. The older request it still has
    in flight answers afterwards: a deletion from that answer would delete the new cookie too."""
    changer = server.test_client()
    _login(changer)
    old = _cookie_value(changer)
    worker, answered = _slow_read_in_a_thread(server, old)

    changed = changer.post("/_save-settings", data={"settings-auth-password": "a different password"})
    assert _renewed_cookie(changed) is not None
    server.slow_proceed.set()
    worker.join(10)

    assert answered["response"].get_data(as_text=True) == "late"
    assert _session_cookie_headers(answered["response"]) == []
    assert "Cookie" in answered["response"].headers.get("Vary", "")  # it read the session: caches still key on it


def test_a_request_in_flight_across_a_logout_and_a_new_sign_in_does_not_delete_the_new_cookie(server):
    client = server.test_client()
    _login(client)
    old = _cookie_value(client)
    worker, answered = _slow_read_in_a_thread(server, old)

    assert _logout(client).status_code == 204
    assert _login(client).status_code == 204
    server.slow_proceed.set()
    worker.join(10)

    assert _session_cookie_headers(answered["response"]) == []
    assert _document_embeds_key(client.get("/"))


def test_a_confirmation_that_finishes_after_the_session_went_stale_is_not_stored(server, saveable):
    """A request that writes into a session the generation has since moved past stores nothing."""
    changer = server.test_client()
    _login(changer)
    old = _cookie_value(changer)
    answered = {}

    def confirm():
        answered["response"] = _bare_client(server).get("/_slow-confirm", headers=_with_cookie(old))

    worker = threading.Thread(target=confirm)
    worker.start()
    assert server.slow_started.wait(10)

    changer.post("/_save-settings", data={"settings-auth-password": "a different password"})
    server.slow_proceed.set()
    worker.join(10)

    assert answered["response"].get_data(as_text=True) == "ok"
    assert _session_cookie_headers(answered["response"]) == []
    assert changer.get("/_confirmed").get_data(as_text=True) == "no"


def test_a_stale_cookie_is_neither_renewed_nor_deleted_by_a_read(server):
    """A read that opens an already revoked cookie must leave the browser's cookie alone: by the
    time its answer arrives the browser may hold a newer one, and a deletion cannot tell."""
    client = server.test_client()
    _login(client)
    copy = _cookie_value(client)
    _logout(client)

    late_read = _bare_client(server).get(STATUS_URL, headers={**_with_cookie(copy), "X-API-KEY": API_KEY})

    assert late_read.status_code == 200
    assert _session_cookie_headers(late_read) == []


def test_logout_revokes_without_a_cookie_deletion_that_could_erase_a_later_login(server):
    client = server.test_client()
    _login(client)

    response = _logout(client)

    assert _session_cookie_headers(response) == []
    assert not _document_embeds_key(client.get("/"))


def test_a_logout_response_arriving_after_a_new_login_leaves_its_cookie_alone(server, monkeypatch):
    from api.system import account

    old = _signed_in_cookie(server)
    revoked, release = threading.Event(), threading.Event()
    answered = {}

    def pause_response():
        revoked.set()
        assert release.wait(10)

    monkeypatch.setattr(account.gc, "collect", pause_response)

    def logout():
        answered["response"] = _bare_client(server).post(
            ACCOUNT_URL, data={"action": "logout"}, headers=_with_cookie(old))

    worker = threading.Thread(target=logout)
    worker.start()
    try:
        assert revoked.wait(10)
        fresh = server.test_client()
        assert _login(fresh).status_code == 204
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert _session_cookie_headers(answered["response"]) == []
    assert _document_embeds_key(fresh.get("/"))


def test_an_already_stale_logout_response_leaves_a_new_cookie_alone(server):
    old = _signed_in_cookie(server)
    fresh = server.test_client()
    _login(fresh)
    _logout(fresh)
    _login(fresh)

    response = _bare_client(server).post(ACCOUNT_URL, data={"action": "logout"}, headers=_with_cookie(old))

    assert response.status_code == 204
    assert _generation() == 1
    assert _session_cookie_headers(response) == []
    assert _document_embeds_key(fresh.get("/"))


def test_a_logout_waiting_for_a_password_save_cannot_revoke_its_replacement(server, saveable, monkeypatch):
    from app.auth import session as session_module
    from discover import metadata

    old = _signed_in_cookie(server)
    changer = server.test_client()
    _login(changer)
    lock = metadata.CONFIG_LOCK
    checkpoint = _observe_reader(monkeypatch, metadata, "CONFIG_LOCK", "is_session_authenticated",
                                 read_module=session_module)
    answered = {}

    def logout():
        answered["response"] = _bare_client(server).post(
            ACCOUNT_URL, data={"action": "logout"}, headers=_with_cookie(old))

    with lock:
        worker = threading.Thread(target=logout, name="generation-reader")
        worker.start()
        assert checkpoint.wait(10)
        changed = changer.post("/_save-settings", data={"settings-auth-password": "a different password"})
        assert changed.status_code == 200
    worker.join(10)
    assert not worker.is_alive()

    assert answered["response"].status_code == 204
    assert _generation() == 1
    assert _document_embeds_key(changer.get("/"))


def test_logout_advances_the_generation_and_saves_it(server, writes):
    client = server.test_client()
    _login(client)
    assert _generation() == 0

    _logout(client)

    assert _generation() == 1
    assert writes == [1]


def test_logging_in_again_after_logout_works(server):
    client = server.test_client()
    _login(client)
    _logout(client)

    assert _login(client).status_code == 204
    assert _document_embeds_key(_document_for(server, _cookie_value(client)))


def test_logout_by_a_browser_that_is_not_signed_in_revokes_nobody(server, writes):
    """An anonymous or already-revoked cookie posting logout must not sign everyone else out."""
    bystander = _signed_in_cookie(server)

    anonymous = server.test_client()
    assert _logout(anonymous).status_code == 204

    stale_client = server.test_client()
    _login(stale_client)
    stale = _cookie_value(stale_client)
    _logout(stale_client)
    generation_after_real_logout = _generation()
    writes.clear()
    _bare_client(server).post(ACCOUNT_URL, data={"action": "logout"}, headers=_with_cookie(stale))

    assert _generation() == generation_after_real_logout
    assert writes == []
    assert generation_after_real_logout == 1
    # The bystander was signed in before the one real logout and is out with it:
    # the point is that the two no-op logouts above added nothing on top.
    assert not _document_embeds_key(_document_for(server, bystander))


def test_a_failed_login_changes_nothing(server, writes):
    cookie = _signed_in_cookie(server)

    other = server.test_client()
    assert _login(other, password="wrong").status_code == 403

    assert _generation() == 0
    assert writes == []
    assert _document_embeds_key(_document_for(server, cookie))


# ---------------------------------------------------------------------------
# What stays valid
# ---------------------------------------------------------------------------


def test_the_same_generation_survives_ordinary_requests(server):
    client = server.test_client()
    _login(client)

    for _ in range(3):
        assert client.get(STATUS_URL, headers={"X-API-KEY": API_KEY}).status_code == 200
        assert _document_embeds_key(client.get("/"))
    with client.session_transaction() as stored:
        assert stored["logged_in"] is True
        assert stored["gen"] == 0
    assert _generation() == 0


def test_a_refreshed_cookie_keeps_its_generation(server):
    """The cookie is re-signed on each request; the number it carries must not drift."""
    client = server.test_client()
    _login(client)
    _logout(client)
    _login(client)
    assert _generation() == 1

    client.get("/")
    with client.session_transaction() as stored:
        assert stored["gen"] == 1
        assert stored["logged_in"] is True


def test_the_generation_read_does_not_touch_the_database(server, monkeypatch):
    """The gate runs on every request: it reads the in-memory setting, nothing else."""
    from app.auth import generation, is_session_authenticated
    from app.database import database

    cookie = _signed_in_cookie(server)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the session gate reached the database")

    for name in ("execute", "scalar", "select", "begin", "commit"):
        monkeypatch.setattr(database, name, forbidden, raising=False)

    with server.test_request_context("/", headers=_with_cookie(cookie)):
        assert is_session_authenticated() is True
        assert generation.current_generation() == 0


def test_a_concurrent_advance_gives_every_caller_its_own_number(login_settings, monkeypatch):
    from app.auth import generation

    numbers = []
    barrier = threading.Barrier(8)

    def advance():
        barrier.wait()
        numbers.append(generation.advance_session_generation())

    threads = [threading.Thread(target=advance) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    assert sorted(numbers) == list(range(1, 9))
    assert _generation() == 8


# ---------------------------------------------------------------------------
# Upgrade: cookies from before the generation existed
# ---------------------------------------------------------------------------


def _legacy_cookie(app):
    """A cookie signed the way releases before the generation signed it: no `gen`.

    Signed with Flask's own interface, not the application's, which would stamp it.
    """
    from flask.sessions import SecureCookieSessionInterface

    serializer = SecureCookieSessionInterface().get_signing_serializer(app)
    return serializer.dumps({"logged_in": True, "_permanent": True})


def _forged_cookie(app, **contents):
    """A cookie signed with the server's own key, carrying exactly these contents."""
    serializer = app.session_interface.get_signing_serializer(app)
    return serializer.dumps({"logged_in": True, **contents})


@pytest.mark.parametrize("forged", [True, 1.0, "1"])
def test_a_cookie_whose_generation_only_equals_the_current_number_is_refused(server, forged):
    """True == 1 and 1.0 == 1 in Python: only a real integer is a generation."""
    _set_auth(session_generation=1)
    cookie = _forged_cookie(server, gen=forged)

    assert not _document_embeds_key(_document_for(server, cookie))
    assert _bare_client(server).get("/_gate", headers=_with_cookie(cookie)).get_data(as_text=True) == "closed"


def test_a_cookie_carrying_the_real_integer_generation_is_honoured(server):
    """Control for the forged cases above: the same forgery with an int opens."""
    _set_auth(session_generation=1)
    cookie = _forged_cookie(server, gen=1)

    assert _document_embeds_key(_document_for(server, cookie))


def test_a_cookie_without_a_generation_is_honoured_while_the_generation_is_zero(server):
    """An upgrade must not sign anybody out."""
    assert _document_embeds_key(_document_for(server, _legacy_cookie(server)))


def test_a_cookie_without_a_generation_is_refused_once_the_generation_moved(server):
    legacy = _legacy_cookie(server)
    client = server.test_client()
    _login(client)
    _logout(client)
    assert _generation() == 1

    assert not _document_embeds_key(_document_for(server, legacy))


def test_a_legacy_cookie_is_stamped_and_keeps_working_until_the_next_revocation(server):
    legacy = _legacy_cookie(server)
    client = server.test_client()
    client.set_cookie("session", legacy)

    assert _document_embeds_key(client.get("/"))
    with client.session_transaction() as stored:
        assert stored["gen"] == 0


def test_a_new_login_after_the_upgrade_carries_the_generation(server):
    client = server.test_client()
    _login(client)

    with client.session_transaction() as stored:
        assert stored["gen"] == 0


def _plain_gate_app():
    """A bare Flask app with Flask's own session handling: only the gate itself stands between."""
    from flask import Flask

    from app.auth import is_session_authenticated

    plain = Flask(__name__)
    plain.config["SECRET_KEY"] = "test-secret-key"

    @plain.route("/gate")
    def gate():
        return "open" if is_session_authenticated() else "closed"

    return plain


def _gate_says(plain, **stored_values):
    client = plain.test_client()
    with client.session_transaction() as stored:
        stored.update(stored_values)
    return client.get("/gate").get_data(as_text=True)


def test_the_gate_itself_refuses_a_cookie_from_another_generation(login_settings):
    """The gate must not lean on the cookie interface: it is what every route asks."""
    login_settings.auth.session_generation = 3
    plain = _plain_gate_app()

    assert _gate_says(plain, logged_in=True, gen=3) == "open"
    assert _gate_says(plain, logged_in=True, gen=2) == "closed"
    assert _gate_says(plain, logged_in=True, gen=4) == "closed"
    assert _gate_says(plain, logged_in=True) == "closed"
    assert _gate_says(plain, logged_in=False, gen=3) == "closed"


def test_the_gate_itself_treats_a_missing_generation_as_zero(login_settings):
    plain = _plain_gate_app()

    assert _gate_says(plain, logged_in=True) == "open"
    assert _gate_says(plain, logged_in=True, gen=0) == "open"
    assert _gate_says(plain, logged_in=True, gen=1) == "closed"


def test_a_legacy_request_in_flight_during_a_revocation_is_not_renewed(server):
    """A pre-upgrade cookie has no number to go stale; the open step must give it one."""
    legacy = _legacy_cookie(server)
    other = server.test_client()
    _login(other)

    answered = {}

    def late_request():
        answered["response"] = _bare_client(server).get("/_slow-read", headers=_with_cookie(legacy))

    worker = threading.Thread(target=late_request)
    worker.start()
    assert server.slow_started.wait(10)
    _logout(other)
    server.slow_proceed.set()
    worker.join(10)

    assert answered["response"].get_data(as_text=True) == "late"
    assert _renewed_cookie(answered["response"]) is None


def _legacy_upgrade_cookie(app, **contents):
    """A cookie carrying the keys older releases stored, signed with the server's own key."""
    return app.session_interface.get_signing_serializer(app).dumps(contents)


def _aged_cookie(app, **contents):
    """A cookie signed with the server's key and stamped older than the session lifetime."""
    from unittest import mock

    from itsdangerous import TimestampSigner

    lifetime = app.permanent_session_lifetime.total_seconds()
    stamp = int(time.time() - lifetime - 86400)
    with mock.patch.object(TimestampSigner, "get_timestamp", return_value=stamp):
        return app.session_interface.get_signing_serializer(app).dumps(contents)


def _expired_session_cookie(response):
    """True when the response deletes the session cookie: an empty value that expired in 1970."""
    headers = _session_cookie_headers(response)
    return (len(headers) == 1 and headers[0].split(";", 1)[0] == "session="
            and "Max-Age=0" in headers[0] and "1970" in headers[0])


def test_a_legacy_upgrade_cookie_is_expired_not_reissued_by_a_normal_request(server):
    """Older releases kept the plaintext password under _pw_for_upgrade; no request may re-sign it."""
    cookie = _legacy_upgrade_cookie(server, logged_in=True, _pw_for_upgrade="hunter2", _upgrade_token="stale-token")

    response = _bare_client(server).get("/_keys", headers=_with_cookie(cookie))

    assert json.loads(response.get_data(as_text=True)) == []
    assert _renewed_cookie(response) is None
    assert _expired_session_cookie(response)


def test_an_aged_legacy_upgrade_cookie_is_expired_by_a_normal_request(server):
    """A legacy cookie older than the session lifetime must still be expired, not silently dropped."""
    cookie = _aged_cookie(server, logged_in=True, _pw_for_upgrade="hunter2", _upgrade_token="stale-token")

    response = _bare_client(server).get("/_keys", headers=_with_cookie(cookie))

    assert json.loads(response.get_data(as_text=True)) == []
    assert _renewed_cookie(response) is None
    assert _expired_session_cookie(response)


def test_an_aged_cookie_without_legacy_keys_gets_no_set_cookie(server):
    """Guards the lifetime path: an expired cookie with no legacy key is left alone."""
    cookie = _aged_cookie(server, logged_in=True, gen=0)

    response = _bare_client(server).get("/_keys", headers=_with_cookie(cookie))

    assert json.loads(response.get_data(as_text=True)) == []
    assert _session_cookie_headers(response) == []


def test_logout_with_a_legacy_upgrade_cookie_expires_it(server):
    """A browser that declined the upgrade still holds the old key; logout must delete the cookie."""
    cookie = _legacy_upgrade_cookie(server, logged_in=True, _pw_for_upgrade="hunter2", _upgrade_token="stale-token")

    response = _bare_client(server).post(ACCOUNT_URL, data={"action": "logout"}, headers=_with_cookie(cookie))

    assert response.status_code == 204
    assert _expired_session_cookie(response)


def test_a_refused_request_with_a_legacy_upgrade_cookie_leaks_neither_key_nor_password(server):
    cookie = _legacy_upgrade_cookie(server, _pw_for_upgrade="hunter2", _upgrade_token="stale-token")

    response = _bare_client(server).get("/_gate", headers=_with_cookie(cookie))

    assert response.get_data(as_text=True) == "closed"
    body_and_headers = response.get_data(as_text=True) + "".join(
        f"{name}: {value}" for name, value in response.headers.items())
    assert "hunter2" not in body_and_headers
    assert "stale-token" not in body_and_headers
    assert _renewed_cookie(response) is None


def test_a_cookie_without_the_legacy_keys_is_not_expired_by_logout(server):
    """Guards the deliberate choice: only a cookie that carries the old keys is ever expired."""
    client = server.test_client()
    assert _login(client).status_code == 204
    copy = _cookie_value(client)

    response = _bare_client(server).post(ACCOUNT_URL, data={"action": "logout"}, headers=_with_cookie(copy))

    assert response.status_code == 204
    assert _session_cookie_headers(response) == []


# ---------------------------------------------------------------------------
# Password change and auth-type change
# ---------------------------------------------------------------------------


def _save(saveable, *items):
    saveable.save_settings([(key, [value]) for key, value in items])


def test_a_cookie_from_before_a_password_change_is_refused(server, saveable):
    cookie = _signed_in_cookie(server)

    _save(saveable, ("settings-auth-password", "a different password"))

    assert _generation() == 1
    assert not _document_embeds_key(_document_for(server, cookie))


def test_a_password_change_is_saved_with_the_new_generation_in_one_write(server, saveable, writes):
    _signed_in_cookie(server)

    _save(saveable, ("settings-auth-password", "a different password"))

    assert writes == [1]


def test_the_browser_that_changes_the_password_stays_signed_in(server, saveable):
    changer = server.test_client()
    _login(changer)
    other = _signed_in_cookie(server)

    assert changer.post("/_save-settings", data={"settings-auth-password": "a different password"}
                        ).status_code == 200

    assert _generation() == 1
    assert _document_embeds_key(changer.get("/"))
    assert not _document_embeds_key(_document_for(server, other))


def test_a_browser_revoked_while_its_password_save_ran_is_not_signed_back_in(server, saveable):
    """The save re-stamps only a session that was current just before its own advance."""
    client = server.test_client()
    _login(client)
    assert client.get("/_gate").get_data(as_text=True) == "open"

    client.post("/_revoked-then-save", data={"settings-auth-password": "a different password"})

    assert _generation() == 2
    assert client.get("/_gate").get_data(as_text=True) == "closed"


def test_an_auth_type_change_refuses_the_earlier_cookies(server, saveable, monkeypatch):
    cookie = _signed_in_cookie(server)

    _save(saveable, ("settings-auth-type", "basic"))
    _set_auth(type="form")

    assert _generation() == 1
    assert not _document_embeds_key(_document_for(server, cookie))


def test_a_username_change_refuses_the_earlier_cookies(server, saveable):
    cookie = _signed_in_cookie(server)

    _save(saveable, ("settings-auth-username", "someone-else"))

    assert _generation() == 1
    assert not _document_embeds_key(_document_for(server, cookie))


def test_a_save_that_leaves_the_login_alone_keeps_every_cookie(server, saveable):
    cookie = _signed_in_cookie(server)

    _save(saveable, ("settings-general-instance_name", "Living room"))
    _save(saveable, ("settings-auth-type", "form"))
    _save(saveable, ("settings-auth-username", USERNAME))

    assert _generation() == 0
    assert _document_embeds_key(_document_for(server, cookie))


def test_a_save_refused_for_its_values_does_not_move_the_generation(server, saveable, monkeypatch):
    """The advance comes after validation, so a refused save never revokes anybody."""
    from dynaconf.validator import ValidationError

    cookie = _signed_in_cookie(server)

    def refuse():
        raise ValidationError("refused")

    monkeypatch.setattr(saveable.settings.validators, "validate", refuse)
    monkeypatch.setattr(saveable, "restore_persisted_settings", lambda: None)
    with pytest.raises(ValidationError):
        _save(saveable, ("settings-auth-password", "a different password"))

    assert _generation() == 0
    assert _document_embeds_key(_document_for(server, cookie))


def test_a_save_that_cannot_be_written_reloads_the_settings_it_advanced(server, saveable, monkeypatch):
    """The reload is what takes the in-memory advance back with the rest of the refused save."""
    from dynaconf.validator import ValidationError

    monkeypatch.setattr(saveable, "write_config", lambda *a, **k: False)
    seen = []
    monkeypatch.setattr(saveable, "restore_persisted_settings", lambda: seen.append(_generation()))

    with pytest.raises(ValidationError):
        _save(saveable, ("settings-auth-password", "a different password"))

    assert seen == [1]


def test_the_generation_is_hidden_from_the_settings_read(server):
    from app.config import get_settings

    assert "session_generation" not in get_settings()["auth"]


def test_the_generation_cannot_be_written_through_the_settings_save(server, saveable):
    cookie = _signed_in_cookie(server)
    client = server.test_client()
    _login(client)
    _logout(client)
    assert _generation() == 1

    _save(saveable, ("settings-auth-session_generation", "0"))
    _save(saveable, ("settings-Auth-Session_Generation", "0"))

    assert _generation() == 1
    # Nor a second, differently spelled copy of the key beside the real one.
    assert [key for key in server_settings() if key.lower() == "session_generation"] == ["session_generation"]
    assert not _document_embeds_key(_document_for(server, cookie))


def test_the_generation_is_written_to_the_config_file():
    """The real write, to the test run's own config directory: it must name the number."""
    import yaml

    from app import config

    original = config.settings.auth.session_generation
    try:
        config.settings.auth.session_generation = 7
        assert config.write_config() is True
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            stored = yaml.safe_load(handle)
        assert stored["auth"]["session_generation"] == 7
    finally:
        config.settings.auth.session_generation = original
        config.write_config()


def test_a_failed_write_still_revokes_until_restart(server, monkeypatch, caplog):
    from app import config
    from app.auth.generation import revoke_all_sessions

    cookie = _signed_in_cookie(server)
    monkeypatch.setattr(config, "write_config", lambda *a, **k: False)

    revoke_all_sessions()

    assert _generation() == 1
    assert not _document_embeds_key(_document_for(server, cookie))
    assert "could not be saved" in caplog.text
    assert "restart" in caplog.text


def test_a_failed_logout_write_survives_a_settings_reload(server, monkeypatch):
    """The reload of a refused save must not hand a revoked cookie its life back."""
    from app import config
    from app.auth.generation import revoke_all_sessions

    monkeypatch.setattr(config, "write_config", lambda *a, **k: False)
    revoke_all_sessions()
    assert _generation() == 1

    config.restore_persisted_settings()

    assert _generation() == 1


def _observe_reader(monkeypatch, module, lock_name, read_name=None, read_module=None):
    """Signal when the reader either blocks on the lock or has captured its value.

    The same checkpoint works with the fix removed: an unlocked reader has
    already captured the transient value before the writer is released. A
    locked reader signals its failed nonblocking acquire before waiting.
    """
    checkpoint = threading.Event()
    read_module = read_module or module
    lock = getattr(module, lock_name)

    class ObservedLock:
        def __enter__(self):
            if not lock.acquire(blocking=False):
                if threading.current_thread().name == "generation-reader":
                    checkpoint.set()
                lock.acquire()
            return self

        def __exit__(self, *_args):
            lock.release()

    monkeypatch.setattr(module, lock_name, ObservedLock())
    if read_name is not None:
        read = getattr(read_module, read_name)

        def observed_read(*args, **kwargs):
            result = read(*args, **kwargs)
            if threading.current_thread().name == "generation-reader":
                checkpoint.set()
            return result

        monkeypatch.setattr(read_module, read_name, observed_read)
    return checkpoint


def _admits_during_a_reload(server, settled_disk, monkeypatch, admits, make_cookie=_signed_in_cookie):
    """Does a revoked cookie get in while the settings are being reloaded from a disk that is behind?

    The logout's write failed, so memory holds generation 1 and the file still holds 0, and the
    reload that undoes a refused save installs the file's value until it puts the larger one back.
    `admits(cookie)` is the reader under test: it runs on its own thread while the reload is held
    open, and its answer is what counts, whether it came back during the reload or after it.
    """
    from app import config
    from app.auth import generation
    from app.auth.generation import revoke_all_sessions

    cookie = make_cookie(server)
    checkpoint = _observe_reader(monkeypatch, generation, "_advance_lock")
    monkeypatch.setattr(config, "write_config", lambda *a, **k: False)
    revoke_all_sessions()
    assert _generation() == 1

    in_reload, release = threading.Event(), threading.Event()
    real_migrate = config.migrate_legacy_plex_encryption

    def hold_the_reload_open(settings_object):
        in_reload.set()
        assert release.wait(10)
        return real_migrate(settings_object)

    monkeypatch.setattr(config, "migrate_legacy_plex_encryption", hold_the_reload_open)
    reloader = threading.Thread(target=config.restore_persisted_settings)
    reloader.start()
    assert in_reload.wait(10)

    answer = {}
    def read_cookie():
        answer["admitted"] = admits(cookie)
        checkpoint.set()  # An unlocked reader must finish the whole gate before the reload proceeds.

    reader = threading.Thread(target=read_cookie, name="generation-reader")
    reader.start()
    assert checkpoint.wait(10)
    release.set()
    reloader.join(10)
    reader.join(10)
    assert not reloader.is_alive() and not reader.is_alive()

    assert _generation() == 1
    return answer["admitted"]


def _document_is_signed_in(response):
    """The shell carries an apiKey field only for a signed-in browser (whatever the key's value)."""
    return "apiKey" in response.get_data(as_text=True)


def test_a_revoked_cookie_is_not_admitted_by_the_document_during_a_reload(server, settled_disk, monkeypatch):
    assert not _admits_during_a_reload(server, settled_disk, monkeypatch,
                                       lambda cookie: _document_is_signed_in(_document_for(server, cookie)))


def test_a_revoked_cookie_is_not_admitted_by_the_session_gate_during_a_reload(server, settled_disk, monkeypatch):
    assert not _admits_during_a_reload(
        server, settled_disk, monkeypatch,
        lambda cookie: _bare_client(server).get("/_gate", headers=_with_cookie(cookie)).get_data(as_text=True) == "open")


def test_a_revoked_cookie_is_not_admitted_by_the_socket_gate_during_a_reload(server, settled_disk, monkeypatch):
    assert not _admits_during_a_reload(server, settled_disk, monkeypatch,
                                       lambda cookie: _connect_gate(server, cookie))


def test_a_revoked_confirmation_is_not_admitted_during_a_reload(server, settled_disk, monkeypatch):
    def confirmed_cookie(app):
        client = app.test_client()
        assert _login(client).status_code == 204
        assert client.post(ACCOUNT_URL + "/confirm", data={"password": PASSWORD},
                           headers={"X-API-KEY": API_KEY}).status_code == 204
        return _cookie_value(client)

    assert not _admits_during_a_reload(
        server, settled_disk, monkeypatch,
        lambda cookie: _bare_client(server).get("/_confirmation-gate", headers=_with_cookie(cookie)).status_code == 200,
        make_cookie=confirmed_cookie)


def test_a_response_never_renews_a_revoked_cookie_while_a_reload_exposes_its_number(
        server, settled_disk, monkeypatch):
    from app import config
    from app.auth import generation

    old = _signed_in_cookie(server)
    answered = {}

    def slow_response():
        answered["response"] = _bare_client(server).get("/_slow-read", headers=_with_cookie(old))
        checkpoint.set()

    reader = threading.Thread(target=slow_response, name="generation-reader")
    reader.start()
    assert server.slow_started.wait(10)
    monkeypatch.setattr(config, "write_config", lambda *a, **k: False)
    generation.revoke_all_sessions()
    checkpoint = _observe_reader(monkeypatch, generation, "_advance_lock")
    in_reload, release = threading.Event(), threading.Event()

    def hold_reload(settings_object):
        in_reload.set()
        assert release.wait(10)

    monkeypatch.setattr(config, "migrate_legacy_plex_encryption", hold_reload)
    reloader = threading.Thread(target=config.restore_persisted_settings)
    reloader.start()
    try:
        assert in_reload.wait(10)
        server.slow_proceed.set()
        assert checkpoint.wait(10)
    finally:
        release.set()
        reloader.join(10)
        reader.join(10)
    assert not reloader.is_alive() and not reader.is_alive()
    assert _generation() == 1
    assert _session_cookie_headers(answered["response"]) == []


def test_the_generation_never_reads_lower_than_the_one_in_force_during_a_reload(server, settled_disk, monkeypatch):
    """Straight at the reader: whatever it is asked while the reload runs, it never answers 0."""
    from app import config
    from app.auth import generation
    from app.auth.generation import revoke_all_sessions

    monkeypatch.setattr(config, "write_config", lambda *a, **k: False)
    revoke_all_sessions()
    checkpoint = _observe_reader(monkeypatch, generation, "_advance_lock", "current_generation")
    in_reload, release = threading.Event(), threading.Event()

    def hold_the_reload_open(settings_object):
        in_reload.set()
        assert release.wait(10)

    monkeypatch.setattr(config, "migrate_legacy_plex_encryption", hold_the_reload_open)
    reloader = threading.Thread(target=config.restore_persisted_settings)
    reloader.start()
    assert in_reload.wait(10)

    seen = []
    reader = threading.Thread(target=lambda: seen.append(generation.current_generation()), name="generation-reader")
    reader.start()
    assert checkpoint.wait(10)
    release.set()
    reloader.join(10)
    reader.join(10)
    assert not reloader.is_alive() and not reader.is_alive()

    assert seen == [1]


def test_a_reload_never_takes_the_generation_backwards(server):
    from app import config

    _set_auth(session_generation=5)

    config.restore_persisted_settings()

    assert _generation() == 5


def test_a_reload_takes_the_generation_the_disk_holds_when_it_is_ahead():
    from app import config

    original = _generation()
    try:
        _set_auth(session_generation=7)
        assert config.write_config() is True
        _set_auth(session_generation=3)

        config.restore_persisted_settings()

        assert _generation() == 7
    finally:
        _set_auth(session_generation=original)
        config.write_config()


def test_a_revocation_advances_and_saves_under_the_config_lock(server, monkeypatch):
    """A refused save reloads under this lock, so it cannot land between the two steps."""
    from app import config
    from app.auth import generation
    from discover.metadata import CONFIG_LOCK

    def free_from_another_thread():
        taken = []

        def probe():
            if CONFIG_LOCK.acquire(blocking=False):
                CONFIG_LOCK.release()
                taken.append(True)

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(10)
        return bool(taken)

    seen = {}
    real_advance = generation.advance_session_generation

    def advance():
        seen["advance"] = free_from_another_thread()
        return real_advance()

    def write(*_args, **_kwargs):
        seen["write"] = free_from_another_thread()
        return True

    monkeypatch.setattr(generation, "advance_session_generation", advance)
    monkeypatch.setattr(config, "write_config", write)

    generation.revoke_all_sessions()

    assert seen == {"advance": False, "write": False}


def test_a_refused_save_takes_back_its_own_advance_and_nothing_else(server, saveable, settled_disk, monkeypatch):
    """A save that never reached the disk revokes nobody, even though a reload cannot go backwards."""
    from dynaconf.validator import ValidationError

    monkeypatch.setattr(saveable, "write_config", lambda *a, **k: False)

    with pytest.raises(ValidationError):
        _save(saveable, ("settings-auth-password", "a different password"))

    assert _generation() == 0


def test_a_refused_save_keeps_a_revocation_that_landed_during_it(server, saveable, settled_disk, monkeypatch):
    from dynaconf.validator import ValidationError

    from app.auth.generation import advance_session_generation

    def refuse_after_a_logout(*_args, **_kwargs):
        advance_session_generation()
        return False

    monkeypatch.setattr(saveable, "write_config", refuse_after_a_logout)

    with pytest.raises(ValidationError):
        _save(saveable, ("settings-auth-password", "a different password"))

    assert _generation() == 2


def test_a_save_reported_refused_after_its_file_landed_still_revokes(server, saveable, settled_disk,
                                                                     monkeypatch, caplog):
    """The write can report failure after the file is already in place (a rename that
    fell back to a copy, then failed on the file's mode). The new password is on disk
    and in force then, so the advance that goes with it must stay."""
    from dynaconf.validator import ValidationError

    from app import config

    cookie = _signed_in_cookie(server)
    assert _document_embeds_key(_document_for(server, cookie))
    password_before = _live_auth().password

    def landed_then_refused(*_args, **_kwargs):
        assert config._write_config() is True  # the file holds the new login and generation 1
        return False  # and a step after it reports failure

    monkeypatch.setattr(saveable, "write_config", landed_then_refused)

    with pytest.raises(ValidationError):
        _save(saveable, ("settings-auth-password", "a different password"))

    assert _live_auth().password != password_before  # the new password is the one in force
    assert _generation() == 1
    assert not _document_embeds_key(_document_for(server, cookie))
    assert "reached the disk" in caplog.text


def test_a_save_that_wrote_nothing_keeps_every_cookie(server, saveable, settled_disk, monkeypatch):
    """The other half: when nothing landed, the login is unchanged and nobody is signed out."""
    from dynaconf.validator import ValidationError

    cookie = _signed_in_cookie(server)
    password_before = _live_auth().password
    monkeypatch.setattr(saveable, "write_config", lambda *a, **k: False)

    with pytest.raises(ValidationError):
        _save(saveable, ("settings-auth-password", "a different password"))

    assert _live_auth().password == password_before
    assert _generation() == 0
    assert _document_embeds_key(_document_for(server, cookie))


@pytest.mark.parametrize("landed", ["the login only", "the generation only"])
def test_a_refused_save_keeps_its_advance_when_either_half_reached_the_disk(server, saveable, settled_disk,
                                                                          monkeypatch, landed):
    """The decision reads both the file's login and the file's generation, and either one is enough."""
    from dynaconf.validator import ValidationError

    from app import config

    cookie = _signed_in_cookie(server)
    login_before = {name: _live_auth()[name] for name in ("type", "username", "password")}

    def half_landed(*_args, **_kwargs):
        mine = {name: _live_auth()[name] for name in ("type", "username", "password", "session_generation")}
        try:
            if landed == "the login only":
                _set_auth(session_generation=0)
            else:
                _set_auth(**login_before)
            assert config._write_config() is True
        finally:
            _set_auth(**mine)
        return False

    monkeypatch.setattr(saveable, "write_config", half_landed)

    with pytest.raises(ValidationError):
        _save(saveable, ("settings-auth-password", "a different password"))

    assert _generation() == 1
    assert not _document_embeds_key(_document_for(server, cookie))


def test_a_refused_save_whose_file_generation_is_unreadable_keeps_its_advance(server, saveable, settled_disk,
                                                                            monkeypatch):
    """A file that cannot be read for its generation cannot show the advance missing: fail closed."""
    from dynaconf.validator import ValidationError

    from app import config

    cookie = _signed_in_cookie(server)
    login_before = {name: _live_auth()[name] for name in ("type", "username", "password")}

    def wrote_a_broken_number(*_args, **_kwargs):
        mine = {name: _live_auth()[name] for name in ("type", "username", "password", "session_generation")}
        try:
            _set_auth(session_generation="not a number", **login_before)
            assert config._write_config() is True
        finally:
            _set_auth(**mine)
        return False

    monkeypatch.setattr(saveable, "write_config", wrote_a_broken_number)

    with pytest.raises(ValidationError):
        _save(saveable, ("settings-auth-password", "a different password"))

    assert _generation() == 1
    assert not _document_embeds_key(_document_for(server, cookie))


def test_a_metadata_save_that_wrote_nothing_takes_back_its_advance(server, saveable, settled_disk, monkeypatch):
    """The strict-metadata path refuses in its own branch, and gives the advance back there too."""
    from app import config

    cookie = _signed_in_cookie(server)

    def refuse(*_args, **_kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr(saveable, "write_config", refuse)

    with pytest.raises(config.MetadataPersistenceError):
        saveable._save_settings([("settings-auth-password", ["a different password"])], strict_metadata=True)

    assert _generation() == 0
    assert _document_embeds_key(_document_for(server, cookie))


def test_a_metadata_save_reported_refused_after_its_file_landed_still_revokes(server, saveable, settled_disk,
                                                                              monkeypatch):
    from app import config

    cookie = _signed_in_cookie(server)

    def landed_then_raised(*_args, **_kwargs):
        assert config._write_config() is True
        raise OSError("failed after the file landed")

    monkeypatch.setattr(saveable, "write_config", landed_then_raised)

    with pytest.raises(config.MetadataPersistenceError):
        saveable._save_settings([("settings-auth-password", ["a different password"])], strict_metadata=True)

    assert _generation() == 1
    assert not _document_embeds_key(_document_for(server, cookie))


def test_a_cookie_issued_during_a_refused_save_is_never_revived(server, saveable, settled_disk, monkeypatch):
    """The save's advance is in memory while it runs, so a sign-in with the new password
    in that window is stamped with the advanced number. When the save is refused the
    number goes back, and the same number must not be handed out again later."""
    from dynaconf.validator import ValidationError

    from app.auth.generation import revoke_all_sessions

    old = _signed_in_cookie(server)
    window = {}

    def refuse_after_a_sign_in(*_args, **_kwargs):
        client = server.test_client()
        assert _login(client, "a different password").status_code == 204
        window["cookie"] = _cookie_value(client)
        return False

    monkeypatch.setattr(saveable, "write_config", refuse_after_a_sign_in)

    with pytest.raises(ValidationError):
        _save(saveable, ("settings-auth-password", "a different password"))

    assert _generation() == 0
    assert _document_embeds_key(_document_for(server, old))
    assert not _document_embeds_key(_document_for(server, window["cookie"]))

    monkeypatch.setattr(saveable, "write_config", lambda *a, **k: True)
    revoke_all_sessions()

    assert _generation() == 2  # not 1: that number was issued once already
    assert not _document_embeds_key(_document_for(server, window["cookie"]))
    assert not _document_embeds_key(_document_for(server, old))


@pytest.mark.parametrize("strict_metadata", [False, True])
def test_two_refused_saves_in_a_row_leave_the_generation_where_it_was(
        server, saveable, settled_disk, monkeypatch, strict_metadata):
    """Advances skip numbers (the high-water mark), so a withdraw is not "one less".

    The first refused save advances 0 to 1 and a sign-in in its window is stamped 1.
    The second advances to 2. Taking that back as 2 minus 1 landed on 1, which is the
    number the window's cookie carries: the one thing a withdraw must never do.
    """
    from dynaconf.validator import ValidationError

    old = _signed_in_cookie(server)
    windows = []

    def refuse_after_a_sign_in(*_args, **_kwargs):
        client = server.test_client()
        assert _login(client, "a different password").status_code == 204
        windows.append(_cookie_value(client))
        if strict_metadata:
            raise OSError("the write was refused")
        return False

    monkeypatch.setattr(saveable, "write_config", refuse_after_a_sign_in)

    for _ in range(2):
        error = saveable.MetadataPersistenceError if strict_metadata else ValidationError
        with pytest.raises(error):
            saveable._save_settings([("settings-auth-password", ["a different password"])],
                                    strict_metadata=strict_metadata)
        assert _generation() == 0

    assert _document_embeds_key(_document_for(server, old))
    for cookie in windows:
        assert not _document_embeds_key(_document_for(server, cookie))


def test_a_withdraw_restores_the_number_the_advance_started_from(login_settings):
    """Whatever the advance skipped over, taking it back returns to where it began."""
    from app.auth import generation

    first = generation.advance_for_login_change()[1]
    generation.withdraw_advance(first)
    second = generation.advance_for_login_change()[1]
    assert (first.previous, first.number) == (0, 1)
    assert (second.previous, second.number) == (0, 2)

    generation.withdraw_advance(second)

    assert _generation() == 0


def test_a_withdraw_leaves_a_number_somebody_else_moved_on_from(login_settings):
    from app.auth import generation

    taken = generation.advance_for_login_change()[1]
    generation.advance_session_generation()

    generation.withdraw_advance(taken)

    assert _generation() == 2


def test_an_advance_after_a_withdraw_skips_the_number_that_was_taken_back(login_settings):
    from app.auth import generation

    token = generation.advance_for_login_change()[1]
    assert token.number == 1
    generation.withdraw_advance(token)
    assert _generation() == 0

    assert generation.advance_session_generation() == 2
    assert _generation() == 2


def test_the_high_water_mark_does_not_follow_the_number_down(login_settings):
    """The next number is past everything issued, whatever the current one has dropped to."""
    from app.auth import generation

    generation.advance_session_generation()
    generation.advance_session_generation()
    _set_auth(session_generation=0)  # moved by hand: nothing in the product does this

    assert generation.advance_session_generation() == 3


def test_the_next_number_follows_a_current_one_the_mark_has_never_seen(login_settings):
    """After a start or a reload the current number comes from the file, ahead of the mark."""
    from app.auth import generation

    _set_auth(session_generation=5)

    assert generation.advance_session_generation() == 6


@pytest.mark.parametrize("archive_format", ["yaml", "ini"])
@pytest.mark.parametrize("archived_generation", [0, 9])
def test_restoring_a_backup_never_revives_its_signed_browser_cookie(
        server, settled_disk, monkeypatch, tmp_path, archive_format, archived_generation):
    import configparser
    import sqlite3

    import yaml

    from api import api_bp
    from app import app as app_module, config, ui
    from app.auth import generation
    from utilities import backup

    _set_auth(session_generation=archived_generation)
    archived_cookie = _signed_in_cookie(server)
    archived = yaml.safe_load(settled_disk.read_text(encoding="utf-8"))
    archived["auth"]["session_generation"] = archived_generation
    signing_key = archived["general"]["flask_secret_key"]
    _set_auth(session_generation=0)
    generation.revoke_all_sessions()
    withdrawn = generation.advance_for_login_change()[1]
    generation.withdraw_advance(withdrawn)
    assert _generation() == 1

    root = tmp_path / "restore-target"
    for name in ("restore", "config", "db"):
        (root / name).mkdir(parents=True)
    (root / "config" / "config.yaml").write_bytes(settled_disk.read_bytes())
    source = root / "restore" / ("config." + archive_format)
    if archive_format == "yaml":
        source.write_text(yaml.safe_dump(archived), encoding="utf-8")
    else:
        parser = configparser.RawConfigParser()
        for section, values in archived.items():
            parser[section] = {key: repr(value) for key, value in values.items()}
        with source.open("w", encoding="utf-8") as handle:
            parser.write(handle)
    with sqlite3.connect(root / "restore" / "bazarr.db") as database:
        database.execute("CREATE TABLE restored (value INTEGER)")
        database.execute("INSERT INTO restored VALUES (1)")
    monkeypatch.setattr(backup, "args", SimpleNamespace(config_dir=str(root)))
    monkeypatch.setattr(backup, "_postgres_enabled", lambda: False)
    monkeypatch.setattr(backup, "restart_bazarr", lambda: None)
    monkeypatch.setitem(sys.modules, "app.server", SimpleNamespace(webserver=None))

    assert backup.restore_from_backup() is True

    restored_path = root / "config" / "config.yaml"
    restored = yaml.safe_load(restored_path.read_text(encoding="utf-8"))
    restored_generation = restored["auth"]["session_generation"]
    assert restored_generation > max(1, archived_generation, withdrawn.number)
    assert restored["general"]["flask_secret_key"] == signing_key
    assert restored["auth"]["apikey"] == archived["auth"]["apikey"]
    assert _generation() == 1  # Staging reserves a number without changing the live settings.

    # Rebuild the app from the file as startup would, with the archived signing
    # key intact. The restored number, rather than a new key, rejects the cookie.
    settled_disk.write_bytes(restored_path.read_bytes())
    config.restore_persisted_settings()
    restarted = app_module.create_app()
    restarted.register_blueprint(api_bp, url_prefix="/api")
    restarted.register_blueprint(ui.ui_bp)
    assert not _document_embeds_key(_document_for(restarted, archived_cookie))
    assert _bare_client(restarted).get(STATUS_URL, headers={"X-API-KEY": API_KEY}).status_code == 200
    assert generation.advance_session_generation() > restored_generation


class _Session(dict):
    """The part of a Flask session the cleanup hooks use: a mapping with a modified flag."""
    modified = False


@pytest.mark.parametrize("carried", [True, 1.0, "1"])
def test_opening_a_cookie_whose_generation_only_equals_the_number_clears_it(login_settings, carried):
    """The cleanup runs before the gate does, and must not let a look-alike through either.

    Cleared, but not marked modified: a modified empty session turns the response into a
    cookie deletion, which can delete a newer cookie the browser holds by then."""
    from app.auth.generation import open_session_cleanup

    _set_auth(session_generation=1)
    opened = open_session_cleanup(_Session({"logged_in": True, "gen": carried}))

    assert dict(opened) == {}
    assert opened.modified is False


def test_opening_a_stale_cookie_clears_it_without_marking_it_modified(login_settings):
    from app.auth.generation import open_session_cleanup

    _set_auth(session_generation=2)
    opened = open_session_cleanup(_Session({"logged_in": True, "gen": 1}))

    assert dict(opened) == {}
    assert opened.modified is False


def test_opening_a_cookie_with_the_real_generation_keeps_it(login_settings):
    from app.auth.generation import open_session_cleanup

    _set_auth(session_generation=1)
    opened = open_session_cleanup(_Session({"logged_in": True, "gen": 1}))

    assert dict(opened) == {"logged_in": True, "gen": 1}
    assert opened.modified is False


@pytest.mark.parametrize("carried", [True, 1.0, "1", 0])
def test_a_session_whose_generation_is_not_the_number_is_not_written(login_settings, carried):
    from app.auth.generation import before_save_cleanup

    _set_auth(session_generation=1)
    outgoing = _Session({"logged_in": True, "gen": carried})

    assert before_save_cleanup(outgoing) is False


def test_a_session_with_the_real_generation_is_written_as_it_is(login_settings):
    from app.auth.generation import before_save_cleanup

    _set_auth(session_generation=1)
    outgoing = _Session({"logged_in": True, "gen": 1})

    assert before_save_cleanup(outgoing) is True
    assert dict(outgoing) == {"logged_in": True, "gen": 1}
    assert outgoing.modified is False


def test_a_session_without_a_generation_is_stamped_and_written(login_settings):
    from app.auth.generation import before_save_cleanup

    _set_auth(session_generation=3)
    outgoing = _Session({"password_confirmed_until": 1})

    assert before_save_cleanup(outgoing) is True
    assert outgoing["gen"] == 3


def test_an_empty_session_never_writes_a_cookie_deletion(login_settings):
    from app.auth.generation import before_save_cleanup

    assert before_save_cleanup(_Session()) is False


def test_a_session_is_not_written_while_the_generation_is_unreadable(login_settings):
    from app.auth.generation import before_save_cleanup

    _set_auth(session_generation="not a number")

    assert before_save_cleanup(_Session({"password_confirmed_until": 1})) is False


def test_the_reload_that_keeps_the_number_runs_under_the_config_lock(login_settings):
    """A revocation advances and writes under this lock, so the reload must hold it too."""
    from app.auth.generation import never_going_backwards
    from discover.metadata import CONFIG_LOCK

    taken = []

    def probe():
        if CONFIG_LOCK.acquire(blocking=False):
            CONFIG_LOCK.release()
            taken.append(True)

    with never_going_backwards():
        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(10)
        assert not taken, "another thread could take the config lock during the reload"

    worker = threading.Thread(target=probe)
    worker.start()
    worker.join(10)
    assert taken, "the config lock was not released afterwards"


@pytest.mark.parametrize("unreadable", ["not a number", False, -1, None, 1.0])
def test_an_unreadable_generation_refuses_every_cookie(server, monkeypatch, unreadable):
    """False equals 0 in Python, which is the cookie's number: it must still refuse."""
    cookie = _signed_in_cookie(server)
    _set_auth(session_generation=unreadable)

    assert not _document_embeds_key(_document_for(server, cookie))


def server_settings():
    return _live_auth()


# ---------------------------------------------------------------------------
# A sign-in that overlaps a change of the login
# ---------------------------------------------------------------------------


def _pause_first_verification(monkeypatch, which=1):
    """Hold the first password check after it has read the stored hash, until released."""
    from utilities import helper

    real = helper._verify_password
    verifying, proceed = threading.Event(), threading.Event()
    calls = []

    def paused(pw, stored_hash):
        calls.append(stored_hash)
        if len(calls) == which:
            verifying.set()
            assert proceed.wait(10)
        return real(pw, stored_hash)

    monkeypatch.setattr(helper, "_verify_password", paused)
    return verifying, proceed


def _login_in_a_thread(app, password, completed=None):
    outcome = {}

    def run():
        client = app.test_client()
        outcome["response"] = _login(client, password)
        outcome["cookie"] = _cookie_value(client)
        if completed is not None:
            completed.set()

    worker = threading.Thread(target=run, name="generation-reader")
    worker.start()
    return worker, outcome


def test_a_sign_in_checked_against_the_old_password_does_not_survive_the_change(server, saveable, monkeypatch):
    """The password was verified, then the password changed and the generation moved on.

    Stamping whatever number is current after the check gave credentials that were
    right for the old password a session that outlived the change.
    """
    verifying, proceed = _pause_first_verification(monkeypatch)
    worker, outcome = _login_in_a_thread(server, PASSWORD)
    assert verifying.wait(10)

    _save(saveable, ("settings-auth-password", "a different password"))
    assert _generation() == 1
    proceed.set()
    worker.join(10)

    assert outcome["response"].status_code == 403
    assert outcome["cookie"] is None or not _document_embeds_key(_document_for(server, outcome["cookie"]))


def test_a_sign_in_checked_against_the_old_username_does_not_survive_the_change(server, saveable, monkeypatch):
    verifying, proceed = _pause_first_verification(monkeypatch)
    worker, outcome = _login_in_a_thread(server, PASSWORD)
    try:
        assert verifying.wait(10)
        _save(saveable, ("settings-auth-username", "new-owner"))
    finally:
        proceed.set()
        worker.join(10)
    assert not worker.is_alive()
    assert outcome["response"].status_code == 403
    assert _session_cookie_headers(outcome["response"]) == []


@pytest.mark.parametrize("auth_type", ["form", "basic"])
def test_a_real_password_confirmation_cannot_outlive_the_password_it_checked(
        server, saveable, monkeypatch, auth_type):
    from base64 import b64encode

    _set_auth(type=auth_type)
    client = server.test_client()
    if auth_type == "form":
        assert _login(client).status_code == 204
    headers = {"X-API-KEY": API_KEY}
    if auth_type == "basic":
        encoded = b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    verifying, proceed = _pause_first_verification(monkeypatch, which=2 if auth_type == "basic" else 1)
    answered = {}

    def confirm():
        answered["response"] = client.post(ACCOUNT_URL + "/confirm", data={"password": PASSWORD},
                                           headers=headers)

    worker = threading.Thread(target=confirm)
    worker.start()
    try:
        assert verifying.wait(10)
        _save(saveable, ("settings-auth-password", "a different password"))
    finally:
        proceed.set()
        worker.join(10)
    assert not worker.is_alive()
    assert answered["response"].status_code in (401, 403)
    assert _session_cookie_headers(answered["response"]) == []


@pytest.mark.parametrize("auth_type", ["form", "basic"])
def test_a_real_password_confirmation_keeps_its_window_without_a_login_change(server, auth_type):
    from base64 import b64encode

    _set_auth(type=auth_type)
    client = server.test_client()
    if auth_type == "form":
        assert _login(client).status_code == 204
    headers = {"X-API-KEY": API_KEY}
    if auth_type == "basic":
        encoded = b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"

    assert client.post(ACCOUNT_URL + "/confirm", data={"password": PASSWORD}, headers=headers).status_code == 204
    with client.session_transaction() as stored:
        assert stored["gen"] == 0
        assert time.time() < stored["password_confirmed_until"] <= time.time() + 600


def test_a_basic_confirmation_gate_rechecks_the_cookie_after_slow_credentials(server, saveable, monkeypatch):
    from base64 import b64encode

    _set_auth(type="basic")
    client = server.test_client()
    encoded = b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    headers = {"X-API-KEY": API_KEY, "Authorization": f"Basic {encoded}"}
    assert client.post(ACCOUNT_URL + "/confirm", data={"password": PASSWORD}, headers=headers).status_code == 204
    verifying, proceed = _pause_first_verification(monkeypatch)
    answered = {}

    def gated_request():
        answered["response"] = client.get("/_confirmation-gate", headers=headers)

    worker = threading.Thread(target=gated_request)
    worker.start()
    try:
        assert verifying.wait(10)
        _save(saveable, ("settings-auth-password", "a different password"))
    finally:
        proceed.set()
        worker.join(10)
    assert not worker.is_alive()
    assert answered["response"].status_code == 403
    assert _session_cookie_headers(answered["response"]) == []


def test_a_sign_in_that_overlaps_an_unrelated_revocation_still_succeeds(server, monkeypatch):
    """A logout elsewhere moves the number but not the password: the sign-in is checked again."""
    from app.auth.generation import revoke_all_sessions

    verifying, proceed = _pause_first_verification(monkeypatch)
    worker, outcome = _login_in_a_thread(server, PASSWORD)
    assert verifying.wait(10)

    revoke_all_sessions()
    proceed.set()
    worker.join(10)

    assert outcome["response"].status_code == 204
    assert _generation() == 1
    assert _document_embeds_key(_document_for(server, outcome["cookie"]))


def test_a_sign_in_during_a_refused_password_save_is_not_kept(server, saveable, settled_disk, monkeypatch):
    """The proposed password is live in memory while the save runs, and gone when it is refused.

    A sign-in with it inside that window was stamped with the number the refused save
    then gave back, so the session outlived a password that never took effect.
    """
    from dynaconf.validator import ValidationError
    from discover import metadata

    in_window, release = threading.Event(), threading.Event()
    checkpoint = _observe_reader(monkeypatch, metadata, "CONFIG_LOCK")

    def hold_the_save_open():
        in_window.set()
        assert release.wait(10)

    monkeypatch.setattr(saveable, "validate_log_regex", hold_the_save_open)
    monkeypatch.setattr(saveable, "write_config", lambda *a, **k: False)
    refused = []

    def save():
        try:
            _save(saveable, ("settings-auth-password", "a different password"))
        except ValidationError:
            refused.append(True)

    saver = threading.Thread(target=save)
    saver.start()
    assert in_window.wait(10)

    worker, outcome = _login_in_a_thread(server, "a different password", completed=checkpoint)
    assert checkpoint.wait(10)
    release.set()
    saver.join(10)
    worker.join(10)
    assert not saver.is_alive() and not worker.is_alive()

    assert refused == [True]
    assert outcome["response"].status_code == 403
    assert outcome["cookie"] is None or not _document_embeds_key(_document_for(server, outcome["cookie"]))


def test_a_password_hash_upgrade_does_not_overwrite_a_password_changed_since_the_check(server, saveable,
                                                                                    monkeypatch):
    """The upgrade stores a new hash of the password that was typed: only while that is still the stored one."""
    import hashlib

    from api.system import account
    from utilities import helper

    legacy = hashlib.md5(PASSWORD.encode("utf-8")).hexdigest()
    _set_auth(password=legacy)
    real_upgrade = account.upgrade_password_hash

    def change_lands_first(*args, **kwargs):
        _save(saveable, ("settings-auth-password", "a different password"))
        return real_upgrade(*args, **kwargs)

    monkeypatch.setattr(account, "upgrade_password_hash", change_lands_first)

    _login(server.test_client())

    assert helper._verify_password("a different password", _live_auth().password)
    assert not helper._verify_password(PASSWORD, _live_auth().password)


def test_a_password_hash_upgrade_applies_when_nothing_changed(server, writes):
    import hashlib

    from utilities import helper

    legacy = hashlib.md5(PASSWORD.encode("utf-8")).hexdigest()
    _set_auth(password=legacy)

    assert _login(server.test_client()).status_code == 204

    assert _live_auth().password.startswith("pbkdf2:")
    assert helper._verify_password(PASSWORD, _live_auth().password)


def test_a_sign_in_while_the_generation_is_unreadable_is_refused(server):
    """No number to stamp, no session: the sign-in fails closed instead of leaving a bare cookie."""
    _set_auth(session_generation="not a number")
    client = server.test_client()

    assert _login(client).status_code == 403

    _set_auth(session_generation=0)
    assert not _document_embeds_key(_document_for(server, _cookie_value(client) or ""))


# ---------------------------------------------------------------------------
# The document that carries the key
# ---------------------------------------------------------------------------


def test_the_document_with_the_key_is_never_stored(server):
    cookie = _signed_in_cookie(server)

    response = _document_for(server, cookie)

    assert _document_embeds_key(response)
    assert "no-store" in response.headers["Cache-Control"]
    assert "Cookie" in response.headers["Vary"]


def test_the_document_is_never_stored_when_no_login_is_configured(server, monkeypatch):
    """With login off the key is embedded for everybody, which is the worst case to cache."""
    _set_auth(type=None)

    response = _bare_client(server).get("/")

    assert _document_embeds_key(response)
    assert "no-store" in response.headers["Cache-Control"]
    assert "Cookie" in response.headers["Vary"]


def test_the_document_is_never_stored_under_basic_auth(server, monkeypatch):
    import base64

    _set_auth(type="basic")
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

    response = _bare_client(server).get("/", headers={"Authorization": f"Basic {token}"})

    assert _document_embeds_key(response)
    assert "no-store" in response.headers["Cache-Control"]
    assert "Cookie" in response.headers["Vary"]


def test_the_login_shell_carries_no_key(server):
    response = _bare_client(server).get("/")

    assert not _document_embeds_key(response)


def test_bundle_files_keep_their_caching(server):
    """Only the document is no-store: the hashed bundle must stay cacheable."""
    from app import ui

    asset = Path(ui.ui_bp.static_folder) / "generation-probe.js"
    existed_folder = asset.parent.exists()
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text("window.probe = 1;\n", encoding="utf-8")
    try:
        response = _bare_client(server).get("/assets/generation-probe.js")
    finally:
        asset.unlink()
        if not existed_folder:
            asset.parent.rmdir()

    assert response.status_code == 200
    assert "no-store" not in response.headers.get("Cache-Control", "")


# ---------------------------------------------------------------------------
# The API key: the credential the cookie never was
# ---------------------------------------------------------------------------


def test_the_api_key_alone_opens_the_api(server):
    response = _bare_client(server).get(STATUS_URL, headers={"X-API-KEY": API_KEY})

    assert response.status_code == 200
    assert "Set-Cookie" not in response.headers


def test_a_wrong_api_key_is_refused(server):
    response = _bare_client(server).get(STATUS_URL, headers={"X-API-KEY": API_KEY + "x"})

    assert response.status_code == 401


def test_a_missing_api_key_is_refused(server):
    assert _bare_client(server).get(STATUS_URL).status_code == 401


def test_a_signed_in_cookie_is_not_an_api_credential(server):
    """The cookie opens the UI; the API still wants the key."""
    cookie = _signed_in_cookie(server)

    response = _bare_client(server).get(STATUS_URL, headers=_with_cookie(cookie))

    assert response.status_code == 401


def test_a_wrong_key_with_a_live_cookie_is_still_refused(server):
    cookie = _signed_in_cookie(server)

    response = _bare_client(server).get(STATUS_URL, headers={**_with_cookie(cookie), "X-API-KEY": "nope"})

    assert response.status_code == 401


def test_a_redirect_and_a_refusal_do_not_carry_the_key(server, monkeypatch):
    """A 3xx or a 4xx is never an authenticated answer, and neither embeds the key."""
    _set_auth(type=None)
    client = _bare_client(server)

    redirected = client.get("/login")
    assert 300 <= redirected.status_code < 400
    assert API_KEY not in redirected.get_data(as_text=True)

    refused = client.get(STATUS_URL)
    assert refused.status_code == 401
    assert API_KEY not in refused.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The login modes keep working
# ---------------------------------------------------------------------------


def test_login_off_needs_no_cookie_and_sets_none(server, monkeypatch):
    _set_auth(type=None)
    client = _bare_client(server)

    document = client.get("/")
    status = client.get(STATUS_URL, headers={"X-API-KEY": API_KEY})

    assert _document_embeds_key(document)
    assert status.status_code == 200
    assert "Set-Cookie" not in document.headers
    assert "Set-Cookie" not in status.headers


def test_basic_auth_still_wants_its_credentials_and_ignores_the_generation(server, monkeypatch):
    import base64

    _set_auth(type="basic")
    _set_auth(session_generation=5)
    client = _bare_client(server)
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

    assert client.get("/").status_code == 401
    assert _document_embeds_key(client.get("/", headers={"Authorization": f"Basic {token}"}))
    assert client.get(STATUS_URL, headers={"X-API-KEY": API_KEY}).status_code == 200


def test_basic_auth_logout_is_still_refused_and_revokes_nothing(server, monkeypatch, writes):
    _set_auth(type="basic")

    response = _logout(_bare_client(server))

    assert response.status_code == 406
    assert _generation() == 0
    assert writes == []


def test_the_form_login_page_flow_is_unchanged(server):
    client = server.test_client()

    assert not _document_embeds_key(client.get("/"))
    assert _login(client, password="wrong").status_code == 403
    assert not _document_embeds_key(client.get("/"))
    assert _login(client).status_code == 204
    assert _document_embeds_key(client.get("/"))
    assert _logout(client).status_code == 204
    assert not _document_embeds_key(client.get("/"))


def test_a_script_using_only_the_api_key_is_unaffected_by_revocations(server):
    """Home Assistant style clients never hold a cookie, so no revocation touches them."""
    client = server.test_client()
    _login(client)
    _logout(client)

    assert _bare_client(server).get(STATUS_URL, headers={"X-API-KEY": API_KEY}).status_code == 200
    assert _bare_client(server).get(f"{STATUS_URL}?apikey={API_KEY}").status_code == 200


def _connect_gate(app, cookie, auth=None):
    from app.app import _authorize_socket_connection

    with app.test_request_context("/api/socket.io/", headers=_with_cookie(cookie) if cookie else {}):
        return _authorize_socket_connection(auth)


def test_the_socket_gate_follows_the_generation(server):
    client = server.test_client()
    _login(client)
    copy = _cookie_value(client)

    assert _connect_gate(server, copy) is True

    _logout(client)

    assert _connect_gate(server, copy) is False
    assert _connect_gate(server, copy, {"apiKey": API_KEY}) is True
    assert _connect_gate(server, None, {"apiKey": "wrong"}) is False
    assert _connect_gate(server, None) is False


# ---------------------------------------------------------------------------
# A confirmation riding in the session dies with the session
# ---------------------------------------------------------------------------


def test_anything_else_in_a_revoked_session_goes_with_it(server):
    """
    The password-confirmation window lives in the session cookie too. Logout
    must take it with the sign-in: a revoked cookie keeps none of its keys.
    """
    client = server.test_client()
    _login(client)
    with client.session_transaction() as stored:
        stored["password_confirmed_until"] = time.time() + 600
    copy = _cookie_value(client)
    _logout(client)

    assert json.loads(_bare_client(server).get("/_keys", headers=_with_cookie(copy)).get_data(as_text=True)) == []


def test_a_session_without_a_sign_in_still_works_after_a_revocation(server):
    """
    Under basic login a confirmation is the only thing in the session and no
    sign-in stamped it: it must still be kept, stamped with the current number.
    """
    client = server.test_client()
    _login(client)
    _logout(client)
    assert _generation() == 1

    fresh = server.test_client()
    fresh.get("/_confirm")

    assert fresh.get("/_confirmed").get_data(as_text=True) == "yes"
