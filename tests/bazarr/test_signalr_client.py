import json
import logging
import threading
import time
import types
from unittest.mock import patch as _patch
from urllib.error import URLError

import pytest
from signalrcore.transport.base_transport import TransportState
from signalrcore.transport.sockets.errors import SocketHandshakeError
from signalrcore.transport.websockets.websocket_transport import WebsocketTransport

from app import signalr_client

_real_sleep = time.sleep


class _FakeState:
    def __init__(self, value):
        self.value = value


class _FakeTransport:
    def __init__(self, value):
        self.state = _FakeState(value)


class _FakeConnection:
    def __init__(self):
        self.transport = None
        self.starts = 0
        self.stops = 0
        self.events = []

    def start(self):
        self.starts += 1
        self.transport = _FakeTransport(1)
        self.events.append("started")
        return True

    def stop(self):
        # Like the patched signalrcore stop: the transport ends disconnected.
        self.stops += 1
        self.events.append("stop")
        if self.transport is not None:
            self.transport.state = _FakeState(3)


class _RefusedOnceConnection(_FakeConnection):
    """signalrcore negotiates over urllib, so an arr that is down raises
    URLError rather than requests' ConnectionError."""

    def start(self):
        if self.starts == 0:
            self.starts += 1
            raise URLError(ConnectionRefusedError(111, "Connection refused"))
        return super().start()


class _AlwaysRefusedConnection(_FakeConnection):
    """An arr that stays down: every negotiation is refused."""

    def __init__(self):
        super().__init__()
        self.threads = set()

    def start(self):
        self.starts += 1
        self.threads.add(threading.current_thread())
        raise URLError(ConnectionRefusedError(111, "Connection refused"))


class _WebsocketRefusedOnceConnection(_FakeConnection):
    """Negotiation succeeds, then the websocket is refused.

    signalrcore builds the transport and marks it connecting (state 0) before
    it opens the socket, so the failed attempt leaves a transport that looks
    active behind it.
    """

    def start(self):
        if self.starts == 0:
            self.starts += 1
            self.transport = _FakeTransport(0)
            raise ConnectionRefusedError(111, "Connection refused")
        return super().start()


class _FailingThenConnectingConnection(_FakeConnection):
    def __init__(self, *errors):
        super().__init__()
        self.errors = list(errors)

    def start(self):
        if self.errors:
            self.starts += 1
            raise self.errors.pop(0)
        return super().start()


class _RetriedForever(BaseException):
    """Raised by the fake sleep so a loop that never gives up fails the test
    instead of hanging it. A BaseException, so no retry handler swallows it."""


def _patch_sleep(monkeypatch, sleep):
    """Swap the sleep that signalr_client's own loops call. signalr_client
    holds the time module itself, so patching time.sleep would also reach every
    other thread in the test process."""
    monkeypatch.setattr(signalr_client, "time", types.SimpleNamespace(sleep=sleep))


def _fake_sleep(monkeypatch, on_sleep=None, limit=5):
    calls = []
    test_thread = threading.current_thread()

    def sleep(seconds):
        if threading.current_thread() is not test_thread:
            # A feed thread some other test left running, not the loop under test.
            return _real_sleep(seconds)
        calls.append(seconds)
        if on_sleep is not None:
            on_sleep()
        if len(calls) > limit:
            raise _RetriedForever()

    _patch_sleep(monkeypatch, sleep)
    return calls


def _client(monkeypatch, client_cls, connection, arr_instance_id=None):
    """A client whose start() goes straight to connecting ``connection``."""
    client = client_cls(arr_instance_id)
    if client_cls is signalr_client.SonarrSignalrClient:
        monkeypatch.setattr(client, "_support_state", lambda: (True, "4.0.0"))

    def configure():
        client.connection = connection
        return True

    monkeypatch.setattr(client, "configure", configure)
    return client


_CLIENT_CLASSES = pytest.mark.parametrize(
    "client_cls",
    [signalr_client.SonarrSignalrClient, signalr_client.RadarrSignalrClient],
    ids=["sonarr", "radarr"],
)


def test_sonarr_signalr_start_retries_when_the_arr_refuses_the_connection(monkeypatch):
    connection = _RefusedOnceConnection()
    client = signalr_client.SonarrSignalrClient()

    monkeypatch.setattr(signalr_client.get_sonarr_info, "version", lambda: "4.0.0")
    monkeypatch.setattr(signalr_client.get_sonarr_info, "supports_signalr_core", lambda: True)
    sleep_calls = _fake_sleep(monkeypatch)

    def configure():
        client.connection = connection
        return True

    monkeypatch.setattr(client, "configure", configure)

    client.start()

    assert sleep_calls == [5]
    assert connection.starts == 2


def test_radarr_signalr_start_retries_when_the_arr_refuses_the_connection(monkeypatch):
    connection = _RefusedOnceConnection()
    client = signalr_client.RadarrSignalrClient()

    sleep_calls = _fake_sleep(monkeypatch)

    def configure():
        client.connection = connection
        return True

    monkeypatch.setattr(client, "configure", configure)

    client.start()

    assert sleep_calls == [5]
    assert connection.starts == 2


def test_sonarr_signalr_start_handles_missing_transport_before_first_start(monkeypatch):
    connection = _FakeConnection()
    client = signalr_client.SonarrSignalrClient()

    monkeypatch.setattr(signalr_client.get_sonarr_info, "version", lambda: "4.0.0")
    monkeypatch.setattr(signalr_client.get_sonarr_info, "supports_signalr_core", lambda: True)

    def configure():
        client.connection = connection
        return True

    monkeypatch.setattr(client, "configure", configure)

    client.start()

    assert connection.starts == 1


def test_sonarr_signalr_start_waits_when_version_is_temporarily_unknown(monkeypatch):
    connection = _FakeConnection()
    client = signalr_client.SonarrSignalrClient()
    versions = iter(["unknown", "4.0.0"])

    monkeypatch.setattr(signalr_client.get_sonarr_info, "version", lambda: next(versions))
    monkeypatch.setattr(signalr_client.get_sonarr_info, "supports_signalr_core", lambda: True)
    sleep_calls = _fake_sleep(monkeypatch)

    def configure():
        client.connection = connection
        return True

    monkeypatch.setattr(client, "configure", configure)

    client.start()

    assert sleep_calls == [5]
    assert connection.starts == 1


def test_sonarr_signalr_start_disables_known_unsupported_version(monkeypatch):
    client = signalr_client.SonarrSignalrClient()
    events = []

    monkeypatch.setattr(signalr_client.get_sonarr_info, "version", lambda: "3.0.0")
    monkeypatch.setattr(signalr_client.get_sonarr_info, "supports_signalr_core", lambda: False)
    monkeypatch.setattr(signalr_client, "event_stream", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(client, "configure", lambda: (_ for _ in ()).throw(AssertionError("configure called")))

    client.start()

    assert client.connected is False
    assert events == [{"type": "badges"}]


def test_radarr_signalr_start_handles_missing_transport_before_first_start(monkeypatch):
    connection = _FakeConnection()
    client = signalr_client.RadarrSignalrClient()

    def configure():
        client.connection = connection
        return True

    monkeypatch.setattr(client, "configure", configure)

    client.start()

    assert connection.starts == 1


@_CLIENT_CLASSES
def test_signalr_start_stops_retrying_once_the_client_is_stopped(monkeypatch, client_cls):
    connection = _AlwaysRefusedConnection()
    client = _client(monkeypatch, client_cls, connection)
    sleep_calls = _fake_sleep(monkeypatch, on_sleep=lambda: client.stop())

    client.start()

    assert sleep_calls == [5]
    assert connection.starts == 1


def test_sonarr_version_probe_stops_retrying_once_the_client_is_stopped(monkeypatch):
    client = signalr_client.SonarrSignalrClient()

    monkeypatch.setattr(signalr_client.get_sonarr_info, "version", lambda: "unknown")
    monkeypatch.setattr(client, "configure", lambda: (_ for _ in ()).throw(AssertionError("configure called")))
    sleep_calls = _fake_sleep(monkeypatch, on_sleep=lambda: client.stop())

    client.start()

    assert sleep_calls == [5]
    assert client.connection is None


@_CLIENT_CLASSES
def test_signalr_start_retries_after_the_websocket_is_refused(monkeypatch, client_cls):
    connection = _WebsocketRefusedOnceConnection()
    client = _client(monkeypatch, client_cls, connection)
    sleep_calls = _fake_sleep(monkeypatch)

    client.start()

    assert sleep_calls == [5]
    assert connection.starts == 2
    # The half-built transport was stopped before the retry built a new one.
    assert connection.events == ["stop", "started"]
    assert connection.transport.state.value == 1


@_CLIENT_CLASSES
def test_signalr_start_survives_handshake_and_negotiation_errors(monkeypatch, client_cls):
    connection = _FailingThenConnectingConnection(
        SocketHandshakeError("Handshake failed: HTTP/1.1 502 Bad Gateway"),
        json.JSONDecodeError("Expecting value", "<html>proxy error</html>", 0),
    )
    client = _client(monkeypatch, client_cls, connection)
    sleep_calls = _fake_sleep(monkeypatch)

    client.start()

    assert sleep_calls == [5, 5]
    assert connection.starts == 3
    assert connection.transport.state.value == 1


@_CLIENT_CLASSES
def test_signalr_start_failure_log_leaves_out_the_api_key(monkeypatch, caplog, client_cls):
    connection = _FailingThenConnectingConnection(
        SocketHandshakeError("Handshake failed: HTTP/1.1 400 Bad Request\r\nContent-Length: 70\r\n\r\n"
                             "GET /signalr/messages?access_token=s3cr3tkey&id=1 was rejected"),
    )
    client = _client(monkeypatch, client_cls, connection)
    _fake_sleep(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        client.start()

    failures = [record.getMessage() for record in caplog.records if "SocketHandshakeError" in record.getMessage()]
    assert len(failures) == 1
    assert "s3cr3tkey" not in caplog.text
    # One record per line in the log file, even for a raw HTTP response.
    assert "\n" not in failures[0] and "\r" not in failures[0]


def _refused():
    return URLError(ConnectionRefusedError(111, "Connection refused"))


def _bad_gateway(date):
    # A refused upgrade carries the raw response, and its Date header differs
    # on every attempt.
    return SocketHandshakeError(f"Handshake failed: HTTP/1.1 502 Bad Gateway\r\nDate: {date}\r\n\r\n")


def _start_failure_levels(caplog):
    return [record.levelno for record in caplog.records if "cannot connect" in record.getMessage()]


@_CLIENT_CLASSES
def test_signalr_start_warns_once_for_each_distinct_failure(monkeypatch, caplog, client_cls):
    """At the default log level a feed that cannot connect has to say why, but
    not every 5 seconds while the same failure repeats."""
    connection = _FailingThenConnectingConnection(
        _refused(),
        _refused(),
        _bad_gateway("Sat, 26 Sep 2026 10:00:00 GMT"),
        _bad_gateway("Sat, 26 Sep 2026 10:00:05 GMT"),
        json.JSONDecodeError("Expecting value", "<html>proxy error</html>", 0),
    )
    client = _client(monkeypatch, client_cls, connection)
    _fake_sleep(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        client.start()

    assert connection.starts == 6
    assert _start_failure_levels(caplog) == [
        logging.WARNING, logging.DEBUG, logging.WARNING, logging.DEBUG, logging.WARNING,
    ]


@_CLIENT_CLASSES
def test_signalr_start_warns_again_when_a_new_start_fails(monkeypatch, caplog, client_cls):
    connection = _FailingThenConnectingConnection(_refused())
    client = _client(monkeypatch, client_cls, connection)
    _fake_sleep(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        client.start()
        connection.errors.append(_refused())
        client.start()

    assert connection.starts == 4
    assert _start_failure_levels(caplog) == [logging.WARNING, logging.WARNING]


@_CLIENT_CLASSES
def test_signalr_start_gives_up_when_its_instance_is_deleted(monkeypatch, client_cls):
    connection = _AlwaysRefusedConnection()
    client = _client(monkeypatch, client_cls, connection, arr_instance_id=3)
    present = iter([True, False])
    monkeypatch.setattr(signalr_client, "client_for_instance",
                        lambda db, instance_id: object() if next(present) else None)
    sleep_calls = _fake_sleep(monkeypatch)
    client.connected = True

    client.start()

    assert sleep_calls == [5]
    assert connection.starts == 2
    assert client.connected is False


@_CLIENT_CLASSES
def test_configuring_a_deleted_instance_returns_no_connection_and_touches_nothing(monkeypatch, client_cls):
    connection = _FakeConnection()
    client = client_cls(7)
    client.connection = connection
    monkeypatch.setattr(signalr_client, "client_for_instance", lambda db, instance_id: None)
    built = []
    monkeypatch.setattr(signalr_client, "build_signalr_connection", lambda url, headers: built.append(url))

    assert client._configure_if_current(client._begin_generation()) is None

    assert built == []
    assert client.connection is connection
    assert connection.events == []


@_CLIENT_CLASSES
def test_signalr_client_stopped_while_connecting_closes_that_connection(monkeypatch, client_cls):
    """A stop() that lands while start() is still negotiating must not leave
    the connection it was racing running unowned."""

    class _StoppedMidStartConnection(_FakeConnection):
        def start(self):
            client.stop()
            return super().start()

    connection = _StoppedMidStartConnection()
    client = _client(monkeypatch, client_cls, connection)

    client.start()

    assert connection.events[-1] == "stop"
    assert connection.transport.state.value == 3


class _CallbackConnection(_FakeConnection):
    """Keeps the callbacks configure() registers, like a HubConnection, and
    opens when started."""

    def __init__(self):
        super().__init__()
        self.callbacks = {}

    def on_open(self, callback):
        self.callbacks["open"] = callback

    def on_reconnect(self, callback):
        self.callbacks["reconnect"] = callback

    def on_close(self, callback):
        self.callbacks["close"] = callback

    def on_error(self, callback):
        self.callbacks["error"] = callback

    def on(self, event, callback):
        self.callbacks[event] = callback

    def start(self):
        self.callbacks["open"]()
        return super().start()


def _configured_client(monkeypatch, client_cls, *connections):
    """A client for instance 7 whose own configure() builds ``connections`` in
    turn. Returns it with the sync jobs its connects trigger."""
    client = client_cls(7)
    if client_cls is signalr_client.SonarrSignalrClient:
        monkeypatch.setattr(client, "_support_state", lambda: (True, "4.0.0"))
    arr = types.SimpleNamespace(base_url=lambda: "http://arr:8989", api_key="key")
    monkeypatch.setattr(signalr_client, "client_for_instance", lambda db, instance_id: arr)
    built = iter(connections)
    monkeypatch.setattr(signalr_client, "build_signalr_connection", lambda url, headers: next(built))
    monkeypatch.setattr(signalr_client, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(signalr_client.settings.sonarr, "series_sync_on_live", True)
    monkeypatch.setattr(signalr_client.settings.radarr, "movies_sync_on_live", True)
    sync_jobs = []
    monkeypatch.setattr(signalr_client.scheduler, "execute_job_now", lambda taskid=None: sync_jobs.append(taskid))
    return client, sync_jobs


class _ReconnectingTransportConnection(_FakeConnection):
    def __init__(self):
        super().__init__()
        with _patch_websocket_init():
            self.transport = WebsocketTransport()
        self.transport.state = TransportState.disconnected
        self.transport.logger = logging.getLogger("SignalRCoreClient")
        self.transport._on_open = lambda: None
        self.transport._on_reconnect = lambda: None
        self.transport._on_close = lambda: None

    def on_open(self, callback):
        self.transport._on_open = callback

    def on_reconnect(self, callback):
        self.transport._on_reconnect = callback

    def on_close(self, callback):
        self.transport._on_close = callback

    def on_error(self, callback):
        pass

    def on(self, event, callback):
        pass

    def start(self):
        self.starts += 1
        self.transport._set_state(TransportState.connecting)
        self.transport._set_state(TransportState.connected)
        return True


def _patch_websocket_init():
    return _patch.object(WebsocketTransport, "__init__", lambda self, **kwargs: None)


class _ConnectingOnlyTransportConnection(_ReconnectingTransportConnection):
    def start(self):
        self.starts += 1
        self.transport._set_state(TransportState.connecting)
        return True


@_CLIENT_CLASSES
def test_signalr_connection_opening_marks_the_client_connected(monkeypatch, client_cls):
    connection = _CallbackConnection()
    client, sync_jobs = _configured_client(monkeypatch, client_cls, connection)

    client.start()

    assert client.connected is True
    assert len(sync_jobs) == 1


@_CLIENT_CLASSES
def test_signalr_connection_opening_after_a_stop_leaves_the_client_disconnected(monkeypatch, client_cls):
    """Turning the integration off stops the client while a handshake may still
    be in flight. That handshake finishing afterwards must not mark the stopped
    client connected or trigger a sync."""

    class _OpensAfterStopConnection(_CallbackConnection):
        def start(self):
            client.stop()
            return super().start()

    connection = _OpensAfterStopConnection()
    client, sync_jobs = _configured_client(monkeypatch, client_cls, connection)

    client.start()

    assert client.connected is False
    assert sync_jobs == []
    assert connection.events[-1] == "stop"


@_CLIENT_CLASSES
def test_signalr_callbacks_from_a_replaced_connection_are_ignored(monkeypatch, client_cls):
    first = _CallbackConnection()
    second = _CallbackConnection()
    client, sync_jobs = _configured_client(monkeypatch, client_cls, first, second)

    client.start()
    client.start()
    assert client.connection is second
    assert client.connected is True
    assert len(sync_jobs) == 2

    first.callbacks["reconnect"]()
    assert client.connected is True

    first.callbacks["open"]()
    assert len(sync_jobs) == 2


@_CLIENT_CLASSES
def test_signalr_automatic_reconnect_restores_connected_state(monkeypatch, client_cls):
    """When signalrcore automatically reconnects an established feed, entering
    TransportState.reconnecting marks the client disconnected and completing
    the new handshake (TransportState.connected) marks it connected again.
    A restored feed runs exactly one full sync, from the first connect."""
    connection = _ReconnectingTransportConnection()
    client, sync_jobs = _configured_client(monkeypatch, client_cls, connection)

    client.start()
    assert client.connected is True
    assert len(sync_jobs) == 1

    connection.transport._set_state(TransportState.reconnecting)
    assert client.connected is False

    connection.transport._set_state(TransportState.connected)
    assert client.connected is True
    assert len(sync_jobs) == 1


@pytest.mark.parametrize(
    ("client_cls", "taskid"),
    [
        (signalr_client.SonarrSignalrClient, "update_series_7"),
        (signalr_client.RadarrSignalrClient, "update_movies_7"),
    ],
    ids=["sonarr", "radarr"],
)
def test_signalr_automatic_reconnect_skips_the_full_sync(monkeypatch, client_cls, taskid):
    """A reconnect restores the connected state without queuing another full
    sync. An enabled instance's scheduled sync reconciles the changes it
    covers, unless its interval is set to Manually."""
    connection = _ReconnectingTransportConnection()
    client, sync_jobs = _configured_client(monkeypatch, client_cls, connection)

    client.start()
    assert sync_jobs == [taskid]

    connection.transport._set_state(TransportState.reconnecting)
    connection.transport._set_state(TransportState.connected)

    assert client.connected is True
    assert sync_jobs == [taskid]


@_CLIENT_CLASSES
def test_signalr_first_connect_after_a_restart_still_syncs(monkeypatch, client_cls):
    """A restart builds a new feed, so its first connect runs the full sync,
    even when the feed was lost before the restart."""
    first = _ReconnectingTransportConnection()
    second = _ReconnectingTransportConnection()
    client, sync_jobs = _configured_client(monkeypatch, client_cls, first, second)

    client.start()
    assert len(sync_jobs) == 1

    first.transport._set_state(TransportState.reconnecting)
    client.stop()
    client.start()

    assert client.connected is True
    assert len(sync_jobs) == 2


@pytest.mark.parametrize(
    ("client_cls", "kind", "taskid"),
    [
        (signalr_client.SonarrSignalrClient, "sonarr", "update_series_7"),
        (signalr_client.RadarrSignalrClient, "radarr", "update_movies_7"),
    ],
    ids=["sonarr", "radarr"],
)
def test_an_automatic_reconnect_skips_the_full_sync_and_a_restart_runs_it_again(
        monkeypatch, client_cls, kind, taskid):
    """The on_reconnect and on_open callbacks of an automatic reconnect restore
    the connected state without a full sync. A restart builds a new feed, and
    its first connect runs the full sync again."""
    first = _CallbackConnection()
    second = _CallbackConnection()
    client, sync_jobs = _configured_client(monkeypatch, client_cls, first, second)
    monkeypatch.setattr(signalr_client.settings.general, f"use_{kind}", True)

    client.start()
    assert sync_jobs == [taskid]

    first.callbacks["reconnect"]()
    first.callbacks["open"]()

    assert client.connected is True
    assert sync_jobs == [taskid]

    client.restart()

    assert client.connected is True
    assert sync_jobs == [taskid, taskid]


@_CLIENT_CLASSES
def test_signalr_reconnect_that_fails_keeps_the_client_disconnected(monkeypatch, client_cls):
    """A reconnect that never reaches the open callback leaves the client
    disconnected and triggers no sync."""
    connection = _ReconnectingTransportConnection()
    client, sync_jobs = _configured_client(monkeypatch, client_cls, connection)

    client.start()
    assert len(sync_jobs) == 1

    connection.transport._set_state(TransportState.reconnecting)

    assert client.connected is False
    assert len(sync_jobs) == 1


@pytest.mark.parametrize(
    ("client_cls", "taskid"),
    [
        (signalr_client.SonarrSignalrClient, "update_series_7"),
        (signalr_client.RadarrSignalrClient, "update_movies_7"),
    ],
    ids=["sonarr", "radarr"],
)
def test_signalr_reconnect_before_the_first_connect_still_syncs(monkeypatch, client_cls, taskid):
    """A socket that drops before the SignalR handshake completes reconnects
    with no live feed to lose, so the first live connect still runs the sync."""
    connection = _ConnectingOnlyTransportConnection()
    client, sync_jobs = _configured_client(monkeypatch, client_cls, connection)

    client.start()
    assert client.connected is False
    assert sync_jobs == []

    connection.transport._set_state(TransportState.reconnecting)
    connection.transport._set_state(TransportState.connected)

    assert client.connected is True
    assert sync_jobs == [taskid]


@_CLIENT_CLASSES
def test_signalr_stop_marks_the_client_disconnected(client_cls):
    client = client_cls(4)
    client.connection = _FakeConnection()
    client.connected = True

    client.stop()

    assert client.connected is False
    assert client.connection.stops == 1


def _wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        _real_sleep(0.01)
    return True


@pytest.mark.parametrize(
    ("kind", "client_cls"),
    [("sonarr", signalr_client.SonarrSignalrClient), ("radarr", signalr_client.RadarrSignalrClient)],
    ids=["sonarr", "radarr"],
)
def test_refanout_retires_the_retry_loops_it_replaces(monkeypatch, kind, client_cls):
    """Saving settings while an arr is down re-fans the clients out. The loops
    of the clients it replaces, singleton included, must end rather than keep
    retrying beside the new ones."""
    connections = []

    class _DownArrClient(client_cls):
        def _support_state(self):
            return True, "4.0.0"

        def configure(self):
            self.connection = _AlwaysRefusedConnection()
            connections.append(self.connection)
            return True

    monkeypatch.setattr(signalr_client, "_enabled_instances",
                        lambda _kind: [types.SimpleNamespace(id=1), types.SimpleNamespace(id=2)])
    monkeypatch.setattr(signalr_client, "client_for_instance", lambda db, instance_id: object())
    _patch_sleep(monkeypatch, lambda seconds: _real_sleep(0.01))

    singleton = _DownArrClient()
    extras = []

    def running_threads():
        return {thread for conn in connections for thread in conn.threads if thread.is_alive()}

    try:
        signalr_client._start_clients_for_kind(kind, singleton, extras, _DownArrClient)
        assert _wait_until(lambda: len(connections) == 2 and all(c.starts for c in connections))
        first_round = list(connections)
        first_threads = running_threads()
        assert len(first_threads) == 2

        signalr_client._start_clients_for_kind(kind, singleton, extras, _DownArrClient)
        assert _wait_until(lambda: len(connections) == 4 and all(c.starts for c in connections))

        for thread in first_threads:
            thread.join(timeout=2)
        assert not any(thread.is_alive() for thread in first_threads)
        retired_attempts = [conn.starts for conn in first_round]
        _real_sleep(0.05)
        assert [conn.starts for conn in first_round] == retired_attempts
        assert len(running_threads()) == 2
    finally:
        for client in [singleton, *extras]:
            client.stop()
        leftover = running_threads()
        for thread in leftover:
            thread.join(timeout=2)

    assert not any(thread.is_alive() for thread in leftover)


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_restart_stops_every_client_when_the_integration_is_turned_off(monkeypatch, kind):
    class _RecordingClient:
        def __init__(self):
            self.connection = object()
            self.stopped = False

        def stop(self):
            self.stopped = True

    singleton = _RecordingClient()
    extra = _RecordingClient()
    extras = [extra]
    monkeypatch.setattr(signalr_client, f"{kind}_signalr_client", singleton)
    monkeypatch.setattr(signalr_client, f"_{kind}_signalr_clients", extras)
    monkeypatch.setattr(signalr_client.settings.general, f"use_{kind}", False)
    monkeypatch.setattr(signalr_client, f"start_{kind}_signalr",
                        lambda: (_ for _ in ()).throw(AssertionError("started a disabled integration")))

    getattr(signalr_client, f"restart_{kind}_signalr")()

    assert singleton.stopped is True
    assert extra.stopped is True
    assert extras == []


def _control_flow_in_finally(tree):
    """Lines of a return, or a break or continue that leaves the block, in a
    finally block. Python 3.14 warns about each one when it compiles the
    module."""
    import ast

    found = []

    def visit(node, in_loop):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        if isinstance(node, ast.Return) or (
                isinstance(node, (ast.Break, ast.Continue)) and not in_loop):
            found.append(node.lineno)
        in_loop = in_loop or isinstance(node, (ast.For, ast.AsyncFor, ast.While))
        for child in ast.iter_child_nodes(node):
            visit(child, in_loop)

    for node in ast.walk(tree):
        for statement in getattr(node, "finalbody", ()):
            visit(statement, False)
    return found


def test_signalr_client_compiles_without_a_return_in_a_finally_block():
    import ast
    import inspect

    assert _control_flow_in_finally(ast.parse(inspect.getsource(signalr_client))) == []


_MOVIE_EVENT = {"name": "movie", "body": {"action": "updated",
                                          "resource": {"id": 1, "title": "M", "year": 2020}}}


class _NotAnException(BaseException):
    pass


@pytest.mark.parametrize("outcome", ["returns", "raises", "raises_base", "malformed"])
def test_dispatcher_always_refreshes_the_badges_and_never_raises(monkeypatch, outcome):
    """What the feed's consumer loop relies on: every event ends with a badge
    refresh, and nothing an event raises reaches the loop."""
    emitted = []
    monkeypatch.setattr(signalr_client, "event_stream", lambda **event: emitted.append(event))

    def update_one_movie(**_kwargs):
        if outcome == "raises":
            raise RuntimeError("fixture failure")
        if outcome == "raises_base":
            raise _NotAnException()

    monkeypatch.setattr(signalr_client, "update_one_movie", update_one_movie)
    data = {"name": "movie"} if outcome == "malformed" else _MOVIE_EVENT

    assert signalr_client.dispatcher(data) is None
    assert emitted == [{"type": "badges"}]


def test_a_queued_series_refresh_for_a_disabled_instance_touches_nothing(monkeypatch, schema_session,
                                                                        caplog):
    """The feed queue is drained asynchronously, so an event can be dispatched
    after its instance was disabled or deleted. Its episode sync would fetch
    through the default Sonarr connection while the deletion keeps the named
    instance's scope, and an empty default answer would delete that instance's
    rows. The dispatcher skips the event instead, the way the per-instance entry
    points skip an unknown instance, so nothing is fetched or deleted."""
    from sqlalchemy import select

    from app.database import TableArrInstances, TableEpisodes

    schema_session.add(TableArrInstances(
        id=2, kind='sonarr', stable_key='sonarr-4k', name='Sonarr 4K',
        enabled=0, is_default=0, ip='127.0.0.1', port=8990, base_url='/', ssl=0,
        verify_ssl=0, http_timeout=60, api_key='k'))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=706, series_id=None, sonarrEpisodeId=46, sonarrSeriesId=7,
        arr_instance_id=2, path="/series/gamma/s01e01.mkv", title="Kept",
        season=1, episode=1, monitored="True", subtitles="[]"))

    monkeypatch.setattr(signalr_client, "database", schema_session)
    monkeypatch.setattr(signalr_client, "event_stream", lambda **event: None)
    synced = []
    monkeypatch.setattr(signalr_client, "sync_episodes",
                        lambda **kwargs: synced.append(kwargs))

    event = {"name": "series", "_arr_instance_id": 2,
             "body": {"action": "updated",
                      "resource": {"id": 7, "title": "Gamma", "year": 2020,
                                   "episodesChanged": True}}}

    with caplog.at_level(logging.WARNING):
        signalr_client.dispatcher(event)

    assert synced == [], (
        "an event for a disabled or deleted instance reached the episode sync")
    remaining = sorted(schema_session.execute(select(TableEpisodes.id)).scalars().all())
    assert remaining == [706], (
        "a queued event for a disabled or deleted instance deleted rows")
    assert "unknown or disabled instance" in caplog.text, (
        "the skipped event was not reported")
