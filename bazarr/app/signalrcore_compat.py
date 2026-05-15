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
        if self._ws is not None:
            self._ws.close()
        self._set_state(TransportState.disconnected)
        self.handshake_received = False

    stop._bazarr_stop_patch = True
    WebsocketTransport.stop = stop
