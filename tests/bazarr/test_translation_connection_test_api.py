"""The connection test: one small request to a back end as it is saved, under the rules a save keeps.

A key reaches only the address it was entered under, a response and a log never carry one, a refused or
unanswered test is a 200 with ``ok: false`` and a sentence that says what to do, and a priced test is
reserved against the cost caps and settled in the usage ledger.
"""
import json
import logging

import pytest
from flask import Blueprint, Flask
from flask_restx import Api

from plugin_platform_helpers import service_manifest
from subtitles.tools.translate.runtime.settings_service import (InstalledPlugin, ProbeRequest,
                                                                TranslatorSettingsService)
from subtitles.tools.translate.runtime.store import OFFICIAL_ENGINE_PLUGIN_ID, TranslatorStore
from subtitles.tools.translate.runtime.usage import CostLimits, UsageLedger

API_KEY = "translation-test-route-key"  # pragma: allowlist secret
SECRET = "sk-live-0123456789abcdef-WXYZ"  # pragma: allowlist secret
LIST_KEYS = ["gem-key-one-0123456789", "gem-key-two-0123456789"]  # pragma: allowlist secret
PLUGIN = OFFICIAL_ENGINE_PLUGIN_ID
LAN = "http://192.168.1.20:1234/v1"
LAN_ORIGIN = "http://192.168.1.20:1234"


def _manifest():
    return service_manifest(
        PLUGIN, settings_version=2, kind="translator", kind_api="bazarr.translator.v1",
        settings={
            "fields": [{"key": "concurrency", "type": "integer", "min": 1, "max": 8, "default": 2}],
            "backend_types": {
                "openrouter": {"label": "OpenRouter", "endpoints": ["https://openrouter.ai:443"],
                               "fields": [{"key": "api_key", "type": "secret", "required": True}]},
                "gemini": {"label": "Gemini", "endpoints": ["https://generativelanguage.googleapis.com:443"],
                           "fields": [{"key": "api_keys", "type": "key_list", "secret": True}]},
                "openai_compatible": {"label": "OpenAI compatible", "fields": [
                    {"key": "base_url", "type": "text", "destination": True, "default": "https://api.openai.com/v1"},
                    {"key": "api_key", "type": "secret"}]},
            }})


class MemoryPointers:
    def __init__(self):
        self.engine = self.fallback = None

    def get(self):
        return self.engine, self.fallback

    def set(self, engine, fallback):
        self.engine, self.fallback = engine, fallback
        return True


@pytest.fixture
def world(monkeypatch, schema_session):
    from subtitles.tools.translate import runtime

    import api.translation.translation as module
    from api import api_bp
    from app.config import settings

    monkeypatch.setattr(settings.auth, "apikey", API_KEY)
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: True)
    plugins = {PLUGIN: InstalledPlugin(PLUGIN, "AI Subtitle Translator", "2.2.0", True, _manifest())}
    store = TranslatorStore(schema_session, pointers=MemoryPointers(), plugin_active=lambda pid: pid in plugins)

    class World:
        pass

    w = World()
    w.session, w.store, w.plugins, w.runtime = schema_session, store, plugins, runtime
    w.requests = []
    w.hooks = {}
    w.limits = CostLimits()

    def service():
        return TranslatorSettingsService(store, plugin_lookup=plugins.get, catalog_engines=lambda: [],
                                         update_lookup=lambda plugin_id: None, cost_limits=lambda: w.limits,
                                         **w.hooks)

    monkeypatch.setattr(module, "get_service", service)
    blueprint = Blueprint("translation_test_route", __name__, url_prefix="/api")
    Api(blueprint, authorizations={"apikey": {"type": "apiKey", "in": "header", "name": "X-API-KEY"}},
        security="apikey").add_namespace(module.api_ns_translation, "/")
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.register_blueprint(api_bp)
    client = app.test_client()
    w.instance = store.create_instance(PLUGIN, name="AI", settings_version=2, plugin_version="2.2.0")

    def call(method, path, body=None, key=True, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body))
        response = getattr(client, method)("/api/translation/" + path, headers={"X-API-KEY": API_KEY} if key else {},
                                           data=data, content_type="application/json")
        w.last_text = response.get_data(as_text=True)
        return response

    def probe(answer):
        """Bind a probe that records what it was asked and answers ``answer`` (or raises it)."""
        def run(request):
            w.requests.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer(request) if callable(answer) else answer

        w.hooks["connection_probe"] = run

    def connection(backend_type="openai_compatible", values=None, accept=(), **extra):
        body = {"backend_type": backend_type, "values": values or {}, "accept_destinations": list(accept), **extra}
        response = call("post", f"engines/{w.instance.id}/connections", body)
        assert response.status_code == 201, response.json
        return response.json["connection"]["id"]

    def lan_connection(**values):
        return connection(values={"base_url": LAN, "api_key": SECRET, "model": "qwen", **values}, accept=[LAN])

    w.call, w.probe, w.connection, w.lan_connection = call, probe, connection, lan_connection
    return w


def _calls(world):
    from app.database import TableTranslationBudget, TableTranslationUsage

    return (world.session.query(TableTranslationUsage).all(), world.session.query(TableTranslationBudget).all())


def _nothing_leaked(world, caplog):
    for secret in (SECRET, *LIST_KEYS):
        assert secret not in world.last_text
        assert secret not in caplog.text


# ------------------------------------------------------------------- the switch

def test_the_route_is_404_while_the_switch_is_off_and_nothing_is_probed(world, monkeypatch):
    world.probe({"ok": True})
    connection_id = world.lan_connection()
    monkeypatch.setattr(world.runtime, "runtime_v2_enabled", lambda: False)
    assert world.call("post", f"connections/{connection_id}/test", {}).status_code == 404
    assert world.call("post", f"connections/{connection_id}/test", {}, key=False).status_code == 404
    assert world.requests == []


def test_the_route_needs_the_api_key(world):
    world.probe({"ok": True})
    connection_id = world.lan_connection()
    assert world.call("post", f"connections/{connection_id}/test", {}, key=False).status_code == 401
    assert world.requests == []


def test_an_unknown_connection_is_404_in_the_error_shape(world):
    world.probe({"ok": True})
    response = world.call("post", "connections/999/test", {})
    assert response.status_code == 404 and response.json["error"] == "not_found" and response.json["message"]


def test_a_back_end_whose_plugin_is_gone_is_409_plugin_unavailable(world):
    world.probe({"ok": True})
    connection_id = world.lan_connection()
    del world.plugins[PLUGIN]
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 409 and response.json["error"] == "plugin_unavailable" and response.json["message"]
    assert world.requests == []


def test_with_no_engine_to_ask_the_route_is_409_and_the_capability_is_not_listed(world):
    connection_id = world.lan_connection()
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 409 and response.json["error"] == "plugin_unavailable"
    assert "test" not in world.call("get", "state").json["capabilities"]


@pytest.mark.parametrize("hook", ["connection_probe", "options_source"])
def test_the_capability_is_listed_once_something_can_answer_a_test(world, hook):
    world.hooks[hook] = lambda *args: {"ok": True, "items": []}
    assert world.call("get", "state").json["capabilities"] == ["summary", "limits", "test", "queue"]


# ------------------------------------------------------------------ the request

@pytest.mark.parametrize("body", [{"values": {"model": "x"}}, {"base_url": LAN}, [1], "text"])
def test_a_draft_is_refused_because_a_test_uses_what_is_saved(world, body):
    world.probe({"ok": True})
    connection_id = world.lan_connection()
    response = world.call("post", f"connections/{connection_id}/test", body)
    assert response.status_code == 400 and response.json["error"] == "invalid"
    assert world.requests == []


@pytest.mark.parametrize("raw", [None, "", "{}"])
def test_an_empty_body_is_the_request(world, raw):
    world.probe({"ok": True})
    connection_id = world.lan_connection()
    response = world.call("post", f"connections/{connection_id}/test", raw=raw)
    assert response.status_code == 200 and response.json["ok"] is True


# ------------------------------------------------------------------- the answer

def test_a_back_end_that_answers_is_ok_with_its_latency_and_the_key_it_was_given(world, caplog):
    world.probe({"ok": True, "input_tokens": 0, "output_tokens": 0})
    connection_id = world.lan_connection()
    caplog.set_level(logging.DEBUG)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200
    assert response.json["ok"] is True and response.json["message"] == ""
    assert isinstance(response.json["latency_ms"], int) and response.json["latency_ms"] >= 0
    (asked,) = world.requests
    assert isinstance(asked, ProbeRequest)
    assert (asked.backend_type, asked.model, asked.connection_id) == ("openai_compatible", "qwen", connection_id)
    assert asked.secrets == {"api_key": SECRET}
    assert asked.destinations == frozenset({LAN})
    assert asked.values == {"base_url": LAN}
    assert (asked.input_token_limit, asked.output_token_limit) == (64, 16)
    _nothing_leaked(world, caplog)
    assert SECRET not in repr(asked)


def test_a_key_list_reaches_the_probe_whole_and_is_masked_in_what_comes_back(world, caplog):
    world.probe(lambda request: {"ok": False, "message": f"Rejected {request.secrets['api_keys']}"})
    connection_id = world.connection("gemini", {"api_keys": LIST_KEYS, "model": "gemini-x"})
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    assert json.loads(world.requests[0].secrets["api_keys"]) == LIST_KEYS
    assert "***" in response.json["message"]
    _nothing_leaked(world, caplog)


def test_a_back_end_that_does_not_answer_says_what_failed_and_what_to_do(world, caplog):
    world.probe({"ok": False, "message": f"No answer from {LAN_ORIGIN}. Check that the server is running."})
    connection_id = world.lan_connection()
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200
    assert response.json == {"ok": False, "message": f"No answer from {LAN_ORIGIN}. Check that the server is running.",
                             "latency_ms": None}
    _nothing_leaked(world, caplog)


def test_a_failure_with_no_message_gets_one_that_names_the_address_and_never_the_path_or_key(world, caplog):
    world.probe({"ok": False})
    connection_id = world.lan_connection()
    message = world.call("post", f"connections/{connection_id}/test", {}).json["message"]
    assert message.startswith(f"No answer at {LAN_ORIGIN}.")
    assert "running" in message and "reachable" in message
    assert "/v1" not in message
    _nothing_leaked(world, caplog)


def test_a_probe_message_is_one_short_line_with_every_key_and_credential_masked(world, caplog):
    world.probe({"ok": False, "message": f"401 for key {SECRET}\n\n  retry later\t" + "x" * 1000})
    connection_id = world.lan_connection()
    message = world.call("post", f"connections/{connection_id}/test", {}).json["message"]
    assert SECRET not in message and "***" in message
    assert "\n" not in message and "\t" not in message and "  " not in message
    assert len(message) <= 300


def test_a_short_saved_secret_is_masked_as_a_whole_word_and_never_inside_another_word(world):
    world.probe({"ok": False, "message": "bad key abc, a cabinet is not a key"})
    connection_id = world.connection(values={"base_url": LAN, "api_key": "abc", "model": "m"}, accept=[LAN])
    message = world.call("post", f"connections/{connection_id}/test", {}).json["message"]
    assert message == "bad key ***, a cabinet is not a key"


def test_a_secret_is_masked_before_whitespace_is_collapsed_so_it_cannot_hide_in_a_gap(world):
    spaced = "sk-first-part  second-part"
    world.probe({"ok": False, "message": f"rejected {spaced} today"})
    connection_id = world.connection(values={"base_url": LAN, "api_key": spaced, "model": "m"}, accept=[LAN])
    message = world.call("post", f"connections/{connection_id}/test", {}).json["message"]
    assert message == "rejected *** today"


def test_every_saved_secret_is_masked_not_only_the_ones_the_probe_was_given():
    held = {"api_key": "xy", "extra": json.dumps(["first-key-0123", "ab"])}
    said = TranslatorSettingsService._said("xy and first-key-0123 and ab and xyz", held)
    assert said == "*** and *** and *** and xyz"


def test_a_probe_that_raises_never_puts_its_text_in_the_answer_or_the_log(world, caplog):
    world.probe(RuntimeError(f"boom with {SECRET} at {LAN}"))
    connection_id = world.lan_connection()
    caplog.set_level(logging.DEBUG)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    assert "Bazarr+ log" in response.json["message"]
    _nothing_leaked(world, caplog)
    assert "192.168.1.20" not in response.get_data(as_text=True)


# --------------------------------------------------- destination and key rules

def test_a_key_is_not_sent_to_an_address_it_was_not_entered_under(world, caplog):
    world.probe({"ok": True})
    connection_id = world.lan_connection()
    other = "http://10.0.0.9:9999/v1"
    changed = world.call("patch", f"connections/{connection_id}", {
        "expected_revision": world.store.get_connection(connection_id).revision,
        "values": {"base_url": other}, "accept_destinations": [other]})
    assert changed.status_code == 200 and changed.json["connection"]["awaiting_secrets"] is True
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    assert "Enter the key again" in response.json["message"]
    assert world.requests == []
    _nothing_leaked(world, caplog)


def test_a_saved_address_that_is_not_a_clean_one_is_refused_before_anything_is_sent(world, caplog):
    world.probe({"ok": True})
    connection_id = world.lan_connection()
    row = world.store.get_connection(connection_id)
    row.settings_json = json.dumps({"base_url": "http://admin:hunter2@192.168.1.20:1234/v1"})
    world.session.commit()
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    assert "not a valid web address" in response.json["message"]
    assert "hunter2" not in world.last_text and "hunter2" not in caplog.text
    assert world.requests == []


def test_a_secret_bound_to_other_addresses_is_left_out_of_the_probe_request(world, caplog):
    world.probe({"ok": True})
    connection_id = world.connection(values={"api_key": SECRET, "model": "m"})  # the manifest default address
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is True
    assert world.requests[0].destinations == frozenset({"https://api.openai.com/v1"})
    assert world.requests[0].secrets == {"api_key": SECRET}
    # The stored binding no longer covers the request's destinations: the key is not handed out.
    from app.database import TableTranslatorSecret

    row = world.session.query(TableTranslatorSecret).one()
    row.destinations_json = json.dumps(["https://elsewhere.example"])
    world.session.commit()
    world.requests.clear()
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.json["ok"] is False and world.requests == []
    _nothing_leaked(world, caplog)


# -------------------------------------------------------------- cost and the ledger

def _priced(world, price_in=3.0, price_out=15.0):
    connection_id = world.lan_connection(price_input_per_million=price_in, price_output_per_million=price_out)
    return connection_id


def test_a_free_test_writes_nothing_to_the_ledger(world):
    world.probe({"ok": True})
    connection_id = world.lan_connection()
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is True
    assert _calls(world) == ([], [])


def test_a_priced_test_is_settled_at_what_it_used_and_its_budget_is_given_back(world):
    world.probe({"ok": True, "input_tokens": 10, "output_tokens": 2})
    connection_id = _priced(world)
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is True
    usage, budgets = _calls(world)
    (call,) = usage
    # 10 input tokens at 3 and 2 output tokens at 15, in micro-dollars.
    assert (call.state, call.actual_micros, call.worst_case_micros) == ("settled", 60, 432)
    assert (call.input_tokens, call.output_tokens, call.connection_id) == (10, 2, connection_id)
    assert [item.state for item in budgets] == ["released"]
    assert UsageLedger(world.session).month_usage()[1] == 60


def test_a_priced_test_that_reports_no_tokens_is_charged_at_its_worst_case(world):
    world.probe({"ok": True})
    connection_id = _priced(world)
    world.call("post", f"connections/{connection_id}/test", {})
    (call,), _ = _calls(world)
    assert call.actual_micros == call.worst_case_micros == 432


def test_a_priced_test_that_failed_keeps_its_worst_case_charge_because_the_provider_may_have_billed(world):
    world.probe({"ok": False, "message": "refused"})
    connection_id = _priced(world)
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is False
    (call,), (budget,) = _calls(world)
    assert (call.actual_micros, call.state, budget.state) == (432, "settled", "released")


def test_a_priced_test_that_never_left_is_the_one_failure_settled_at_nothing(world):
    world.probe({"ok": False, "sent": False, "message": "connection refused"})
    connection_id = _priced(world)
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is False
    (call,), (budget,) = _calls(world)
    assert (call.actual_micros, call.input_tokens, call.output_tokens, budget.state) == (0, 0, 0, "released")


def test_a_priced_test_whose_probe_raised_keeps_its_worst_case_charge_and_is_released(world, caplog):
    world.probe(RuntimeError(SECRET))
    connection_id = _priced(world)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.json["ok"] is False
    (call,), (budget,) = _calls(world)
    assert (call.actual_micros, budget.state) == (432, "released")
    _nothing_leaked(world, caplog)


def test_the_hold_is_released_even_when_settling_fails(world, monkeypatch):
    world.probe({"ok": True, "input_tokens": 1, "output_tokens": 1})
    connection_id = _priced(world)

    def broken(self, *args, **kwargs):
        raise RuntimeError("ledger gone")

    monkeypatch.setattr(UsageLedger, "settle_call", broken)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    usage, budgets = _calls(world)
    # The call was never settled, so it keeps counting at its worst case, and the budget is given back.
    assert [(item.state, item.actual_micros) for item in usage] == [("reserved", None)]
    assert [item.state for item in budgets] == ["released"]


def test_a_priced_test_is_refused_when_the_month_cap_is_used_up_and_nothing_is_sent(world):
    world.probe({"ok": True})
    world.limits = CostLimits(month_cap=0, translation_limit=1)
    connection_id = _priced(world)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    assert "month" in response.json["message"] and "no test was sent" in response.json["message"]
    assert world.requests == []
    assert _calls(world)[0] == []


def test_a_priced_test_is_refused_when_the_job_cap_cannot_cover_it(world):
    world.probe({"ok": True})
    world.limits = CostLimits(job_cap=100, translation_limit=1)  # 100 micro-dollars, the test may cost 432
    connection_id = _priced(world)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.json["ok"] is False and "limit" in response.json["message"]
    assert world.requests == []
    usage, budgets = _calls(world)
    assert usage == [] and [item.state for item in budgets] == ["released"]


def test_a_free_back_end_is_still_tested_on_a_spent_month(world):
    world.probe({"ok": True})
    world.limits = CostLimits(month_cap=0, job_cap=0, translation_limit=1)
    connection_id = world.lan_connection()
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is True
    assert len(world.requests) == 1


# ------------------------------------------------- the model list as the probe

def test_with_no_probe_the_model_list_the_engine_can_fetch_answers_the_test(world, caplog):
    asked = []

    def source(instance_id, backend_type, field_key, values):
        asked.append((instance_id, backend_type, field_key, dict(values)))
        return {"items": [{"value": "qwen", "label": "qwen"}]}

    world.hooks["options_source"] = source
    connection_id = _priced(world)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.json["ok"] is True and isinstance(response.json["latency_ms"], int)
    assert asked == [(world.instance.id, "openai_compatible", "model", {"base_url": LAN, "model": "qwen"})]
    # Listing models costs nothing, so a priced back end is not charged for it.
    assert _calls(world) == ([], [])
    _nothing_leaked(world, caplog)


def test_a_model_list_that_cannot_be_fetched_is_a_failed_test_that_names_the_address(world, caplog):
    world.hooks["options_source"] = lambda *args: None
    connection_id = world.lan_connection()
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False and response.json["latency_ms"] is None
    assert response.json["message"].startswith(f"No answer at {LAN_ORIGIN}.")
    _nothing_leaked(world, caplog)


def test_saved_settings_that_no_longer_fit_the_plugin_are_a_failed_test_not_a_server_error(world, monkeypatch):
    from subtitles.tools.translate.runtime.settings_service import InvalidValuesError

    world.hooks["options_source"] = lambda *args: {"items": []}
    connection_id = world.lan_connection()

    def refuse(self, *args, **kwargs):
        raise InvalidValuesError("The values are not valid.", field_errors={"base_url": "must be an address"})

    monkeypatch.setattr(TranslatorSettingsService, "options", refuse)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    assert "save it again" in response.json["message"]


def test_the_refusal_names_the_month_cap_when_only_that_one_is_set(world):
    world.probe({"ok": True})
    world.limits = CostLimits(month_cap=100, translation_limit=1)  # fits the budget, not the 432 a test may cost
    connection_id = _priced(world)
    message = world.call("post", f"connections/{connection_id}/test", {}).json["message"]
    assert "monthly limit" in message and "one translation" not in message
    assert world.requests == []


def test_the_refusal_names_the_job_cap_when_that_is_the_one_set(world):
    world.probe({"ok": True})
    world.limits = CostLimits(job_cap=100, translation_limit=1)
    connection_id = _priced(world)
    message = world.call("post", f"connections/{connection_id}/test", {}).json["message"]
    assert "one translation" in message and "monthly" not in message


# ------------------------------------------------------- redaction, rollback, timeout

def test_a_probe_that_raises_one_of_our_own_refusals_with_a_key_in_it_is_still_redacted(world, caplog):
    from subtitles.tools.translate.runtime.settings_service import InvalidValuesError

    world.probe(InvalidValuesError(f"bad key {SECRET}", field_errors={"api_key": SECRET}))
    connection_id = world.lan_connection()
    caplog.set_level(logging.DEBUG)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    assert "field_errors" not in response.json and "Bazarr+ log" in response.json["message"]
    _nothing_leaked(world, caplog)


def test_a_model_list_source_that_raises_a_refusal_with_a_key_is_redacted_too(world, caplog):
    from subtitles.tools.translate.runtime.settings_service import PluginUnavailableError

    def source(*args):
        raise PluginUnavailableError(f"down for {SECRET}", extra=SECRET)

    world.hooks["options_source"] = source
    connection_id = world.lan_connection()
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    _nothing_leaked(world, caplog)


def test_a_failure_while_reserving_gives_the_budget_back_and_sends_nothing(world, monkeypatch, caplog):
    world.probe({"ok": True})
    connection_id = _priced(world)

    def broken(self, *args, **kwargs):
        raise RuntimeError(f"ledger gone {SECRET}")

    monkeypatch.setattr(UsageLedger, "reserve_call", broken)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 200 and response.json["ok"] is False
    assert world.requests == []
    usage, budgets = _calls(world)
    assert usage == [] and [item.state for item in budgets] == ["released"]
    _nothing_leaked(world, caplog)


def test_a_priced_test_holds_only_what_it_may_cost_not_a_whole_job_budget(world):
    world.probe({"ok": True, "input_tokens": 1, "output_tokens": 1})
    world.limits = CostLimits(month_cap=50_000_000, translation_limit=1)  # a whole month's share would be 50 dollars
    connection_id = _priced(world)
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is True
    (budget,) = _calls(world)[1]
    assert budget.budget_micros == 432


def test_the_probe_is_given_a_timeout_and_a_hung_one_is_a_failed_test_that_keeps_its_charge(world, monkeypatch):
    import threading

    from subtitles.tools.translate.runtime import settings_service

    monkeypatch.setattr(settings_service, "TEST_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(settings_service, "TEST_TIMEOUT_GRACE_SECONDS", 0.1)
    release = threading.Event()
    seen = []

    def hang(request):
        seen.append(request.timeout_seconds)
        release.wait(5)
        return {"ok": True}

    world.hooks["connection_probe"] = hang
    connection_id = _priced(world)
    try:
        # The request carries the limit as it was when the service was built.
        response = world.call("post", f"connections/{connection_id}/test", {})
    finally:
        release.set()
    assert response.status_code == 200 and response.json["ok"] is False and "Bazarr+ log" in response.json["message"]
    assert seen and seen[0] > 0
    (call,), (budget,) = _calls(world)
    assert (call.actual_micros, budget.state) == (432, "released")


def test_the_default_timeout_is_part_of_every_probe_request(world):
    world.probe({"ok": True})
    connection_id = world.lan_connection()
    world.call("post", f"connections/{connection_id}/test", {})
    assert world.requests[0].timeout_seconds == 20.0


def test_both_caps_set_names_the_one_that_leaves_less_room(world):
    world.probe({"ok": True})
    connection_id = _priced(world)
    world.limits = CostLimits(month_cap=100, job_cap=5_000_000, translation_limit=1)
    assert "monthly limit" in world.call("post", f"connections/{connection_id}/test", {}).json["message"]
    world.limits = CostLimits(month_cap=50_000_000, job_cap=100, translation_limit=1)
    assert "one translation" in world.call("post", f"connections/{connection_id}/test", {}).json["message"]
    assert world.requests == []


def test_a_back_end_served_by_an_extension_is_tested_against_that_plugin_s_model_list(world):
    extension = service_manifest(
        "extension_backend", settings_version=1, kind="translator", kind_api="bazarr.translator.v1",
        settings={"fields": [], "backend_types": {"ext": {"label": "Ext", "fields": [
            {"key": "base_url", "type": "text", "destination": True, "default": "https://ext.example.test"}]}}})
    world.plugins["extension_backend"] = InstalledPlugin("extension_backend", "Ext", "1.0.0", True, extension)
    asked = []
    world.hooks["options_source"] = lambda *args: asked.append(args) or {"items": []}
    response = world.call("post", f"engines/{world.instance.id}/connections", {
        "backend_type": "ext", "plugin_id": "extension_backend", "values": {"model": "m"},
        "accept_destinations": []})
    assert response.status_code == 201, response.json
    connection_id = response.json["connection"]["id"]
    result = world.call("post", f"connections/{connection_id}/test", {})
    assert result.status_code == 200 and result.json["ok"] is True, result.json
    assert asked and asked[0][1] == "ext"


def test_our_own_programming_errors_are_not_turned_into_a_failed_test(world, caplog):
    world.probe(TypeError(f"bug near {SECRET}"))
    connection_id = _priced(world)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.status_code == 500
    assert SECRET not in response.get_data(as_text=True)
    # The charge was still settled and the hold given back.
    (call,), (budget,) = _calls(world)
    assert (call.actual_micros, budget.state) == (432, "released")


def test_the_probe_failure_traceback_goes_to_the_debug_log_with_every_secret_masked(world, caplog):
    world.probe(RuntimeError(f"boom with {SECRET}"))
    connection_id = world.lan_connection()
    caplog.set_level(logging.DEBUG)
    response = world.call("post", f"connections/{connection_id}/test", {})
    assert response.json["ok"] is False and "debug logging" in response.json["message"]
    debug = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG
             and "connection test failed" in record.getMessage()]
    assert debug and "RuntimeError" in debug[0] and "Traceback" in debug[0] and "***" in debug[0]
    _nothing_leaked(world, caplog)


def test_only_one_test_runs_per_connection_and_the_claim_is_always_given_back(world):
    from subtitles.tools.translate.runtime import settings_service

    world.probe({"ok": True})
    connection_id = world.lan_connection()
    other_id = world.lan_connection()
    assert settings_service._claim_test(connection_id) is True
    try:
        busy = world.call("post", f"connections/{connection_id}/test", {})
        assert busy.status_code == 200 and busy.json["ok"] is False and "already running" in busy.json["message"]
        assert world.requests == []
        # Another connection is not held up by it.
        assert world.call("post", f"connections/{other_id}/test", {}).json["ok"] is True
    finally:
        settings_service._release_test(connection_id)
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is True
    # A failed test and a refused one give the claim back too.
    world.probe(RuntimeError("down"))
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is False
    world.probe({"ok": True})
    assert world.call("post", f"connections/{connection_id}/test", {}).json["ok"] is True
    assert settings_service._RUNNING_TESTS == set()
