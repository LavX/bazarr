# coding=utf-8
"""The host half of the authenticating front: topology, the container it builds, leases and tokens, restarts, removal.

Docker is ``FakeDocker`` on a temp Unix socket; no test reaches a real daemon and no container is started (the fake
flips flags). The admin channel is real: a harness builds a ``front.FrontServer`` from the environment the
runtime put in the container's configuration, on loopback, and routes the runtime's HTTP connections to it, so the
env contract, the admin secret and the token checks are exercised end to end.
"""
import copy
import http.client
import http.server
import json
import pickle
import threading
import time
import urllib.parse

import pytest

from provider_hub.runtime import front as front_server
from provider_hub.runtime import oci as oci_module
from provider_hub.runtime.front_runtime import (
    FRONT_ADMIN_PORT,
    FRONT_ENTRYPOINT,
    FRONT_PORT,
    FRONT_USER,
    ROLE_FRONT,
    FrontError,
    FrontLease,
    FrontRuntime,
    FrontSpec,
    front_container_name,
    front_network_name,
    new_lease_token,
)
from provider_hub.runtime.oci import (
    CREATOR_LABEL,
    MANAGED_LABEL,
    OWNER_LABEL,
    PLUGIN_LABEL,
    ROLE_LABEL,
    OciDaemonRuntime,
    OciError,
    OciUnavailable,
    check_daemon_host,
    cleanup_orphans,
    flaresolverr_spec,
    network_name,
    remove_plugin,
)
from provider_hub.runtime.testing.fake_docker import FakeDocker, digest_of, forbid_real_sockets

OWNER = "0123456789abcdef"
PLUGIN = "flaresolverr"
REPO = "ghcr.io/flaresolverr/flaresolverr"
DIGEST = digest_of("flaresolverr-index")
SELF_ID = "ab" * 32
SELF_IMAGE = digest_of("bazarr-image")
FRONT_NAME = front_container_name(OWNER, PLUGIN)
NET_A = front_network_name(OWNER, PLUGIN)
NET_B = network_name(OWNER, PLUGIN)


@pytest.fixture(autouse=True)
def never_the_real_daemon(monkeypatch):
    forbid_real_sockets(monkeypatch)
    oci_module.reset_offer_cache()
    yield
    oci_module.reset_offer_cache()


@pytest.fixture
def docker():
    with FakeDocker() as fake:
        fake.add_registry_image(REPO, DIGEST)
        fake.add_self(SELF_ID, networks=("bridge",))
        fake.add_image_by_id(SELF_IMAGE)
        yield fake


class Stub:
    """A stand-in for the daemon behind the front."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                return None

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append((self.path, data))
                body = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Harness:
    """Routes the runtime's admin connections to a real front built from the container's own environment."""

    def __init__(self, docker, stub):
        self.docker = docker
        self.stub = stub
        self.server = None
        self.admin = None
        self.port = None
        self.admin_port = None
        self.container = None
        self.down = False
        self.targets = []

    def container_env(self):
        record = self.docker.container_named(FRONT_NAME)
        return dict(item.split("=", 1) for item in record["Config"]["Env"]), record["Id"]

    def boot(self):
        env, identity = self.container_env()
        assert set(env) == {front_server.ENV_ADMIN_SHA256, front_server.ENV_UPSTREAM, front_server.ENV_PORT,
                            front_server.ENV_ADMIN_PORT, front_server.ENV_ALLOW_NET, front_server.ENV_MAX_BODY, front_server.ENV_MAX_RESPONSE,
                            front_server.ENV_UPSTREAM_TIMEOUT}
        # What a loopback test cannot keep: the real port, the daemon's alias and the network the front serves.
        env = dict(env, **{front_server.ENV_PORT: "0", front_server.ENV_ADMIN_PORT: "0",
                           front_server.ENV_UPSTREAM: "127.0.0.1:%d" % self.stub.port})
        env.pop(front_server.ENV_ALLOW_NET)
        self.close_server()
        config = front_server.FrontConfig.from_env(env)
        self.server = front_server.FrontServer(("127.0.0.1", 0), config, role=front_server.ROLE_PROXY)
        self.admin = front_server.FrontServer(("127.0.0.1", 0), config, self.server.leases,
                                              role=front_server.ROLE_ADMIN)
        self.port = self.server.server_address[1]
        self.admin_port = self.admin.server_address[1]
        self.container = identity
        for server in (self.server, self.admin):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def connect(self, host, port, timeout):
        if self.down:
            raise OSError("connection refused")
        self.targets.append((host, port))
        assert port == FRONT_ADMIN_PORT, "Bazarr+ talks to the front's admin listener"
        record = self.docker.container_named(FRONT_NAME)
        assert record is not None
        if record["Id"] != self.container:
            self.boot()
        return http.client.HTTPConnection("127.0.0.1", self.admin_port, timeout=timeout)

    def post(self, lease_or_url, body=b'{"cmd": "request.get"}'):
        url = lease_or_url.url() if hasattr(lease_or_url, "url") else lease_or_url
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("POST", urllib.parse.urlsplit(url).path, body, {"Content-Type": "application/json"})
            return connection.getresponse().status
        finally:
            connection.close()

    def close_server(self):
        for name in ("server", "admin"):
            server = getattr(self, name)
            if server is not None:
                server.shutdown()
                server.server_close()
                setattr(self, name, None)


@pytest.fixture
def harness(docker):
    stub = Stub()
    instance = Harness(docker, stub)
    yield instance
    instance.close_server()
    stub.close()


@pytest.fixture
def runtimes():
    made = []
    yield made
    for runtime in made:
        runtime.close(0)


def host_ok(client):
    return check_daemon_host(client, in_container=True, container_id=SELF_ID)


def make_front(docker, harness, runtimes, spec=None, **kwargs):
    kwargs.setdefault("host_check", host_ok)
    kwargs.setdefault("connect", harness.connect)
    kwargs.setdefault("poll_interval", 0.02)
    kwargs.setdefault("ready_interval", 0.001)
    kwargs.setdefault("sleep", lambda seconds: time.sleep(min(seconds, 0.005)))
    runtime = FrontRuntime(spec or FrontSpec(plugin_id=PLUGIN, upstream_port=8191), client=docker.client(),
                           owner_id=OWNER, **kwargs)
    runtimes.append(runtime)
    return runtime


@pytest.fixture
def front(docker, harness, runtimes):
    runtime = make_front(docker, harness, runtimes)
    runtime.start()
    return runtime


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def front_record(docker):
    return docker.container_named(FRONT_NAME)


def network_of(docker, name):
    for record in docker.networks.values():
        if record["Name"] == name:
            return record
    return None


def create_body(docker):
    return docker.calls("POST", "/containers/create")[-1]["body"]


# ------------------------------------------------------------------ the spec


@pytest.mark.parametrize("overrides", [
    {"plugin_id": "Bad Id"},
    {"upstream_port": 0},
    {"upstream_port": 70000},
    {"upstream_port": True},
    {"upstream_host": "bad host"},
    {"upstream_host": "-x"},
    {"memory_bytes": 1},
    {"memory_bytes": 1 << 40},
    {"pids_limit": 1},
    {"max_body_bytes": 0},
    {"max_response_bytes": 1 << 40},
    {"upstream_timeout": 0},
    {"start_timeout": 0},
    {"stop_timeout": -1},
])
def test_a_bad_spec_is_refused(overrides):
    values = {"plugin_id": PLUGIN, "upstream_port": 8191, **overrides}

    with pytest.raises(ValueError):
        FrontSpec(**values)


def test_a_front_spec_follows_the_daemon_it_fronts():
    daemon = flaresolverr_spec(PLUGIN, REPO, DIGEST)

    spec = FrontSpec.for_daemon(daemon, max_body_bytes=2048)

    assert (spec.plugin_id, spec.upstream_port, spec.upstream_host, spec.max_body_bytes) == (PLUGIN, 8191, "daemon", 2048)


def test_the_names_are_per_install_and_per_plugin():
    assert FRONT_NAME == "bazarr-plus-01234567-flaresolverr-front"
    assert NET_A == "bazarr-plus-01234567-flaresolverr-front-net"
    assert NET_A != NET_B and FRONT_NAME != oci_module.container_name(OWNER, PLUGIN)


# ------------------------------------------------------------------ what is built


def test_the_front_is_created_hardened_from_bazarrs_own_image_and_never_pulled(docker, front):
    body = create_body(docker)
    host = body["HostConfig"]

    assert body["Image"] == SELF_IMAGE and "@" not in body["Image"]
    assert docker.calls("POST", "/images/create") == []
    assert body["Entrypoint"] == list(FRONT_ENTRYPOINT) and body["Cmd"] == []
    assert body["User"] == FRONT_USER == "65534:65534"
    assert host["CapDrop"] == ["ALL"] and host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["ReadonlyRootfs"] is True and host["Privileged"] is False
    assert host["PortBindings"] == {} and host["PublishAllPorts"] is False
    assert host["RestartPolicy"] == {"Name": "no"} and host["IpcMode"] == "private"
    assert host["Memory"] == host["MemorySwap"] == 128 * 1024 * 1024 and host["PidsLimit"] == 128
    assert not host.get("Binds") and not host.get("Devices") and not host.get("CapAdd")
    assert host["NetworkMode"] == NET_A
    assert list(body["NetworkingConfig"]["EndpointsConfig"]) == [NET_A]
    assert body["NetworkingConfig"]["EndpointsConfig"][NET_A]["Aliases"] == []
    assert "ExposedPorts" not in body
    assert body["Healthcheck"]["Test"][:3] == ["CMD", "python3", "-I"]


def test_the_front_carries_this_installs_labels_and_the_front_role(docker, front):
    labels = front_record(docker)["Config"]["Labels"]

    assert labels == {MANAGED_LABEL: "true", OWNER_LABEL: OWNER, PLUGIN_LABEL: PLUGIN, ROLE_LABEL: ROLE_FRONT,
                      CREATOR_LABEL: SELF_ID}


def test_the_environment_holds_the_digest_of_the_secret_and_the_limits_and_nothing_else(docker, front):
    env = dict(item.split("=", 1) for item in front_record(docker)["Config"]["Env"])

    assert env[front_server.ENV_UPSTREAM] == "daemon:8191"
    assert env[front_server.ENV_PORT] == str(FRONT_PORT)
    assert env[front_server.ENV_ALLOW_NET] == "198.51.101.0/24"  # network A, the second one made
    assert env[front_server.ENV_ADMIN_PORT] == str(FRONT_ADMIN_PORT)
    assert env[front_server.ENV_ADMIN_SHA256] == front_server.hashlib.sha256(front._secret.encode()).hexdigest()
    assert front._secret not in "".join(env.values())
    assert len(env) == 8


def test_the_image_config_volume_is_replaced_by_an_empty_read_only_mount(docker, harness, runtimes):
    docker.image_volumes = [{"Destination": "/config"}]
    runtime = make_front(docker, harness, runtimes)

    runtime.start()

    record = front_record(docker)
    assert [(m["Type"], m["Destination"]) for m in record["Mounts"]] == [("tmpfs", "/config")]
    assert record["HostConfig"]["Tmpfs"]["/config"].startswith("ro,")
    assert "size=1m" in record["HostConfig"]["Tmpfs"]["/config"]


def test_the_two_networks_are_the_front_on_both_and_bazarr_on_the_front_one_only(docker, front):
    net_a, net_b = network_of(docker, NET_A), network_of(docker, NET_B)

    assert net_a["Internal"] is True and net_b["Internal"] is False
    for net in (net_a, net_b):
        assert net["Labels"][MANAGED_LABEL] == "true" and net["Labels"][OWNER_LABEL] == OWNER
        assert net["Labels"][PLUGIN_LABEL] == PLUGIN and net["Labels"][CREATOR_LABEL] == SELF_ID
    attached = front_record(docker)["NetworkSettings"]["Networks"]
    assert set(attached) == {NET_A, NET_B}
    assert attached[NET_A]["NetworkID"] == net_a["Id"] and attached[NET_B]["NetworkID"] == net_b["Id"]
    assert set(docker.containers[SELF_ID]["NetworkSettings"]["Networks"]) == {"bridge", NET_A}
    assert docker.calls("POST", "/networks/.*/connect")


def test_a_front_with_a_daemon_leaves_the_daemon_on_its_private_network_alone(docker, harness, runtimes):
    daemon = OciDaemonRuntime(flaresolverr_spec(PLUGIN, REPO, DIGEST), client=docker.client(), owner_id=OWNER,
                              host_check=host_ok, poll_interval=0.02, ready_interval=0.001)
    runtimes.append(daemon)
    daemon.start()
    runtime = make_front(docker, harness, runtimes)

    runtime.start()

    daemon_nets = set(docker.container_named(oci_module.container_name(OWNER, PLUGIN))["NetworkSettings"]["Networks"])
    assert daemon_nets == {NET_B}
    assert set(front_record(docker)["NetworkSettings"]["Networks"]) == {NET_A, NET_B}
    assert NET_B not in docker.containers[SELF_ID]["NetworkSettings"]["Networks"]
    # The daemon can start again beside the front: the front is its own, so the member audit accepts it.
    daemon.close(0)
    second = OciDaemonRuntime(flaresolverr_spec(PLUGIN, REPO, DIGEST), client=docker.client(), owner_id=OWNER,
                              host_check=host_ok, poll_interval=0.02, ready_interval=0.001)
    runtimes.append(second)
    second.start()
    assert second.alive


def test_no_port_is_published_and_nothing_is_mounted_from_the_host(docker, front):
    record = front_record(docker)

    assert not record["HostConfig"]["PortBindings"] and not record["HostConfig"].get("Binds")
    assert all(mount["Type"] == "tmpfs" for mount in record["Mounts"])
    assert "ExposedPorts" not in create_body(docker)


# ------------------------------------------------------------------ the secret and the tokens


def test_neither_the_admin_secret_nor_a_token_ever_reaches_docker(docker, front):
    lease = front.open_lease()
    second = front.open_lease()

    seen = json.dumps(docker.requests, default=str) + json.dumps(docker.containers, default=str) \
        + json.dumps(docker.networks, default=str)
    assert front._secret not in seen
    assert lease.token not in seen and second.token not in seen


def test_a_lease_gets_a_random_url_safe_token_and_the_front_takes_it(docker, harness, front):
    first, second = front.open_lease(), front.open_lease()

    assert first.token != second.token and len(first.token) == 43
    assert len({new_lease_token() for _ in range(50)}) == 50
    assert front.lease_count == 2
    assert harness.post(first) == 200 and harness.post(second) == 200
    assert harness.stub.requests == [("/v1", b'{"cmd": "request.get"}')] * 2
    assert len(harness.server.leases) == 2
    host, port = front.address()
    assert port == FRONT_PORT and host == harness.targets[-1][0] and harness.targets[-1][1] == FRONT_ADMIN_PORT
    assert host.startswith("198.51.101.")
    assert first.url() == "http://%s:%d/l/%s/v1" % (host, port, first.token)


def test_the_front_holds_digests_and_not_the_tokens(front, harness):
    lease = front.open_lease()

    assert lease.token.encode() not in b"".join(harness.server.leases._digests)
    assert lease.token not in repr(front.__dict__)
    assert set(front._book.values()) == {front_server.hashlib.sha256(lease.token.encode()).hexdigest()}


def test_closing_a_lease_revokes_it_at_once_and_leaves_the_others(front, harness):
    kept, ended = front.open_lease(), front.open_lease()
    assert harness.post(ended) == 200
    url = ended.url()

    ended.close()

    assert harness.post(url) == 401
    assert harness.post(kept) == 200
    assert front.lease_count == 1
    ended.close()
    assert front.lease_count == 1
    with pytest.raises(FrontError):
        ended.url()


def test_a_lease_works_as_a_context_manager(front, harness):
    with front.open_lease() as lease:
        url = lease.url()
        assert harness.post(url) == 200

    assert lease.closed and harness.post(url) == 401


def test_a_lease_never_shows_its_token(front):
    lease = front.open_lease()

    assert lease.token not in repr(lease) and lease.token not in str(lease)
    with pytest.raises(TypeError):
        pickle.dumps(lease)
    with pytest.raises(TypeError):
        copy.copy(lease)
    assert isinstance(lease, FrontLease)


def test_a_token_never_issued_is_refused_by_the_front(front, harness):
    front.open_lease()

    assert harness.post("http://x/l/" + "Z" * 43 + "/v1") == 401


def test_the_number_of_open_leases_is_bounded(front, monkeypatch):
    monkeypatch.setattr(front_server, "MAX_LEASES", 2)
    front.open_lease()
    front.open_lease()

    with pytest.raises(FrontError, match="too many"):
        front.open_lease()
    assert front.lease_count == 2


def test_a_lease_that_the_front_cannot_take_is_not_kept(front, harness):
    harness.down = True

    with pytest.raises(FrontError):
        front.open_lease()

    assert front.lease_count == 0


def test_a_revoke_that_cannot_be_delivered_stops_the_front(docker, front, harness):
    lease = front.open_lease()
    harness.down = True

    lease.close()

    assert front_record(docker) is None
    assert not front.alive and front.status()["state"] == "removed"
    assert "could not be told" in front.status()["error"]
    assert network_of(docker, NET_A) is None
    with pytest.raises(FrontError):
        front.open_lease()


def test_a_front_that_lost_its_set_gets_it_again(front, harness):
    lease = front.open_lease()
    assert harness.post(lease) == 200

    harness.server.leases.replace([])

    assert wait_until(lambda: len(harness.server.leases) == 1)
    assert harness.post(lease) == 200


# ------------------------------------------------------------------ restart and removal


def test_a_restart_gets_a_new_container_and_the_held_leases_are_pushed_again(docker, harness, front):
    lease = front.open_lease()
    before = front_record(docker)["Id"]
    old_url = lease.url()

    front.close(0)
    assert front_record(docker) is None
    assert network_of(docker, NET_A) is None and network_of(docker, NET_B) is not None
    assert NET_A not in docker.containers[SELF_ID]["NetworkSettings"]["Networks"]
    assert not front.alive
    front.start()

    assert front.alive and front_record(docker)["Id"] != before
    assert harness.post(lease) == 200
    assert lease.url().startswith("http://198.51.")  # network A was made again, with a new subnet
    assert old_url.split("/l/")[1] == lease.url().split("/l/")[1]
    assert set(docker.containers[SELF_ID]["NetworkSettings"]["Networks"]) == {"bridge", NET_A}


def test_a_failed_start_can_be_retried(docker, harness, runtimes):
    runtime = make_front(docker, harness, runtimes)
    docker.fail.append((r"POST /containers/.*/start", 500, "boom"))

    with pytest.raises(OciError):
        runtime.start()
    assert front_record(docker) is None and network_of(docker, NET_A) is None

    runtime.start()
    assert runtime.alive


def test_cleanup_at_boot_removes_the_front_and_both_networks_even_with_bazarr_attached(docker, front):
    report = cleanup_orphans(docker.client(), OWNER, self_id=SELF_ID)

    assert report.containers == 1 and report.networks == 2 and not report.errors
    assert front_record(docker) is None
    assert network_of(docker, NET_A) is None and network_of(docker, NET_B) is None
    assert set(docker.containers[SELF_ID]["NetworkSettings"]["Networks"]) == {"bridge"}


def test_uninstall_removes_the_front_and_its_network(docker, front):
    report = remove_plugin(docker.client(), OWNER, PLUGIN, self_id=SELF_ID)

    assert report.containers == 1 and report.networks == 2
    assert front_record(docker) is None and docker.containers[SELF_ID]["NetworkSettings"]["Networks"].keys() == {"bridge"}


def test_a_front_removed_from_outside_is_seen(docker, front):
    del docker.containers[front.container_id]

    assert wait_until(lambda: not front.alive)
    assert "removed from outside" in front.status()["error"]


def test_a_front_that_stops_is_seen(docker, front):
    docker.containers[front.container_id]["State"].update(Running=False, Status="exited")

    assert wait_until(lambda: not front.alive)


def test_a_front_the_admin_channel_cannot_reach_is_unhealthy_after_three_misses(front, harness):
    harness.down = True

    assert wait_until(lambda: not front.alive)
    assert "stopped answering" in front.status()["error"]


# ------------------------------------------------------------------ refusals at start


def test_without_the_local_image_the_front_is_not_started_and_never_pulled(docker, harness, runtimes):
    del docker.images[SELF_IMAGE]
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError, match="never pulled"):
        runtime.start()

    assert docker.calls("POST", "/images/create") == [] and docker.calls("POST", "/containers/create") == []


def test_an_unknown_image_id_is_refused(docker, harness, runtimes):
    docker.containers[SELF_ID]["Image"] = "latest"
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError, match="which image"):
        runtime.start()


def test_a_host_that_cannot_run_managed_containers_is_refused(docker, harness, runtimes):
    runtime = make_front(docker, harness, runtimes,
                         host_check=lambda client: check_daemon_host(client, in_container=False, container_id=None))

    with pytest.raises(OciUnavailable):
        runtime.start()

    assert docker.calls("POST", "/networks/create") == []


def test_a_stranger_on_the_front_network_stops_the_start(docker, harness, runtimes):
    client = docker.client()
    client.create_network(NET_A, internal=True, labels={MANAGED_LABEL: "true", OWNER_LABEL: OWNER,
                                                         PLUGIN_LABEL: PLUGIN, CREATOR_LABEL: SELF_ID})
    client._call("POST", "/containers/create", query={"name": "stranger"}, body={
        "Image": SELF_IMAGE, "HostConfig": {"NetworkMode": NET_A, "Memory": 1, "MemorySwap": 1},
        "NetworkingConfig": {"EndpointsConfig": {NET_A: {}}}})
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError, match="did not create"):
        runtime.start()

    assert front_record(docker) is None


def test_bazarr_on_the_daemons_network_stops_the_start(docker, harness, runtimes):
    client = docker.client()
    net = client.create_network(NET_B, labels={MANAGED_LABEL: "true", OWNER_LABEL: OWNER, PLUGIN_LABEL: PLUGIN,
                                                CREATOR_LABEL: SELF_ID})
    client.connect_network(net, SELF_ID)
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError, match="Bazarr\\+ itself"):
        runtime.start()


@pytest.mark.parametrize("name,labels,internal", [
    (NET_A, {}, True),
    (NET_A, {MANAGED_LABEL: "true", OWNER_LABEL: "f" * 16, PLUGIN_LABEL: PLUGIN}, True),
    (NET_A, {MANAGED_LABEL: "true", OWNER_LABEL: OWNER, PLUGIN_LABEL: PLUGIN}, False),
    (NET_B, {}, False),
])
def test_a_network_of_our_name_that_is_not_ours_or_not_internal_is_refused(docker, harness, runtimes, name, labels,
                                                                           internal):
    docker.client().create_network(name, labels=labels, internal=internal)
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError):
        runtime.start()

    assert front_record(docker) is None


def test_a_container_of_the_front_name_that_is_not_ours_is_left_alone(docker, harness, runtimes):
    docker.client()._call("POST", "/containers/create", query={"name": FRONT_NAME}, body={
        "Image": SELF_IMAGE, "HostConfig": {"NetworkMode": "bridge", "Memory": 1, "MemorySwap": 1}})
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError, match="did not create"):
        runtime.start()

    assert front_record(docker) is not None and front_record(docker)["Config"]["Labels"] == {}


def test_a_stale_front_of_ours_is_replaced(docker, harness, runtimes):
    first = make_front(docker, harness, runtimes)
    first.start()
    old = front_record(docker)["Id"]
    first._stop.set()
    second = make_front(docker, harness, runtimes)

    second.start()

    assert front_record(docker)["Id"] != old and second.alive


@pytest.mark.parametrize("change,message", [
    (lambda view: view["Config"].update(User="0:0"), "another user"),
    (lambda view: view["Config"].update(Entrypoint=["/entrypoint.sh"]), "another program"),
    (lambda view: view["Config"].update(Cmd=["--debug"]), "command"),
    (lambda view: view["Config"].update(Env=[]), "environment"),
    (lambda view: view["HostConfig"].update(Tmpfs={}), "mounts differ"),
    (lambda view: view["Mounts"].append({"Type": "volume", "Destination": "/config", "Name": "v"}), "besides"),
    (lambda view: view["HostConfig"].update(PortBindings={"80/tcp": [{"HostPort": "80"}]}), "publishes"),
    (lambda view: view["HostConfig"].update(CapDrop=[]), "capabilities"),
    (lambda view: view["HostConfig"].update(ReadonlyRootfs=False), "root filesystem"),
    (lambda view: view.update(Image="sha256:" + "0" * 64), "verified image"),
    (lambda view: view["Config"]["Labels"].update({ROLE_LABEL: "daemon"}), "labels"),
])
def test_a_front_that_docker_built_differently_is_removed_before_it_starts(docker, harness, runtimes, change, message):
    def mutate(view):
        if view["Name"] == "/" + FRONT_NAME:
            change(view)

    docker.mutate = mutate
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError, match=message):
        runtime.start()

    assert front_record(docker) is None and network_of(docker, NET_A) is None
    assert not runtime.alive
    assert docker.calls("POST", "/containers/.*/start") == []


def test_a_front_that_comes_up_on_the_wrong_networks_is_removed(docker, harness, runtimes):
    docker.attach_network_id = "f" * 64
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError, match="wrong id"):
        runtime.start()

    assert front_record(docker) is None


def test_a_front_with_an_address_outside_its_network_is_refused(docker, harness, runtimes):
    def mutate(view):
        if view["Name"] == "/" + FRONT_NAME and view["State"]["Running"]:
            for entry in view["NetworkSettings"]["Networks"].values():
                entry["IPAddress"] = "203.0.113.9"

    docker.mutate = mutate
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError, match="not on its network"):
        runtime.start()

    assert front_record(docker) is None


def test_a_front_that_never_gets_healthy_is_removed(docker, harness, runtimes):
    docker.unhealthy = True
    runtime = make_front(docker, harness, runtimes)

    with pytest.raises(OciError, match="unhealthy"):
        runtime.start()

    assert front_record(docker) is None and network_of(docker, NET_A) is None


def test_a_start_can_be_cancelled(docker, harness, runtimes):
    runtime = make_front(docker, harness, runtimes)
    runtime.cancel_start()

    with pytest.raises(OciError, match="cancelled"):
        runtime.start()

    assert docker.calls("POST", "/containers/create") == []


def test_a_started_front_cannot_be_started_twice(front):
    with pytest.raises(RuntimeError):
        front.start()


def test_the_managed_tier_is_still_off_unless_switched_on():
    assert not oci_module.managed_containers_enabled({})
    assert not oci_module.offer_status(environ={}, in_container=True).available


# ------------------------------------------------------------------ a revoke reaches the front whatever the runtime thinks


def test_a_lease_closed_while_docker_reports_the_front_unhealthy_is_still_revoked(docker, harness, front):
    lease = front.open_lease()
    url = lease.url()
    assert harness.post(url) == 200
    docker.unhealthy = True
    assert wait_until(lambda: not front.alive)

    lease.close()

    assert harness.post(url) == 401


def test_a_lease_closed_while_the_docker_api_is_failing_is_still_revoked(docker, harness, front):
    lease = front.open_lease()
    url = lease.url()
    docker.deny.append(r"GET /containers/.*/json")
    assert wait_until(lambda: not front.alive)

    lease.close()

    assert harness.post(url) == 401


def test_a_lease_closed_after_a_close_that_could_not_remove_the_front_is_still_revoked(docker, harness, front):
    lease = front.open_lease()
    url = lease.url()
    docker.deny.append(r"DELETE /containers/")
    front.close(0)
    assert front_record(docker) is not None and not front.alive

    lease.close()

    assert harness.post(url) == 401


def test_a_lease_closed_during_the_start_is_revoked_as_soon_as_the_front_is_up(docker, harness, front):
    lease = front.open_lease()
    url = lease.url()
    front.close(0)
    closer = threading.Thread(target=lease.close, daemon=True)
    real_connect = harness.connect
    started = []

    def connect(host, port, timeout):
        # The first admin call of the restart is the push of the held set: end the lease right then.
        if not started:
            started.append(True)
            closer.start()
            time.sleep(0.1)
        return real_connect(host, port, timeout)

    front._connect = connect

    front.start()
    closer.join(5)

    assert not closer.is_alive() and front.alive
    assert harness.post(url) == 401
    assert front.lease_count == 0


def test_a_revoke_that_fails_ends_with_the_front_verifiably_gone_even_if_removal_needs_retries(docker, front, harness):
    lease = front.open_lease()
    harness.down = True
    docker.fail.append((r"DELETE /containers/", 500, "busy"))
    docker.fail.append((r"DELETE /containers/", 500, "busy"))

    lease.close()

    assert front_record(docker) is None and network_of(docker, NET_A) is None
    assert front.status()["state"] == "removed"


def test_a_revoke_that_fails_and_cannot_remove_the_front_says_so(docker, front, harness):
    lease = front.open_lease()
    harness.down = True
    docker.deny.append(r"DELETE /containers/")

    lease.close()

    assert front_record(docker) is not None
    assert "could not be removed" in front.status()["error"]


# ------------------------------------------------------------------ secrecy and the small things


@pytest.fixture
def clean_scrubber():
    from secret_store import scrub

    scrub.clear_secret_values()
    yield scrub
    scrub.clear_secret_values()


def test_the_admin_secret_and_every_token_are_registered_with_the_credential_scrubber(docker, harness, runtimes,
                                                                                     clean_scrubber):
    runtime = make_front(docker, harness, runtimes)
    runtime.start()
    lease = runtime.open_lease()

    line = "POST %s failed; admin was Bearer %s" % (lease.url(), runtime._secret)
    masked = clean_scrubber.scrub_text(line)

    assert lease.token not in masked and runtime._secret not in masked


def test_a_global_http_debug_level_does_not_print_the_admin_secret(docker, harness, runtimes, monkeypatch, capsys):
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)
    runtime = make_front(docker, harness, runtimes)
    runtime.start()
    lease = runtime.open_lease()
    lease.close()

    printed = capsys.readouterr()
    assert runtime._secret not in printed.out + printed.err
    assert "Authorization" not in printed.out + printed.err


def test_a_front_set_that_differs_with_the_same_count_is_pushed_again(front, harness):
    lease = front.open_lease()
    assert harness.post(lease) == 200

    # Same number of entries, other contents: a count alone would call this in sync.
    harness.server.leases.replace([front_server.token_digest("S" * 43)])
    assert len(harness.server.leases) == front.lease_count

    assert wait_until(lambda: harness.server.leases.allows(lease.token))
    assert not harness.server.leases.allows("S" * 43)


def test_a_front_docker_calls_unhealthy_still_gets_its_set_kept_right(docker, front, harness):
    lease = front.open_lease()
    docker.unhealthy = True
    assert wait_until(lambda: not front.alive)

    harness.server.leases.replace([])

    assert wait_until(lambda: harness.server.leases.allows(lease.token))
    assert "unhealthy" in front.status()["error"]


def test_the_health_check_opens_both_listeners(docker, front):
    test = front_record(docker)["Config"]["Healthcheck"]["Test"]

    assert test[:3] == ["CMD", "python3", "-I"] and str(FRONT_PORT) in test[-1] and str(FRONT_ADMIN_PORT) in test[-1]
    assert FRONT_PORT != FRONT_ADMIN_PORT


def test_the_front_script_path_is_the_one_the_images_layout_gives():
    from pathlib import Path

    from provider_hub.runtime.front_runtime import FRONT_SCRIPT

    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(encoding="utf-8")
    production = dockerfile.split("AS production", 1)[1]
    assert "WORKDIR /app/bazarr\n" in production and "COPY bazarr ./bazarr\n" in production
    assert FRONT_SCRIPT == "/app/bazarr/bazarr/provider_hub/runtime/front.py"
