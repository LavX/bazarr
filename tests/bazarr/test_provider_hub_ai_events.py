import io
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


class EventProvider:
    def __init__(self, events=None, fail_drain=False, fail_search=False):
        self.events = events
        self.fail_drain = fail_drain
        self.fail_search = fail_search
        self.drains = 0

    def search(self, video, languages, config):
        if self.fail_search:
            raise ValueError("search failed")
        return []

    def download(self, provider_payload, language, config):
        return b"subtitle"

    def drain_events(self):
        self.drains += 1
        if self.fail_drain:
            raise ValueError("bad event source")
        return self.events


@pytest.mark.parametrize("operation", ["search", "download"])
def test_successful_worker_operation_emits_provider_events(monkeypatch, operation):
    from provider_hub import worker_runner

    provider = EventProvider([{"type": "translation_quota", "remaining": 7}])
    monkeypatch.setattr(worker_runner, "_load_provider", lambda: (provider, {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"id": 1, "op": operation}) + "\n"))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    worker_runner.main()
    response = json.loads(output.getvalue())
    assert response["ok"] is True
    assert response["events"] == provider.events
    assert provider.drains == 1


def test_worker_event_failures_and_operation_failures_are_isolated(monkeypatch):
    from provider_hub import worker_runner

    provider = EventProvider(fail_drain=True)
    monkeypatch.setattr(worker_runner, "_load_provider", lambda: (provider, {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id":1,"op":"search"}\n'))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    worker_runner.main()
    assert json.loads(output.getvalue())["events"] == []

    provider.fail_search = True
    provider.drains = 0
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id":2,"op":"search"}\n'))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    worker_runner.main()
    response = json.loads(output.getvalue())
    assert response["ok"] is False
    assert response.get("events", []) == []
    assert provider.drains == 1

def test_failed_request_discards_events_before_next_success(monkeypatch):
    from provider_hub import worker_runner

    class StatefulProvider(EventProvider):
        def __init__(self):
            super().__init__()
            self.pending = []
            self.calls = 0

        def search(self, video, languages, config):
            self.calls += 1
            if self.calls == 1:
                self.pending.append({"type": "translation_quota", "remaining": 0})
                raise ValueError("failed after quota side effect")
            return []

        def drain_events(self):
            self.drains += 1
            events, self.pending = self.pending, []
            return events

    provider = StatefulProvider()
    monkeypatch.setattr(worker_runner, "_load_provider", lambda: (provider, {}))
    requests = '{"id":1,"op":"search"}\n{"id":2,"op":"search"}\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(requests))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    worker_runner.main()
    failed, succeeded = map(json.loads, output.getvalue().splitlines())
    assert failed["ok"] is False and "events" not in failed
    assert succeeded["ok"] is True and succeeded["events"] == []
    assert provider.drains == 2


def test_worker_events_are_bounded(monkeypatch):
    from provider_hub import worker_runner

    provider = EventProvider(
        [{"type": "oversized", "text": "x" * 5000}]
        + [{"type": "translation_quota", "remaining": i} for i in range(100)]
    )
    monkeypatch.setattr(worker_runner, "_load_provider", lambda: (provider, {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id":1,"op":"search"}\n'))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    worker_runner.main()
    events = json.loads(output.getvalue())["events"]
    assert events == [{"type": "translation_quota", "remaining": i} for i in range(8)]
    assert len(json.dumps(events, separators=(",", ":")).encode("utf-8")) <= 4096


def test_unserializable_deep_event_does_not_fail_search(monkeypatch):
    from provider_hub import worker_runner

    deep = {}
    cursor = deep
    for _ in range(1100):
        child = {}
        cursor["next"] = child
        cursor = child
    provider = EventProvider([deep])
    monkeypatch.setattr(worker_runner, "_load_provider", lambda: (provider, {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id":1,"op":"search"}\n'))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    worker_runner.main()
    response = json.loads(output.getvalue())
    assert response["ok"] is True
    assert response["events"] == []


def test_event_iteration_failure_does_not_fail_search(monkeypatch):
    from provider_hub import worker_runner

    class BrokenEvents(list):
        def __iter__(self):
            raise RuntimeError("unreadable event list")

    provider = EventProvider(BrokenEvents([{"type": "translation_quota"}]))
    monkeypatch.setattr(worker_runner, "_load_provider", lambda: (provider, {}))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id":1,"op":"search"}\n'))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    worker_runner.main()
    response = json.loads(output.getvalue())
    assert response["ok"] is True
    assert response["events"] == []


def test_non_json_events_are_dropped_without_killing_the_worker(monkeypatch):
    from provider_hub import worker_runner

    circular = {}
    circular["self"] = circular
    first_events = [
        {"type": "valid", "order": 1},
        {"bad": b"bytes"},
        {"bad": datetime(2026, 9, 24, tzinfo=timezone.utc)},
        {"bad": {1}},
        {"bad": float("nan")},
        circular,
        {"type": "valid", "order": 2},
    ]

    class TwoRequestProvider(EventProvider):
        def drain_events(self):
            self.drains += 1
            if self.drains == 1:
                return first_events
            return [{"type": "valid", "order": 3}]

    provider = TwoRequestProvider()
    monkeypatch.setattr(worker_runner, "_load_provider", lambda: (provider, {}))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('{"id":1,"op":"search"}\n{"id":2,"op":"search"}\n'),
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    worker_runner.main()

    first, second = map(json.loads, output.getvalue().splitlines())
    assert first["ok"] is True
    assert first["events"] == [
        {"type": "valid", "order": 1},
        {"type": "valid", "order": 2},
    ]
    assert second["ok"] is True
    assert second["events"] == [{"type": "valid", "order": 3}]


def test_quota_status_accepts_only_valid_typed_fields():
    from provider_hub import runtime_status

    runtime_status.clear()
    runtime_status.consume("examplehub", [
        {"type": "other", "remaining": 2},
        {"type": "translation_quota", "remaining": True, "limit": -1,
         "entitled": "yes", "exhausted": False, "reset_at": "x" * 65},
    ])
    status = runtime_status.get("examplehub")
    assert status["remaining"] is None
    assert status["limit"] is None
    assert status["entitled"] is None
    assert status["exhausted"] is False
    assert status["reset_at"] is None
    assert isinstance(status["reported_at"], str)
    assert runtime_status.get("otherhub") is None
    runtime_status.clear()
    assert runtime_status.get("examplehub") is None


def test_empty_quota_event_does_not_replace_known_status():
    from provider_hub import runtime_status

    runtime_status.clear()
    try:
        runtime_status.consume("examplehub", [
            {"type": "translation_quota", "remaining": 37},
            {"type": "translation_quota"},
            {"type": "translation_quota", "entitled": None, "exhausted": None,
             "remaining": None, "limit": None, "reset_at": None},
        ])
        assert runtime_status.get("examplehub")["remaining"] == 37
    finally:
        runtime_status.clear()


def test_quota_latest_event_wins_and_concurrent_providers_stay_isolated():
    from concurrent.futures import ThreadPoolExecutor

    from provider_hub import runtime_status

    runtime_status.clear()
    try:
        runtime_status.consume("first", [
            {"type": "translation_quota", "remaining": 5},
            {"type": "translation_quota", "remaining": 4},
        ])
        assert runtime_status.get("first")["remaining"] == 4

        def report(index):
            provider_id = f"provider{index}"
            runtime_status.consume(provider_id, [
                {"type": "translation_quota", "remaining": index},
            ])
            return provider_id, runtime_status.get(provider_id)["remaining"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            assert dict(pool.map(report, range(32))) == {
                f"provider{index}": index for index in range(32)
            }
        assert runtime_status.get("first")["remaining"] == 4
        runtime_status.clear("first")
        assert runtime_status.get("first") is None
        assert runtime_status.get("provider7")["remaining"] == 7
    finally:
        runtime_status.clear()


def test_provider_updates_and_removal_clear_account_quota(tmp_path, monkeypatch):
    from provider_hub import runtime_status, service
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(service, "_forget_credential_throttle", lambda provider_id: None)
    enabled_provider_ids = {"examplehub"}
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: list(enabled_provider_ids))
    monkeypatch.setattr(
        service, "_set_bazarr_provider_enabled",
        lambda provider_id, enabled: (enabled_provider_ids.add(provider_id)
                                      if enabled else enabled_provider_ids.discard(provider_id)) or True,
    )
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example",
            "active_version": "1.0.0",
            "state": "active",
            "enabled": True,
            "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
            "config": {},
        }
    }
    save_state(state)
    runtime_status.clear()
    try:
        runtime_status.consume("examplehub", [{"type": "translation_quota", "remaining": 2}])
        assert service.list_providers()[0]["runtime_status"]["remaining"] == 2
        service.update_provider("examplehub", config={"region": "new-account"})
        assert "runtime_status" not in service.list_providers()[0]

        runtime_status.consume("examplehub", [{"type": "translation_quota", "remaining": 1}])
        service.update_provider("examplehub", enabled=False)
        runtime_status.consume("examplehub", [{"type": "translation_quota", "remaining": 1}])
        assert "runtime_status" not in service.list_providers()[0]

        enabled_provider_ids.add("examplehub")
        runtime_status.consume("examplehub", [{"type": "translation_quota", "remaining": 0}])
        assert service.remove_installation("examplehub") is True
        assert runtime_status.get("examplehub") is None
        enabled_provider_ids.add("examplehub")
        runtime_status.consume("examplehub", [{"type": "translation_quota", "remaining": 0}])
        assert "runtime_status" not in service.list_providers()[0]
    finally:
        runtime_status.clear()


def test_worker_process_start_clears_quota_and_captures_new_generation(tmp_path):
    from provider_hub import runtime_status
    from provider_hub.worker import ProviderWorkerClient

    worker_file = tmp_path / "quota_worker.py"
    worker_file.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    response = {\n"
        "        'abi': 'bazarr.provider-worker.v1',\n"
        "        'id': request['id'],\n"
        "        'ok': True,\n"
        "        'payload': {},\n"
        "        'events': [],\n"
        "    }\n"
        "    print(json.dumps(response), flush=True)\n"
        "    if request['op'] == 'shutdown':\n"
        "        break\n",
        encoding="utf-8",
    )
    runtime_status.clear()
    client = ProviderWorkerClient(
        [sys.executable, "-I", "-B", str(worker_file)],
        provider_id="examplehub",
    )
    try:
        runtime_status.consume(
            "examplehub", [{"type": "translation_quota", "remaining": 9}]
        )
        before_first_start = runtime_status.generation("examplehub")
        first = client.request("health", {}, timeout=3)
        assert runtime_status.get("examplehub") is None
        assert first.status_generation == runtime_status.generation("examplehub")
        assert first.status_generation > before_first_start
        assert first.status_configuration_generation == (
            runtime_status.configuration_generation("examplehub")
        )

        runtime_status.consume(
            "examplehub", [{"type": "translation_quota", "remaining": 8}]
        )
        steady = client.request("health", {}, timeout=3)
        assert steady.worker_started is False
        assert steady.status_generation == first.status_generation
        assert (
            steady.status_configuration_generation
            == first.status_configuration_generation
        )
        assert runtime_status.get("examplehub")["remaining"] == 8

        client._kill_worker()
        before_replacement = runtime_status.generation("examplehub")
        second = client.request("health", {}, timeout=3)
        assert runtime_status.get("examplehub") is None
        assert second.status_generation == runtime_status.generation("examplehub")
        assert second.status_generation > before_replacement
        assert (
            second.status_configuration_generation
            == first.status_configuration_generation
        )
    finally:
        client.stop()
        runtime_status.clear()


def test_event_from_request_started_after_worker_replacement_is_kept(monkeypatch):
    from provider_hub import registry, runtime_status

    class Worker:
        def request(self, operation, payload, timeout):
            assert operation == "search"
            runtime_status.worker_started("providerhub")
            return SimpleNamespace(
                payload={"candidates": []},
                events=[{"type": "translation_quota", "remaining": 36}],
                status_generation=runtime_status.generation("providerhub"),
                status_configuration_generation=(
                    runtime_status.configuration_generation("providerhub")
                ),
                worker_started=True,
            )

    runtime_status.clear()
    try:
        runtime_status.consume(
            "providerhub", [{"type": "translation_quota", "remaining": 37}]
        )
        provider = registry.HubProxyProvider(worker_client=Worker())
        assert provider.list_subtitles(SimpleNamespace(), set()) == []
        assert runtime_status.get("providerhub")["remaining"] == 36
    finally:
        runtime_status.clear()


def test_worker_replacement_does_not_hide_a_concurrent_config_clear():
    from provider_hub import registry, runtime_status

    class Worker:
        def request(self, operation, payload, timeout):
            assert operation == "search"
            runtime_status.worker_started("providerhub")
            runtime_status.clear("providerhub")
            return SimpleNamespace(
                payload={"candidates": []},
                events=[{"type": "translation_quota", "remaining": 36}],
                status_generation=runtime_status.generation("providerhub"),
                status_configuration_generation=(
                    runtime_status.configuration_generation("providerhub")
                ),
                worker_started=True,
            )

    runtime_status.clear()
    try:
        provider = registry.HubProxyProvider(worker_client=Worker())
        assert provider.list_subtitles(SimpleNamespace(), set()) == []
        assert runtime_status.get("providerhub") is None
    finally:
        runtime_status.clear()


def test_latest_of_concurrent_worker_starts_can_report_quota():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    from provider_hub import registry, runtime_status

    class Worker:
        def __init__(self, start_order, remaining):
            self.start_order = start_order
            self.remaining = remaining

        def request(self, operation, payload, timeout):
            assert operation == "search"
            requests_ready.wait()
            if self.start_order == 1:
                runtime_status.worker_started("providerhub")
                status_generation = runtime_status.generation("providerhub")
                first_started.set()
                second_started.wait()
            else:
                first_started.wait()
                runtime_status.worker_started("providerhub")
                status_generation = runtime_status.generation("providerhub")
                second_started.set()
            return SimpleNamespace(
                payload={"candidates": []},
                events=[{
                    "type": "translation_quota",
                    "remaining": self.remaining,
                }],
                status_generation=status_generation,
                status_configuration_generation=(
                    runtime_status.configuration_generation("providerhub")
                ),
                worker_started=True,
            )

    requests_ready = Barrier(2)
    first_started = Event()
    second_started = Event()
    runtime_status.clear()
    try:
        runtime_status.consume(
            "providerhub", [{"type": "translation_quota", "remaining": 37}]
        )
        first = registry.HubProxyProvider(worker_client=Worker(1, 35))
        second = registry.HubProxyProvider(worker_client=Worker(2, 36))
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [
                pool.submit(first.list_subtitles, SimpleNamespace(), set()),
                pool.submit(second.list_subtitles, SimpleNamespace(), set()),
            ]
            assert [call.result(timeout=3) for call in calls] == [[], []]
        assert runtime_status.get("providerhub")["remaining"] == 36
    finally:
        runtime_status.clear()


def test_request_started_before_status_clear_cannot_restore_old_quota(monkeypatch):
    from provider_hub import registry, runtime_status
    from provider_hub.protocol import candidate_from_worker

    class Worker:
        calls = 0

        def request(self, operation, payload, timeout):
            assert operation == "download"
            self.calls += 1
            if self.calls == 1:
                runtime_status.clear("providerhub")
                remaining = 37
            else:
                remaining = 36
            return SimpleNamespace(
                payload={},
                events=[{"type": "translation_quota", "remaining": remaining}],
            )

    runtime_status.clear()
    try:
        provider = registry.HubProxyProvider(worker_client=Worker())
        subtitle = candidate_from_worker("providerhub", {
            "id": "sub-1",
            "language": {"alpha3": "eng"},
            "provider_payload": {"provider": "providerhub"},
        })
        monkeypatch.setattr(registry, "worker_download_to_content", lambda *args, **kwargs: b"subtitle")
        provider.download_subtitle(subtitle)
        assert runtime_status.get("providerhub") is None
        provider.download_subtitle(subtitle)
        assert runtime_status.get("providerhub")["remaining"] == 36
    finally:
        runtime_status.clear()

def test_download_quota_event_survives_host_content_validation_error(monkeypatch):
    from provider_hub import registry, runtime_status
    from provider_hub.protocol import candidate_from_worker

    class Worker:
        def request(self, operation, payload, timeout):
            assert operation == "download"
            return SimpleNamespace(
                payload={"content_b64": "invalid"},
                events=[{"type": "translation_quota", "remaining": 4}],
            )

    runtime_status.clear()
    provider = registry.HubProxyProvider(worker_client=Worker())
    subtitle = candidate_from_worker("providerhub", {
        "id": "sub-1",
        "language": {"alpha3": "eng"},
        "provider_payload": {"provider": "providerhub"},
    })
    def fail_content(*args, **kwargs):
        raise ValueError("invalid worker content")

    monkeypatch.setattr(registry, "worker_download_to_content", fail_content)
    with pytest.raises(ValueError, match="invalid worker content"):
        provider.download_subtitle(subtitle)
    assert runtime_status.get("providerhub")["remaining"] == 4
    runtime_status.clear()
