def patch_signalrcore_stop():
    from signalrcore.transport.base_transport import TransportState
    from signalrcore.transport.websockets.websocket_transport import WebsocketTransport

    if not getattr(WebsocketTransport.stop, "_bazarr_stop_patch", False):
        def stop(self):
            self.manually_closing = True
            connection_checker = getattr(self, "connection_checker", None)
            if connection_checker is not None:
                connection_checker.stop()
            websocket = getattr(self, "_ws", None)
            if websocket is not None:
                websocket.close()
            client = getattr(self, "_client", None)
            if client is not None:
                client.close()
            self._set_state(TransportState.disconnected)
            self.handshake_received = False

        stop._bazarr_stop_patch = True
        WebsocketTransport.stop = stop

    if not getattr(WebsocketTransport._set_state, "_bazarr_state_patch", False):
        def _set_state(self, new_state):
            if new_state == self.state:
                return

            old_state = self.state
            self.state = new_state

            self.logger.debug(
                "Transport state changed: %s -> %s", old_state.name, new_state.name)

            was_connecting = old_state == TransportState.connecting
            was_connected = old_state == TransportState.connected
            was_reconnecting = old_state == TransportState.reconnecting

            if (was_connecting or was_reconnecting) and self.is_connected():
                self._on_open()
            elif self.is_reconnecting():
                self._on_reconnect()
            elif (was_connected or was_reconnecting) and self.is_disconnected():
                self._on_close()

        _set_state._bazarr_state_patch = True
        WebsocketTransport._set_state = _set_state


def build_signalr_connection(url, headers):
    from signalrcore.hub_connection_builder import HubConnectionBuilder
    from signalrcore.protocol.json_hub_protocol import JsonHubProtocol

    return HubConnectionBuilder() \
        .with_url(url,
                  options={
                      "verify_ssl": False,
                      "headers": headers
                  }) \
        .with_hub_protocol(JsonHubProtocol()) \
        .with_automatic_reconnect({
            "type": "raw",
            "keep_alive_interval": 5,
            "reconnect_interval": 180,
            "max_attempts": None
        }).build()
