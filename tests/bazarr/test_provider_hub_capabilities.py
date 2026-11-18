# coding=utf-8
"""Capabilities a plugin requires, and the captcha solver a worker reaches over its pipes."""

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from provider_hub import capabilities, captcha, endpoints
from provider_hub.worker import ProviderWorkerClient, WorkerError, worker_command

SOLVER_URL = "http://solver.lan:8191"


def _installation(manifest=None, trusted=False):
    return SimpleNamespace(provider_id="demo", manifest=manifest or {}, trusted=trusted)


def _settings(vendor=None, **sections):
    general = {"anti_captcha_provider": vendor}
    return {"general": general, "endpoints": {"flaresolverr_v1_url": SOLVER_URL}, **sections}


@pytest.fixture(autouse=True)
def _no_environment_address(monkeypatch):
    monkeypatch.delenv(endpoints.FLARESOLVERR_ENV, raising=False)


class TestDeclarations:
    def test_requires_and_the_older_flags_name_the_same_capabilities(self):
        assert capabilities.declared_capabilities({"requires": ["flaresolverr.v1", "host.captcha.v1"]}) == (
            "flaresolverr.v1", "host.captcha.v1")
        assert capabilities.declared_capabilities({"flaresolverr": True, "anti_captcha": True}) == (
            "flaresolverr.v1", "host.captcha.v1")

    def test_what_the_host_does_not_bind_is_left_to_its_provider(self):
        assert capabilities.declared_capabilities({"requires": ["ai_translator.backend.v1", "Bad Id", 7]}) == ()

    @pytest.mark.parametrize("manifest", [None, [], "x", {"requires": "flaresolverr.v1"}, {"flaresolverr": "yes"}])
    def test_junk_declares_nothing(self, manifest):
        assert capabilities.declared_capabilities(manifest) == ()


class TestFlareSolverrThroughRequires:
    def _deliver(self, installation, properties=None):
        config = {}
        capabilities.deliver(config, {"flaresolverr_url": {}} if properties is None else properties,
                             installation, _settings())
        return config

    def test_an_untrusted_plugin_that_requires_it_gets_the_address(self):
        config = self._deliver(_installation({"requires": ["flaresolverr.v1"]}))
        assert config == {"flaresolverr_url": SOLVER_URL}

    def test_an_untrusted_plugin_that_never_asked_gets_nothing(self):
        assert self._deliver(_installation({})) == {}

    def test_a_trusted_plugin_with_the_field_gets_it_unasked(self):
        assert self._deliver(_installation({}, trusted=True)) == {"flaresolverr_url": SOLVER_URL}

    def test_a_plugin_without_the_field_is_left_alone(self):
        assert self._deliver(_installation({"requires": ["flaresolverr.v1"]}), properties={}) == {}

    def test_a_value_the_user_set_is_kept(self):
        installation = _installation({"requires": ["flaresolverr.v1"]})
        config = {"flaresolverr_url": "http://mine:1"}
        capabilities.deliver(config, {"flaresolverr_url": {}}, installation, _settings())
        assert config["flaresolverr_url"] == "http://mine:1"

    def test_with_no_address_the_key_is_cleared_so_a_pool_drops_the_old_one(self):
        config = {}
        capabilities.deliver(config, {"flaresolverr_url": {}}, _installation({"requires": ["flaresolverr.v1"]}),
                             {"general": {}, "endpoints": {}})
        assert config == {"flaresolverr_url": ""}


class TestBindings:
    def test_a_ready_captcha_binding_comes_from_the_host(self):
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
        binding = capabilities.resolve("host.captcha.v1", _installation({"requires": ["host.captcha.v1"]}, True), settings)
        assert (binding.ready, binding.source) == (True, "host")

    def test_an_unset_service_is_reported_not_ready(self):
        binding = capabilities.resolve("host.captcha.v1", _installation({"anti_captcha": True}, True), _settings())
        assert binding.ready is False and "captcha" in binding.reason

    def test_an_untrusted_plugin_never_gets_the_solver(self):
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
        installation = _installation({"anti_captcha": True}, trusted=False)
        assert capabilities.resolve("host.captcha.v1", installation, settings).ready is False
        assert capabilities.host_call_handlers(installation) == {}

    def test_a_trusted_plugin_that_did_not_declare_it_gets_no_handler(self):
        assert capabilities.host_call_handlers(_installation({}, trusted=True)) == {}

    def test_a_declaring_trusted_plugin_gets_the_solver_handler(self):
        handlers = capabilities.host_call_handlers(_installation({"anti_captcha": True}, trusted=True))
        assert list(handlers) == ["host.captcha.v1"]

    def test_resolution_never_raises(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("settings unavailable")

        monkeypatch.setattr(endpoints, "flaresolverr_url_source", boom)
        binding = capabilities.resolve("flaresolverr.v1", _installation({"flaresolverr": True}), None)
        assert binding.ready is False

    def test_the_report_has_one_entry_per_declared_capability(self):
        report = capabilities.binding_report(_installation({"requires": ["flaresolverr.v1", "host.captcha.v1"]}, True),
                                             _settings())
        assert [item["capability"] for item in report] == ["flaresolverr.v1", "host.captcha.v1"]
        assert report[0]["ready"] is True and report[1]["ready"] is False

    def test_a_card_lists_its_capabilities_and_a_plain_one_lists_none(self):
        from provider_hub import service

        with_need = service._redact_installation({"provider_id": "demo", "manifest": {"flaresolverr": True}})
        assert with_need["capabilities"][0]["capability"] == "flaresolverr.v1"
        assert "capabilities" not in service._redact_installation({"provider_id": "demo", "manifest": {}})

    def test_the_installations_payload_carries_both_the_capabilities_and_the_restart_field(self):
        from provider_hub import service

        row = {"provider_id": "demo", "manifest": {"flaresolverr": True}, "pending_restart": True}
        served = service._redact_installation(row)
        assert served["capabilities"][0]["capability"] == "flaresolverr.v1"
        assert served["needs_restart"] is True
        settled = service._redact_installation({**row, "pending_restart": False})
        assert settled["needs_restart"] is False and settled["capabilities"]


class FakePitcher:
    calls = []
    result = "token-123"

    def __init__(self, website_name, website_url, website_key, tries=3, client_key=None, *args, **kwargs):
        FakePitcher.calls.append((website_url, website_key, client_key, kwargs.get("is_invisible")))

    def throw(self):
        if isinstance(FakePitcher.result, Exception):
            raise FakePitcher.result
        return FakePitcher.result


@pytest.fixture
def fake_vendor(monkeypatch):
    from subliminal_patch import pitcher

    FakePitcher.calls = []
    FakePitcher.result = "token-123"
    seen = []

    def get_pitcher(name=None, with_proxy=False):
        seen.append(name)
        return FakePitcher

    monkeypatch.setattr(pitcher.pitchers, "get_pitcher", get_pitcher)
    FakePitcher.seen = seen
    return FakePitcher


RECAPTCHA = {"type": "recaptcha", "site_key": "SITEKEY", "site_url": "https://example.test/login", "invisible": True}


class TestSolver:
    def test_each_vendor_choice_maps_to_its_pitcher_and_key(self):
        assert captcha.vendor_credentials(_settings("anti-captcha", anticaptcha={"anti_captcha_key": "a"})) == (
            "AntiCaptchaProxyLess", "a")
        assert captcha.vendor_credentials(_settings("captchaai", captchaai={"captchaai_key": "c"})) == (
            "CaptchaAIProxyLess", "c")
        # The account is user then password, in that order, whatever they are.
        assert captcha.vendor_credentials(_settings(
            "death-by-captcha", deathbycaptcha={"username": "zed", "password": "abc"})) == (
            "DeathByCaptchaProxyLess", "zed:abc")

    @pytest.mark.parametrize("settings", [
        _settings(None),
        _settings("anti-captcha", anticaptcha={"anti_captcha_key": ""}),
        _settings("death-by-captcha", deathbycaptcha={"username": "u", "password": ""}),
        _settings("unknown", anticaptcha={"anti_captcha_key": "a"}),
    ])
    def test_no_usable_choice_means_no_vendor(self, settings):
        assert captcha.configured_vendor(settings) is None

    def test_a_solve_returns_the_token_alone(self, fake_vendor):
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "the-key"})
        assert captcha.solve_call(dict(RECAPTCHA), 30, settings=settings) == {"response": "token-123"}
        assert fake_vendor.calls == [("https://example.test/login", "SITEKEY", "the-key", True)]
        assert fake_vendor.seen == ["AntiCaptchaProxyLess"]

    @pytest.mark.parametrize("payload,code", [
        ({"type": "image", "image_b64": "AA=="}, "unsupported"),
        ("text", "unsupported"),
        ({**RECAPTCHA, "site_key": ""}, "invalid"),
        ({**RECAPTCHA, "site_key": "k" * 600}, "invalid"),
        ({**RECAPTCHA, "site_url": "ftp://example.test"}, "invalid"),
        ({**RECAPTCHA, "site_url": 5}, "invalid"),
        ({**RECAPTCHA, "invisible": "yes"}, "invalid"),
    ])
    def test_a_malformed_request_is_refused_before_any_vendor_is_asked(self, fake_vendor, payload, code):
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
        with pytest.raises(captcha.CaptchaCallError) as raised:
            captcha.solve_call(payload, 30, settings=settings)
        assert raised.value.code == code
        assert fake_vendor.calls == []

    def test_without_a_service_set_up_the_answer_is_unavailable(self, fake_vendor):
        with pytest.raises(captcha.CaptchaCallError) as raised:
            captcha.solve_call(dict(RECAPTCHA), 30, settings=_settings(None))
        assert raised.value.code == "unavailable"

    def test_a_vendor_failure_never_carries_the_vendor_text(self, fake_vendor):
        fake_vendor.result = RuntimeError("bad key=SECRET-ACCOUNT-KEY for account 42")
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "SECRET-ACCOUNT-KEY"})
        with pytest.raises(captcha.CaptchaCallError) as raised:
            captcha.solve_call(dict(RECAPTCHA), 30, settings=settings)
        assert "SECRET" not in raised.value.message and raised.value.code == "failed"

    def test_no_token_is_a_failure(self, fake_vendor):
        fake_vendor.result = None
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
        with pytest.raises(captcha.CaptchaCallError) as raised:
            captcha.solve_call(dict(RECAPTCHA), 30, settings=settings)
        assert raised.value.code == "failed"

    def test_solves_are_limited_across_plugins(self, monkeypatch, fake_vendor):
        monkeypatch.setattr(captcha, "START_WAIT_SECONDS", 0.05)
        taken = [captcha._slots.acquire() for _ in range(captcha.MAX_CONCURRENT_SOLVES)]
        try:
            settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
            with pytest.raises(captcha.CaptchaCallError) as raised:
                captcha.solve_call(dict(RECAPTCHA), 30, settings=settings)
            assert raised.value.code == "busy" and fake_vendor.calls == []
        finally:
            for _ in taken:
                captcha._slots.release()


class StubJob:
    solution = "tok-anti"

    def __init__(self, behaviour):
        self.behaviour = behaviour

    def join(self, maximum_time=None, on_check=None, backoff=False):
        StubJob.maximum_time = maximum_time
        while self.behaviour == "hang":
            time.sleep(0.02)
            on_check(0, "processing")

    def get_solution_response(self):
        return StubJob.solution


@pytest.fixture
def vendors(monkeypatch):
    """Every vendor library replaced by a stub that records the credentials it was given."""
    from subliminal_patch import pitcher

    seen = {"anti": [], "dbc": [], "captchaai": [], "tasks": 0, "behaviour": "ok"}

    class StubAnti:
        def __init__(self, key, language_pool, host, use_ssl):
            seen["anti"].append(key)

        def createTask(self, task):
            seen["tasks"] += 1
            return StubJob(seen["behaviour"])

    class StubDBC:
        def __init__(self, username, password):
            seen["dbc"].append((username, password))

        def decode(self, timeout=None, **kwargs):
            seen["dbc_timeout"] = timeout
            return {"is_correct": True, "text": "tok-dbc"}

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    class StubSession:
        def post(self, url, data=None, timeout=None):
            seen["captchaai"].append(data["key"])
            return Response({"status": 1, "request": "job-1"})

        def get(self, url, params=None, timeout=None):
            seen["captchaai"].append(params["key"])
            return Response({"status": 1, "request": "tok-ai"})

    monkeypatch.setattr(pitcher, "AnticaptchaClient", StubAnti)
    monkeypatch.setattr(pitcher, "NoCaptchaTaskProxylessTask", lambda **kwargs: object())
    monkeypatch.setattr(pitcher, "DBCClient", StubDBC)
    monkeypatch.setattr(pitcher.requests, "Session", StubSession)
    # No real waiting between CaptchaAI polls; the live and cancelled checks stay.
    monkeypatch.setattr(pitcher.Pitcher, "_sleep", lambda self, seconds: self._check_live())
    # What the old environment path would have used: never the right answer.
    monkeypatch.setenv("ANTICAPTCHA_ACCOUNT_KEY", "ENV-KEY:ENV-PASS")
    monkeypatch.setenv("ANTICAPTCHA_CLASS", "AntiCaptchaProxyLess")
    return seen


class TestEveryVendorUsesTheHostCredentials:
    def test_anti_captcha(self, vendors):
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "host-anti-key"})
        assert captcha.solve_call(dict(RECAPTCHA), 30, settings=settings) == {"response": "tok-anti"}
        assert vendors["anti"] == ["host-anti-key"]

    def test_death_by_captcha_takes_the_account_the_host_built_not_the_environment(self, vendors):
        settings = _settings("death-by-captcha", deathbycaptcha={"username": "zed", "password": "abc"})
        assert captcha.solve_call(dict(RECAPTCHA), 30, settings=settings) == {"response": "tok-dbc"}
        assert vendors["dbc"] == [("zed", "abc")]

    def test_captchaai(self, vendors):
        settings = _settings("captchaai", captchaai={"captchaai_key": "host-ai-key"})
        assert captcha.solve_call(dict(RECAPTCHA), 30, settings=settings) == {"response": "tok-ai"}
        assert set(vendors["captchaai"]) == {"host-ai-key"}

    def test_the_built_in_providers_still_fall_back_to_the_environment(self, vendors):
        from subliminal_patch.pitcher import pitchers

        pitchers.get_pitcher("DeathByCaptchaProxyLess")("site", "https://example.test/", "key")
        assert vendors["dbc"] == []
        instance = pitchers.get_pitcher("DeathByCaptchaProxyLess")("site", "https://example.test/", "key")
        assert (instance.username, instance.password) == ("ENV-KEY", "ENV-PASS")


class TestASolveEndsWithItsRequest:
    def test_a_solve_that_runs_past_the_deadline_is_cancelled_and_gives_its_slot_back(self, vendors):
        vendors["behaviour"] = "hang"
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
        cancelled = threading.Event()
        started = time.monotonic()
        with pytest.raises(captcha.CaptchaCallError) as raised:
            captcha.solve_call(dict(RECAPTCHA), 0.4, cancelled, settings=settings)
        assert raised.value.code == "timeout" and time.monotonic() - started < 5
        assert cancelled.is_set()
        # Not retried: one paid task. The solver thread ends once it sees the cancel, and gives its slot back.
        assert vendors["tasks"] == 1
        slots = [captcha._slots.acquire(timeout=5) for _ in range(captcha.MAX_CONCURRENT_SOLVES)]
        try:
            assert all(slots)
        finally:
            for taken in slots:
                if taken:
                    captcha._slots.release()

    def test_a_solve_for_a_request_that_has_already_ended_creates_no_task(self, vendors):
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
        cancelled = threading.Event()
        cancelled.set()
        with pytest.raises(captcha.CaptchaCallError):
            captcha.solve_call(dict(RECAPTCHA), 30, cancelled, settings=settings)
        assert vendors["tasks"] == 0

    def test_a_token_that_arrives_after_the_deadline_is_not_delivered(self, monkeypatch):
        from subliminal_patch import pitcher

        class Slow:
            def __init__(self, *args, deadline=None, cancel=None, **kwargs):
                pass

            def throw(self):
                time.sleep(0.5)
                return "late-token"

        monkeypatch.setattr(pitcher.pitchers, "get_pitcher", lambda name=None, with_proxy=False: Slow)
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
        with pytest.raises(captcha.CaptchaCallError) as raised:
            captcha.solve_call(dict(RECAPTCHA), 0.2, settings=settings)
        assert raised.value.code == "timeout"

    def test_the_vendor_wait_is_cut_to_what_is_left_of_the_request(self, vendors):
        settings = _settings("death-by-captcha", deathbycaptcha={"username": "u", "password": "p"})
        captcha.solve_call(dict(RECAPTCHA), 20, settings=settings)
        assert vendors["dbc_timeout"] <= 20
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
        captcha.solve_call(dict(RECAPTCHA), 20, settings=settings)
        assert StubJob.maximum_time is not None and StubJob.maximum_time <= 20


PROVIDER = '''
import time
import bazarr_host

class CaptchaProvider:
    def search(self, video, languages, config):
        mode = config.get("mode")
        if mode == "granted":
            return [{"granted": list(bazarr_host.granted())}]
        if mode == "solve":
            token = bazarr_host.solve_recaptcha("SITEKEY", "https://example.test/", invisible=True)
            return [{"token": token}]
        if mode == "raw":
            try:
                bazarr_host.call(config["capability"], config.get("payload", {"x": 1}))
            except bazarr_host.HostCallError as error:
                return [{"code": error.code, "message": str(error)}]
            return [{"code": "none"}]
        if mode == "bulk":
            return [{"blob": "y" * config["size"]}]
        if mode == "many":
            for _ in range(40):
                bazarr_host.call("host.captcha.v1", {})
            return []
        if mode == "frame":
            # A raw call frame the way a hostile worker would write it.
            import __main__, json, sys
            call = {"id": config.get("id", "c1"), "capability": config.get("capability", "host.captcha.v1"),
                    "payload": config.get("payload", {})}
            frame = {"abi": "bazarr.provider-worker.v1", "id": __main__._current["request_id"], "call": call}
            sys.stdout.write(json.dumps(frame, separators=(",", ":")) + "\\n")
            sys.stdout.flush()
            time.sleep(60)
        if mode == "leave":
            # Return a result, leave a child that floods the shared stdout, and let the worker exit.
            import os, subprocess, sys, threading
            code = ("import sys, time\\ntime.sleep(1.5)\\n"
                    "for _ in range(24):\\n    sys.stdout.write('x' * 1048576 + '\\\\n'); sys.stdout.flush()\\n"
                    "time.sleep(120)")
            child = subprocess.Popen([sys.executable, "-c", code])
            threading.Timer(0.8, os._exit, [0]).start()
            return [{"child": child.pid}]
        if mode == "noise":
            # A valid call, then output as fast as the pipe takes it while the host is busy answering.
            import __main__, json, sys
            call = {"id": "c1", "capability": "host.captcha.v1", "payload": {}}
            frame = {"abi": "bazarr.provider-worker.v1", "id": __main__._current["request_id"], "call": call}
            sys.stdout.write(json.dumps(frame, separators=(",", ":")) + "\\n")
            sys.stdout.flush()
            line = "x" * (1024 * 1024) + "\\n"
            for _ in range(400):
                sys.stdout.write(line)
                sys.stdout.flush()
            time.sleep(60)
        if mode == "flood":
            # Valid calls, never reading an answer.
            import __main__, json, sys
            for number in range(16):
                call = {"id": "c%02d" % number, "capability": "host.captcha.v1", "payload": {}}
                frame = {"abi": "bazarr.provider-worker.v1", "id": __main__._current["request_id"], "call": call}
                sys.stdout.write(json.dumps(frame, separators=(",", ":")) + "\\n")
            sys.stdout.flush()
            time.sleep(60)
        if mode == "then-search":
            bazarr_host.solve_recaptcha("SITEKEY", "https://example.test/")
            time.sleep(60)
        return []

    def download(self, provider_payload, language, config):
        return None
'''


def _worker(tmp_path, host_calls):
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "provider.py").write_text(PROVIDER, encoding="utf-8")
    runner = Path(__file__).parents[2] / "bazarr/provider_hub/worker_runner.py"
    return ProviderWorkerClient(
        worker_command(sys.executable, runner),
        cwd=bundle,
        env={
            "BAZARR_PROVIDER_HUB_BUNDLE": str(bundle),
            "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps({"entry_module": "provider", "entry_class": "CaptchaProvider"}),
            "BAZARR_HOST_CALLS": json.dumps(sorted(host_calls)),
        },
        host_calls=host_calls,
    )


@pytest.fixture
def worker(tmp_path):
    made = []

    def build(host_calls):
        client = _worker(tmp_path, host_calls)
        made.append(client)
        return client

    yield build
    for client in made:
        client.stop()


def _search(client, config, timeout=20):
    return client.request("search", {"config": config}, timeout=timeout)


class TestHostCallsOverThePipes:
    def test_a_solve_reaches_the_handler_and_the_token_comes_back(self, worker):
        seen = []

        def handler(payload, timeout, cancelled):
            seen.append((payload, timeout))
            return {"response": "tok"}

        result = _search(worker({"host.captcha.v1": handler}), {"mode": "solve"})
        assert result.payload["candidates"] == [{"token": "tok"}]
        assert seen[0][0] == {"type": "recaptcha", "site_key": "SITEKEY", "site_url": "https://example.test/",
                              "invisible": True}
        assert 0 < seen[0][1] <= 20

    def test_the_worker_is_told_what_it_may_call(self, worker):
        client = worker({"host.captcha.v1": lambda payload, timeout, cancelled: {}})
        assert _search(client, {"mode": "granted"}).payload["candidates"] == [{"granted": ["host.captcha.v1"]}]

    def test_a_capability_that_was_not_granted_is_denied(self, worker):
        result = _search(worker({"host.captcha.v1": lambda payload, timeout, cancelled: {}}),
                         {"mode": "raw", "capability": "host.other.v1"})
        assert result.payload["candidates"][0]["code"] == "denied"

    def test_a_worker_with_no_grant_gets_a_denial_for_the_solver(self, worker):
        result = _search(worker({}), {"mode": "raw", "capability": "host.captcha.v1"})
        assert result.payload["candidates"][0]["code"] == "denied"

    def test_a_refusal_reaches_the_plugin_with_its_code_and_a_safe_message(self, worker):
        def handler(payload, timeout, cancelled):
            raise captcha.CaptchaCallError("unavailable", "No captcha service is set up in Bazarr+.")

        result = _search(worker({"host.captcha.v1": handler}), {"mode": "raw", "capability": "host.captcha.v1"})
        assert result.payload["candidates"] == [
            {"code": "unavailable", "message": "No captcha service is set up in Bazarr+."}]

    def test_an_unexpected_error_is_a_plain_failure_with_no_detail(self, worker):
        def handler(payload, timeout, cancelled):
            raise RuntimeError("secret detail")

        result = _search(worker({"host.captcha.v1": handler}), {"mode": "raw", "capability": "host.captcha.v1"})
        candidate = result.payload["candidates"][0]
        assert candidate["code"] == "failed" and "secret" not in candidate["message"]

    def test_a_runaway_worker_is_ended_after_the_call_limit(self, worker):
        client = worker({"host.captcha.v1": lambda payload, timeout, cancelled: {"response": "t"}})
        with pytest.raises(WorkerError, match="too many host calls"):
            _search(client, {"mode": "many"})

    def test_a_handler_that_never_answers_cannot_hold_the_request_past_its_deadline(self, worker):
        release = threading.Event()

        def handler(payload, timeout, cancelled):
            release.wait(30)
            return {"response": "late"}

        client = worker({"host.captcha.v1": handler})
        started = time.monotonic()
        try:
            with pytest.raises(WorkerError):
                _search(client, {"mode": "solve"}, timeout=2)
        finally:
            release.set()
        assert time.monotonic() - started < 12

    def test_the_worker_is_still_usable_after_a_call(self, worker):
        client = worker({"host.captcha.v1": lambda payload, timeout, cancelled: {"response": "t"}})
        assert _search(client, {"mode": "solve"}).payload["candidates"] == [{"token": "t"}]
        assert _search(client, {"mode": "solve"}).payload["candidates"] == [{"token": "t"}]


class TestCallFrameBounds:
    @pytest.mark.parametrize("config", [
        {"id": "x" * (2 * 1024 * 1024)},
        {"id": "x" * 65},
        {"id": ""},
        {"capability": "c" * 129},
        {"capability": ""},
        {"payload": {"k": "v" * (70 * 1024)}},
        {"payload": "not an object"},
    ])
    def test_an_out_of_bounds_call_ends_the_worker_at_once(self, worker, config):
        answered = []
        client = worker({"host.captcha.v1": lambda payload, timeout, cancelled: answered.append(1) or {}})
        started = time.monotonic()
        with pytest.raises(WorkerError, match="malformed host call"):
            _search(client, {"mode": "frame", **config}, timeout=30)
        assert time.monotonic() - started < 10
        assert answered == []
        assert client._lock.acquire(timeout=1)
        client._lock.release()


class TestAnswerWrite:
    def test_a_worker_that_stops_reading_is_ended_and_the_lock_is_given_back(self, worker, monkeypatch):
        from provider_hub import worker as worker_module

        monkeypatch.setattr(worker_module, "_HOST_REPLY_WRITE_SECONDS", 1.0)
        big = {"response": "t" * (30 * 1024)}
        client = worker({"host.captcha.v1": lambda payload, timeout, cancelled: big})
        started = time.monotonic()
        with pytest.raises(WorkerError, match="did not read"):
            _search(client, {"mode": "flood"}, timeout=60)
        assert time.monotonic() - started < 15
        assert client._lock.acquire(timeout=1)
        client._lock.release()

    def test_an_answer_too_large_to_send_becomes_a_plain_failure(self, worker):
        huge = {"response": "t" * (64 * 1024)}
        result = _search(worker({"host.captcha.v1": lambda payload, timeout, cancelled: huge}),
                         {"mode": "raw", "capability": "host.captcha.v1"})
        assert result.payload["candidates"][0]["code"] == "failed"

    def test_the_handler_is_told_when_the_request_ends(self, worker):
        seen = {}

        def handler(payload, timeout, cancelled):
            seen["cancelled"] = cancelled
            cancelled.wait(20)
            return {"response": "late"}

        client = worker({"host.captcha.v1": handler})
        with pytest.raises(WorkerError):
            _search(client, {"mode": "solve"}, timeout=1.5)
        assert seen["cancelled"].wait(5)


class TestOutputDuringACall:
    def test_a_worker_flooding_output_while_a_call_is_pending_is_held_to_the_bound(self, worker):
        from provider_hub import worker as worker_module

        release = threading.Event()
        entered = threading.Event()

        def handler(payload, timeout, cancelled):
            entered.set()
            release.wait(30)
            return {"response": "t"}

        client = worker({"host.captcha.v1": handler})
        errors = []

        def request():
            try:
                _search(client, {"mode": "noise"}, timeout=40)
            except WorkerError as error:
                errors.append(error)

        thread = threading.Thread(target=request, daemon=True)
        thread.start()
        try:
            assert entered.wait(15)
            # Give the worker time to write far more than the bound (400 MB offered) if it could.
            peak = 0
            for _ in range(15):
                time.sleep(0.2)
                peak = max(peak, client._stdout_queue.queued_bytes)
            # The bound is 8 MiB held plus the chunk being read; unbounded, this run reaches 400 MiB.
            assert worker_module._STDOUT_QUEUE_MAX_BYTES <= 8 * 1024 * 1024
            assert 0 < peak <= 16 * 1024 * 1024
        finally:
            release.set()
            client.stop()
            thread.join(20)

    def test_a_reply_larger_than_the_bound_still_arrives_whole(self, worker):
        client = worker({})
        big = "y" * (20 * 1024 * 1024)
        result = client.request("search", {"config": {"mode": "bulk", "size": len(big)}}, timeout=30)
        assert len(result.payload["candidates"][0]["blob"]) == len(big)


class TestAWorkerThatLeavesAChildBehind:
    def test_the_child_is_cleaned_up_though_the_reader_was_waiting_on_a_full_queue(self, worker):
        import os

        client = worker({})
        result = _search(client, {"mode": "leave"}, timeout=30)
        child = result.payload["candidates"][0]["child"]
        # The worker exits and its child fills the pipe, so the reader waits with the queue full.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and client._stdout_queue.queued_bytes < 4 * 1024 * 1024:
            time.sleep(0.1)
        time.sleep(1.0)  # several of the reader's liveness checks pass
        os.kill(child, 0)  # still there: nothing reaped the worker and cleaned up behind its back
        client.stop()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            try:
                if os.waitpid(child, os.WNOHANG) != (0, 0):
                    return
            except ChildProcessError:
                pass
            time.sleep(0.1)
        pytest.fail("the worker's child was left running")


class TestBoundedChunks:
    def test_put_waits_past_the_bound_and_goes_on_once_the_consumer_reads(self):
        from provider_hub.worker import _BoundedChunks

        chunks = _BoundedChunks(max_bytes=10, max_chunks=3)
        chunks.put("aaaaaa")
        released = threading.Event()

        def producer():
            chunks.put("bbbbbb")
            released.set()

        threading.Thread(target=producer, daemon=True).start()
        assert not released.wait(0.5)
        assert chunks.get(1) == "aaaaaa"
        assert released.wait(2)
        assert chunks.queued_bytes == 6

    def test_markers_are_never_held_back(self):
        from provider_hub.worker import _BoundedChunks

        chunks = _BoundedChunks(max_bytes=1, max_chunks=1)
        chunks.put("aa")
        chunks.put(None)
        assert chunks.get(1) == "aa" and chunks.get(1) is None

    def test_a_waiting_put_gives_up_when_the_process_is_gone(self):
        from provider_hub.worker import _BoundedChunks

        chunks = _BoundedChunks(max_bytes=1, max_chunks=1)
        chunks.put("a")
        done = threading.Event()
        threading.Thread(target=lambda: (chunks.put("b", alive=lambda: False), done.set()), daemon=True).start()
        assert done.wait(2)


class TestLiveVendorCallsAreCapped:
    def test_timeouts_never_mean_more_than_the_cap_of_live_solves(self, monkeypatch):
        from subliminal_patch import pitcher

        release = threading.Event()
        live = {"now": 0, "peak": 0}
        guard = threading.Lock()

        class Blocking:
            """Blocked in a vendor call that cancelling cannot interrupt."""

            def __init__(self, *args, **kwargs):
                pass

            def throw(self):
                with guard:
                    live["now"] += 1
                    live["peak"] = max(live["peak"], live["now"])
                try:
                    release.wait(30)
                finally:
                    with guard:
                        live["now"] -= 1
                return None

        monkeypatch.setattr(pitcher.pitchers, "get_pitcher", lambda name=None, with_proxy=False: Blocking)
        monkeypatch.setattr(captcha, "START_WAIT_SECONDS", 0.1)
        settings = _settings("anti-captcha", anticaptcha={"anti_captcha_key": "k"})
        codes = []
        try:
            for _ in range(4):
                with pytest.raises(captcha.CaptchaCallError) as raised:
                    captcha.solve_call(dict(RECAPTCHA), 0.3, settings=settings)
                codes.append(raised.value.code)
            # Two solves are still blocked, so the next two requests were refused rather than started.
            assert codes == ["timeout", "timeout", "busy", "busy"]
            assert live["peak"] == captcha.MAX_CONCURRENT_SOLVES == 2
        finally:
            release.set()
        # Once the vendor calls really end, the slots come back.
        slots = [captcha._slots.acquire(timeout=5) for _ in range(captcha.MAX_CONCURRENT_SOLVES)]
        try:
            assert all(slots) and live["now"] == 0
        finally:
            for taken in slots:
                if taken:
                    captcha._slots.release()


class TestTrustWithdrawnMidSession:
    def _handler(self, monkeypatch, state):
        monkeypatch.setattr(capabilities, "live_trust", lambda provider_id: state["trusted"])
        installation = SimpleNamespace(provider_id="demo", manifest={"anti_captcha": True}, trusted=True)
        return capabilities.host_call_handlers(installation)["host.captcha.v1"]

    def test_a_demotion_stops_the_next_paid_call(self, monkeypatch, fake_vendor):
        monkeypatch.setattr(captcha, "vendor_credentials", lambda settings=None: ("AntiCaptchaProxyLess", "k"))
        state = {"trusted": True}
        handler = self._handler(monkeypatch, state)
        cancelled = threading.Event()
        assert handler(dict(RECAPTCHA), 30, cancelled) == {"response": "token-123"}
        state["trusted"] = False
        with pytest.raises(captcha.CaptchaCallError) as raised:
            handler(dict(RECAPTCHA), 30, cancelled)
        assert raised.value.code == "denied"
        assert len(fake_vendor.calls) == 1

    def test_a_demotion_during_the_wait_for_a_slot_still_stops_the_call(self, monkeypatch, fake_vendor):
        monkeypatch.setattr(captcha, "vendor_credentials", lambda settings=None: ("AntiCaptchaProxyLess", "k"))
        flips = iter([True, False])
        with pytest.raises(captcha.CaptchaCallError) as raised:
            captcha.solve_call(dict(RECAPTCHA), 30, allowed=lambda: next(flips))
        assert raised.value.code == "denied" and fake_vendor.calls == []

    def test_an_unreadable_record_is_not_trust(self, monkeypatch):
        from provider_hub import state

        def boom(*args, **kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(state, "load_state", boom)
        assert capabilities.live_trust("demo") is False

    def test_a_stored_untrusted_row_is_not_trust(self, monkeypatch):
        from provider_hub import state

        rows = {"installations": {"demo": {"trusted": False}, "ok": {"trusted": True}}}
        monkeypatch.setattr(state, "load_state", lambda *a, **k: rows)
        assert capabilities.live_trust("demo") is False
        assert capabilities.live_trust("ok") is True
        assert capabilities.live_trust("gone") is False

    def test_a_running_worker_loses_its_grant_when_the_refresh_demotes_it(self, monkeypatch, worker):
        import provider_hub.registry as registry

        client = worker({"host.captcha.v1": lambda payload, timeout, cancelled: {"response": "t"}})
        registry._HOST_CALL_CLIENTS["demo"] = __import__("weakref").WeakSet([client])
        try:
            monkeypatch.setattr(capabilities, "live_trust", lambda provider_id: True)
            registry.revoke_untrusted_host_calls()
            assert client._host_calls  # still trusted: kept
            monkeypatch.setattr(capabilities, "live_trust", lambda provider_id: False)
            registry.revoke_untrusted_host_calls()
            assert client._host_calls == {}
            result = _search(client, {"mode": "raw", "capability": "host.captcha.v1"})
            assert result.payload["candidates"][0]["code"] == "denied"
        finally:
            registry._HOST_CALL_CLIENTS.pop("demo", None)


class TestSpawnGrant:
    def _proxy(self, monkeypatch, trusted, manifest):
        import provider_hub.registry as registry

        captured = {}

        class FakeClient:
            def __init__(self, command, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(registry, "ProviderWorkerClient", FakeClient)
        monkeypatch.setattr(capabilities, "live_trust", lambda provider_id: trusted)
        provider_class = type("DemoHubProvider", (registry.HubProxyProvider,), {
            "provider_name": "demo",
            "bundle_path": "/tmp/bundle",
            "manifest_json": json.dumps(manifest),
            "manifest": SimpleNamespace(raw=manifest),
        })
        provider_class()._worker()
        return captured

    def test_a_trusted_declaring_plugin_is_spawned_with_the_solver(self, monkeypatch):
        captured = self._proxy(monkeypatch, True, {"anti_captcha": True})
        assert list(captured["host_calls"]) == ["host.captcha.v1"]
        assert json.loads(captured["env"]["BAZARR_HOST_CALLS"]) == ["host.captcha.v1"]

    def test_an_untrusted_one_is_spawned_with_nothing(self, monkeypatch):
        captured = self._proxy(monkeypatch, False, {"anti_captcha": True})
        assert captured["host_calls"] == {}
        assert json.loads(captured["env"]["BAZARR_HOST_CALLS"]) == []
