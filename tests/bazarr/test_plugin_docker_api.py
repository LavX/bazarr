# coding=utf-8
"""The Docker Engine API client, against a fake daemon on a temp Unix socket of the test's own.

No test here (or anywhere in the managed container suite) reaches a real Docker daemon: the fake
records every request, so the asserts say which calls were made and which never were.
"""
import json

import pytest

from provider_hub.runtime import docker_api
from provider_hub.runtime.docker_api import (
    DockerApiError,
    DockerClient,
    DockerNotFound,
    DockerRefused,
    DockerUnavailable,
    Endpoint,
    check_create_body,
    endpoint_from_env,
    parse_endpoint,
)
from provider_hub.runtime.testing.fake_docker import FakeDocker, digest_of, forbid_real_sockets

REPO = "ghcr.io/example/daemon"
DIGEST = digest_of("index")


@pytest.fixture
def docker(monkeypatch):
    forbid_real_sockets(monkeypatch)
    with FakeDocker() as fake:
        yield fake


def valid_body(**host_overrides):
    host = {
        "NetworkMode": "bazarr-plus-net", "Privileged": False, "CapDrop": ["ALL"], "Memory": 1 << 30,
        "MemorySwap": 1 << 30, "PidsLimit": 512, "PortBindings": {}, "PublishAllPorts": False,
    }
    host.update(host_overrides)
    return {"Image": f"{REPO}@{DIGEST}", "HostConfig": host}


# ------------------------------------------------------------------ endpoints


@pytest.mark.parametrize("value, expected", [
    (None, Endpoint("unix", path="/var/run/docker.sock")),
    ("", Endpoint("unix", path="/var/run/docker.sock")),
    ("/run/docker.sock", Endpoint("unix", path="/run/docker.sock")),
    ("unix:///run/user/1000/docker.sock", Endpoint("unix", path="/run/user/1000/docker.sock")),
    ("tcp://socket-proxy:2375", Endpoint("tcp", host="socket-proxy", port=2375)),
    ("http://192.0.2.5:2375/", Endpoint("tcp", host="192.0.2.5", port=2375)),
])
def test_endpoints_are_read_from_docker_host_style_values(value, expected):
    assert parse_endpoint(value) == expected


@pytest.mark.parametrize("value", [
    "https://host:2376", "ssh://user@host", "npipe:////./pipe/docker_engine", "tcp://host", "tcp://:2375",
    "tcp://user:pw@host:2375", "tcp://host:2375/path", "unix://relative/path", "ftp://host:21", "tcp://host:99999",
])
def test_other_schemes_and_malformed_addresses_are_refused(value):
    with pytest.raises(DockerRefused):
        parse_endpoint(value)


def test_with_no_docker_host_and_no_socket_no_endpoint_is_offered(monkeypatch):
    monkeypatch.setattr(docker_api.os.path, "exists", lambda path: False)

    assert endpoint_from_env({}) is None
    assert endpoint_from_env({"DOCKER_HOST": "  "}) is None
    assert endpoint_from_env({"DOCKER_HOST": "tcp://proxy:2375"}) == Endpoint("tcp", host="proxy", port=2375)


def test_the_default_socket_is_offered_when_it_exists(monkeypatch):
    monkeypatch.setattr(docker_api.os.path, "exists", lambda path: path == docker_api.DEFAULT_SOCKET)

    assert endpoint_from_env({}) == Endpoint("unix", path=docker_api.DEFAULT_SOCKET)


# ------------------------------------------------------------------ ping and version


def test_ping_settles_the_api_version_from_the_daemon(docker):
    client = docker.client()

    assert client.ping() == "1.44"
    client.list_containers()

    assert docker.requests[-1]["version"] == "1.44"
    assert docker.requests[0]["version"] is None  # the ping itself is unversioned


def test_a_newer_daemon_is_capped_at_the_newest_version_the_client_knows(docker):
    docker.api_version = "1.99"
    client = docker.client()

    assert client.ping() == "1.44"


def test_an_older_but_supported_daemon_is_spoken_to_at_its_own_version(docker):
    docker.api_version = "1.41"

    assert docker.client().ping() == "1.41"


def test_a_much_newer_daemon_is_spoken_to_at_the_ceiling_and_still_works(docker):
    docker.api_version = "1.99"
    docker.min_api_version = "1.24"
    client = docker.client()

    assert client.ping() == "%d.%d" % docker_api.API_VERSION_CEILING
    assert client.list_containers() == []

    assert docker.calls("GET", "/containers/json")[0]["version"] == "1.44"


def test_a_daemon_whose_minimum_is_above_the_ceiling_is_refused_with_the_reason(docker):
    docker.api_version = "1.55"
    docker.min_api_version = "1.50"
    client = docker.client()
    client.ping()

    with pytest.raises(DockerUnavailable, match="needs API 1.50 or newer, and this Bazarr\\+ speaks up to 1.44"):
        client.list_containers()


def test_a_daemon_whose_minimum_is_at_or_below_the_ceiling_is_not_refused_for_its_version(docker):
    docker.api_version = "1.55"
    docker.min_api_version = "1.44"

    assert docker.client().ping() == "1.44"
    client = docker.client()
    client.ping()
    assert client.list_containers() == []


def test_a_daemon_older_than_the_oldest_supported_version_is_refused(docker):
    docker.api_version = "1.30"

    with pytest.raises(DockerUnavailable, match="older than API 1.41"):
        docker.client().ping()


def test_a_missing_version_header_falls_back_to_the_oldest_supported_version(docker):
    docker.api_version = None

    assert docker.client().ping() == "1.41"


def test_an_address_that_is_not_a_docker_daemon_is_reported_not_trusted(docker):
    docker.fail.append((r"GET /_ping", 200, "not ok"))

    with pytest.raises(DockerUnavailable, match="does not answer as a Docker daemon"):
        docker.client().ping()


def test_a_socket_that_is_not_there_is_unavailable_and_says_so(tmp_path):
    client = DockerClient(Endpoint("unix", path=str(tmp_path / "missing.sock")))

    with pytest.raises(DockerUnavailable, match="could not be reached"):
        client.ping()


# ------------------------------------------------------------------ routes


def test_only_the_allowed_routes_can_be_sent(docker):
    client = docker.client()
    client.ping()

    for method, path in [("GET", "/containers/abc/logs"), ("POST", "/containers/abc/exec"),
                         ("GET", "/volumes"), ("POST", "/build"), ("GET", "/info"), ("GET", "/version"),
                         ("POST", "/containers/abc/kill"), ("DELETE", "/images/abc"), ("GET", "/swarm"),
                         ("POST", "/containers/abc/update"), ("GET", "/containers/abc/archive")]:
        with pytest.raises(DockerRefused, match="not a Docker route"):
            client._call(method, path)

    # Refused before the socket: nothing but the ping got there.
    assert [r["path"] for r in docker.requests] == ["/_ping"]


@pytest.mark.parametrize("name", ["../x", "a/b", "a b", "a?x=1", "", "-x", "x" * 129, "a\nb", "abc\n", None, "a%2Fb"])
def test_a_name_that_could_add_a_path_segment_is_refused_before_anything_is_sent(docker, name):
    client = docker.client()

    for call in (client.inspect_container, client.remove_container, client.start_container,
                 client.inspect_network, client.remove_network, lambda n: client.stop_container(n, 1)):
        with pytest.raises(DockerRefused):
            call(name)

    assert docker.requests == []


def test_a_trailing_segment_does_not_slip_through_a_route(docker):
    client = docker.client()

    for method, path in [("GET", "/containers/abc/json/extra"), ("POST", "/containers/abc/start/x"),
                         ("DELETE", "/containers/abc/json"), ("GET", "/networks/abc/connect"),
                         ("GET", "/images/abc/json/x"), ("GET", "/containers/json/x")]:
        with pytest.raises(DockerRefused):
            client._call(method, path)

    assert docker.requests == []


def test_the_image_reference_must_be_a_plain_reference(docker):
    client = docker.client()

    for bad in ("Image With Space", "../etc/passwd", "a@sha256:xyz", "x;y", "", "A/B", "a/../b", "a//b", "/a", "a/",
                "a/b/", "a/./../b", "abc\n", f"abc@{DIGEST}\n", "a" * 400):
        with pytest.raises(DockerRefused):
            client.inspect_image(bad)

    assert docker.requests == []


def test_an_image_reference_up_to_the_manifest_maximum_is_accepted(docker):
    client = docker.client()
    longest = "a" * 255 + "@" + DIGEST

    with pytest.raises(DockerNotFound):
        client.inspect_image(longest)

    assert len(docker.requests) == 1


# ------------------------------------------------------------------ the create body


def test_a_conforming_body_passes():
    check_create_body(valid_body())


@pytest.mark.parametrize("overrides, text", [
    ({"Privileged": True}, "privileged"),
    ({"Binds": ["/:/host"]}, "Binds"),
    ({"Devices": [{"PathOnHost": "/dev/sda"}]}, "Devices"),
    ({"CapAdd": ["SYS_ADMIN"]}, "CapAdd"),
    ({"PidMode": "host"}, "PidMode"),
    ({"IpcMode": "container:abc"}, "IpcMode"),
    ({"UsernsMode": "host"}, "UsernsMode"),
    ({"NetworkMode": "host"}, "named network"),
    ({"NetworkMode": "bridge"}, "named network"),
    ({"NetworkMode": "container:abc"}, "named network"),
    ({"NetworkMode": ""}, "named network"),
    ({"PortBindings": {"80/tcp": [{"HostPort": "80"}]}}, "ports"),
    ({"PublishAllPorts": True}, "ports"),
    ({"SecurityOpt": ["seccomp=unconfined"]}, "security"),
    ({"SecurityOpt": ["apparmor=unconfined"]}, "security"),
    ({"Mounts": [{"Type": "bind", "Source": "/", "Target": "/h"}]}, "mounts nothing"),
    ({"VolumesFrom": ["other"]}, "VolumesFrom"),
    ({"Memory": 0, "MemorySwap": 0}, "memory limit"),
    ({"MemorySwap": -1}, "memory limit"),
    ({"PidsLimit": 0}, "process limit"),
    ({"Sysctls": {"net.ipv4.ip_forward": "1"}}, "Sysctls"),
    ({"Runtime": "other"}, "Runtime"),
])
def test_a_body_that_asks_for_an_escape_or_drops_a_limit_is_refused(overrides, text):
    with pytest.raises(DockerRefused, match=text):
        check_create_body(valid_body(**overrides))


def test_exposed_ports_and_volumes_are_refused_at_the_top_level_too():
    for key, value in (("ExposedPorts", {"8191/tcp": {}}), ("Volumes", {"/data": {}})):
        body = valid_body()
        body[key] = value
        with pytest.raises(DockerRefused):
            check_create_body(body)


def test_tmpfs_mounts_are_the_only_mounts_allowed():
    check_create_body(valid_body(Mounts=[{"Type": "tmpfs", "Target": "/tmp"}], Tmpfs={"/tmp": "rw,size=64m"}))
    with pytest.raises(DockerRefused):
        check_create_body(valid_body(Mounts=[{"Type": "volume", "Source": "v", "Target": "/v"}]))


def test_create_refuses_a_bad_body_before_the_socket(docker):
    client = docker.client()

    with pytest.raises(DockerRefused):
        client.create_container("bazarr-plus-x", valid_body(Privileged=True))

    assert docker.requests == []


# ------------------------------------------------------------------ calls against the fake


def test_a_pull_is_by_digest_only_and_reads_the_progress_to_the_end(docker):
    docker.add_registry_image(REPO, DIGEST)
    client = docker.client()
    client.ping()

    client.pull_image(REPO, DIGEST)

    pull = docker.calls("POST", "/images/create")[0]
    assert pull["query"] == {"fromImage": f"{REPO}@{DIGEST}"}
    assert "tag" not in pull["query"]
    assert f"{REPO}@{DIGEST}" in docker.images


@pytest.mark.parametrize("repository, digest", [
    (REPO, "latest"), (REPO, "sha256:abc"), (REPO, DIGEST + "\n"), (REPO + ":latest", DIGEST), (f"{REPO}@{DIGEST}", DIGEST),
    ("UPPER/case", DIGEST),
])
def test_a_pull_by_tag_or_with_a_loose_digest_is_refused_without_a_request(docker, repository, digest):
    with pytest.raises(DockerRefused):
        docker.client().pull_image(repository, digest)

    assert docker.requests == []


def test_an_error_line_in_the_pull_stream_is_raised(docker):
    docker.add_registry_image(REPO, DIGEST)
    docker.pull_error = "manifest unknown"

    with pytest.raises(DockerApiError, match="manifest unknown"):
        docker.client().pull_image(REPO, DIGEST)


def test_a_pull_the_daemon_answers_with_an_error_status_is_raised(docker):
    with pytest.raises(DockerNotFound, match="manifest"):
        docker.client().pull_image(REPO, DIGEST)


def test_error_text_from_the_daemon_is_scrubbed_and_short(docker):
    secret = "abcdefghijklmnopqrstuvwxyz0123"
    docker.fail.append((r"GET /containers/json", 500, f"boom Bearer {secret} " + "x" * 1000))

    with pytest.raises(DockerApiError) as error:
        docker.client().list_containers()

    assert secret not in error.value.message
    assert len(error.value.message) <= docker_api.MAX_MESSAGE_CHARS
    assert error.value.status == 500


def test_a_proxy_that_denies_a_route_gives_a_plain_api_error(docker):
    docker.deny.append(r"POST /containers/create")
    client = docker.client()

    with pytest.raises(DockerApiError) as error:
        client.create_container("bazarr-plus-x", valid_body())

    assert error.value.status == 403


def test_a_missing_container_is_not_found_and_removal_of_it_is_quiet(docker):
    client = docker.client()

    with pytest.raises(DockerNotFound):
        client.inspect_container("nothing")
    client.remove_container("nothing")
    client.remove_network("nothing")


def test_stop_treats_an_already_stopped_container_as_done(docker):
    docker.add_local_image(REPO, DIGEST)
    docker.networks["n"] = {"Id": "n", "Name": "bazarr-plus-net", "Labels": {}, "Containers": {}}
    client = docker.client()
    cid, warnings = client.create_container("bazarr-plus-x", valid_body())

    client.stop_container(cid, 3)  # not running: 304
    client.start_container(cid)
    client.stop_container(cid, 3)
    client.remove_container(cid)

    assert warnings == []
    assert docker.containers == {}
    assert docker.calls("POST", "/stop")[0]["query"] == {"t": "3"}


def test_an_oversized_answer_is_refused_rather_than_buffered(docker, monkeypatch):
    monkeypatch.setattr(docker_api, "MAX_BODY_BYTES", 64)
    docker.add_registry_image(REPO, DIGEST)
    for index in range(10):
        docker.networks[str(index)] = {"Id": str(index), "Name": f"n{index}", "Labels": {}, "Containers": {}}

    with pytest.raises(DockerUnavailable, match="more data than expected"):
        docker.client().list_networks()


def test_a_connection_that_drops_midway_is_unavailable(tmp_path):
    import socket
    import threading

    path = str(tmp_path / "x.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def drop():
        connection, _ = server.accept()
        connection.recv(1024)
        connection.close()

    thread = threading.Thread(target=drop, daemon=True)
    thread.start()
    try:
        with pytest.raises(DockerUnavailable):
            DockerClient(Endpoint("unix", path=path)).ping()
    finally:
        thread.join(timeout=5)
        server.close()


def test_label_and_network_filters_travel_as_one_json_query(docker):
    client = docker.client()
    client.ping()

    client.list_containers(labels={"b": "2", "a": "1"}, network="net1")

    query = docker.calls("GET", "/containers/json")[0]["query"]
    assert query["all"] == "1"
    assert json.loads(query["filters"]) == {"label": ["a=1", "b=2"], "network": ["net1"]}


def test_a_created_network_is_a_plain_bridge_with_outbound_access_and_labels(docker):
    client = docker.client()
    client.ping()

    client.create_network("bazarr-plus-n", labels={"bazarr-plus.managed": "true"})

    body = docker.calls("POST", "/networks/create")[0]["body"]
    assert body["Driver"] == "bridge" and body["Internal"] is False
    assert body["EnableIPv6"] is False and body["Attachable"] is False
    assert body["Labels"] == {"bazarr-plus.managed": "true"}


# ------------------------------------------------------------------ the stricter body rules


@pytest.mark.parametrize("overrides, text", [
    ({"MaskedPaths": []}, "MaskedPaths"),
    ({"ReadonlyPaths": []}, "ReadonlyPaths"),
    ({"SecurityOpt": ["label=disable"]}, "security"),
    ({"SecurityOpt": ["label:disable"]}, "security"),
    ({"SecurityOpt": ["seccomp=/profiles/mine.json"]}, "security"),
    ({"SecurityOpt": ["seccomp:{}"]}, "security"),
    ({"SecurityOpt": ["apparmor=custom"]}, "security"),
    ({"SecurityOpt": ["no-new-privileges:false"]}, "new privileges"),
    ({"SecurityOpt": ["no-new-privileges=false"]}, "new privileges"),
    ({"IpcMode": "shareable"}, "private or none"),
    ({"PidsLimit": "512"}, "process limit"),
    ({"PidsLimit": 5.5}, "process limit"),
    ({"PidsLimit": True}, "process limit"),
    ({"PidsLimit": None}, "process limit"),
])
def test_the_stricter_body_rules(overrides, text):
    with pytest.raises(DockerRefused, match=text):
        check_create_body(valid_body(**overrides))


def test_conforming_security_options_and_ipc_modes_pass():
    check_create_body(valid_body(SecurityOpt=["no-new-privileges:true"], IpcMode="private"))
    check_create_body(valid_body(SecurityOpt=["no-new-privileges"], IpcMode="none"))
    check_create_body(valid_body(MaskedPaths=["/proc/kcore"], ReadonlyPaths=["/proc/sys"]))


def test_a_second_network_in_the_endpoints_is_refused():
    body = valid_body()
    body["NetworkingConfig"] = {"EndpointsConfig": {"bazarr-plus-net": {}, "other": {}}}
    with pytest.raises(DockerRefused, match="only its one private network"):
        check_create_body(body)

    body["NetworkingConfig"] = {"EndpointsConfig": {"bazarr-plus-net": {"Aliases": ["daemon"]}}}
    check_create_body(body)


# ------------------------------------------------------------------ tcp


def test_the_client_speaks_to_a_tcp_daemon_on_loopback(monkeypatch):
    forbid_real_sockets(monkeypatch)
    with FakeDocker(tcp=True) as fake:
        client = fake.client()

        assert client.ping() == "1.44"
        assert client.list_containers() == []
        assert client.endpoint.kind == "tcp" and client.endpoint.host == "127.0.0.1"


def test_the_guard_refuses_a_tcp_address_that_is_not_a_fake(monkeypatch):
    forbid_real_sockets(monkeypatch)
    client = DockerClient(Endpoint("tcp", host="192.0.2.5", port=2375))

    with pytest.raises(AssertionError, match="real Docker connection over tcp"):
        client.ping()


# ------------------------------------------------------------------ cancelling at any point of a call


def test_a_cancel_during_the_wait_for_response_headers_ends_the_pull_within_seconds(docker):
    import threading
    import time

    docker.add_registry_image(REPO, DIGEST)
    docker.header_delay = 30.0  # a slow registry: the daemon says nothing for a long while
    client = docker.client()
    cancel = threading.Event()
    outcome = []

    def pull():
        try:
            client.pull_image(REPO, DIGEST, cancel=cancel)
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=pull)
    thread.start()
    deadline = time.monotonic() + 5
    while not any("/images/create" in path for path in docker.arrivals) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert any("/images/create" in path for path in docker.arrivals)
    began = time.monotonic()

    cancel.set()
    thread.join(timeout=10)

    assert not thread.is_alive() and time.monotonic() - began < 3.0
    assert len(outcome) == 1 and isinstance(outcome[0], docker_api.DockerCancelled)


def test_a_pull_that_is_cancelled_before_it_starts_sends_nothing(docker):
    import threading

    cancel = threading.Event()
    cancel.set()

    with pytest.raises(docker_api.DockerCancelled):
        docker.client().pull_image(REPO, DIGEST, cancel=cancel)

    assert docker.arrivals == []
