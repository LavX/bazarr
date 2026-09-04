import datetime
import json
from pathlib import Path
import sys

import pytest

from provider_hub.worker import ProviderWorkerClient, WorkerError, worker_command
from subliminal.exceptions import (
    AuthenticationError,
    ConfigurationError,
    DownloadLimitExceeded,
    ServiceUnavailable,
)
from subliminal_patch.exceptions import APIThrottled, TooManyRequests


@pytest.fixture
def error_worker(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "provider.py").write_text(
        """
class ErrorProvider:
    def search(self, video, languages, config):
        raise type(config["failure"], (RuntimeError,), {})(config["message"])

    def download(self, provider_payload, language, config):
        raise type(config["failure"], (RuntimeError,), {})(config["message"])
""",
        encoding="utf-8",
    )
    runner = Path(__file__).parents[2] / "bazarr/provider_hub/worker_runner.py"
    client = ProviderWorkerClient(
        worker_command(sys.executable, runner),
        cwd=bundle,
        env={
            "BAZARR_PROVIDER_HUB_BUNDLE": str(bundle),
            "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps(
                {"entry_module": "provider", "entry_class": "ErrorProvider"}
            ),
        },
    )
    yield client
    client.stop()


@pytest.fixture
def throttle_state(monkeypatch, tmp_path):
    from app.get_args import args
    from types import SimpleNamespace

    for name in ("config", "db", "cache", "log", "backup"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(args, "config_dir", str(tmp_path))
    from app import get_providers

    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "throttle_count", {})
    names = ["opensubtitlescom", "examplehub"]
    monkeypatch.setattr(
        get_providers, "settings",
        SimpleNamespace(general=SimpleNamespace(enabled_providers=names)),
    )
    monkeypatch.setattr(get_providers, "provider_registry", SimpleNamespace(names=lambda: names))
    monkeypatch.setattr(get_providers, "event_stream", lambda **kwargs: None)
    return get_providers


@pytest.mark.parametrize("operation", ["search", "download"])
@pytest.mark.parametrize(
    "remote_name,provider,host_type,seconds",
    [
        ("DownloadLimitExceeded", "opensubtitlescom", DownloadLimitExceeded, 21600),
        ("DownloadLimitExceeded", "examplehub", DownloadLimitExceeded, 10800),
        ("RateLimited", "opensubtitlescom", TooManyRequests, 60),
        ("TooManyRequests", "examplehub", TooManyRequests, 3600),
        ("ServiceUnavailable", "examplehub", ServiceUnavailable, 1200),
        ("APIThrottled", "examplehub", APIThrottled, 600),
        ("AuthenticationRequired", "opensubtitlescom", AuthenticationError, 43200),
        ("AuthenticationError", "examplehub", AuthenticationError, 43200),
        ("ConfigurationError", "examplehub", ConfigurationError, 43200),
        ("RuntimeError", "opensubtitlescom", WorkerError, 600),
    ],
)
def test_worker_failure_reaches_persisted_provider_throttle(
    error_worker, throttle_state, operation, remote_name, provider, host_type, seconds,
):
    # The message deliberately looks like quota exhaustion even for unknown errors.
    # Classification must come from the fixed semantic names, never message parsing.
    with pytest.raises(Exception) as raised:
        error_worker.request(operation, {
            "config": {"failure": remote_name, "message": "download limit exceeded"},
        }, timeout=5)

    # Counted transient errors throttle on their fifth failure. Keep the real
    # counter and persistence path while avoiding four retry sleeps in this test.
    before = datetime.datetime.now()
    throttle_state.throttle_count[provider] = {
        "count": 4, "time": before + datetime.timedelta(seconds=120),
    }
    throttle_state.provider_throttle(provider, raised.value)
    after = datetime.datetime.now()

    reason, until, _description = throttle_state.get_throttled_providers()[provider]
    expected_delay = datetime.timedelta(seconds=seconds)
    assert before + expected_delay <= until <= after + expected_delay
    assert reason == host_type.__name__
    assert type(raised.value) is host_type
    remote_error = raised.value if host_type is WorkerError else raised.value.__cause__
    assert isinstance(remote_error, WorkerError)
    assert remote_error.remote_class_name == remote_name
    assert remote_error.code == "provider"
    assert remote_error.retryable is False
    assert str(raised.value) == "download limit exceeded"
    assert error_worker.request("health", timeout=5).payload == {"initialized": True}


@pytest.fixture
def response_worker(tmp_path):
    clients = []

    def start(response):
        script = tmp_path / f"response-{len(clients)}.py"
        script.write_text(
            "import json, sys\n"
            f"response = json.loads({json.dumps(response)!r})\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    if isinstance(response, dict):\n"
            "        response.setdefault('abi', request['abi'])\n"
            "        response.setdefault('id', request['id'])\n"
            "    print(json.dumps(response), flush=True)\n",
            encoding="utf-8",
        )
        client = ProviderWorkerClient(worker_command(sys.executable, script))
        clients.append(client)
        return client

    yield start
    for client in clients:
        client.stop()


@pytest.mark.parametrize("error", [None, [], ["DownloadLimitExceeded"], "failure", 5])
def test_malformed_error_metadata_stays_worker_error(response_worker, error):
    with pytest.raises(WorkerError):
        response_worker({"ok": False, "error": error}).request("search", timeout=5)


@pytest.mark.parametrize(
    "remote_name,code",
    [
        ("builtins.PermissionError", "provider"),
        ("SystemExit", "provider"),
        ("DownloadLimitExceeded", "transport"),
        ("DownloadLimitExceeded", None),
        ("DownloadLimitExceeded", []),
        (["DownloadLimitExceeded"], "provider"),
        ({"class": "DownloadLimitExceeded"}, "provider"),
    ],
)
def test_unknown_or_invalid_semantic_metadata_stays_worker_error(response_worker, remote_name, code):
    with pytest.raises(WorkerError, match="failure") as raised:
        response_worker({"ok": False, "error": {
            "message": "failure", "class_name": remote_name, "code": code, "retryable": True,
        }}).request("search", timeout=5)
    assert type(raised.value) is WorkerError
    assert raised.value.remote_class_name == (remote_name if isinstance(remote_name, str) else None)
    assert raised.value.code == (code if isinstance(code, str) else None)
    assert raised.value.retryable is True


@pytest.mark.parametrize("retryable", [True, False, "false", 1, None, []])
def test_retryable_metadata_accepts_only_booleans(response_worker, retryable):
    with pytest.raises(DownloadLimitExceeded) as raised:
        response_worker({"ok": False, "error": {
            "class_name": "DownloadLimitExceeded", "code": "provider", "message": "quota",
            "retryable": retryable,
        }}).request("download", timeout=5)
    assert raised.value.__cause__.retryable is (retryable if isinstance(retryable, bool) else False)


@pytest.mark.parametrize(
    "response,diagnostic",
    [
        ([], "response must be an object"),
        ({"abi": "unsupported", "ok": False, "error": {"class_name": "DownloadLimitExceeded"}}, "unsupported ABI"),
        ({"id": "wrong-id", "ok": False, "error": {"class_name": "DownloadLimitExceeded"}}, "mismatched request id"),
    ],
)
def test_invalid_envelope_is_not_promoted_to_provider_error(response_worker, response, diagnostic):
    with pytest.raises(WorkerError, match=diagnostic):
        response_worker(response).request("search", timeout=5)


@pytest.mark.parametrize(
    "output,diagnostic",
    [("not-json", "malformed JSON"), (None, "closed stdout")],
)
def test_worker_transport_failure_keeps_generic_error(output, diagnostic):
    script = "import sys; sys.stdin.readline(); "
    script += "sys.exit(1)" if output is None else f"print({output!r}, flush=True)"
    client = ProviderWorkerClient([sys.executable, "-I", "-B", "-c", script])
    try:
        with pytest.raises(WorkerError, match=diagnostic) as raised:
            client.request("download", timeout=5)
        assert raised.value.remote_class_name is None
        assert raised.value.code is None
        assert raised.value.retryable is False
    finally:
        client.stop()


def test_invalid_success_payload_keeps_generic_error(response_worker):
    with pytest.raises(WorkerError, match="payload must be an object"):
        response_worker({"ok": True, "payload": ["candidate"]}).request("search", timeout=5)
