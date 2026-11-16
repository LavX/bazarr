# coding=utf-8
"""Origins, failure classification and the failover rules of the catalog transport."""
import ast
import io
import json
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest
import requests

from provider_hub import transport
from provider_hub.bundle import BundleValidationError
from provider_hub.transport import (
    FailureKind, FetchKey, JobRoute, NoOriginAvailable, PagesOrigin, RawOrigin, TransportError,
    classify_failure, fetch_with_fallback,
)

EMPTY = {"cooldowns": {}, "retry_after": {}, "probes": {}}


@pytest.fixture(autouse=True)
def _fresh_transport_record():
    transport.reset_state()
    yield
    transport.reset_state()


KEY = FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 40, "catalog.json")
NOW = 1_800_000_000.0


class _FakeOrigin(transport.Origin):
    """An origin that serves from memory; any key is addressable."""

    def url(self, key):
        return "https://fake.example/" + key.path


class _Stop(Exception):
    pass


def _response(status=200, body=b"{}", headers=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response.raw = None
    response._content = body
    response._content_consumed = True
    return response


def _http_error(status, headers=None, body=b""):
    response = _response(status, body, headers)
    return requests.HTTPError(response=response)


# -- classification -----------------------------------------------------------

TRIGGERS = [
    ("429 with Retry-After", _http_error(429, {"retry-after": "60"}), FailureKind.RATE_LIMITED),
    ("429 bare", _http_error(429), FailureKind.RATE_LIMITED),
    ("403 remaining zero", _http_error(403, {"x-ratelimit-remaining": "0"}), FailureKind.RATE_LIMITED),
    ("403 retry-after", _http_error(403, {"retry-after": "5"}), FailureKind.RATE_LIMITED),
    ("403 secondary limit", _http_error(
        403, {"x-ratelimit-remaining": "20"},
        b'{"message":"You have exceeded a secondary rate limit. Wait."}'), FailureKind.RATE_LIMITED),
    ("408", _http_error(408), FailureKind.REQUEST_TIMEOUT),
    ("500", _http_error(500), FailureKind.SERVER_ERROR),
    ("502", _http_error(502), FailureKind.SERVER_ERROR),
    ("503", _http_error(503), FailureKind.SERVER_ERROR),
    ("504", _http_error(504), FailureKind.SERVER_ERROR),
    ("read timeout", requests.exceptions.ReadTimeout("slow"), FailureKind.TIMEOUT),
    ("connect timeout", requests.exceptions.ConnectTimeout("slow"), FailureKind.TIMEOUT),
    ("connection reset", requests.exceptions.ConnectionError(ConnectionResetError("reset")),
     FailureKind.CONNECTION),
    ("connection refused", requests.exceptions.ConnectionError(ConnectionRefusedError("refused")),
     FailureKind.CONNECTION),
    ("broken body", requests.exceptions.ChunkedEncodingError("cut"), FailureKind.CONNECTION),
    ("transient dns", requests.exceptions.ConnectionError(
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")), FailureKind.DNS),
]

NON_TRIGGERS = [
    ("404", _http_error(404), FailureKind.NOT_FOUND),
    ("410", _http_error(410), FailureKind.GONE),
    ("plain 401", _http_error(401), FailureKind.UNAUTHORIZED),
    ("plain 403", _http_error(403), FailureKind.FORBIDDEN),
    ("403 with quota left", _http_error(403, {"x-ratelimit-remaining": "20"}), FailureKind.FORBIDDEN),
    ("other 4xx", _http_error(422), FailureKind.CLIENT_ERROR),
    ("tls", requests.exceptions.SSLError("bad certificate"), FailureKind.TLS),
    ("tls wrapped as a connection error",
     requests.exceptions.ConnectionError(ssl.SSLError("handshake")), FailureKind.TLS),
    ("permanent dns", requests.exceptions.ConnectionError(
        socket.gaierror(socket.EAI_NONAME, "Name or service not known")), FailureKind.NAME_NOT_FOUND),
    ("redirect loop", requests.exceptions.TooManyRedirects("loop"), FailureKind.REDIRECT),
    ("malformed", json.JSONDecodeError("bad", "x", 0), FailureKind.MALFORMED),
    ("bad value", ValueError("catalog payload must be an object"), FailureKind.MALFORMED),
    ("oversized", TransportError(FailureKind.OVERSIZED, "too big"), FailureKind.OVERSIZED),
    ("hash mismatch", BundleValidationError("hash mismatch"), FailureKind.INTEGRITY),
    ("something else", RuntimeError("boom"), FailureKind.UNKNOWN),
]


@pytest.mark.parametrize("label, error, kind", TRIGGERS, ids=[row[0] for row in TRIGGERS])
def test_trigger_failures_are_typed_and_may_fail_over(label, error, kind):
    failure = classify_failure(error, now=NOW)
    assert failure.kind is kind
    assert failure.trigger


@pytest.mark.parametrize("label, error, kind", NON_TRIGGERS, ids=[row[0] for row in NON_TRIGGERS])
def test_non_trigger_failures_are_typed_and_never_fail_over(label, error, kind):
    failure = classify_failure(error, now=NOW)
    assert failure.kind is kind
    assert not failure.trigger


def test_stop_and_cancel_never_fail_over():
    failure = classify_failure(_Stop("stopped"), cancel_types=(_Stop,))
    assert failure.kind is FailureKind.CANCELLED
    assert not failure.trigger


def test_tls_is_typed_before_the_connection_error_it_subclasses():
    assert issubclass(requests.exceptions.SSLError, requests.exceptions.ConnectionError)
    assert classify_failure(requests.exceptions.SSLError("x")).kind is FailureKind.TLS


def test_the_reset_time_follows_the_same_precedence_as_the_refresh_message():
    both = _http_error(429, {"retry-after": "60", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"})
    assert classify_failure(both, now=NOW).retry_at == NOW + 60
    reset_only = _http_error(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 90)})
    assert classify_failure(reset_only, now=NOW).retry_at == int(NOW) + 90
    assert classify_failure(_http_error(429, {"retry-after": "soon"}), now=NOW).retry_at is None
    assert classify_failure(
        _http_error(429, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9" * 25}), now=NOW,
    ).retry_at is None


def test_the_refresh_message_follows_the_failure_type_not_its_text():
    from provider_hub.service import _catalog_source_error_message

    # The wording says "rate limit", the status says 500: the status decides.
    response = _response(500)
    error = requests.HTTPError("rate limit exceeded, temporarily limited", response=response)
    assert _catalog_source_error_message(error) == (
        "GitHub returned HTTP 500 while refreshing this catalog source.")
    # A certificate failure is a connection error underneath and keeps the
    # reachability wording it always had.
    assert "Could not reach GitHub" in _catalog_source_error_message(
        requests.exceptions.SSLError("certificate verify failed"))


# -- origins ------------------------------------------------------------------

class _Streamed:
    """A response with a body read in chunks, like ``stream=True`` gives."""

    def __init__(self, status=200, body=b"{}", headers=None, chunk=4):
        self._response = _response(status, body, headers)
        self.status_code = status
        self.headers = self._response.headers
        self._body = body
        self._chunk = chunk
        self.closed = False

    def iter_content(self, chunk_size=1):
        step = max(1, min(self._chunk, chunk_size))
        for start in range(0, len(self._body), step):
            yield self._body[start:start + step]

    def raise_for_status(self):
        self._response._content = getattr(self, "_content", self._response._content)
        self._response.raise_for_status()

    def close(self):
        self.closed = True


class _Net:
    """Scripted answers per URL, recording every request."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.routes[url]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        return outcome


RAW_URL = f"https://raw.githubusercontent.com/LavX/bazarr-provider-catalog/{'a' * 40}/catalog.json"
PAGES_URL = f"{transport.PAGES_BASE_URL}/v1/main/{'a' * 40}/catalog.json"


def test_origins_build_their_own_urls():
    assert RawOrigin().url(KEY) == RAW_URL
    assert PagesOrigin().url(KEY) == PAGES_URL
    assert PagesOrigin("https://mirror.example/catalog/").url(KEY).startswith(
        "https://mirror.example/catalog/v1/main/")


def test_a_fetch_returns_bytes_origin_and_etag_without_following_redirects(monkeypatch):
    net = _Net({RAW_URL: _Streamed(200, b'{"a": 1}', {"ETag": '"abc"'})})
    monkeypatch.setattr(transport.requests, "get", net)

    result = RawOrigin().fetch(KEY)

    assert (result.content, result.origin, result.etag) == (b'{"a": 1}', "raw", '"abc"')
    (_url, kwargs), = net.calls
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True


def test_an_etag_is_sent_and_a_304_reports_not_modified(monkeypatch):
    net = _Net({PAGES_URL: _Streamed(304, b"")})
    monkeypatch.setattr(transport.requests, "get", net)

    result = PagesOrigin().fetch(KEY, etag='"abc"')

    assert result.not_modified and result.origin == "pages"
    assert net.calls[0][1]["headers"] == {"If-None-Match": '"abc"'}


@pytest.mark.parametrize("location", [
    "https://evil.example/catalog.json",
    RAW_URL.replace("https://", "http://"),
    RAW_URL + "?moved=1",
    "/elsewhere",
])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_no_redirect_is_followed_and_exactly_one_request_is_made(monkeypatch, status, location):
    net = _Net({RAW_URL: _Streamed(status, b"", {"Location": location})})
    monkeypatch.setattr(transport.requests, "get", net)

    with pytest.raises(TransportError) as caught:
        RawOrigin().fetch(KEY)

    assert classify_failure(caught.value).kind is FailureKind.REDIRECT
    assert not classify_failure(caught.value).trigger
    assert len(net.calls) == 1
    assert net.calls[0][1]["allow_redirects"] is False


def test_a_redirect_from_the_mirror_does_not_fail_over(monkeypatch):
    raw = _Fake("raw", [_http_error(503)])
    moved = TransportError(FailureKind.REDIRECT, "the origin redirected", status=302)
    pages = _Fake("pages", [moved, b"unreachable"])

    with pytest.raises(TransportError):
        _fetch([raw, pages])

    assert pages.calls == 1


def test_an_oversized_body_stops_the_download(monkeypatch):
    body = b"x" * 100
    streamed = _Streamed(200, body, chunk=10)
    net = _Net({RAW_URL: streamed})
    monkeypatch.setattr(transport.requests, "get", net)

    with pytest.raises(TransportError) as caught:
        RawOrigin().fetch(KEY, max_bytes=35)

    assert caught.value.kind is FailureKind.OVERSIZED
    assert not classify_failure(caught.value).trigger
    assert streamed.closed


def test_an_oversized_declared_length_is_refused_before_reading(monkeypatch):
    streamed = _Streamed(200, b"x", {"Content-Length": "999999"})
    monkeypatch.setattr(transport.requests, "get", _Net({RAW_URL: streamed}))

    with pytest.raises(TransportError) as caught:
        RawOrigin().fetch(KEY, max_bytes=100)

    assert caught.value.kind is FailureKind.OVERSIZED


def test_an_error_status_keeps_the_body_so_a_secondary_limit_is_recognised(monkeypatch):
    body = b'{"message":"You have exceeded a secondary rate limit. Wait."}'
    monkeypatch.setattr(
        transport.requests, "get",
        _Net({RAW_URL: _Streamed(403, body, {"x-ratelimit-remaining": "20"})}),
    )

    with pytest.raises(requests.HTTPError) as caught:
        RawOrigin().fetch(KEY)

    assert classify_failure(caught.value).kind is FailureKind.RATE_LIMITED


def test_an_unexpected_success_class_status_is_not_served(monkeypatch):
    monkeypatch.setattr(transport.requests, "get", _Net({RAW_URL: _Streamed(199, b"{}")}))

    with pytest.raises(TransportError) as caught:
        RawOrigin().fetch(KEY)

    assert caught.value.kind is FailureKind.CLIENT_ERROR


# -- failover -----------------------------------------------------------------

class _Fake(_FakeOrigin):
    def __init__(self, name, outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0
        self.etags = []

    def fetch(self, key, etag=None, *, timeout=30.0, max_bytes=0):
        self.calls += 1
        self.etags.append(etag)
        outcome = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return transport.FetchResult(outcome, self.name)


def _fetch(origins, *, surface="catalog", route=None, official=True, allow=True, now=NOW):
    return fetch_with_fallback(
        origins, KEY, surface=surface, official=official, allow_fallback=allow,
        route=route, clock=lambda: now, cancel_types=(_Stop,),
    )


@pytest.mark.parametrize("surface", ["catalog", "bundle"])
@pytest.mark.parametrize("label, error, kind", TRIGGERS, ids=[row[0] for row in TRIGGERS])
def test_a_trigger_on_the_primary_is_served_by_the_mirror(surface, label, error, kind):
    raw, pages = _Fake("raw", [error]), _Fake("pages", [b"mirror"])

    result = _fetch([raw, pages], surface=surface)

    assert (result.content, result.origin) == (b"mirror", "pages")
    assert (raw.calls, pages.calls) == (1, 1)


@pytest.mark.parametrize("surface", ["catalog", "bundle"])
@pytest.mark.parametrize("label, error, kind", NON_TRIGGERS, ids=[row[0] for row in NON_TRIGGERS])
def test_a_non_trigger_on_the_primary_raises_and_the_mirror_is_never_asked(surface, label, error, kind):
    raw, pages = _Fake("raw", [error]), _Fake("pages", [b"mirror"])

    with pytest.raises(type(error)):
        _fetch([raw, pages], surface=surface)

    assert pages.calls == 0
    assert transport.export_state()["cooldowns"] == {}


def test_stop_never_fails_over():
    raw, pages = _Fake("raw", [_Stop("stop")]), _Fake("pages", [b"mirror"])
    with pytest.raises(_Stop):
        _fetch([raw, pages])
    assert pages.calls == 0


def test_a_user_source_never_leaves_the_primary_and_touches_no_state():
    raw, pages = _Fake("raw", [_http_error(503)]), _Fake("pages", [b"mirror"])

    with pytest.raises(requests.HTTPError):
        _fetch([raw, pages], official=False)

    assert pages.calls == 0
    assert transport.export_state() == EMPTY


def test_the_setting_off_is_a_plain_primary_fetch_that_touches_no_state():
    raw, pages = _Fake("raw", [_http_error(429, {"retry-after": "60"})]), _Fake("pages", [b"mirror"])

    with pytest.raises(requests.HTTPError):
        _fetch([raw, pages], allow=False)

    assert pages.calls == 0
    assert transport.export_state() == EMPTY


def test_a_failure_of_both_origins_raises_the_last_error():
    raw = _Fake("raw", [_http_error(503)])
    pages = _Fake("pages", [requests.exceptions.ReadTimeout("slow")])

    with pytest.raises(requests.exceptions.ReadTimeout):
        _fetch([raw, pages])


def test_a_non_trigger_from_the_mirror_stops_the_job():
    raw = _Fake("raw", [_http_error(503)])
    pages = _Fake("pages", [_http_error(403)])

    with pytest.raises(requests.HTTPError) as caught:
        _fetch([raw, pages])

    assert classify_failure(caught.value).kind is FailureKind.FORBIDDEN


def test_a_lagging_mirror_404_reports_the_primarys_failure_not_a_missing_source():
    from provider_hub.service import _catalog_source_error_message

    primary = _http_error(503)
    raw = _Fake("raw", [primary])
    pages = _Fake("pages", [_http_error(404)])

    with pytest.raises(requests.HTTPError) as caught:
        _fetch([raw, pages])

    assert caught.value is primary
    assert "does not have this catalog source" not in _catalog_source_error_message(caught.value)
    assert _catalog_source_error_message(caught.value) == (
        "GitHub returned HTTP 503 while refreshing this catalog source.")


def test_a_mirror_404_after_a_primary_404_is_not_masked():
    # Nothing to prefer: the primary never failed over on a 404 to begin with.
    raw, pages = _Fake("raw", [_http_error(404)]), _Fake("pages", [b"x"])

    with pytest.raises(requests.HTTPError) as caught:
        _fetch([raw, pages])

    assert classify_failure(caught.value).kind is FailureKind.NOT_FOUND
    assert pages.calls == 0


def test_a_mirror_that_cannot_address_the_key_is_skipped_without_a_failover_claim():
    channel_less = FetchKey("LavX/bazarr-provider-catalog", "", "a" * 40, "catalog.json")
    primary = _http_error(503)
    route = JobRoute()

    with pytest.raises(requests.HTTPError) as caught:
        fetch_with_fallback(
            [_Fake("raw", [primary]), PagesOrigin()], channel_less, surface="catalog", official=True,
            route=route, clock=lambda: NOW)

    assert caught.value is primary
    assert route.sticky is None and route.failovers == 0


def test_a_primary_that_cannot_address_the_key_raises_at_once(monkeypatch):
    bad = FetchKey("LavX/bazarr-provider-catalog", "main", "main", "catalog.json")
    net = _Net({})
    monkeypatch.setattr(transport.requests, "get", net)
    pages = _Fake("pages", [b"x"])

    with pytest.raises(TransportError):
        fetch_with_fallback([RawOrigin(), pages], bad, surface="catalog", official=True, clock=lambda: NOW)

    assert net.calls == [] and pages.calls == 0


def test_the_mirror_repository_is_the_official_one_and_compared_without_case(monkeypatch):
    from provider_hub.state import OFFICIAL_CATALOG_REPO

    assert transport.OFFICIAL_REPO == OFFICIAL_CATALOG_REPO
    lowered = FetchKey(OFFICIAL_CATALOG_REPO.lower(), "main", "a" * 40, "catalog.json")
    assert PagesOrigin().url(lowered).endswith("/v1/main/" + "a" * 40 + "/catalog.json")


def test_a_job_fails_over_once_and_stays_on_the_mirror():
    raw = _Fake("raw", [_http_error(503), b"raw"])
    pages = _Fake("pages", [b"one", b"two", requests.exceptions.ReadTimeout("slow")])
    route = JobRoute()

    assert _fetch([raw, pages], route=route).origin == "pages"
    # The next file of the same job goes straight to the mirror, although the
    # primary has recovered.
    assert _fetch([raw, pages], surface="bundle", route=route).content == b"two"
    assert raw.calls == 1
    # A trigger on the mirror does not move the job back: there is one failover.
    with pytest.raises(requests.exceptions.ReadTimeout):
        _fetch([raw, pages], surface="bundle", route=route)
    assert raw.calls == 1
    assert route.failovers == 1


def test_a_refresh_storm_gets_one_failover():
    raw = _Fake("raw", [_http_error(503)])
    pages = _Fake("pages", [b"mirror"])
    route = JobRoute()
    results, errors = [], []
    start = threading.Barrier(8)

    def worker():
        start.wait()
        try:
            results.append(_fetch([raw, pages], route=route).origin)
        except Exception as error:  # pragma: no cover
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert results == ["pages"] * 8
    assert route.failovers == 1


def test_a_remembered_retry_after_skips_the_primary_until_its_reset_then_probes_once():
    error = _http_error(429, {"retry-after": "120"})
    raw = _Fake("raw", [error, b"raw back"])
    pages = _Fake("pages", [b"mirror"])

    assert _fetch([raw, pages], now=NOW).origin == "pages"
    assert transport.export_state()["retry_after"]["raw"]["until"] == NOW + 120

    # A later job inside the window never asks the primary.
    assert _fetch([raw, pages], now=NOW + 60).origin == "pages"
    assert raw.calls == 1

    # After the reset exactly one caller probes it.
    assert _fetch([raw, pages], now=NOW + 121).content == b"raw back"
    assert raw.calls == 2
    section = transport.export_state()
    assert section["retry_after"] == {} and section["probes"] == {}


def test_only_one_caller_probes_a_recovering_origin():
    raw = _Fake("raw", [_http_error(429, {"retry-after": "10"}), _http_error(503)])
    pages = _Fake("pages", [b"mirror"])
    _fetch([raw, pages], now=NOW)

    # The probe fails: the primary is parked again.
    assert _fetch([raw, pages], now=NOW + 11).origin == "pages"
    assert raw.calls == 2
    assert _fetch([raw, pages], now=NOW + 12).origin == "pages"
    assert raw.calls == 2


def test_a_second_caller_waits_while_a_probe_is_fresh():
    raw = _Fake("raw", [_http_error(429, {"retry-after": "10"}), b"raw"])
    pages = _Fake("pages", [b"mirror"])
    _fetch([raw, pages], now=NOW)
    transport.restore_state({"probes": {"raw": NOW + 11}})  # a probe in flight

    assert _fetch([raw, pages], now=NOW + 12).origin == "pages"
    assert raw.calls == 1


def test_a_retry_after_far_in_the_future_is_capped():
    raw = _Fake("raw", [_http_error(429, {"retry-after": str(10 ** 9)})])
    pages = _Fake("pages", [b"mirror"])

    _fetch([raw, pages], now=NOW)

    assert transport.export_state()["retry_after"]["raw"]["until"] == NOW + transport.MAX_COOLDOWN


def test_cooldowns_are_per_surface():
    raw = _Fake("raw", [_http_error(503), b"raw bundle"])
    pages = _Fake("pages", [b"mirror"])

    assert _fetch([raw, pages], surface="catalog", now=NOW).origin == "pages"
    cooldown = transport.export_state()["cooldowns"]["raw:catalog"]
    assert cooldown["kind"] == "server_error" and cooldown["status"] == 503

    # The catalog surface skips the primary while parked, the bundle surface
    # is unaffected.
    assert _fetch([raw, pages], surface="catalog", now=NOW + 5).origin == "pages"
    assert raw.calls == 1
    assert _fetch([raw, pages], surface="bundle", now=NOW + 5).content == b"raw bundle"
    # Once the cooldown has passed the catalog surface asks the primary again.
    raw.outcomes = [b"raw again"]
    raw.calls = 0
    assert _fetch([raw, pages], surface="catalog", now=NOW + 61).content == b"raw again"


def test_every_origin_parked_raises_instead_of_requesting():
    raw, pages = _Fake("raw", [b"x"]), _Fake("pages", [b"x"])
    transport.restore_state({
        "retry_after": {"raw": NOW + 100},
        "cooldowns": {"pages:catalog": {"until": NOW + 100, "kind": "server_error", "status": 503}},
    })

    with pytest.raises(NoOriginAvailable):
        _fetch([raw, pages], now=NOW)

    assert (raw.calls, pages.calls) == (0, 0)


# -- state --------------------------------------------------------------------

def test_restoring_keeps_the_later_expiry_per_key_and_never_shortens():
    # The process already knows some of these before the startup load.
    transport.record_failure(
        "raw", "catalog", classify_failure(_http_error(429, {"retry-after": "900"}), now=NOW), NOW)
    transport.record_failure("raw", "bundle", classify_failure(_http_error(503), now=NOW), NOW)
    longer = {"until": NOW + 500, "kind": "rate_limited", "status": 429}

    transport.restore_state({
        "cooldowns": {"raw:bundle": longer, "pages:catalog": {"until": NOW + 50}},
        "retry_after": {"raw": NOW + 100}, "probes": {"raw": NOW + 5},
    })

    record = transport.export_state()
    assert record["cooldowns"]["raw:bundle"]["until"] == NOW + 500
    assert record["cooldowns"]["pages:catalog"]["until"] == NOW + 50
    assert record["retry_after"]["raw"]["until"] == NOW + 900
    assert record["probes"]["raw"]["at"] == NOW + 5


def test_restoring_is_a_startup_call_and_a_later_call_changes_nothing():
    transport.restore_state({"cooldowns": {"raw:catalog": {"until": NOW + 30}}})
    before = transport.export_state()

    transport.restore_state({"cooldowns": {"raw:bundle": {"until": NOW + 99}}, "retry_after": {"raw": NOW + 9}})

    assert transport.export_state() == before
    transport.reset_state()
    transport.restore_state({"cooldowns": {"raw:bundle": {"until": NOW + 99}}})
    assert "raw:bundle" in transport.export_state()["cooldowns"]


@pytest.mark.parametrize("saved", [
    None, [], "x", {"cooldowns": "garbage", "retry_after": [], "probes": 3},
    {"cooldowns": {"raw:catalog": {"until": "never"}, "raw:bundle": True, "x": None, "y": {"until": -5}}},
])
def test_restoring_drops_anything_that_is_not_a_well_formed_entry(saved):
    transport.restore_state(saved)
    assert transport.export_state() == EMPTY


def test_a_bare_number_is_read_as_an_expiry():
    transport.restore_state({"retry_after": {"raw": NOW + 30}})
    assert transport.export_state()["retry_after"]["raw"]["until"] == NOW + 30


def test_an_export_is_a_copy_the_caller_cannot_use_to_edit_the_record():
    transport.restore_state({"cooldowns": {"raw:catalog": {"until": NOW + 30}}})
    exported = transport.export_state()
    exported["cooldowns"].clear()
    assert transport.export_state()["cooldowns"]


def test_a_cooldown_survives_a_save_and_a_fresh_start(tmp_path, monkeypatch):
    from provider_hub.state import load_state, mutate_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))
    raw, pages = _Fake("raw", [_http_error(429, {"retry-after": "120"})]), _Fake("pages", [b"mirror"])
    _fetch([raw, pages], now=NOW)

    mutate_state(lambda state: state.__setitem__("transport", transport.export_state()))
    # A fresh process: nothing in memory, only what the state file holds.
    transport.reset_state()
    transport.restore_state(load_state()["transport"])

    assert transport.export_state()["retry_after"]["raw"]["until"] == NOW + 120
    assert _fetch([raw, pages], now=NOW + 30).origin == "pages"
    assert raw.calls == 1


def test_repairs_and_clearings_land_in_the_one_record_every_caller_shares():
    # A clamp repair made while one caller reads is what the next caller, and
    # the next save, sees: there is no copy to lose it.
    transport.restore_state({"cooldowns": {"raw:catalog": {"until": NOW + 10 ** 9, "at": NOW}}})
    assert transport._admit("raw", "catalog", NOW) is None
    assert transport.export_state()["cooldowns"]["raw:catalog"]["until"] == NOW + transport.MAX_COOLDOWN

    transport.record_success("raw", "catalog", transport.Admission(transport.current_generation()))
    assert transport.export_state()["cooldowns"] == {}


def test_transport_never_takes_the_state_lock():
    source = Path(transport.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {
        f"{node.module or ''}.{alias.name}" for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert not any(name.split(".")[-1] == "state" or ".state." in name for name in imported), imported
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not names & {
        "mutate_state", "mutate_state_checked", "load_state", "save_state", "state_write_lock",
        "_STATE_LOCK",
    }


# -- clocks, ordering and probes ---------------------------------------------

class _Ticker:
    """A clock the fetch itself moves forward."""

    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Slow(_FakeOrigin):
    """An origin whose request takes time on the ticker, then fails."""

    def __init__(self, name, ticker, took, outcome):
        self.name, self.ticker, self.took, self.outcome = name, ticker, took, outcome

    def fetch(self, key, etag=None, *, timeout=30.0, max_bytes=0):
        self.ticker.advance(self.took)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return transport.FetchResult(self.outcome, self.name)


def test_a_slow_failure_is_recorded_from_the_clock_after_the_request():
    ticker = _Ticker()
    raw = _Slow("raw", ticker, 100, _http_error(429, {"retry-after": "60"}))
    pages = _Fake("pages", [b"mirror"])

    fetch_with_fallback([raw, pages], KEY, surface="catalog", official=True, clock=ticker)

    entry = transport.export_state()["retry_after"]["raw"]
    assert (entry["until"], entry["at"]) == (NOW + 100 + 60, NOW + 100)


def test_a_cooldown_counts_from_when_the_request_failed():
    ticker = _Ticker()
    raw = _Slow("raw", ticker, 40, _http_error(503))

    fetch_with_fallback(
        [raw, _Fake("pages", [b"mirror"])], KEY, surface="catalog", official=True,
        clock=ticker)

    assert transport.export_state()["cooldowns"]["raw:catalog"]["until"] == NOW + 40 + transport.DEFAULT_COOLDOWN


def test_a_reset_that_passed_while_the_request_ran_is_not_remembered():
    ticker = _Ticker()
    reset = str(int(NOW) + 50)
    raw = _Slow("raw", ticker, 100, _http_error(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}))

    fetch_with_fallback(
        [raw, _Fake("pages", [b"mirror"])], KEY, surface="catalog", official=True,
        clock=ticker)

    section = transport.export_state()
    assert section["retry_after"] == {}
    assert section["cooldowns"]["raw:catalog"]["until"] == NOW + 100 + transport.RATE_LIMIT_COOLDOWN


@pytest.mark.parametrize("reset", [-5, 0])
def test_a_reset_that_is_not_in_the_future_uses_the_ordinary_rate_limit_cooldown(reset):
    error = _http_error(429, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + reset)})

    _fetch([_Fake("raw", [error]), _Fake("pages", [b"mirror"])])

    section = transport.export_state()
    assert section["retry_after"] == {}
    assert section["cooldowns"]["raw:catalog"]["until"] == NOW + transport.RATE_LIMIT_COOLDOWN


class _Gate(_FakeOrigin):
    """First call parks until released, then succeeds; later calls fail 503."""

    name = "raw"

    def __init__(self, ticker):
        self.ticker = ticker
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def fetch(self, key, etag=None, *, timeout=30.0, max_bytes=0):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            assert self.release.wait(10)
            return transport.FetchResult(b"late success", "raw")
        raise _http_error(503)


def test_a_late_success_does_not_erase_a_newer_overlapping_failure():
    ticker = _Ticker()
    gate = _Gate(ticker)
    pages = _Fake("pages", [b"mirror"])
    outcome = []

    def slow_job():
        outcome.append(fetch_with_fallback(
            [gate, pages], KEY, surface="catalog", official=True, clock=ticker))

    worker = threading.Thread(target=slow_job)
    worker.start()
    assert gate.entered.wait(10)
    ticker.advance(5)
    # A second job started later fails against the same origin and surface.
    assert fetch_with_fallback(
        [gate, pages], KEY, surface="catalog", official=True, clock=ticker,
    ).origin == "pages"
    ticker.advance(5)
    gate.release.set()
    worker.join(10)

    assert outcome[0].content == b"late success"
    assert "raw:catalog" in transport.export_state()["cooldowns"]


def test_a_success_clears_a_restriction_recorded_before_it_began():
    transport.record_failure("raw", "catalog", classify_failure(_http_error(503), now=NOW), NOW)

    transport.record_success("raw", "catalog", transport.Admission(transport.current_generation()))

    assert transport.export_state()["cooldowns"] == {}


def test_a_plain_success_does_not_lift_the_reset_only_the_probe_may():
    transport.record_failure(
        "raw", "bundle", classify_failure(_http_error(429, {"retry-after": "30"}), now=NOW), NOW)

    transport.record_success("raw", "bundle", transport.Admission(transport.current_generation()))

    assert "raw" in transport.export_state()["retry_after"]


def test_a_success_that_began_earlier_leaves_a_later_rate_limit_standing():
    admitted = transport.Admission(transport.current_generation())
    transport.record_failure(
        "raw", "bundle", classify_failure(_http_error(429, {"retry-after": "30"}), now=NOW + 5), NOW + 5)

    transport.record_success("raw", "bundle", admitted)

    assert transport.export_state()["retry_after"]["raw"]["until"] == NOW + 35


def test_only_one_of_many_concurrent_callers_probes_a_recovering_origin(monkeypatch):
    real_until = transport._until

    def slow_until(value):
        # Widen the gap between reading the probe mark and writing it.
        time.sleep(0.0005)
        return real_until(value)

    monkeypatch.setattr(transport, "_until", slow_until)
    transport.restore_state({"retry_after": {"raw": {"until": NOW - 1, "at": NOW - 100}}})
    callers = 24
    start = threading.Barrier(callers)
    reservations = []

    def caller():
        start.wait()
        reservations.append(transport._admit("raw", "catalog", NOW) is not None)

    threads = [threading.Thread(target=caller) for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert reservations.count(True) == 1


def test_concurrent_jobs_send_one_probe_to_a_recovering_origin():
    transport.restore_state({"retry_after": {"raw": {"until": NOW - 1, "at": NOW - 100}}})
    inside = threading.Event()
    release = threading.Event()
    seen = []

    class Probe(_FakeOrigin):
        name = "raw"

        def fetch(self, key, etag=None, *, timeout=30.0, max_bytes=0):
            seen.append(1)
            inside.set()
            assert release.wait(10)
            return transport.FetchResult(b"raw", "raw")

    primary, pages = Probe(), _Fake("pages", [b"mirror"])
    results = []
    start = threading.Barrier(8)

    def job():
        start.wait()
        results.append(_fetch([primary, pages]).origin)

    threads = [threading.Thread(target=job) for _ in range(8)]
    for thread in threads:
        thread.start()
    assert inside.wait(10)
    # The other seven are parked behind the probe and finish on the mirror
    # while it is still in flight.
    deadline = time.monotonic() + 10
    while len(results) < 7 and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()
    for thread in threads:
        thread.join()

    assert len(seen) == 1
    assert sorted(results) == ["pages"] * 7 + ["raw"]


def _resolver_chain(errno_value, text="Failed to resolve 'raw.githubusercontent.com' (resolver)"):
    """requests.ConnectionError > MaxRetryError > NameResolutionError > gaierror,
    nested the way requests and urllib3 build it."""
    from urllib3.exceptions import MaxRetryError, NameResolutionError

    resolver = socket.gaierror(errno_value, text)
    try:
        try:
            raise resolver
        except socket.gaierror as inner:
            raise NameResolutionError("raw.githubusercontent.com", None, inner) from inner
    except NameResolutionError as name_error:
        retry = MaxRetryError(None, RAW_URL, reason=name_error)
    return requests.exceptions.ConnectionError(retry)


def test_real_urllib3_nesting_of_a_transient_resolver_failure_fails_over():
    failure = classify_failure(_resolver_chain(socket.EAI_AGAIN, "Temporary failure in name resolution"))
    assert failure.kind is FailureKind.DNS and failure.trigger


def test_the_errno_decides_even_when_the_text_says_temporary():
    failure = classify_failure(_resolver_chain(socket.EAI_NONAME, "Temporary failure, name not known"))
    assert failure.kind is FailureKind.NAME_NOT_FOUND and not failure.trigger


def test_the_errno_decides_even_when_the_text_says_nothing():
    failure = classify_failure(_resolver_chain(socket.EAI_AGAIN, "resolver said no"))
    assert failure.kind is FailureKind.DNS


def test_text_is_only_a_fallback_when_there_is_no_gaierror():
    from urllib3.exceptions import MaxRetryError, NameResolutionError

    class Plain(NameResolutionError):
        def __init__(self, text):
            Exception.__init__(self, text)

    transient = requests.exceptions.ConnectionError(
        MaxRetryError(None, RAW_URL, reason=Plain("Temporary failure in name resolution")))
    permanent = requests.exceptions.ConnectionError(
        MaxRetryError(None, RAW_URL, reason=Plain("Name or service not known")))
    assert classify_failure(transient).kind is FailureKind.DNS
    assert classify_failure(permanent).kind is FailureKind.NAME_NOT_FOUND


def test_a_tls_eof_is_tls_and_never_fails_over():
    failure = classify_failure(requests.exceptions.ConnectionError(ssl.SSLEOFError(8, "EOF occurred")))
    assert failure.kind is FailureKind.TLS and not failure.trigger


def test_a_503_retry_after_parks_the_surface_not_the_origin():
    _fetch([_Fake("raw", [_http_error(503, {"retry-after": "1800"})]), _Fake("pages", [b"m"])])

    section = transport.export_state()
    assert section["retry_after"] == {}
    assert section["cooldowns"]["raw:catalog"]["until"] == NOW + transport.DEFAULT_COOLDOWN


BAD_KEYS = [
    FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 40, "../../etc/passwd"),
    FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 40, "providers/./x/provider.json"),
    FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 40, "catalog.json?x=1"),
    FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 40, "catalog.json#frag"),
    FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 40, "%2e%2e/catalog.json"),
    FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 40, "/catalog.json"),
    FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 40, "a//b"),
    FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 40, ""),
    FetchKey("LavX/bazarr-provider-catalog", "main", "a" * 39, "catalog.json"),
    FetchKey("LavX/bazarr-provider-catalog", "main", "A" * 40, "catalog.json"),
    FetchKey("LavX/bazarr-provider-catalog", "main", "main", "catalog.json"),
    FetchKey("LavX/bazarr-provider-catalog", "main/../beta", "a" * 40, "catalog.json"),
    FetchKey("LavX/bazarr-provider-catalog", "..", "a" * 40, "catalog.json"),
    FetchKey("LavX/bazarr-provider-catalog", "a/b", "a" * 40, "catalog.json"),
    FetchKey("../bazarr-provider-catalog", "main", "a" * 40, "catalog.json"),
    FetchKey("LavX/..", "main", "a" * 40, "catalog.json"),
    FetchKey("LavX", "main", "a" * 40, "catalog.json"),
    FetchKey("LavX/x/y", "main", "a" * 40, "catalog.json"),
]


@pytest.mark.parametrize("key", BAD_KEYS, ids=lambda key: f"{key.repo}|{key.channel}|{key.commit[:6]}|{key.path}")
def test_a_key_that_is_not_a_catalog_file_is_refused_before_any_request(monkeypatch, key):
    net = _Net({})
    monkeypatch.setattr(transport.requests, "get", net)

    for origin in (RawOrigin(), PagesOrigin()):
        with pytest.raises(TransportError) as caught:
            origin.fetch(key)
        assert classify_failure(caught.value).kind is FailureKind.MALFORMED
    assert net.calls == []


def test_a_bundle_file_key_is_accepted():
    key = FetchKey("LavX/bazarr-provider-catalog", "beta", "0123456789abcdef" * 2 + "01234567",
                   "providers/opensubtitles_org/provider.json")
    assert RawOrigin().url(key).endswith("/providers/opensubtitles_org/provider.json")
    assert PagesOrigin().url(key).endswith("/v1/beta/" + key.commit + "/providers/opensubtitles_org/provider.json")


def test_the_mirror_needs_a_channel():
    with pytest.raises(TransportError):
        PagesOrigin().url(FetchKey("LavX/bazarr-provider-catalog", "", "a" * 40, "catalog.json"))


@pytest.mark.parametrize("kind", ["cooldowns", "retry_after"])
def test_a_persisted_expiry_beyond_the_maximum_is_cut_back_and_then_expires(kind):
    far = {"until": NOW + 10 ** 9, "at": NOW}
    transport.restore_state({kind: {("raw:catalog" if kind == "cooldowns" else "raw"): far}})
    raw, pages = _Fake("raw", [b"raw"]), _Fake("pages", [b"mirror"])

    assert _fetch([raw, pages], now=NOW).origin == "pages"
    assert raw.calls == 0
    assert _fetch([raw, pages], now=NOW + transport.MAX_COOLDOWN + 1).origin == "raw"


def test_an_etag_is_only_sent_to_the_origin_that_issued_it():
    raw = _Fake("raw", [_http_error(503)])
    pages = _Fake("pages", [b"mirror"])

    fetch_with_fallback(
        [raw, pages], KEY, surface="pointer", official=True,
        etags={"raw": '"raw-tag"', "pages": '"pages-tag"'}, clock=lambda: NOW)

    assert raw.etags == ['"raw-tag"']
    assert pages.etags == ['"pages-tag"']


def test_a_user_source_sends_the_primarys_etag_only():
    raw, pages = _Fake("raw", [b"raw"]), _Fake("pages", [b"mirror"])

    fetch_with_fallback(
        [raw, pages], KEY, surface="pointer", official=False,
        etags={"raw": '"raw-tag"', "pages": '"pages-tag"'})

    assert raw.etags == ['"raw-tag"'] and pages.calls == 0


# -- redirect bodies, TLS in error bodies, probes ------------------------------

class _CountingRaw:
    """A raw body of ``total`` bytes served in chunks, counting what is read."""

    def __init__(self, total, chunk=1 << 20):
        self.total, self.chunk, self.read_bytes, self.closed = total, chunk, 0, False

    def stream(self, amt=None, decode_content=None):
        while self.read_bytes < self.total:
            size = min(self.chunk, self.total - self.read_bytes)
            self.read_bytes += size
            yield b"x" * size

    def read(self, amt=None, decode_content=None, **_kwargs):
        return b"".join(self.stream())

    def close(self):
        self.closed = True

    def release_conn(self):
        self.closed = True


class _ScriptedAdapter(requests.adapters.BaseAdapter):
    def __init__(self, make):
        super().__init__()
        self.make = make
        self.sent = []

    def send(self, request, **_kwargs):
        self.sent.append(request.url)
        return self.make(request)

    def close(self):
        pass


def _through_a_real_session(monkeypatch, make):
    adapter = _ScriptedAdapter(make)
    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: adapter)
    return adapter


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_body_is_never_downloaded(monkeypatch, status):
    raw_body = _CountingRaw(256 * 1024 * 1024)

    def make(request):
        response = requests.Response()
        response.status_code = status
        response.headers["Location"] = "https://evil.example/x"
        response.raw = raw_body
        response.url = request.url
        response.request = request
        return response

    adapter = _through_a_real_session(monkeypatch, make)

    with pytest.raises(TransportError) as caught:
        RawOrigin().fetch(KEY)

    assert classify_failure(caught.value).kind is FailureKind.REDIRECT
    assert adapter.sent == [RAW_URL]
    assert raw_body.read_bytes == 0
    assert raw_body.closed


def test_the_real_session_path_still_serves_an_ordinary_answer(monkeypatch):
    def make(request):
        response = requests.Response()
        response.status_code = 200
        response.headers["ETag"] = '"t"'
        response.raw = io.BytesIO(b'{"ok": true}')
        response.url = request.url
        response.request = request
        return response

    _through_a_real_session(monkeypatch, make)

    result = RawOrigin().fetch(KEY)

    assert (result.content, result.etag) == (b'{"ok": true}', '"t"')


def test_a_tls_error_while_reading_an_error_body_does_not_fail_over(monkeypatch):
    class Broken(_Streamed):
        def iter_content(self, chunk_size=1):
            raise requests.exceptions.SSLError("bad record mac")
            yield b""

    net = _Net({RAW_URL: Broken(503, b"")})
    monkeypatch.setattr(transport.requests, "get", net)
    pages = PagesOrigin()
    monkeypatch.setattr(PagesOrigin, "fetch", lambda *a, **k: pytest.fail("the mirror was asked"))

    with pytest.raises(requests.exceptions.SSLError) as caught:
        _fetch([RawOrigin(), pages])

    assert classify_failure(caught.value).kind is FailureKind.TLS
    assert len(net.calls) == 1
    assert transport.export_state() == EMPTY


def test_an_ordinary_error_body_read_failure_keeps_the_status_handling(monkeypatch):
    class Cut(_Streamed):
        def iter_content(self, chunk_size=1):
            raise requests.exceptions.ChunkedEncodingError("cut")
            yield b""

    monkeypatch.setattr(transport.requests, "get", _Net({RAW_URL: Cut(503, b"")}))

    with pytest.raises(requests.HTTPError) as caught:
        RawOrigin().fetch(KEY)

    assert classify_failure(caught.value).kind is FailureKind.SERVER_ERROR


def test_a_304_without_an_etag_is_a_client_error(monkeypatch):
    monkeypatch.setattr(transport.requests, "get", _Net({RAW_URL: _Streamed(304, b"")}))

    with pytest.raises(TransportError) as caught:
        RawOrigin().fetch(KEY)

    assert classify_failure(caught.value).kind is FailureKind.CLIENT_ERROR


def test_a_304_through_the_fallback_is_served_for_the_origin_that_issued_the_etag(monkeypatch):
    seen = []

    def get(url, **kwargs):
        seen.append(kwargs["headers"])
        return _Streamed(304, b"")

    monkeypatch.setattr(transport.requests, "get", get)

    result = fetch_with_fallback(
        [RawOrigin(), PagesOrigin()], KEY, surface="pointer", official=True,
        etags={"raw": '"r"'}, clock=lambda: NOW)

    assert result.not_modified and result.origin == "raw" and result.etag == '"r"'
    assert seen == [{"If-None-Match": '"r"'}]


def test_a_304_for_an_origin_that_sent_no_etag_does_not_fail_over(monkeypatch):
    monkeypatch.setattr(transport.requests, "get", lambda url, **kw: _Streamed(304, b""))
    monkeypatch.setattr(PagesOrigin, "fetch", lambda *a, **k: pytest.fail("the mirror was asked"))

    with pytest.raises(TransportError) as caught:
        fetch_with_fallback([RawOrigin(), PagesOrigin()], KEY, surface="pointer", official=True,
                            clock=lambda: NOW)

    assert caught.value.kind is FailureKind.CLIENT_ERROR


def test_every_origin_parked_is_an_unavailable_failure_that_is_not_a_trigger():
    transport.restore_state({"retry_after": {"raw": NOW + 100, "pages": NOW + 100}})

    with pytest.raises(NoOriginAvailable) as caught:
        _fetch([_Fake("raw", [b"x"]), _Fake("pages", [b"x"])])

    failure = classify_failure(caught.value)
    assert failure.kind is FailureKind.UNAVAILABLE and not failure.trigger


def test_a_probe_mark_from_the_future_is_not_a_probe_in_flight():
    transport.restore_state({
        "retry_after": {"raw": {"until": NOW - 1, "at": NOW - 100}}, "probes": {"raw": NOW + 500},
    })
    raw, pages = _Fake("raw", [b"raw"]), _Fake("pages", [b"mirror"])

    assert _fetch([raw, pages], now=NOW).origin == "raw"


def test_a_probe_mark_older_than_the_window_is_stale():
    transport.restore_state({
        "retry_after": {"raw": {"until": NOW - 100, "at": NOW - 200}},
        "probes": {"raw": NOW - transport.PROBE_WINDOW - 1},
    })
    raw, pages = _Fake("raw", [b"raw"]), _Fake("pages", [b"mirror"])

    assert _fetch([raw, pages], now=NOW).origin == "raw"


def test_a_probe_that_ends_in_a_non_trigger_failure_releases_the_reservation():
    transport.restore_state({"retry_after": {"raw": {"until": NOW - 1, "at": NOW - 100}}})
    raw = _Fake("raw", [_http_error(404), b"raw"])
    pages = _Fake("pages", [b"mirror"])

    with pytest.raises(requests.HTTPError):
        _fetch([raw, pages], now=NOW)
    assert transport.export_state()["probes"] == {}

    # The next caller may probe at once instead of waiting out the window.
    assert _fetch([raw, pages], now=NOW + 1).content == b"raw"


def test_an_error_chain_is_not_followed_through_a_suppressed_context():
    try:
        try:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        except socket.gaierror:
            raise RuntimeError("something else entirely") from None
    except RuntimeError as error:
        assert classify_failure(error).kind is FailureKind.UNKNOWN


def test_a_chain_with_an_integrity_or_value_error_on_top_is_typed_by_it():
    try:
        try:
            raise ssl.SSLError("handshake")
        except ssl.SSLError as inner:
            raise BundleValidationError("hash mismatch") from inner
    except BundleValidationError as error:
        assert classify_failure(error).kind is FailureKind.INTEGRITY
    try:
        try:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")
        except socket.gaierror as inner:
            raise ValueError("bad payload") from inner
    except ValueError as error:
        assert classify_failure(error).kind is FailureKind.MALFORMED


def test_the_refresh_messages_are_the_ones_users_have_always_seen(monkeypatch):
    from provider_hub.service import _catalog_source_error_message as message

    monkeypatch.setattr(time, "time", lambda: 1789228800)
    unreachable = (
        "Could not reach GitHub while refreshing this catalog source. "
        "Check network or DNS and try again.")
    assert message(requests.exceptions.ReadTimeout("x")) == unreachable
    assert message(requests.exceptions.SSLError("x")) == unreachable
    # Exceptions that are not requests' own used to show their raw text. They
    # are now typed, so a body cut mid-transfer and the builtin timeout and
    # connection errors get the reachability wording.
    assert message(requests.exceptions.ChunkedEncodingError("Connection broken")) == unreachable
    assert message(TimeoutError("x")) == unreachable
    assert message(ConnectionResetError("x")) == unreachable
    assert message(_resolver_chain(socket.EAI_NONAME)) == unreachable
    assert message(_http_error(429, {"retry-after": "60"})) == (
        "GitHub temporarily limited catalog requests. Retry after 2026-09-12 16:01:00 UTC. "
        "Your cached catalog is unchanged.")
    assert message(_http_error(429)) == (
        "GitHub temporarily limited catalog requests. GitHub did not provide a reset time. "
        "Retry later. Your cached catalog is unchanged.")
    assert message(_http_error(403)) == (
        "GitHub denied access to this catalog source. Check the repository URL and access permissions.")
    does_not_have = (
        "GitHub does not have this catalog source. Check the repository URL and that its "
        "catalog file exists.")
    assert message(_http_error(404)) == does_not_have
    assert message(_http_error(401)) == does_not_have
    assert message(_http_error(410)) == "GitHub returned HTTP 410 while refreshing this catalog source."
    assert message(_http_error(503)) == "GitHub returned HTTP 503 while refreshing this catalog source."
    assert message(requests.HTTPError("no response")) == (
        "GitHub returned an error while refreshing this catalog source.")
    assert message(ValueError("catalog payload must be an object")) == "catalog payload must be an object"


def test_a_certificate_verification_error_is_tls_not_a_malformed_payload():
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    assert isinstance(error, ValueError)
    failure = classify_failure(error)
    assert failure.kind is FailureKind.TLS and not failure.trigger


def test_a_future_recording_time_is_cut_back_to_now():
    transport.restore_state({"cooldowns": {"raw:catalog": {"until": NOW + 30, "at": NOW + 10 ** 6}}})

    assert transport._admit("raw", "catalog", NOW) is None

    entry = transport.export_state()["cooldowns"]["raw:catalog"]
    assert entry["at"] == NOW and entry["until"] == NOW + 30


class _Held(_FakeOrigin):
    """Request one blocks until released; every later request is a 429."""

    name = "raw"

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def fetch(self, key, etag=None, *, timeout=30.0, max_bytes=0):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            assert self.release.wait(10)
            return transport.FetchResult(b"late success", "raw")
        raise _http_error(429, {"retry-after": "60"})


def test_a_success_admitted_earlier_keeps_a_rate_limit_recorded_in_the_same_second():
    # A constant clock gives every reading the same value, so only the order
    # of events can tell the limit is newer than the request that succeeds.
    held, pages = _Held(), _Fake("pages", [b"mirror"])
    outcome = []

    def first_job():
        outcome.append(fetch_with_fallback(
            [held, pages], KEY, surface="catalog", official=True, clock=lambda: NOW))

    worker = threading.Thread(target=first_job)
    worker.start()
    assert held.entered.wait(10)
    # Admitted after the first job, it meets a rate limit and records it.
    assert fetch_with_fallback(
        [held, pages], KEY, surface="catalog", official=True, clock=lambda: NOW).origin == "pages"
    held.release.set()
    worker.join(10)

    assert outcome[0].content == b"late success"
    assert "raw" in transport.export_state()["retry_after"]


def _probe_pair():
    transport.restore_state({"retry_after": {"raw": {"until": NOW - 1, "at": NOW - 100}}})
    first = transport._admit("raw", "catalog", NOW)
    # The first probe never reports back; its mark goes stale and a second
    # caller takes the reservation over.
    second = transport._admit("raw", "catalog", NOW + transport.PROBE_WINDOW + 1)
    return first, second


def test_admission_hands_out_one_token_per_probe():
    first, second = _probe_pair()
    assert first.probe_token is not None and second.probe_token is not None
    assert first.probe_token != second.probe_token
    assert transport._admit("raw", "catalog", NOW + transport.PROBE_WINDOW + 2) is None


def test_a_stale_probe_cannot_release_the_reservation_that_replaced_it():
    first, second = _probe_pair()

    transport.release_probe("raw", first)

    assert transport.export_state()["probes"]["raw"]["token"] == second.probe_token
    transport.release_probe("raw", second)
    assert transport.export_state()["probes"] == {}


def test_a_stale_probe_failing_does_not_retire_the_reset_the_new_probe_is_testing():
    first, second = _probe_pair()

    transport.record_failure(
        "raw", "catalog", classify_failure(_http_error(503), now=NOW + 100), NOW + 100, first)

    record = transport.export_state()
    assert "raw" in record["retry_after"]
    assert record["probes"]["raw"]["token"] == second.probe_token


def test_a_stale_probe_succeeding_changes_nothing_and_the_new_holder_completes_the_recovery():
    first, second = _probe_pair()

    transport.record_success("raw", "catalog", first)

    record = transport.export_state()
    assert "raw" in record["retry_after"]
    assert record["probes"]["raw"]["token"] == second.probe_token
    # The surviving reservation still keeps every other caller away ...
    assert transport._admit("raw", "catalog", NOW + transport.PROBE_WINDOW + 2) is None

    # ... and its own completion is what lifts the reset.
    transport.record_success("raw", "catalog", second)
    record = transport.export_state()
    assert record["retry_after"] == {} and record["probes"] == {}
    assert transport._admit("raw", "catalog", NOW + transport.PROBE_WINDOW + 3) is not None


def test_a_probe_that_fails_releases_its_reservation_whatever_the_failure():
    transport.restore_state({"retry_after": {"raw": {"until": NOW - 1, "at": NOW - 100}}})
    probe = transport._admit("raw", "catalog", NOW)

    transport.record_failure("raw", "catalog", classify_failure(_http_error(503), now=NOW), NOW, probe)

    record = transport.export_state()
    assert record["probes"] == {} and record["retry_after"] == {}
    assert record["cooldowns"]["raw:catalog"]["kind"] == "server_error"


@pytest.mark.parametrize("status", [201, 204, 206])
def test_only_a_200_is_a_whole_document(monkeypatch, status):
    monkeypatch.setattr(transport.requests, "get", _Net({RAW_URL: _Streamed(status, b"{}")}))

    with pytest.raises(TransportError) as caught:
        RawOrigin().fetch(KEY)

    assert classify_failure(caught.value).kind is FailureKind.CLIENT_ERROR


def test_the_mirror_refuses_a_key_for_another_repository(monkeypatch):
    net = _Net({})
    monkeypatch.setattr(transport.requests, "get", net)
    other = FetchKey("someone/else", "main", "a" * 40, "catalog.json")

    with pytest.raises(TransportError) as caught:
        PagesOrigin().fetch(other)

    assert classify_failure(caught.value).kind is FailureKind.MALFORMED
    assert net.calls == []
    assert RawOrigin().url(other).startswith("https://raw.githubusercontent.com/someone/else/")


def test_a_sibling_fetch_that_failed_over_keeps_this_one_off_the_primary():
    # Two fetches of one job (say, two files of a bundle). B reads the clock
    # just before it is admitted; while it waits there, A fails over.
    route = JobRoute()
    raw = _Fake("raw", [_http_error(503)])
    pages = _Fake("pages", [b"mirror"])
    before_admission = threading.Event()
    go = threading.Event()
    outcome = []

    def b_clock():
        before_admission.set()
        assert go.wait(10)
        return NOW

    def b_job():
        outcome.append(fetch_with_fallback(
            [raw, pages], KEY, surface="bundle", official=True, route=route, clock=b_clock))

    worker = threading.Thread(target=b_job)
    worker.start()
    assert before_admission.wait(10)

    assert fetch_with_fallback(
        [raw, pages], KEY, surface="catalog", official=True, route=route, clock=lambda: NOW,
    ).origin == "pages"
    assert route.sticky == "pages"
    go.set()
    worker.join(10)

    assert outcome[0].origin == "pages"
    assert raw.calls == 1  # only the first fetch ever asked the primary
    assert route.failovers == 1


def test_the_mirror_is_not_claimed_while_it_is_parked():
    transport.restore_state({"cooldowns": {"pages:catalog": {"until": NOW + 100}}})
    route = JobRoute()
    raw, pages = _Fake("raw", [_http_error(503)]), _Fake("pages", [b"mirror"])

    with pytest.raises(requests.HTTPError):
        _fetch([raw, pages], route=route)

    assert route.sticky is None and route.failovers == 0
