from app import signalr_client


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

    def start(self):
        self.starts += 1
        self.transport = _FakeTransport(1)
        return True


def test_sonarr_signalr_start_handles_missing_transport_before_first_start(monkeypatch):
    connection = _FakeConnection()
    client = signalr_client.SonarrSignalrClient()

    monkeypatch.setattr(signalr_client.get_sonarr_info, "supports_signalr_core", lambda: True)
    monkeypatch.setattr(client, "configure", lambda: setattr(client, "connection", connection))

    client.start()

    assert connection.starts == 1


def test_radarr_signalr_start_handles_missing_transport_before_first_start(monkeypatch):
    connection = _FakeConnection()
    client = signalr_client.RadarrSignalrClient()

    monkeypatch.setattr(client, "configure", lambda: setattr(client, "connection", connection))

    client.start()

    assert connection.starts == 1
