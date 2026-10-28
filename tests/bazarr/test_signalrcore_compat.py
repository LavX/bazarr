import base64
import hashlib
import socket
from pathlib import Path
from threading import Event, Thread, current_thread
from time import monotonic
from unittest.mock import MagicMock, patch

import signalrcore
from signalrcore.transport.base_transport import TransportState
from signalrcore.transport.reconnection import RawReconnectionHandler
from signalrcore.transport.websockets.websocket_client import WebSocketClient
from signalrcore.transport.websockets.websocket_transport import WebsocketTransport

from app.signalrcore_compat import build_signalr_connection, patch_signalrcore_stop


def _make_transport(state=TransportState.disconnected, ws=None):
    with patch.object(WebsocketTransport, "__init__", lambda self, **kwargs: None):
        transport = WebsocketTransport()
    transport.state = state
    transport._ws = MagicMock() if ws is None else ws
    transport.handshake_received = False
    transport.connection_checker = MagicMock()
    transport.logger = MagicMock()
    transport._on_open = MagicMock()
    transport._on_close = MagicMock()
    transport._on_reconnect = MagicMock()
    return transport


def test_signalrcore_is_loaded_from_python_environment_not_custom_libs():
    repo_root = Path(__file__).resolve().parents[2]
    custom_signalrcore_dir = repo_root / "custom_libs" / "signalrcore"
    signalrcore_path = Path(signalrcore.__file__).resolve()

    assert not signalrcore_path.is_relative_to(custom_signalrcore_dir)


def test_signalrcore_stop_patch_closes_websocket_when_not_connected():
    patch_signalrcore_stop()
    transport = _make_transport(state=TransportState.disconnected)

    transport.stop()

    transport.connection_checker.stop.assert_called_once()
    transport._ws.close.assert_called_once()
    assert transport.state == TransportState.disconnected


def test_signalrcore_stop_patch_keeps_connected_cleanup_behavior():
    patch_signalrcore_stop()
    transport = _make_transport(state=TransportState.connected)

    transport.stop()

    transport.connection_checker.stop.assert_called_once()
    transport._ws.close.assert_called_once()
    assert transport.state == TransportState.disconnected


def test_signalrcore_stop_patch_handles_new_transport_client_cleanup():
    patch_signalrcore_stop()
    transport = _make_transport(state=TransportState.connected, ws=None)
    del transport._ws
    transport._client = MagicMock()

    transport.stop()

    transport.connection_checker.stop.assert_called_once()
    transport._client.close.assert_called_once()
    assert transport.state == TransportState.disconnected


def test_signalr_connection_builder_forces_json_protocol_version_one():
    connection = build_signalr_connection(
        "http://example.invalid/signalr/messages?access_token=test",
        {"User-Agent": "Bazarr-Test"},
    )
    protocol = getattr(connection, "_selected_protocol", None)
    if protocol is None:
        protocol = connection.transport.protocol

    assert protocol.protocol == "json"
    assert protocol.version == 1


def test_signalrcore_reconnect_transitions_fire_lost_then_open_callbacks():
    patch_signalrcore_stop()
    transport = _make_transport(state=TransportState.connecting)

    transport._set_state(TransportState.connected)
    transport._on_open.assert_called_once()
    transport._on_reconnect.assert_not_called()

    # Entering reconnecting (and the duplicate _set_state inside
    # start(reconnection=True)) notifies on_reconnect once.
    transport._set_state(TransportState.reconnecting)
    transport._set_state(TransportState.reconnecting)
    transport._on_reconnect.assert_called_once()
    assert transport._on_open.call_count == 1

    # Finishing the reconnect handshake notifies on_open so the client marks
    # the feed connected again instead of firing on_reconnect a second time.
    transport._set_state(TransportState.connected)
    assert transport._on_open.call_count == 2
    assert transport._on_reconnect.call_count == 1
    transport._on_close.assert_not_called()


def _make_reconnecting_transport(retry_interval=0.01):
    transport = _make_transport(state=TransportState.connected)
    transport.url = "http://example.invalid/signalr/messages"
    transport.manually_closing = False
    transport.reconnection_handler = RawReconnectionHandler(retry_interval, None)
    transport._client = MagicMock()
    transport._client.is_connection_closed.return_value = True
    transport.create_client = MagicMock(return_value=transport._client)
    return transport


def test_signalrcore_reconnect_retries_failed_negotiation_until_connected():
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport()
    transport.negotiate = MagicMock(side_effect=[OSError("connection refused"), None])

    def finish_handshake():
        transport._client.is_connection_closed.return_value = False
        transport._set_state(TransportState.connected)

    transport._client.connect.side_effect = finish_handshake

    try:
        assert transport.handle_reconnect() is True
        assert transport.negotiate.call_count == 2
        assert transport.is_connected()
        assert not transport.reconnection_handler.reconnecting
    finally:
        transport.stop()


def test_signalrcore_reconnect_accepts_closed_client_in_reconnecting_state():
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport()
    transport._set_state(TransportState.reconnecting)
    transport.negotiate = MagicMock()

    def finish_handshake():
        transport._client.is_connection_closed.return_value = False
        transport._set_state(TransportState.connected)

    transport._client.connect.side_effect = finish_handshake

    try:
        assert transport.handle_reconnect() is True
        transport.negotiate.assert_called_once()
        assert transport.is_connected()
        assert not transport.is_reconnecting()
    finally:
        transport.stop()


def test_signalrcore_reconnect_retries_first_attempt_closed_before_handshake():
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport(retry_interval=0.5)
    first_attempt_opened = Event()
    handshake_landed = Event()
    opened = MagicMock()
    opened.is_connection_closed.return_value = False
    opened.connect.side_effect = first_attempt_opened.set
    recovered = MagicMock()
    recovered.is_connection_closed.return_value = False

    def finish_handshake():
        transport.handshake_received = True
        transport._set_state(TransportState.connected)
        handshake_landed.set()

    recovered.connect.side_effect = finish_handshake
    transport.create_client = MagicMock(side_effect=[opened, recovered])
    transport.negotiate = MagicMock(return_value=None)
    reconnect_results = []

    def reconnect():
        reconnect_results.append(transport.handle_reconnect())

    reconnect_thread = Thread(target=reconnect, daemon=True)
    reconnect_thread.start()

    try:
        assert first_attempt_opened.wait(timeout=2)
        assert transport.create_client.call_count == 1
        opened.is_connection_closed.return_value = True
        transport.on_socket_close()
        assert handshake_landed.wait(timeout=5)
        assert transport.negotiate.call_count == 2
        assert transport.is_connected()
        reconnect_thread.join(timeout=5)
        assert reconnect_results == [True]
        assert not transport.reconnection_handler.reconnecting
    finally:
        transport.stop()
        reconnect_thread.join(timeout=5)
        assert not reconnect_thread.is_alive()


def test_signalrcore_reconnect_reruns_when_first_socket_closes_after_handshake():
    patch_signalrcore_stop()
    retry_interval = 1.0
    transport = _make_reconnecting_transport(retry_interval=retry_interval)
    landed = Event()
    closed_at = []
    negotiated_at = []
    first = MagicMock()
    first.is_connection_closed.return_value = False

    def close_after_handshake():
        assert current_thread() is worker
        assert transport._bazarr_reconnect_lock.locked()
        transport.handshake_received = True
        transport.reconnection_handler.reconnecting = False
        transport._set_state(TransportState.connected)
        first.is_connection_closed.return_value = True
        closed_at.append(monotonic())
        transport.on_socket_close()

    first.connect.side_effect = close_after_handshake
    second = MagicMock()
    second.is_connection_closed.return_value = False

    def finish_handshake():
        transport.handshake_received = True
        transport.reconnection_handler.reconnecting = False
        transport._set_state(TransportState.connected)
        landed.set()

    second.connect.side_effect = finish_handshake
    transport.create_client = MagicMock(side_effect=[first, second])
    transport.negotiate = MagicMock(side_effect=lambda: negotiated_at.append(monotonic()))
    results = []
    worker = Thread(target=lambda: results.append(transport.handle_reconnect()), daemon=True)
    worker.start()

    try:
        worker.join(timeout=5)
        assert transport.negotiate.call_count == 2
        assert negotiated_at[1] - closed_at[0] < retry_interval
        assert transport.is_connected()
        assert landed.is_set()
        assert not worker.is_alive()
        assert results == [True]
    finally:
        transport.stop()
        worker.join(timeout=5)
        assert not worker.is_alive()


def _assert_reconnect_when_handshake_never_lands(fail_first_negotiation=False):
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport(retry_interval=0.05)
    abandoned = MagicMock()
    abandoned.is_connection_closed.return_value = False
    abandoned.connect.return_value = None
    events = []
    abandoned.dispose.side_effect = lambda: events.append("dispose")
    recovered = MagicMock()
    recovered.is_connection_closed.return_value = False

    def finish_handshake():
        transport.handshake_received = True
        transport.reconnection_handler.reconnecting = False
        transport._set_state(TransportState.connected)

    recovered.connect.side_effect = finish_handshake
    clients = iter([abandoned, recovered])

    def create_client():
        events.append("create")
        return next(clients)

    transport.create_client = MagicMock(side_effect=create_client)
    transport.negotiate = MagicMock(return_value=None)
    if fail_first_negotiation:
        transport.negotiate.side_effect = [OSError("connection refused"), None, None]
    results = []
    worker = Thread(target=lambda: results.append(transport.handle_reconnect()), daemon=True)
    worker.start()

    try:
        worker.join(timeout=5)
        assert transport.create_client.call_count == 2
        assert abandoned.dispose.call_count == 1
        assert events == ["create", "dispose", "create"]
        assert transport.negotiate.call_count == (3 if fail_first_negotiation else 2)
        assert transport.is_connected()
        assert not worker.is_alive()
        assert results == [True]
    finally:
        transport.stop()
        worker.join(timeout=5)
        assert not worker.is_alive()


def test_signalrcore_reconnect_retries_when_handshake_never_lands():
    _assert_reconnect_when_handshake_never_lands()


def test_signalrcore_reconnect_retries_when_deferred_handshake_never_lands():
    _assert_reconnect_when_handshake_never_lands(fail_first_negotiation=True)


def test_signalrcore_reconnect_releases_abandoned_socket_before_retry():
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport(retry_interval=0.2)
    transport.negotiate = MagicMock(return_value=None)
    transport._set_state = MagicMock(wraps=transport._set_state)
    first_eof = Event()
    second_accepted = Event()
    release_server = Event()
    peers = []
    clients = []
    disposing_threads = []
    receive_threads = []
    server_errors = []
    results = []

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(5)

        def serve():
            try:
                for attempt in range(2):
                    peer, _ = listener.accept()
                    peers.append(peer)
                    peer.settimeout(5)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = peer.recv(4096)
                        if not chunk:
                            raise EOFError("Client closed before the websocket upgrade")
                        request += chunk
                    key = next(
                        line.split(b":", 1)[1].strip()
                        for line in request.split(b"\r\n")
                        if line.lower().startswith(b"sec-websocket-key:")
                    )
                    accept = base64.b64encode(hashlib.sha1(
                        key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                    ).digest())
                    peer.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"Upgrade: websocket\r\n"
                        b"Connection: Upgrade\r\n"
                        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                    )
                    if attempt == 0:
                        while peer.recv(4096):
                            pass
                        first_eof.set()
                    else:
                        second_accepted.set()
                        release_server.wait(timeout=5)
            except Exception as ex:
                server_errors.append(ex)

        def create_client():
            def on_open():
                if len(clients) == 2:
                    # Complete recovery in-process while the upgraded peer stays silent.
                    transport.handshake_received = True
                    transport._set_state(TransportState.connected)

            client = WebSocketClient(
                url="ws://127.0.0.1:%s/" % listener.getsockname()[1],
                on_open=on_open,
                on_message=transport.on_message,
                on_error=transport.on_socket_error,
                on_close=transport.on_socket_close,
            )
            if not clients:
                dispose = client.dispose

                def record_dispose():
                    disposing_threads.append(current_thread())
                    receive_threads.append(client.recv_thread)
                    dispose()

                client.dispose = MagicMock(side_effect=record_dispose)
            clients.append(client)
            return client

        transport.create_client = MagicMock(side_effect=create_client)
        server = Thread(target=serve, daemon=True)
        worker = Thread(target=lambda: results.append(transport.handle_reconnect()), daemon=True)
        server.start()
        worker.start()

        try:
            assert first_eof.wait(timeout=3)
            assert second_accepted.wait(timeout=3)
            worker.join(timeout=3)
            assert not worker.is_alive()
            assert results == [True]
            assert len(peers) == 2
            assert transport.create_client.call_count == 2
            assert disposing_threads == [worker]
            assert receive_threads[0] is not worker
            assert not receive_threads[0].is_alive()
            assert clients[0].recv_thread is None
            assert clients[0].dispose.call_count == 1
            assert not server_errors
            assert all(
                call.args[0] != TransportState.disconnected
                for call in transport._set_state.call_args_list
            )
            transport._on_close.assert_not_called()
        finally:
            transport.manually_closing = True
            transport._bazarr_retry_wakeup.set()
            for client in clients:
                client.on_message = client.on_close = client.on_error = lambda *args: None
                raw_socket = getattr(client, "sock", None)
                if raw_socket is not None:
                    try:
                        raw_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            release_server.set()
            for peer in peers:
                try:
                    peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                peer.close()
            server.join(timeout=5)
            worker.join(timeout=5)
            assert not server.is_alive()
            assert not worker.is_alive()
            transport.stop()


def test_signalrcore_stop_during_first_start_disposes_new_client():
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport()
    created = MagicMock()
    transport.create_client = MagicMock(return_value=created)
    transport.deferred_reconnect = MagicMock()

    def stop_during_negotiate():
        transport.stop()
        return None

    transport.negotiate = MagicMock(side_effect=stop_during_negotiate)

    try:
        returned = transport.handle_reconnect()
        assert transport.create_client.call_count == 1
        assert transport.negotiate.call_count == 1
        assert created.dispose.call_count == 1
        transport.deferred_reconnect.assert_not_called()
        assert returned is True
    finally:
        transport.stop()


def test_signalrcore_stop_ends_repeated_failed_negotiations():
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport()
    retried = Event()

    def fail_negotiate():
        if transport.negotiate.call_count >= 2:
            retried.set()
        raise OSError("connection refused")

    transport.negotiate = MagicMock(side_effect=fail_negotiate)
    reconnect_thread = Thread(target=transport.handle_reconnect, daemon=True)
    reconnect_thread.start()

    try:
        assert retried.wait(timeout=2)
        transport.stop()
        reconnect_thread.join(timeout=5)
        assert not reconnect_thread.is_alive()
        stopped_attempts = transport.negotiate.call_count
        Event().wait(timeout=0.05)
        assert transport.negotiate.call_count == stopped_attempts
        assert transport.is_disconnected()
    finally:
        transport.stop()
        reconnect_thread.join(timeout=5)


def test_signalrcore_reconnect_rejects_concurrent_negotiation():
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport()
    negotiating = Event()
    release_negotiate = Event()

    def block_negotiate():
        if transport.negotiate.call_count == 1:
            negotiating.set()
            release_negotiate.wait(timeout=5)
        raise OSError("connection refused")

    transport.negotiate = MagicMock(side_effect=block_negotiate)
    reconnect_thread = Thread(target=transport.handle_reconnect, daemon=True)
    reconnect_thread.start()

    try:
        assert negotiating.wait(timeout=2)
        transport.on_socket_close()
        assert transport.is_disconnected()
        assert transport.handle_reconnect() is False
        assert transport.negotiate.call_count == 1
    finally:
        transport.stop()
        release_negotiate.set()
        reconnect_thread.join(timeout=5)

    assert not reconnect_thread.is_alive()


def test_signalrcore_reconnect_retries_when_deferred_handshake_closes():
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport(retry_interval=0.5)
    connecting = Event()
    connected = Event()
    pending_client = MagicMock()
    pending_client.is_connection_closed.return_value = False
    pending_client.connect.side_effect = connecting.set
    recovered_client = MagicMock()
    recovered_client.is_connection_closed.return_value = False

    def finish_handshake():
        transport.handshake_received = True
        transport._set_state(TransportState.connected)
        connected.set()

    recovered_client.connect.side_effect = finish_handshake
    transport.create_client.side_effect = [pending_client, recovered_client]
    transport.negotiate = MagicMock(side_effect=[OSError("connection refused"), None, None])
    reconnect_thread = Thread(target=transport.handle_reconnect, daemon=True)
    reconnect_thread.start()

    try:
        assert connecting.wait(timeout=2)
        assert transport.negotiate.call_count == 2
        pending_client.is_connection_closed.return_value = True
        transport.on_socket_close()
        assert connected.wait(timeout=2)
        reconnect_thread.join(timeout=5)
        assert not reconnect_thread.is_alive()
        assert transport.negotiate.call_count == 3
        assert transport.is_connected()
        assert not transport.reconnection_handler.reconnecting
    finally:
        transport.stop()
        reconnect_thread.join(timeout=5)


def test_signalrcore_stop_wakes_long_reconnect_wait():
    patch_signalrcore_stop()
    transport = _make_reconnecting_transport(retry_interval=30)
    retry_wait = Event()
    next_retry = transport.reconnection_handler.next

    def record_retry_wait():
        interval = next_retry()
        retry_wait.set()
        return interval

    transport.reconnection_handler.next = record_retry_wait
    transport.negotiate = MagicMock(side_effect=OSError("connection refused"))
    reconnect_thread = Thread(target=transport.handle_reconnect, daemon=True)
    reconnect_thread.start()

    try:
        assert retry_wait.wait(timeout=2)
        transport.stop()
        reconnect_thread.join(timeout=5)
        assert not reconnect_thread.is_alive()
        assert transport.negotiate.call_count == 1
        assert transport.is_disconnected()
    finally:
        transport.stop()
        reconnect_thread.join(timeout=5)
