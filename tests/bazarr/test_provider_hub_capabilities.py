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
        # Not retried: one paid task, and both slots are free again.
        assert vendors["tasks"] == 1
        slots = [captcha._slots.acquire(blocking=False) for _ in range(captcha.MAX_CONCURRENT_SOLVES)]
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


class TestSpawnGrant:
    def _proxy(self, monkeypatch, trusted, manifest):
        import provider_hub.registry as registry

        captured = {}

        class FakeClient:
            def __init__(self, command, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(registry, "ProviderWorkerClient", FakeClient)
        provider_class = type("DemoHubProvider", (registry.HubProxyProvider,), {
            "provider_name": "demo",
            "bundle_path": "/tmp/bundle",
            "manifest_json": json.dumps(manifest),
            "manifest": SimpleNamespace(raw=manifest),
            "installation_trusted": trusted,
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
