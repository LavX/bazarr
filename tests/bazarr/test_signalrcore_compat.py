from pathlib import Path
from unittest.mock import MagicMock, patch

import signalrcore
from signalrcore.transport.base_transport import TransportState
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
