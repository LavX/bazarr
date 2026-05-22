def patch_signalrcore_stop():
    from signalrcore.transport.base_transport import TransportState
    from signalrcore.transport.websockets.websocket_transport import WebsocketTransport

    if getattr(WebsocketTransport.stop, "_bazarr_stop_patch", False):
        return

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
