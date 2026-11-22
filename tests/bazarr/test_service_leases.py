# coding=utf-8
"""The service kind, ``requires`` resolved to a lease, and the lease lifecycle.

A provider call gets the address of a service it requires through a lease and never keeps it. A remote service (an
address the user typed) is a tokenless lease on a local HTTP server that plays FlareSolverr. A managed service is a
real front over the fake Docker daemon from the front runtime tests. No test reaches the network.
"""
import logging
import pickle
import threading
import time
from types import SimpleNamespace

import pytest

from provider_hub import capabilities, endpoints, leases, service_health, state
from provider_hub.kinds import SERVICE, get_kind, kind_for_api, known_kinds
from provider_hub.kinds.service import SERVICE_KIND_API
from provider_hub.manifest import ManifestValidationError

from test_flaresolverr_address_test import FakeFlareSolverr
import test_plugin_front_runtime as fronts
from test_plugin_front_runtime import PLUGIN as FRONT_PLUGIN, make_front

# The fake daemon, the front harness and the helper that builds a real front on it are the front runtime tests' own.
docker = fronts.docker
harness = fronts.harness
runtimes = fronts.runtimes
never_the_real_daemon = fronts.never_the_real_daemon

FIELD = "flaresolverr_url"
CAPABILITY = endpoints.FLARESOLVERR_CAPABILITY
ADDRESS = "http://solver.lan:8191"
WAIT = 10


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(endpoints.FLARESOLVERR_ENV, raising=False)
    monkeypatch.setattr(service_health, "_checks", lambda: {})
    service_health.forget()
    yield
    # No test leaves a lease, a managed service or a health check behind for the next one.
    for lease in leases._LEDGER.select():
        lease.close()
    with leases._MANAGED_LOCK:
        leases._MANAGED.clear()
    for thread in threading.enumerate():
        if thread.name.startswith(("hub-health-", "hub-stop-")) and thread is not threading.current_thread():
            thread.join(WAIT)
    service_health.forget()


def _settings(url=ADDRESS):
    return {"endpoints": {endpoints.FLARESOLVERR_SETTING_KEY: url}}


def _installation(trusted=False, manifest=None):
    return {"provider_id": "alpha", "manifest": manifest if manifest is not None else {}, "trusted": trusted}


def _manifest(properties=None, **extra):
    raw = {"id": "alpha", "config_schema": {"type": "object", "properties": (
        {FIELD: {"type": "string", "default": ""}} if properties is None else properties)}}
    raw.update(extra)
    return SimpleNamespace(raw=raw)


def _live(monkeypatch, row="trusted"):
    """What the host's record says about the plugin now. ``None`` is a release that is not in force."""
    value = {"trusted": True} if row == "trusted" else row
    monkeypatch.setattr(state, "load_state", lambda: {"installations": {"alpha": value}})


_DEFAULT = object()


def _scope(manifest=_DEFAULT, settings=None, owner="alpha#1"):
    return capabilities.CallLeases.for_provider(
        "alpha", _manifest() if manifest is _DEFAULT else manifest, owner, settings or _settings())


def _wait_for(condition, seconds=WAIT):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# --------------------------------------------------------------------------- the kind


class TestServiceKind:
    def test_the_kind_is_defined_and_stops_live(self):
        kind = get_kind(SERVICE)

        assert SERVICE in known_kinds()
        assert kind_for_api(SERVICE_KIND_API) is kind
        assert kind.detach_policy == "live"
        assert kind.attach_policy == "restart"
        assert kind.noun == "service"

    def _manifest(self, **overrides):
        digest = "a" * 64
        manifest = {
            "schema_version": 2, "id": "flaresolverr", "kind": SERVICE, "kind_api": SERVICE_KIND_API,
            "name": "FlareSolverr", "version": "3.3.21", "provides": [CAPABILITY],
            "runtime": {"artifact": "oci", "process": "daemon", "image": "ghcr.io/flaresolverr/flaresolverr",
                        "index_digest": "sha256:" + digest},
            "permissions": ["docker", "network"],
            "files": {"README.md": digest}, "bundle_sha256": digest,
            "source": {"type": "github", "repo": "owner/repo", "ref": "main", "commit": "b" * 40, "path": "x"},
        }
        manifest.update(overrides)
        return manifest

    def test_a_service_manifest_validates_with_the_shared_bundle_checks(self):
        validated = get_kind(SERVICE).validate_manifest(self._manifest(), True)

        assert (validated.provider_id, validated.kind, validated.api_version) == ("flaresolverr", SERVICE, SERVICE_KIND_API)

    def test_a_service_that_provides_nothing_is_refused(self):
        with pytest.raises(ManifestValidationError, match="provide at least one capability"):
            get_kind(SERVICE).validate_manifest(self._manifest(provides=[]), True)

    def test_another_kinds_manifest_is_refused(self):
        with pytest.raises(ManifestValidationError):
            get_kind(SERVICE).validate_manifest(self._manifest(kind="translator", kind_api="bazarr.translator.v1"), True)

    def test_the_managed_runtime_stays_dormant_unless_the_host_offers_it(self):
        from provider_hub.compatibility import HostFacts, entry_compatibility

        host = HostFacts(version="2.8.0", platform="linux-x86_64", runtimes=frozenset({("python", "worker")}),
                         in_container=True)

        compatible, reason = entry_compatibility({"manifest": self._manifest()}, host)

        assert compatible is False and "oci daemon runtime" in reason
        offered = HostFacts(version="2.8.0", platform="linux-x86_64",
                            runtimes=frozenset({("python", "worker"), ("oci", "daemon")}), in_container=True)
        assert entry_compatibility({"manifest": self._manifest()}, offered) == (True, None)


# --------------------------------------------------------------------------- the lease object


class TestLease:
    def test_a_remote_lease_is_the_typed_address_with_no_token(self):
        lease = leases.DirectLease(CAPABILITY, "alpha#1", ADDRESS, leases.SOURCE_SETTING)

        assert lease.url() == ADDRESS
        assert lease.external is True and lease.service_id is None
        assert leases.open_count(owner="alpha#1") == 1

    def test_closing_it_ends_it_and_is_safe_twice(self):
        lease = leases.DirectLease(CAPABILITY, "alpha#1", ADDRESS, leases.SOURCE_ENV)

        lease.close()
        lease.close()

        assert lease.closed and leases.open_count() == 0
        with pytest.raises(leases.LeaseError):
            lease.url()

    def test_a_context_manager_closes_on_the_way_out(self):
        with leases.DirectLease(CAPABILITY, "alpha#1", ADDRESS, leases.SOURCE_ENV) as lease:
            assert leases.open_count() == 1
        assert lease.closed

    def test_it_is_not_printed_copied_or_pickled_with_its_address(self):
        lease = leases.DirectLease(CAPABILITY, "alpha#1", "http://user:hunter2pw@solver.lan:8191", leases.SOURCE_SETTING)

        assert "solver.lan" not in repr(lease) and "hunter2pw" not in repr(lease)
        with pytest.raises(TypeError):
            pickle.dumps(lease)

    def test_credentials_in_a_typed_address_are_masked_in_logs(self, caplog):
        from secret_store.scrub import scrub_text

        leases.DirectLease(CAPABILITY, "alpha#1", "http://someone:sw0rdf1shpw@solver.lan:8191", leases.SOURCE_SETTING)

        assert "sw0rdf1shpw" not in scrub_text("failed to reach http://someone:sw0rdf1shpw@solver.lan:8191")

    def test_a_managed_lease_closes_the_front_lease_it_wraps(self):
        front = SimpleNamespace(closed=0, url=lambda: "http://front/l/token/v1")
        front.close = lambda: setattr(front, "closed", front.closed + 1)
        lease = leases.ManagedLease(CAPABILITY, "alpha#1", "flaresolverr", front)

        assert lease.url() == "http://front/l/token/v1" and lease.service_id == "flaresolverr"
        lease.close()
        lease.close()
        assert front.closed == 1

    def test_a_front_that_cannot_answer_is_a_lease_error_without_its_text(self):
        def broken():
            raise RuntimeError("http://front/l/secrettoken/v1 refused")

        lease = leases.ManagedLease(CAPABILITY, "alpha#1", "flaresolverr",
                                    SimpleNamespace(url=broken, close=lambda: None))

        with pytest.raises(leases.LeaseError) as error:
            lease.url()
        assert "secrettoken" not in str(error.value)

    def test_a_front_that_fails_to_close_is_logged_by_kind_and_never_raises(self, caplog):
        def broken():
            raise RuntimeError("http://front/l/secrettoken/v1 refused")

        lease = leases.ManagedLease(CAPABILITY, "alpha#1", "flaresolverr",
                                    SimpleNamespace(url=lambda: "x", close=broken))

        with caplog.at_level(logging.WARNING):
            lease.close()

        assert "secrettoken" not in caplog.text and "RuntimeError" in caplog.text and lease.closed


# --------------------------------------------------------------------------- resolving a requirement


class TestOpenLease:
    def test_an_untrusted_plugin_that_requires_it_gets_a_tokenless_lease(self):
        installation = _installation(manifest={"requires": [CAPABILITY]})

        lease = capabilities.open_lease(CAPABILITY, installation, _settings(), owner="alpha#1")

        assert isinstance(lease, leases.DirectLease) and lease.url() == ADDRESS

    def test_a_trusted_plugin_gets_one_unasked(self):
        assert capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(), owner="a").url() == ADDRESS

    @pytest.mark.parametrize("flag", [False, "true", 1, None])
    def test_an_unflagged_untrusted_plugin_never_gets_one(self, flag):
        installation = _installation(manifest={"flaresolverr": flag})

        assert capabilities.open_lease(CAPABILITY, installation, _settings(), owner="a") is None

    def test_the_older_flag_still_counts(self):
        installation = _installation(manifest={"flaresolverr": True})

        assert capabilities.open_lease(CAPABILITY, installation, _settings(), owner="a").url() == ADDRESS

    def test_the_environment_address_is_leased_when_no_setting_is_saved(self, monkeypatch):
        monkeypatch.setenv(endpoints.FLARESOLVERR_ENV, "http://from-env:8191")

        lease = capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a")

        assert lease.url() == "http://from-env:8191" and lease.source == leases.SOURCE_ENV

    def test_with_no_address_and_no_service_there_is_no_lease(self):
        assert capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a") is None

    def test_a_capability_the_host_does_not_lease_gives_nothing(self):
        assert capabilities.open_lease("host.captcha.v1", _installation(trusted=True), _settings(), owner="a") is None
        assert capabilities.open_lease("other.thing.v1", _installation(trusted=True), _settings(), owner="a") is None

    def test_a_failure_gives_no_lease_and_logs_only_its_kind(self, monkeypatch, caplog):
        def broken(settings=None):
            raise RuntimeError("http://user:pw@solver.lan:8191 exploded")

        monkeypatch.setattr(endpoints, "flaresolverr_url_source", broken)

        with caplog.at_level(logging.WARNING):
            assert capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(), owner="a") is None

        assert "RuntimeError" in caplog.text and "pw@" not in caplog.text


def _managed(service_id="flaresolverr", *, ready=True, stop=None):
    """A running service that leases through a fake front lease; ``front.opened`` lists what it gave out."""
    front = SimpleNamespace(opened=[], ready=ready)

    def open_lease():
        number = len(front.opened) + 1
        lease = SimpleNamespace(closed=False, url=lambda: f"http://front.internal/l/tok{number}/v1")
        lease.close = lambda: setattr(lease, "closed", True)
        front.opened.append(lease)
        return lease

    leases.register_managed(service_id, {CAPABILITY}, open_lease, ready=lambda: front.ready, stop=stop)
    return front


def _service_row(monkeypatch, service_id="flaresolverr", **row):
    """The record of a service that is in force, and of the provider that calls it (trusted, as alpha)."""
    value = {"provider_id": service_id, "kind": SERVICE, "enabled": True, **row}
    monkeypatch.setattr(state, "load_state",
                        lambda: {"installations": {"alpha": {"trusted": True}, service_id: value}})
    return value


class TestManagedBinding:
    def test_a_managed_service_is_leased_when_no_address_was_typed(self, monkeypatch):
        _service_row(monkeypatch)
        front = _managed()

        lease = capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="alpha#1")

        assert isinstance(lease, leases.ManagedLease) and lease.url() == "http://front.internal/l/tok1/v1"
        assert lease.service_id == "flaresolverr" and not lease.external and len(front.opened) == 1

    def test_an_address_the_user_typed_wins_over_a_managed_service(self, monkeypatch):
        _service_row(monkeypatch)
        front = _managed()

        lease = capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(), owner="alpha#1")

        assert lease.url() == ADDRESS and front.opened == []

    def test_a_service_that_is_not_ready_gives_no_lease(self, monkeypatch):
        _service_row(monkeypatch)
        front = _managed(ready=False)

        assert capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a") is None
        assert front.opened == []

    @pytest.mark.parametrize("row", [None, {"revoked": True}, {"blocked": True}, {"enabled": False}])
    def test_a_service_that_is_not_in_force_is_not_offered(self, monkeypatch, row):
        if row is None:
            monkeypatch.setattr(state, "load_state", lambda: {"installations": {}})
        else:
            _service_row(monkeypatch, **row)
        front = _managed()

        assert leases.managed_service_for(CAPABILITY) is None
        assert capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a") is None
        assert front.opened == []

    def test_a_front_that_refuses_a_lease_gives_none_and_leaves_nothing_open(self, monkeypatch):
        _service_row(monkeypatch)

        def refuse():
            raise RuntimeError("the front is not running")

        leases.register_managed("flaresolverr", {CAPABILITY}, refuse)

        assert capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a") is None
        assert leases.open_count() == 0

    def test_a_service_detached_while_a_lease_is_being_opened_leaves_no_lease(self, monkeypatch):
        _service_row(monkeypatch)
        front = SimpleNamespace(opened=[])

        def open_lease():
            # The detach lands between the front handing the token out and the host taking the lease.
            leases.unregister_managed("flaresolverr")
            lease = SimpleNamespace(closed=False, url=lambda: "http://front/l/x/v1")
            lease.close = lambda: setattr(lease, "closed", True)
            front.opened.append(lease)
            return lease

        leases.register_managed("flaresolverr", {CAPABILITY}, open_lease)

        assert capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a") is None
        assert front.opened[0].closed is True and leases.open_count() == 0

    def test_the_card_says_who_answers(self, monkeypatch):
        _service_row(monkeypatch)
        installation = _installation(trusted=True, manifest={"requires": [CAPABILITY]})
        _managed()

        assert capabilities.resolve(CAPABILITY, installation, _settings("")).as_dict() == {
            "capability": CAPABILITY, "source": "managed", "ready": True, "reason": ""}
        leases.register_managed("flaresolverr", {CAPABILITY}, lambda: None, ready=lambda: False)
        starting = capabilities.resolve(CAPABILITY, installation, _settings("")).as_dict()
        assert starting["source"] == "managed" and starting["ready"] is False and "not running" in starting["reason"]


# --------------------------------------------------------------------------- remote mode and its health


class TestRemoteBinding:
    def test_the_card_calls_a_typed_address_external(self):
        installation = _installation(trusted=True, manifest={"requires": [CAPABILITY]})

        report = capabilities.resolve(CAPABILITY, installation, _settings()).as_dict()

        assert report == {"capability": CAPABILITY, "source": "setting", "ready": True, "reason": "", "external": True}

    def test_a_managed_or_missing_binding_is_not_external(self):
        installation = _installation(trusted=True)

        report = capabilities.resolve(CAPABILITY, installation, _settings("")).as_dict()

        assert report["source"] == "none" and "external" not in report and "health" not in report

    def test_the_test_of_the_address_in_use_is_the_health_the_card_shows(self):
        server = FakeFlareSolverr()
        try:
            settings = _settings(server.address)
            result = endpoints.run_flaresolverr_test("", settings)
            assert result["ok"] is True

            health = service_health.last(CAPABILITY, server.address)
            report = capabilities.resolve(CAPABILITY, _installation(trusted=True), settings).as_dict()

            assert (health["state"], health["code"]) == ("ready", "ok") and health["version"] == "3.3.21"
            assert report["health"] == health
        finally:
            server.stop()

    def test_a_test_of_a_different_typed_address_is_not_the_health_of_the_binding(self):
        server = FakeFlareSolverr()
        try:
            endpoints.run_flaresolverr_test(server.address, _settings("http://elsewhere.lan:8191"))

            assert service_health.last(CAPABILITY, "http://elsewhere.lan:8191") is None
        finally:
            server.stop()

    def test_a_service_that_does_not_answer_is_unreachable(self):
        server = FakeFlareSolverr()
        address = server.address
        server.stop()

        endpoints.run_flaresolverr_test("", _settings(address))

        assert service_health.last(CAPABILITY, address)["state"] == "unreachable"

    def test_the_root_and_the_v1_endpoint_are_one_service(self):
        service_health.record(CAPABILITY, "http://solver.lan:8191/v1", {"ok": True, "code": "ok"})

        assert service_health.last(CAPABILITY, "http://SOLVER.lan:8191/")["state"] == "ready"

    def test_a_changed_address_shows_no_result_until_it_is_tested(self):
        service_health.record(CAPABILITY, ADDRESS, {"ok": True, "code": "ok"})

        assert service_health.last(CAPABILITY, "http://other.lan:8191") is None

    def test_no_address_is_kept_with_the_result(self):
        service_health.record(CAPABILITY, "http://user:topsecretpw@solver.lan:8191", {"ok": True, "code": "ok"})

        held = repr(service_health._records)

        assert "topsecretpw" not in held and "solver.lan" not in held

    def test_using_the_service_asks_again_in_the_background_when_the_answer_is_old(self, monkeypatch):
        monkeypatch.setattr(service_health, "_checks", lambda: {CAPABILITY: endpoints.check_flaresolverr_address})
        server = FakeFlareSolverr()
        try:
            lease = capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(server.address), owner="a")
            assert lease is not None
            assert _wait_for(lambda: service_health.last(CAPABILITY, server.address) is not None)
            assert service_health.last(CAPABILITY, server.address)["state"] == "ready"
            hits = len(server.requests)

            # A fresh answer is not asked for again, call after call.
            for _ in range(3):
                capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(server.address), owner="a")
            time.sleep(0.2)
            assert len(server.requests) == hits
        finally:
            server.stop()

    def test_an_answer_that_has_gone_stale_is_asked_for_again(self, monkeypatch):
        monkeypatch.setattr(service_health, "_checks", lambda: {CAPABILITY: endpoints.check_flaresolverr_address})
        server = FakeFlareSolverr()
        try:
            service_health.record(CAPABILITY, server.address, {"ok": False, "code": "unreachable"})
            monkeypatch.setattr(service_health, "FRESH_SECONDS", 0.0)

            thread = service_health.note_use(CAPABILITY, server.address)

            assert thread is not None
            thread.join(WAIT)
            assert service_health.last(CAPABILITY, server.address)["state"] == "ready"
        finally:
            server.stop()

    def test_one_check_runs_at_a_time(self, monkeypatch):
        release = threading.Event()
        calls = []

        def slow(address):
            calls.append(address)
            release.wait(WAIT)
            return {"ok": True, "code": "ok"}

        monkeypatch.setattr(service_health, "_checks", lambda: {CAPABILITY: slow})

        first = service_health.note_use(CAPABILITY, ADDRESS)
        second = service_health.note_use(CAPABILITY, ADDRESS)
        release.set()
        first.join(WAIT)

        assert first is not None and second is None and calls == [ADDRESS]

    def test_a_check_that_raises_is_recorded_unreachable_and_logged_by_kind(self, monkeypatch, caplog):
        def broken(address):
            raise RuntimeError(address + " exploded")

        monkeypatch.setattr(service_health, "_checks", lambda: {CAPABILITY: broken})

        with caplog.at_level(logging.WARNING):
            service_health.note_use(CAPABILITY, "http://user:pw@solver.lan:8191").join(WAIT)

        assert "pw@" not in caplog.text and "RuntimeError" in caplog.text
        assert service_health.last(CAPABILITY, "http://user:pw@solver.lan:8191")["state"] == "unreachable"


# --------------------------------------------------------------------------- what a call holds


class TestCallLeases:
    def test_the_request_gets_the_leased_address_and_the_held_config_does_not(self, monkeypatch):
        _live(monkeypatch)
        scope = _scope()
        held = {FIELD: "", "apikey": "k"}

        sent = scope.config(held)

        assert sent == {FIELD: ADDRESS, "apikey": "k"} and held[FIELD] == ""
        scope.close()

    def test_a_value_the_user_set_for_the_provider_is_kept_and_leases_nothing(self, monkeypatch):
        _live(monkeypatch)
        scope = _scope()
        held = {FIELD: "http://own:8191"}

        assert scope.config(held) is held
        assert leases.open_count() == 0

    def test_a_lease_is_shared_by_the_requests_of_one_call_and_closed_at_its_end(self, monkeypatch):
        _live(monkeypatch)
        scope = _scope()

        scope.config({FIELD: ""})
        scope.config({FIELD: ""})
        assert leases.open_count(owner="alpha#1") == 1

        scope.close()
        scope.close()
        assert leases.open_count() == 0

    def test_a_managed_address_changes_with_the_front_at_each_request(self, monkeypatch):
        _service_row(monkeypatch)
        front = SimpleNamespace(address="http://front-a/l/t/v1")
        lease = SimpleNamespace(url=lambda: front.address, close=lambda: None)
        leases.register_managed("flaresolverr", {CAPABILITY}, lambda: lease)
        scope = _scope(settings=_settings(""))

        first = scope.config({FIELD: ""})[FIELD]
        front.address = "http://front-b/l/t/v1"

        assert (first, scope.config({FIELD: ""})[FIELD]) == ("http://front-a/l/t/v1", "http://front-b/l/t/v1")

    def test_one_state_read_covers_the_provider_and_managed_service_for_a_call(self, monkeypatch):
        reads = []
        revoked = False

        def load_state():
            reads.append(True)
            return {"installations": {
                "alpha": {"trusted": True},
                "flaresolverr": {"kind": SERVICE, "enabled": True, "revoked": revoked},
            }}

        monkeypatch.setattr(state, "load_state", load_state)
        _managed()
        config = {FIELD: ""}
        scope = _scope(settings=_settings(""))

        assert scope.config(config)[FIELD] == "http://front.internal/l/tok1/v1"
        assert scope.config(config)[FIELD] == "http://front.internal/l/tok1/v1"
        assert len(reads) == 1
        scope.close()

        revoked = True
        next_scope = _scope(settings=_settings(""))
        assert next_scope.config(config) is config
        assert len(reads) == 2
        next_scope.close()

    def test_a_provider_with_no_field_or_no_manifest_pays_for_nothing(self, monkeypatch):
        monkeypatch.setattr(state, "load_state", lambda: pytest.fail("no state may be read"))

        for manifest in (None, SimpleNamespace(raw=None), _manifest(properties={"apikey": {}}),
                         SimpleNamespace(raw={"config_schema": []})):
            config = {FIELD: ""}
            scope = _scope(manifest=manifest)
            assert scope.config(config) is config
            scope.close()

    def test_with_nothing_typed_and_nothing_managed_no_state_is_read(self, monkeypatch):
        monkeypatch.setattr(state, "load_state", lambda: pytest.fail("no state may be read"))
        scope = _scope(settings=_settings(""))
        config = {FIELD: ""}

        assert scope.config(config) is config and scope.config(config) is config

    def test_a_release_that_is_not_in_force_leases_nothing(self, monkeypatch):
        _live(monkeypatch, row=None)
        scope = _scope()
        config = {FIELD: ""}

        assert scope.config(config) is config and leases.open_count() == 0

    def test_an_untrusted_plugin_that_never_asked_leases_nothing(self, monkeypatch):
        _live(monkeypatch, row={"trusted": False})
        scope = _scope(manifest=_manifest())
        config = {FIELD: ""}

        assert scope.config(config) is config and leases.open_count() == 0

    def test_a_plugin_that_declares_it_leases_though_it_is_untrusted(self, monkeypatch):
        _live(monkeypatch, row={"trusted": False})
        scope = _scope(manifest=_manifest(requires=[CAPABILITY]))

        assert scope.config({FIELD: ""})[FIELD] == ADDRESS
        scope.close()

    def test_a_closed_scope_gives_nothing_more(self, monkeypatch):
        _live(monkeypatch)
        scope = _scope()
        scope.close()
        config = {FIELD: ""}

        assert scope.config(config) is config and leases.open_count() == 0

    def test_a_lease_that_cannot_give_its_address_leaves_the_key_empty(self, monkeypatch):
        _service_row(monkeypatch)
        leases.register_managed("flaresolverr", {CAPABILITY}, lambda: SimpleNamespace(
            url=lambda: (_ for _ in ()).throw(RuntimeError("down")), close=lambda: None))
        scope = _scope(settings=_settings(""))

        assert scope.config({FIELD: ""}) == {FIELD: ""}
        scope.close()


# --------------------------------------------------------------------------- a provider's calls


class FakeWorker:
    """A worker client that records the configuration of each request and can hold a call until released."""

    def __init__(self, gate=None):
        self.configs = []
        self.entered = threading.Event()
        self.gate = gate
        self.command = None
        self.process = None
        self.cancelled = threading.Event()

    def request(self, op, request, timeout=None, admit=None):
        self.configs.append(dict(request["config"]))
        self.entered.set()
        if self.gate is not None:
            for _ in range(WAIT * 100):
                if self.gate.wait(0.01):
                    break
                if self.cancelled.is_set():
                    raise RuntimeError("worker closed stdout")
            else:
                raise TimeoutError("fake worker gate was not released")
        return SimpleNamespace(payload={"candidates": []}, events=[], status_generation=None,
                               status_configuration_generation=None)

    def stop(self):
        pass

    def cancel(self):
        self.cancelled.set()

    def revoke_host_calls(self):
        pass

    def outcome_recorded(self):
        pass


def _proxy_class(manifest=None):
    from subzero.language import Language
    from provider_hub.registry import HubProxyProvider

    return type("Alpha", (HubProxyProvider,), {
        "provider_name": "alpha", "languages": {Language("eng")}, "manifest": manifest or _manifest()})


def _movie():
    from subliminal.video import Movie

    return Movie("movie.mkv", "Movie", year=2000)


def _search(proxy):
    from subzero.language import Language

    return proxy.list_subtitles(_movie(), {Language("eng")})


class TestProviderCall:
    def test_a_search_reaches_the_worker_with_the_leased_address_and_gives_it_back(self, monkeypatch):
        _live(monkeypatch)
        monkeypatch.setattr(endpoints, "flaresolverr_url_source", lambda settings=None: (ADDRESS, "setting"))
        worker = FakeWorker()
        proxy = _proxy_class()(worker_client=worker, **{FIELD: ""})

        _search(proxy)

        assert worker.configs == [{FIELD: ADDRESS}]
        assert proxy.config == {FIELD: ""} and leases.open_count() == 0

    def test_the_lease_is_held_while_the_call_runs(self, monkeypatch):
        _live(monkeypatch)
        monkeypatch.setattr(endpoints, "flaresolverr_url_source", lambda settings=None: (ADDRESS, "setting"))
        gate = threading.Event()
        worker = FakeWorker(gate=gate)
        proxy = _proxy_class()(worker_client=worker, **{FIELD: ""})
        done = []
        thread = threading.Thread(target=lambda: done.append(_search(proxy)))
        thread.start()
        try:
            assert worker.entered.wait(WAIT)
            assert leases.open_count(capability=CAPABILITY) == 1
        finally:
            gate.set()
            thread.join(WAIT)

        assert done == [[]] and leases.open_count() == 0

    def test_a_call_that_fails_gives_its_lease_back(self, monkeypatch):
        _live(monkeypatch)
        monkeypatch.setattr(endpoints, "flaresolverr_url_source", lambda settings=None: (ADDRESS, "setting"))
        worker = FakeWorker()

        def fail(*args, **kwargs):
            raise RuntimeError("worker closed stdout")

        worker.request = fail
        proxy = _proxy_class()(worker_client=worker, **{FIELD: ""})

        with pytest.raises(RuntimeError):
            _search(proxy)

        assert leases.open_count() == 0

    def test_a_download_and_its_member_selection_share_one_lease(self, monkeypatch):
        from provider_hub import registry

        _live(monkeypatch)
        monkeypatch.setattr(endpoints, "flaresolverr_url_source", lambda settings=None: (ADDRESS, "setting"))
        worker = FakeWorker()
        selected = []

        def select(payload, timeout=None):
            selected.append(dict(payload["config"]))
            return SimpleNamespace(payload={"member": "a.srt"})

        worker.select_archive_member = select

        def download(subtitle, payload, select_member_cb=None):
            assert leases.open_count() == 1
            select_member_cb([{"name": "a.srt"}])

        monkeypatch.setattr(registry, "worker_download_to_content", download)
        subtitle = SimpleNamespace(provider_payload={}, language="eng")
        monkeypatch.setattr(registry, "language_to_payload", lambda language: {"alpha3": "eng"})
        proxy = _proxy_class()(worker_client=worker, **{FIELD: ""})

        assert proxy.download_subtitle(subtitle) is True

        assert worker.configs == [{FIELD: ADDRESS}] and selected == [{FIELD: ADDRESS}]
        assert leases.open_count() == 0

    def test_a_provider_that_needs_no_service_is_called_as_before(self, monkeypatch):
        monkeypatch.setattr(state, "load_state", lambda: pytest.fail("no state may be read"))
        worker = FakeWorker()
        proxy = _proxy_class(manifest=_manifest(properties={"apikey": {}}))(worker_client=worker, apikey="k")

        _search(proxy)

        assert worker.configs == [{"apikey": "k"}]


# --------------------------------------------------------------------------- detach


class TestDetach:
    def test_retiring_a_provider_instance_gives_back_what_a_cut_call_held(self, monkeypatch):
        """A revoked or removed provider's calls are cut; the lease of a call whose thread has not ended yet goes with
        the instance."""
        from provider_hub import registry

        _live(monkeypatch)
        monkeypatch.setattr(endpoints, "flaresolverr_url_source", lambda settings=None: (ADDRESS, "setting"))
        gate = threading.Event()
        worker = FakeWorker(gate=gate)
        proxy = _proxy_class()(worker_client=worker, **{FIELD: ""})
        errors = []

        def call():
            try:
                _search(proxy)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=call)
        thread.start()
        try:
            assert worker.entered.wait(WAIT)
            assert leases.open_count(capability=CAPABILITY) == 1
            proxy.retire()

            registry._drain_instance(proxy, lambda: None, 0.05)

            assert worker.cancelled.is_set()
            assert leases.open_count() == 0
            thread.join(WAIT)
            assert len(errors) == 1
        finally:
            gate.set()
            thread.join(WAIT)

    def test_ending_an_instance_takes_back_a_lease_its_call_never_closed(self, monkeypatch):
        _live(monkeypatch)
        proxy = _proxy_class()(worker_client=FakeWorker(), **{FIELD: ""})
        proxy._call_leases()
        stranded = leases.DirectLease(CAPABILITY, proxy._lease_owner, ADDRESS, leases.SOURCE_SETTING)
        other = leases.DirectLease(CAPABILITY, "another#9", ADDRESS, leases.SOURCE_SETTING)

        proxy._terminate_holding()

        assert stranded.closed and not other.closed

    def test_a_drain_that_waits_for_the_call_leaves_the_lease_to_the_call(self, monkeypatch):
        from provider_hub import registry

        _live(monkeypatch)
        monkeypatch.setattr(endpoints, "flaresolverr_url_source", lambda settings=None: (ADDRESS, "setting"))
        gate = threading.Event()
        worker = FakeWorker(gate=gate)
        proxy = _proxy_class()(worker_client=worker, **{FIELD: ""})
        thread = threading.Thread(target=lambda: _search(proxy))
        thread.start()
        assert worker.entered.wait(WAIT)
        proxy.retire()
        drain = threading.Thread(target=registry._drain_instance, args=(proxy, lambda: None))
        drain.start()
        try:
            time.sleep(0.1)
            # The call is still running on the lease it took: a disabled provider finishes what it has.
            assert drain.is_alive() and leases.open_count() == 1
        finally:
            gate.set()
            thread.join(WAIT)
            drain.join(WAIT)

        assert leases.open_count() == 0

    def test_detaching_a_service_closes_every_lease_it_gave_and_stops_it(self, monkeypatch):
        _service_row(monkeypatch)
        stopped = threading.Event()
        front = _managed(stop=stopped.set)
        first = capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a#1")
        second = capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="b#1")
        remote = leases.DirectLease(CAPABILITY, "c#1", ADDRESS, leases.SOURCE_SETTING)

        threads = get_kind(SERVICE).detach("flaresolverr", withdraw=True, grace=15.0)
        for thread in threads:
            thread.join(WAIT)

        assert first.closed and second.closed and all(item.closed for item in front.opened)
        assert stopped.is_set()
        # A remote lease is nobody's service to take away.
        assert not remote.closed
        assert leases.managed_service_for(CAPABILITY) is None

    def test_a_service_that_is_only_switched_off_is_detached_the_same_way(self, monkeypatch):
        _service_row(monkeypatch)
        front = _managed()
        lease = capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a#1")

        assert get_kind(SERVICE).detach("flaresolverr", withdraw=False) == []

        assert lease.closed and front.opened[0].closed
        assert capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a#1") is None

    def test_a_service_that_was_never_started_detaches_quietly(self):
        assert get_kind(SERVICE).detach("flaresolverr", withdraw=True) == []

    def test_a_stop_that_fails_is_logged_by_kind_and_does_not_fail_the_detach(self, monkeypatch, caplog):
        _service_row(monkeypatch)

        def broken():
            raise RuntimeError("socket /var/run/docker.sock refused")

        _managed(stop=broken)

        with caplog.at_level(logging.WARNING):
            for thread in get_kind(SERVICE).detach("flaresolverr", withdraw=True):
                thread.join(WAIT)

        assert "RuntimeError" in caplog.text and "docker.sock" not in caplog.text

    def test_a_detach_through_the_hub_closes_the_leases_of_a_removed_service(self, monkeypatch):
        """The same hook the removal path calls (``detach.detach``)."""
        from provider_hub import detach

        _service_row(monkeypatch)
        front = _managed()
        capabilities.open_lease(CAPABILITY, _installation(trusted=True), _settings(""), owner="a#1")

        assert detach.detaches_live(SERVICE) is True
        drains = detach.detach("flaresolverr", SERVICE, withdraw=True, grace=detach.REMOVE_GRACE_SECONDS)
        detach.when_drained(drains, lambda: None, name="flaresolverr")
        assert detach.wait_idle(WAIT)

        assert all(item.closed for item in front.opened) and leases.open_count() == 0


# --------------------------------------------------------------------------- a managed service, end to end


class TestManagedFront:
    """A real front, started on the fake Docker daemon, hands out the leases a provider call takes."""

    def test_a_call_reaches_the_service_through_the_front_only_while_it_holds_the_lease(self, docker, harness, runtimes,
                                                                                       monkeypatch):
        front = make_front(docker, harness, runtimes)
        front.start()
        _service_row(monkeypatch, FRONT_PLUGIN)
        leases.register_managed(FRONT_PLUGIN, {CAPABILITY}, front.open_lease, ready=lambda: front.alive,
                                stop=lambda: front.close(0))
        scope = _scope(settings=_settings(""))

        address = scope.config({FIELD: ""})[FIELD]

        assert address.startswith("http://") and "/l/" in address and address.endswith("/v1")
        assert harness.post(address) == 200
        assert leases.open_count(service_id=FRONT_PLUGIN) == 1 and front.lease_count == 1
        scope.close()
        assert harness.post(address) == 401
        assert leases.open_count() == 0 and front.lease_count == 0

    def test_detaching_the_service_takes_every_token_back_and_removes_the_front(self, docker, harness, runtimes,
                                                                               monkeypatch):
        front = make_front(docker, harness, runtimes)
        front.start()
        _service_row(monkeypatch, FRONT_PLUGIN)
        leases.register_managed(FRONT_PLUGIN, {CAPABILITY}, front.open_lease, ready=lambda: front.alive,
                                stop=lambda: front.close(0))
        scope = _scope(settings=_settings(""))
        address = scope.config({FIELD: ""})[FIELD]
        assert harness.post(address) == 200

        for thread in get_kind(SERVICE).detach(FRONT_PLUGIN, withdraw=True):
            thread.join(WAIT)

        assert harness.post(address) == 401
        assert front.lease_count == 0 and front.status()["state"] in ("removed", "stopping")
        scope.close()
