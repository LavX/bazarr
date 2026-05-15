from pathlib import Path
from unittest.mock import MagicMock, patch

import signalrcore
from signalrcore.transport.base_transport import TransportState
from signalrcore.transport.websockets.websocket_transport import WebsocketTransport

from app.signalrcore_compat import patch_signalrcore_stop


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
