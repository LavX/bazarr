import socket
import threading
import time


# Seconds to wait before each reconnect attempt. The last value repeats, so an
# arr that stays down is retried every 30s without hammering it, while one that
# comes back is picked up within seconds.
RECONNECT_INTERVALS = (5, 10, 15, 30)


def _make_reconnection_handler(intervals=RECONNECT_INTERVALS):
    from signalrcore.transport.reconnection import ReconnectionHandler

    class BackoffReconnectionHandler(ReconnectionHandler):
        """Bounded backoff that never gives up.

        ``sleep_time`` is the ceiling. The reconnect loops use it as the longest
        a new socket may take to finish its handshake before it is abandoned.
        """

        def __init__(self):
            super().__init__()
            self.intervals = tuple(intervals)
            self.sleep_time = self.intervals[-1]

        def next(self):
            self.reconnecting = True
            index = min(self.attempt_number, len(self.intervals) - 1)
            self.attempt_number += 1
            return self.intervals[index]

    return BackoffReconnectionHandler()


def _bazarr_retry_wakeup(self):
    return self.__dict__.setdefault("_bazarr_retry_wakeup", threading.Event())


def _wait_for_handshake(self, wakeup, timeout=None):
    deadline = None if timeout is None else time.monotonic() + timeout
    while not self.manually_closing:
        if self.is_connected():
            return True
        client = getattr(self, "_client", None)
        if client is None or client.is_connection_closed():
            return False
        wait_time = 0.5
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wait_time = min(wait_time, remaining)
        if wakeup.wait(wait_time):
            return False
    return False


def _abandon_client(client):
    client.on_message = client.on_close = client.on_error = lambda *args, **kwargs: None
    try:
        client.sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        client.dispose()
    except Exception:
        pass


def patch_signalrcore_reconnect():
    from signalrcore.transport.base_transport import TransportState
    from signalrcore.transport.websockets.websocket_transport import WebsocketTransport

    if not getattr(WebsocketTransport.handle_reconnect, "_bazarr_reconnect_patch", False):
        def handle_reconnect(self) -> bool:
            lock = self.__dict__.setdefault("_bazarr_reconnect_lock", threading.Lock())
            wakeup = _bazarr_retry_wakeup(self)
            while True:
                if self.manually_closing:
                    return False
                if self.reconnection_handler is None:
                    return False
                if not lock.acquire(blocking=False):
                    return False

                try:
                    client = getattr(self, "_client", None)
                    if client is None or not client.is_connection_closed():
                        return False
                    try:
                        self.reconnection_handler.reconnecting = True
                        self._set_state(TransportState.reconnecting)
                        client.dispose()
                        self.start(reconnection=True)
                    except Exception as ex:
                        self.logger.error(ex)
                        self.deferred_reconnect(self.reconnection_handler.next())
                    else:
                        if self.manually_closing:
                            try:
                                self._client.dispose()
                            except Exception:
                                pass
                            return True
                        if _wait_for_handshake(
                                self, wakeup,
                                timeout=getattr(self.reconnection_handler, "sleep_time", None)):
                            self.reconnection_handler.reset()
                        else:
                            _abandon_client(self._client)
                            self.deferred_reconnect(self.reconnection_handler.next())
                finally:
                    lock.release()

                client = getattr(self, "_client", None)
                if self.manually_closing or client is None or not client.is_connection_closed():
                    return True

        handle_reconnect._bazarr_reconnect_patch = True
        WebsocketTransport.handle_reconnect = handle_reconnect

    if not getattr(WebsocketTransport.deferred_reconnect, "_bazarr_deferred_reconnect_patch", False):
        def deferred_reconnect(self, sleep_time):
            wakeup = _bazarr_retry_wakeup(self)
            while not self.manually_closing:
                if wakeup.wait(sleep_time) or self.manually_closing:
                    return
                try:
                    self.start(reconnection=True)
                except Exception as ex:
                    self.logger.warning("Reconnect attempt failed: %s", ex)
                    sleep_time = self.reconnection_handler.next()
                    continue

                if self.manually_closing:
                    try:
                        self._client.dispose()
                    except Exception:
                        pass
                    return
                if _wait_for_handshake(
                        self, wakeup,
                        timeout=getattr(self.reconnection_handler, "sleep_time", None)):
                    self.reconnection_handler.reset()
                    return
                _abandon_client(self._client)
                sleep_time = self.reconnection_handler.next()

        deferred_reconnect._bazarr_deferred_reconnect_patch = True
        WebsocketTransport.deferred_reconnect = deferred_reconnect


def patch_signalrcore_stop():
    from signalrcore.transport.base_transport import TransportState
    from signalrcore.transport.websockets.websocket_transport import WebsocketTransport

    if not getattr(WebsocketTransport.stop, "_bazarr_stop_patch", False):
        def stop(self):
            self.manually_closing = True
            _bazarr_retry_wakeup(self).set()
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

    patch_signalrcore_reconnect()


def build_signalr_connection(url, headers):
    from signalrcore.hub_connection_builder import HubConnectionBuilder
    from signalrcore.protocol.json_hub_protocol import JsonHubProtocol

    builder = HubConnectionBuilder() \
        .with_url(url,
                  options={
                      "verify_ssl": False,
                      "headers": headers
                  }) \
        .with_hub_protocol(JsonHubProtocol()) \
        .with_automatic_reconnect({
            "type": "raw",
            "keep_alive_interval": 5,
            "max_attempts": None
        })
    # The stock raw handler waits one fixed interval between attempts, which
    # left the feed down for minutes after the arr was back.
    builder.reconnection_handler = _make_reconnection_handler()
    return builder.build()
