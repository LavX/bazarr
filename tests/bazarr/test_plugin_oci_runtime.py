# coding=utf-8
"""The OCI daemon runtime: spec and container config, host checks, lifecycle, removal and the pool seam.

Every test runs against ``FakeDocker`` on a temp Unix socket (or a stub client). No test reaches a real Docker
daemon and no container is ever started: the fake only flips flags in a dict.
"""
import os
import stat
import time

import pytest

from provider_hub.runtime import oci as oci_module
from provider_hub.runtime.docker_api import DockerApiError, DockerClient, Endpoint, check_create_body
from provider_hub.runtime.errors import RequestError, SessionClosed
from provider_hub.runtime.frames import Codes
from provider_hub.runtime.oci import (
    DAEMON_ALIAS,
    MANAGED_LABEL,
    OWNER_LABEL,
    PLUGIN_LABEL,
    ROLE_LABEL,
    ContainerSpec,
    HealthSpec,
    HostCheck,
    OciDaemonRuntime,
    OciError,
    OciUnavailable,
    check_daemon_host,
    cleanup_orphans,
    container_config,
    container_name,
    flaresolverr_spec,
    load_owner_id,
    network_name,
    oci_worker_definition,
    offer_status,
    offered_runtimes,
    own_container_id,
    remove_plugin,
)
from provider_hub.runtime.pool import PoolLimits, PoolUnavailable, WorkerPool, WorkerState
from provider_hub.runtime.testing.fake_docker import FakeDocker, digest_of, forbid_real_sockets

OWNER = "0123456789abcdef"
OTHER = "fedcba9876543210"
REPO = "ghcr.io/flaresolverr/flaresolverr"
DIGEST = digest_of("flaresolverr-index")
SELF_ID = "ab" * 32


@pytest.fixture(autouse=True)
def never_the_real_daemon(monkeypatch):
    """A test that forgot to inject a client must fail, not open the machine's own Docker socket."""
    forbid_real_sockets(monkeypatch)
    oci_module.reset_offer_cache()
    yield
    oci_module.reset_offer_cache()


@pytest.fixture
def docker():
    with FakeDocker() as fake:
        fake.add_registry_image(REPO, DIGEST)
        fake.add_self(SELF_ID, networks=("bridge",))
        yield fake


def spec_for(**overrides):
    return flaresolverr_spec("flaresolverr", REPO, DIGEST, **overrides)


def host_ok(client):
    return check_daemon_host(client, in_container=True, container_id=SELF_ID)


def make_runtime(docker, spec=None, **kwargs):
    kwargs.setdefault("host_check", host_ok)
    kwargs.setdefault("poll_interval", 0.02)
    kwargs.setdefault("ready_interval", 0.001)
    kwargs.setdefault("sleep", lambda seconds: time.sleep(min(seconds, 0.005)))
    return OciDaemonRuntime(spec or spec_for(), client=docker.client(), owner_id=OWNER, **kwargs)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def raw_create(docker, name, body):
    """Create a container the way something other than this client would, bypassing its body checks."""
    docker.add_local_image(REPO, DIGEST)
    client = docker.client()
    result = client._call("POST", "/containers/create", query={"name": name}, body=body)
    return result["Id"]


def managed(docker):
    return [r for r in docker.containers.values() if not r.get("_self")]


# ------------------------------------------------------------------ the spec


def test_a_spec_needs_an_untagged_image_and_a_sha256_index_digest():
    spec = ContainerSpec(plugin_id="flaresolverr", image=REPO, index_digest=DIGEST, port=8191)

    assert spec.reference == f"{REPO}@{DIGEST}"


@pytest.mark.parametrize("overrides", [
    {"image": REPO + ":latest"},
    {"image": f"{REPO}@{DIGEST}"},
    {"image": "UPPER/case"},
    {"index_digest": "latest"},
    {"index_digest": "sha512:" + "a" * 128},
    {"index_digest": "sha256:" + "A" * 64},
    {"plugin_id": "Bad Id"},
    {"port": 0},
    {"port": 70000},
    {"port": True},
    {"memory_bytes": 1},
    {"memory_bytes": 1 << 40},
    {"pids_limit": 1},
    {"env": {"1BAD": "x"}},
    {"env": {"OK": 5}},
    {"tmpfs": (("tmp", 1 << 20),)},
    {"tmpfs": (("/", 1 << 20),)},
    {"tmpfs": (("/a/../b", 1 << 20),)},
    {"tmpfs": (("/tmp", 1),)},
    {"user": "root; rm"},
    {"start_timeout": 0},
])
def test_a_bad_spec_is_refused_when_it_is_built(overrides):
    values = {"plugin_id": "flaresolverr", "image": REPO, "index_digest": DIGEST, "port": 8191}
    values.update(overrides)

    with pytest.raises(ValueError):
        ContainerSpec(**values)


def test_a_spec_is_built_from_a_parsed_manifest_and_nothing_else_names_the_image():
    from plugin_platform_helpers import oci_runtime, service_manifest
    from provider_hub.plugin_manifest import parse_plugin_manifest

    plugin = parse_plugin_manifest(service_manifest(**oci_runtime(REPO, DIGEST)))

    spec = ContainerSpec.from_manifest(plugin, port=8191)

    assert (spec.image, spec.index_digest, spec.plugin_id) == (REPO, DIGEST, plugin.plugin_id)
    with pytest.raises(ValueError, match="does not declare an oci runtime"):
        ContainerSpec.from_manifest(parse_plugin_manifest(service_manifest()), port=8191)


def test_the_flaresolverr_preset_keeps_the_log_level_at_warning_whatever_is_asked():
    assert spec_for().env["LOG_LEVEL"] == "warning"
    assert spec_for(env={"LOG_LEVEL": "debug", "X": "1"}).env == {"LOG_LEVEL": "warning", "X": "1"}
    assert spec_for().port == 8191 and spec_for().memory_bytes == 1536 * 1024 * 1024


def test_a_health_check_needs_a_command_and_sane_timings():
    with pytest.raises(ValueError):
        HealthSpec(test=())
    with pytest.raises(ValueError):
        HealthSpec(test=("true",), interval=0)
    assert HealthSpec(test=("true",)).docker()["Test"] == ["CMD", "true"]


# ------------------------------------------------------------------ the container config


def test_the_container_config_states_every_limit_and_none_of_the_escapes():
    body = container_config(spec_for(), owner_id=OWNER)
    host = body["HostConfig"]

    check_create_body(body)
    assert body["Image"] == f"{REPO}@{DIGEST}"
    assert host["Privileged"] is False and host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["ReadonlyRootfs"] is True
    assert host["Memory"] == host["MemorySwap"] == 1536 * 1024 * 1024
    assert host["PidsLimit"] == 512
    assert host["PortBindings"] == {} and host["PublishAllPorts"] is False and "ExposedPorts" not in body
    assert host["RestartPolicy"] == {"Name": "no"} and host["AutoRemove"] is False
    assert host["IpcMode"] == "private"
    assert host["LogConfig"]["Type"] == "json-file" and host["LogConfig"]["Config"]["max-size"]
    for absent in ("Binds", "Devices", "CapAdd", "Mounts", "VolumesFrom", "PidMode", "UsernsMode", "ExtraHosts"):
        assert not host.get(absent)
    assert host["NetworkMode"] == network_name(OWNER, "flaresolverr")
    assert body["NetworkingConfig"]["EndpointsConfig"] == {host["NetworkMode"]: {"Aliases": [DAEMON_ALIAS]}}
    assert "LOG_LEVEL=warning" in body["Env"]
    assert body["Healthcheck"]["Test"][0] == "CMD"


def test_the_container_carries_the_ownership_labels():
    labels = container_config(spec_for(), owner_id=OWNER)["Labels"]

    assert labels == {MANAGED_LABEL: "true", OWNER_LABEL: OWNER, PLUGIN_LABEL: "flaresolverr", ROLE_LABEL: "daemon"}


def test_names_are_per_install_and_per_plugin():
    assert container_name(OWNER, "flaresolverr") != container_name(OTHER, "flaresolverr")
    assert network_name(OWNER, "a_b") != network_name(OWNER, "a_c")


# ------------------------------------------------------------------ the owner id


def test_the_owner_id_is_made_once_and_kept_private(tmp_path):
    path = tmp_path / "cfg" / "oci-owner"

    first = load_owner_id(path)

    assert oci_module.OWNER_ID_RE.match(first)
    assert load_owner_id(path) == first
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_damaged_owner_file_is_replaced(tmp_path):
    path = tmp_path / "oci-owner"
    path.write_text("not an id")

    assert oci_module.OWNER_ID_RE.match(load_owner_id(path))


# ------------------------------------------------------------------ the host


def test_the_own_container_id_comes_from_the_containers_own_mounts_first():
    cid = "c" * 64
    mountinfo = f"1 2 0:3 /docker/containers/{cid}/hostname /etc/hostname rw - ext4 /dev/sda rw\n"

    assert own_container_id(mountinfo, hostname="abcdef123456") == cid
    assert own_container_id("nothing here", hostname="abcdef123456") == "abcdef123456"
    assert own_container_id("nothing here", hostname="my-host") is None


def test_the_host_check_names_the_first_condition_that_fails(docker, tmp_path):
    client = docker.client()

    assert check_daemon_host(client, in_container=False, container_id=SELF_ID).reason.startswith(
        "managed containers need Bazarr+ itself to run in a container")
    assert "no Docker socket" in check_daemon_host(None, in_container=True, container_id=SELF_ID).reason
    dead = DockerClient(Endpoint("unix", path=str(tmp_path / "missing.sock")))
    assert "cannot be used" in check_daemon_host(dead, in_container=True, container_id=SELF_ID).reason
    assert "cannot tell which container" in check_daemon_host(client, in_container=True, container_id=None).reason
    assert "not the same host" in check_daemon_host(client, in_container=True, container_id="d" * 64).reason
    assert docker.requests and all(r["method"] == "GET" for r in docker.requests)


def test_a_bazarr_on_the_host_network_or_with_no_network_gets_no_managed_containers(docker):
    for mode, text in (("host", "host network"), ("none", "no network of its own"),
                       ("container:abc", "no network of its own")):
        docker.containers[SELF_ID]["HostConfig"]["NetworkMode"] = mode

        check = check_daemon_host(docker.client(), in_container=True, container_id=SELF_ID)

        assert check.available is False and text in check.reason


def test_a_same_host_bazarr_is_available_and_reports_its_networks(docker):
    check = check_daemon_host(docker.client(), in_container=True, container_id=SELF_ID)

    assert check.available and check.networks == ("bridge",) and check.api_version == "1.44"
    assert check.container_id == SELF_ID


def test_an_answer_for_another_container_is_not_the_same_host(docker):
    other = "ee" * 32
    docker.containers[other] = dict(docker.containers[SELF_ID], Id=other)
    docker.containers[SELF_ID]["Id"] = other

    check = check_daemon_host(docker.client(), in_container=True, container_id=SELF_ID)

    assert not check.available and "another container" in check.reason


def test_nothing_is_contacted_outside_a_container_or_without_an_endpoint(monkeypatch):
    made = []
    monkeypatch.setattr(oci_module, "DockerClient", lambda endpoint: made.append(endpoint))

    outside = offer_status(environ={"DOCKER_HOST": "tcp://proxy:2375"}, in_container=False)
    oci_module.reset_offer_cache()
    monkeypatch.setattr("provider_hub.runtime.docker_api.os.path.exists", lambda path: False)
    no_endpoint = offer_status(environ={}, in_container=True)

    assert made == []
    assert not outside.available and not no_endpoint.available
    assert "no Docker socket" in no_endpoint.reason


def test_a_malformed_docker_host_is_a_reason_not_a_crash():
    status = offer_status(environ={"DOCKER_HOST": "https://x:2376"}, in_container=True)

    assert not status.available and "unix socket or a plain tcp" in status.reason


def test_the_offer_is_cached_and_a_failure_is_retried_sooner(docker, monkeypatch):
    docker.containers[SELF_ID]["HostConfig"]["NetworkMode"] = "host"
    monkeypatch.setattr(oci_module, "own_container_id", lambda *a, **k: SELF_ID)
    now = [0.0]
    kwargs = dict(environ={"DOCKER_HOST": "unix://" + docker.socket_path}, in_container=True,
                  clock=lambda: now[0])

    assert not offer_status(**kwargs).available
    first = len(docker.requests)
    assert not offer_status(**kwargs).available
    assert len(docker.requests) == first
    docker.containers[SELF_ID]["HostConfig"]["NetworkMode"] = "bridge"
    now[0] = 31.0
    assert offer_status(**kwargs).available
    now[0] = 31.0 + 59.0
    assert offered_runtimes(**kwargs) == frozenset({("oci", "daemon")})
    assert len(docker.calls("GET", "/_ping")) == 2


def test_current_host_adds_the_runtime_only_inside_a_container(monkeypatch):
    from provider_hub import compatibility

    monkeypatch.setattr(compatibility, "running_in_container", lambda: False)
    monkeypatch.setattr(oci_module, "offered_runtimes", lambda **kw: (_ for _ in ()).throw(AssertionError("probed")))
    assert compatibility.current_host().runtimes == compatibility.AVAILABLE_RUNTIMES

    monkeypatch.setattr(compatibility, "running_in_container", lambda: True)
    monkeypatch.setattr(oci_module, "offered_runtimes", lambda **kw: frozenset({("oci", "daemon")}))
    assert ("oci", "daemon") in compatibility.current_host().runtimes

    def broken(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(oci_module, "offered_runtimes", broken)
    assert compatibility.current_host().runtimes == compatibility.AVAILABLE_RUNTIMES


# ------------------------------------------------------------------ start


def test_a_start_pulls_by_digest_makes_a_private_network_and_runs_a_locked_down_container(docker):
    runtime = make_runtime(docker)

    started = runtime.start()

    assert runtime.alive and started["network"] == network_name(OWNER, "flaresolverr")
    [record] = managed(docker)
    assert record["State"]["Running"] is True
    host = record["HostConfig"]
    assert host["Privileged"] is False and not host["PortBindings"] and host["NetworkMode"] == started["network"]
    # The network: one plugin-owned bridge, labelled, with nothing but this container on it.
    [network] = docker.networks.values()
    assert network["Name"] == started["network"] and network["Labels"][OWNER_LABEL] == OWNER
    assert list(record["NetworkSettings"]["Networks"]) == [network["Name"]]
    # The pull was by digest, before the container was created, and the container ran the digest reference.
    order = [(r["method"], r["path"]) for r in docker.requests]
    assert order.index(("POST", "/images/create")) < order.index(("POST", "/containers/create"))
    assert docker.calls("POST", "/images/create")[0]["query"] == {"fromImage": f"{REPO}@{DIGEST}"}
    assert docker.calls("POST", "/containers/create")[0]["body"]["Image"] == f"{REPO}@{DIGEST}"
    # Nothing outside the allowed routes, and nothing that reaches the host: no exec, volumes, kill or log calls.
    assert not docker.calls(None, r"/exec|/volumes|/kill|/logs|/build|/info|/version")
    runtime.close(0)


def test_every_request_is_made_at_the_negotiated_api_version(docker):
    runtime = make_runtime(docker)
    runtime.start()
    runtime.close(0)

    versioned = [r for r in docker.requests if r["path"] != "/_ping"]
    assert versioned and {r["version"] for r in versioned} == {"1.44"}


def test_a_local_image_with_the_pinned_digest_is_not_pulled_again(docker):
    docker.add_local_image(REPO, DIGEST)
    runtime = make_runtime(docker)

    runtime.start()

    assert not docker.calls("POST", "/images/create")
    runtime.close(0)


def test_a_local_image_that_does_not_name_the_digest_is_pulled(docker):
    docker.images[f"{REPO}@{DIGEST}"] = {"Id": "sha256:" + "0" * 64, "RepoDigests": [f"{REPO}@sha256:" + "1" * 64]}
    runtime = make_runtime(docker)

    runtime.start()

    assert len(docker.calls("POST", "/images/create")) == 1
    runtime.close(0)


def test_a_pulled_image_that_does_not_carry_the_digest_stops_the_start_before_a_container_exists(docker):
    docker.registry[f"{REPO}@{DIGEST}"]["RepoDigests"] = [f"{REPO}@sha256:" + "2" * 64]
    runtime = make_runtime(docker)

    with pytest.raises(OciError, match="does not carry the pinned digest"):
        runtime.start()

    assert not docker.calls("POST", "/containers/create")
    assert not runtime.alive


def test_a_pull_that_fails_is_reported_and_no_container_is_created(docker):
    docker.pull_error = "toomanyrequests: rate limit"
    runtime = make_runtime(docker)

    with pytest.raises(OciError, match="rate limit"):
        runtime.start()

    assert not docker.containers.get("x") and not managed(docker)


def test_docker_hub_style_names_match_their_familiar_repo_digests(docker):
    spec = flaresolverr_spec("flaresolverr", "docker.io/flaresolverr/flaresolverr", DIGEST)
    docker.images[spec.reference] = {"Id": "sha256:" + "3" * 64,
                                     "RepoDigests": ["flaresolverr/flaresolverr@" + DIGEST]}

    runtime = make_runtime(docker, spec)
    runtime.start()

    assert not docker.calls("POST", "/images/create")
    runtime.close(0)


def test_a_host_that_cannot_run_managed_containers_creates_nothing(docker):
    runtime = make_runtime(docker, host_check=lambda client: HostCheck(False, "Bazarr+ runs on the host network"))

    with pytest.raises(OciUnavailable, match="host network"):
        runtime.start()

    assert not docker.calls("POST", "/networks/create") and not docker.calls("POST", "/containers/create")
    assert not runtime.alive


@pytest.mark.parametrize("knob, text", [
    ({"exit_on_start": 1}, r"exited while it started \(exit code 1\)"),
    ({"exit_on_start": 137, "oom": True}, "ran out of memory"),
    ({"unhealthy": True}, "unhealthy while it started"),
])
def test_a_container_that_dies_or_is_unhealthy_at_start_is_removed_and_the_start_fails(docker, knob, text):
    for key, value in knob.items():
        setattr(docker, key, value)
    runtime = make_runtime(docker)

    with pytest.raises(OciError, match=text):
        runtime.start()

    assert not managed(docker) and not runtime.alive


def test_a_container_that_never_gets_healthy_times_out_and_is_removed(docker):
    docker.health_after = 10_000
    now = [0.0]

    def sleep(seconds):
        now[0] += 30.0

    runtime = make_runtime(docker, clock=lambda: now[0], sleep=sleep)

    with pytest.raises(OciError, match="did not become healthy in 120 seconds"):
        runtime.start()

    assert not managed(docker)


@pytest.mark.parametrize("mutation, text", [
    (lambda view: view["HostConfig"].update(Privileged=True), "privileged"),
    (lambda view: view["HostConfig"].update(Binds=["/:/host"]), "Binds"),
    (lambda view: view["HostConfig"].update(PortBindings={"8191/tcp": [{"HostPort": "8191"}]}), "publishes ports"),
    (lambda view: view["HostConfig"].update(CapAdd=["NET_ADMIN"]), "CapAdd"),
    (lambda view: view["HostConfig"].update(CapDrop=[]), "keeps capabilities"),
    (lambda view: view["HostConfig"].update(SecurityOpt=[]), "gain privileges"),
    (lambda view: view["HostConfig"].update(PidMode="host"), "shares a namespace"),
    (lambda view: view["HostConfig"].update(Memory=0), "memory cap"),
    (lambda view: view["HostConfig"].update(PidsLimit=0), "process cap"),
    (lambda view: view["HostConfig"].update(ReadonlyRootfs=False), "root filesystem"),
    (lambda view: view["HostConfig"].update(RestartPolicy={"Name": "always"}), "restarts by itself"),
    (lambda view: view["HostConfig"].update(NetworkMode="bridge"), "wrong network"),
    (lambda view: view["NetworkSettings"]["Networks"].update(bridge={}), "another network"),
    (lambda view: view["Mounts"].append({"Type": "bind", "Source": "/", "Destination": "/host"}), "from the host"),
    (lambda view: view.update(Image="sha256:" + "9" * 64), "not the verified image"),
    (lambda view: view["Config"]["Labels"].update({OWNER_LABEL: OTHER}), "labels"),
])
def test_a_container_that_differs_from_what_was_asked_is_removed_before_it_starts(docker, mutation, text):
    docker.mutate = mutation
    runtime = make_runtime(docker)

    with pytest.raises(OciError, match=text):
        runtime.start()

    assert not docker.calls("POST", "/start")
    assert not managed(docker) and not runtime.alive


def test_a_volume_the_image_declares_is_accepted_but_a_host_mount_is_not(docker):
    docker.image_volumes = [{"Destination": "/data"}]
    runtime = make_runtime(docker)

    runtime.start()

    assert runtime.alive
    runtime.close(0)


def test_a_host_that_cannot_enforce_the_memory_or_process_cap_is_refused(docker):
    docker.warnings = ["Your kernel does not support memory limit capabilities or the cgroup is not mounted. "
                       "Limitation discarded."]
    with pytest.raises(OciError, match="cannot enforce"):
        make_runtime(docker).start()
    assert not managed(docker)

    docker.warnings = ["Your kernel does not support pids limit capabilities or the cgroup is not mounted. "
                       "PIDs limit discarded."]
    with pytest.raises(OciError, match="cannot enforce"):
        make_runtime(docker).start()
    assert not managed(docker)


def test_a_missing_swap_limit_is_only_a_warning(docker):
    docker.warnings = ["Your kernel does not support swap limit capabilities or the cgroup is not mounted. "
                       "Memory limited without swap."]
    runtime = make_runtime(docker)

    runtime.start()

    assert runtime.alive
    runtime.close(0)


def test_a_leftover_container_of_ours_is_replaced(docker):
    first = make_runtime(docker)
    first.start()
    first._stop.set()  # the first runtime's process "crashed": its container stays behind
    old = first.container_id

    second = make_runtime(docker)
    second.start()

    assert second.container_id != old and old not in docker.containers
    assert len(managed(docker)) == 1
    second.close(0)


def test_a_container_under_our_name_that_is_not_ours_is_never_touched(docker):
    stranger = container_name(OWNER, "flaresolverr")
    raw_create(docker, stranger, {"Image": f"{REPO}@{DIGEST}", "HostConfig": {
        "NetworkMode": "bridge", "Memory": 1 << 28, "MemorySwap": 1 << 28, "PidsLimit": 64}})
    runtime = make_runtime(docker)

    with pytest.raises(OciError, match="did not create"):
        runtime.start()

    assert docker.container_named(stranger) is not None
    assert not docker.calls("DELETE", "/containers/")


def test_a_network_under_our_name_that_is_not_ours_is_refused(docker):
    docker.networks["n"] = {"Id": "n", "Name": network_name(OWNER, "flaresolverr"), "Labels": {}, "Containers": {}}
    runtime = make_runtime(docker)

    with pytest.raises(OciError, match="network named .* exists that Bazarr\\+ did not create"):
        runtime.start()

    assert not docker.calls("POST", "/containers/create")


def test_a_network_of_another_install_with_our_names_is_refused(docker):
    docker.networks["n"] = {"Id": "n", "Name": network_name(OWNER, "flaresolverr"),
                            "Labels": {MANAGED_LABEL: "true", OWNER_LABEL: OTHER}, "Containers": {}}

    with pytest.raises(OciError, match="did not create"):
        make_runtime(docker).start()


def test_a_stranger_on_the_private_network_stops_the_start(docker):
    runtime = make_runtime(docker)
    docker.networks["n"] = {"Id": "n", "Name": runtime.network,
                            "Labels": {MANAGED_LABEL: "true", OWNER_LABEL: OWNER}, "Containers": {}}
    raw_create(docker, "someone-else", {"Image": f"{REPO}@{DIGEST}", "HostConfig": {
        "NetworkMode": runtime.network, "Memory": 1 << 28, "MemorySwap": 1 << 28, "PidsLimit": 64}})

    with pytest.raises(OciError, match="attached to the plugin's private network"):
        runtime.start()

    assert [r["query"]["name"] for r in docker.calls("POST", "/containers/create")] == ["someone-else"]


def test_bazarr_on_the_private_network_stops_the_start(docker):
    runtime = make_runtime(docker)
    docker.networks["n"] = {"Id": "n", "Name": runtime.network,
                            "Labels": {MANAGED_LABEL: "true", OWNER_LABEL: OWNER}, "Containers": {}}
    docker.containers[SELF_ID]["NetworkSettings"]["Networks"][runtime.network] = {}

    with pytest.raises(OciError, match="Bazarr\\+ itself is attached"):
        runtime.start()

    assert not docker.calls("POST", "/containers/create")


def test_a_proxy_that_denies_a_route_fails_the_start_with_its_reason(docker):
    docker.deny.append(r"POST /containers/create")
    runtime = make_runtime(docker)

    with pytest.raises(OciError, match="Docker daemon refused"):
        runtime.start()

    assert not managed(docker)


def test_a_runtime_starts_once_and_takes_no_per_request_secrets(docker):
    runtime = make_runtime(docker)
    runtime.start()

    with pytest.raises(RuntimeError):
        runtime.start()
    with pytest.raises(ValueError, match="no per-request secrets"):
        runtime.submit("status", secrets={"token": "value"})
    runtime.close(0)


# ------------------------------------------------------------------ operations


def test_status_and_endpoint_are_answered_locally(docker):
    runtime = make_runtime(docker)
    runtime.start()

    status = runtime.request("status").result
    endpoint = runtime.request("endpoint").result

    assert status["state"] == "running" and status["health"] == "healthy" and status["image"].endswith(DIGEST)
    assert endpoint["network"] == runtime.network and endpoint["alias"] == DAEMON_ALIAS
    assert endpoint["port"] == 8191 and endpoint["ip"].startswith("172.30.")
    assert runtime.in_flight == 0 and runtime.process is None and runtime.recycle_needed is False
    runtime.close(0)


def test_an_unknown_operation_is_a_request_error_not_a_crash(docker):
    runtime = make_runtime(docker)
    runtime.start()

    with pytest.raises(RequestError) as error:
        runtime.request("exec", {"cmd": "id"})

    assert error.value.code == Codes.UNKNOWN_OP
    assert not docker.calls(None, "/exec")
    runtime.close(0)


def test_a_closed_runtime_takes_no_requests(docker):
    runtime = make_runtime(docker)
    runtime.start()
    runtime.close(0)

    with pytest.raises(SessionClosed):
        runtime.submit("status")


# ------------------------------------------------------------------ watching


def test_a_container_that_exits_on_its_own_reads_as_dead_with_its_exit_code(docker):
    runtime = make_runtime(docker)
    runtime.start()
    record = managed(docker)[0]

    record["State"].update(Running=False, Status="exited", ExitCode=137, OOMKilled=True)

    assert wait_until(lambda: not runtime.alive)
    assert runtime.exit_code == 137 and "ran out of memory" in runtime.last_error
    runtime.close(0)


def test_a_container_that_turns_unhealthy_reads_as_dead(docker):
    runtime = make_runtime(docker)
    runtime.start()

    docker.unhealthy = True

    assert wait_until(lambda: not runtime.alive)
    assert "unhealthy" in runtime.last_error
    runtime.close(0)


def test_a_container_removed_from_outside_reads_as_dead(docker):
    runtime = make_runtime(docker)
    runtime.start()

    docker.containers.pop(runtime.container_id)

    assert wait_until(lambda: not runtime.alive)
    assert "removed from outside" in runtime.last_error
    runtime.close(0)


def test_one_missed_answer_is_not_a_death_but_three_in_a_row_are(docker):
    runtime = make_runtime(docker)
    runtime.start()
    docker.fail.extend([(r"GET /containers/.*/json", 500, "hiccup")])
    time.sleep(0.2)
    assert runtime.alive

    docker.fail.extend([(r"GET /containers/.*/json", 500, "down")] * 3)

    assert wait_until(lambda: not runtime.alive)
    assert "stopped answering" in runtime.last_error
    runtime.close(0)


# ------------------------------------------------------------------ closing


def test_close_stops_and_removes_the_container_but_keeps_the_network(docker):
    runtime = make_runtime(docker)
    runtime.start()

    runtime.close(grace=3)

    assert not runtime.alive and not managed(docker)
    assert docker.calls("POST", "/stop")[-1]["query"] == {"t": "3"}
    assert len(docker.networks) == 1
    runtime.close(0)  # idempotent
    assert not runtime.alive


def test_close_before_a_start_has_nothing_to_remove(docker):
    runtime = make_runtime(docker)

    runtime.close(0)

    assert not runtime.alive and docker.requests == []
    with pytest.raises(SessionClosed):
        runtime.start()


def test_a_removal_that_cannot_be_confirmed_leaves_the_runtime_alive_so_the_pool_retries(docker):
    runtime = make_runtime(docker)
    runtime.start()
    docker.fail.append((r"DELETE /containers/", 500, "daemon busy"))

    runtime.close(0)

    assert runtime.alive and managed(docker)
    runtime.close(0)
    assert not runtime.alive and not managed(docker)


def test_a_daemon_that_went_away_leaves_the_runtime_alive_until_a_close_can_confirm(docker):
    runtime = make_runtime(docker)
    runtime.start()
    docker.fail.extend([(r"POST /containers/.*/stop", 500, "down"), (r"DELETE /containers/", 500, "down")])

    runtime.close(0)

    assert runtime.alive
    runtime.close(0)
    assert not runtime.alive


# ------------------------------------------------------------------ removal by label


def seed(docker, name, owner=OWNER, plugin="flaresolverr", running=True, network="bridge", labelled=True):
    if network not in [n["Name"] for n in docker.networks.values()] + ["bridge"]:
        docker.networks[network] = {"Id": network, "Name": network, "Labels": {}, "Containers": {}}
    body = {"Image": f"{REPO}@{DIGEST}", "HostConfig": {
        "NetworkMode": network, "Memory": 1 << 28, "MemorySwap": 1 << 28, "PidsLimit": 64}}
    if labelled:
        body["Labels"] = {MANAGED_LABEL: "true", OWNER_LABEL: owner, PLUGIN_LABEL: plugin}
    cid = raw_create(docker, name, body)
    if running:
        docker.client().start_container(cid)
    return cid


def seed_network(docker, name, owner=OWNER, plugin="flaresolverr"):
    docker.networks[name] = {"Id": name, "Name": name, "Containers": {},
                             "Labels": {MANAGED_LABEL: "true", OWNER_LABEL: owner, PLUGIN_LABEL: plugin}}


def test_boot_cleanup_removes_only_this_installs_managed_containers_and_networks(docker):
    ours = seed(docker, "ours", network="net-ours")
    ours_stopped = seed(docker, "ours-stopped", running=False, network="net-ours")
    theirs = seed(docker, "theirs", owner=OTHER, network="net-theirs")
    plain = seed(docker, "plain", labelled=False)
    seed_network(docker, "net-ours")
    seed_network(docker, "net-theirs", owner=OTHER)
    docker.networks["net-plain"] = {"Id": "net-plain", "Name": "net-plain", "Labels": {}, "Containers": {}}

    report = cleanup_orphans(docker.client(), OWNER, self_id=SELF_ID)

    assert report.containers == 2 and report.networks == 1 and report.errors == ()
    assert ours not in docker.containers and ours_stopped not in docker.containers
    assert theirs in docker.containers and plain in docker.containers and SELF_ID in docker.containers
    assert set(docker.networks) == {"net-theirs", "net-plain"}


def test_boot_cleanup_never_removes_bazarr_itself_even_if_it_carries_our_labels(docker):
    docker.containers[SELF_ID]["Config"]["Labels"] = {MANAGED_LABEL: "true", OWNER_LABEL: OWNER}

    report = cleanup_orphans(docker.client(), OWNER, self_id=SELF_ID)

    assert report.containers == 0 and SELF_ID in docker.containers
    assert not docker.calls("DELETE", "/containers/")


def test_the_labels_are_checked_again_on_what_the_daemon_returned():
    class Greedy:
        """A daemon whose label filter returns everything."""

        def __init__(self):
            self.removed = []

        def list_containers(self, **kwargs):
            return [{"Id": "a" * 12, "Labels": {MANAGED_LABEL: "true", OWNER_LABEL: OTHER}},
                    {"Id": "b" * 12, "Labels": {}},
                    {"Id": "c" * 12, "Labels": {MANAGED_LABEL: "true", OWNER_LABEL: OWNER, PLUGIN_LABEL: "x"}}]

        def list_networks(self, **kwargs):
            return [{"Id": "n1", "Labels": {}}]

        def stop_container(self, ident, timeout):
            pass

        def remove_container(self, ident, force=True):
            self.removed.append(ident)

        def remove_network(self, ident):
            self.removed.append(ident)

    greedy = Greedy()

    report = cleanup_orphans(greedy, OWNER)

    assert greedy.removed == ["c" * 12] and report.containers == 1 and report.networks == 0


def test_uninstall_removes_one_plugins_containers_and_then_its_network(docker):
    mine = seed(docker, "mine", plugin="flaresolverr", network="net-fs")
    other = seed(docker, "other", plugin="otherplugin", network="net-op")
    seed_network(docker, "net-fs")
    seed_network(docker, "net-op", plugin="otherplugin")

    report = remove_plugin(docker.client(), OWNER, "flaresolverr", self_id=SELF_ID)

    assert report.containers == 1 and report.networks == 1 and report.errors == ()
    assert mine not in docker.containers and other in docker.containers
    assert set(docker.networks) == {"net-op"}
    deletes = [r["path"] for r in docker.calls("DELETE")]
    assert deletes.index(f"/containers/{mine}") < deletes.index("/networks/net-fs")


def test_a_network_someone_else_is_attached_to_is_left_and_reported(docker):
    seed_network(docker, "net-fs")
    seed(docker, "intruder", labelled=False, network="net-fs")

    report = remove_plugin(docker.client(), OWNER, "flaresolverr", self_id=SELF_ID)

    assert "net-fs" in docker.networks and report.networks == 0
    assert report.errors and "removing a network failed" in report.errors[0]


def test_removal_with_the_daemon_down_reports_instead_of_raising(tmp_path):
    dead = DockerClient(Endpoint("unix", path=str(tmp_path / "missing.sock")))

    report = cleanup_orphans(dead, OWNER)

    assert report.containers == 0 and report.errors and "cannot be used" in report.errors[0]


def test_removal_refuses_a_bad_owner_or_plugin_id(docker):
    with pytest.raises(ValueError):
        cleanup_orphans(docker.client(), "not-an-owner")
    with pytest.raises(ValueError):
        remove_plugin(docker.client(), OWNER, "Bad Id")


# ------------------------------------------------------------------ through the pool


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_pool(docker, clock):
    pools = []

    def make(**limits):
        values = dict(tick=0.05, heartbeat_interval=0.1, backoff_base=1.0, backoff_cap=8.0, breaker_threshold=3,
                      breaker_window=300.0, breaker_open=30.0, idle_timeout=60.0, acquire_timeout=1.0,
                      close_grace=1.0, budget_interval=0.0)
        values.update(limits)
        pool = WorkerPool(PoolLimits(**values), clock=clock, autostart=False)
        pools.append(pool)
        return pool

    yield make
    for pool in pools:
        pool.close()


def pooled(docker, key="fs", **kwargs):
    return oci_worker_definition(key, spec_for(), owner_id=OWNER, client_factory=docker.client,
                                 poll_interval=0.02, host_check=host_ok, **kwargs)


def test_the_pool_starts_a_container_for_a_request_and_removes_it_when_stopped(docker, make_pool):
    pool = make_pool()
    pool.register(pooled(docker))

    assert pool.run("fs", "status").result["state"] == "running"
    assert pool.state("fs") is WorkerState.READY and len(managed(docker)) == 1

    pool.stop("fs")

    assert pool.state("fs") is WorkerState.STOPPED and not managed(docker)
    pool.run("fs", "status")
    assert len(managed(docker)) == 1


def test_the_pools_memory_cap_for_a_container_is_the_specs_and_nothing_local_is_sampled(docker, make_pool):
    definition = pooled(docker)

    assert definition.memory_limit() == 1536 * 1024 * 1024
    assert definition.writes_data is False
    pool = make_pool()
    pool.register(definition)
    pool.run("fs", "status")
    assert pool.status()["workers"]["fs"]["memory_bytes"] == 0


def test_an_idle_container_is_stopped_and_removed_after_the_idle_timeout(docker, make_pool, clock):
    pool = make_pool(idle_timeout=30.0)
    pool.register(pooled(docker, idle_timeout=None))
    pool.run("fs", "status")
    clock.advance(20)
    pool.sweep()
    assert pool.state("fs") is WorkerState.READY
    clock.advance(20)

    pool.sweep()

    assert wait_until(lambda: pool.state("fs") is WorkerState.STOPPED)
    assert wait_until(lambda: not managed(docker))
    assert pool.status()["workers"]["fs"]["failures"] == 0


def test_a_container_that_dies_is_cleaned_up_and_the_next_request_starts_a_new_one(docker, make_pool, clock):
    pool = make_pool()
    pool.register(pooled(docker))
    pool.run("fs", "status")
    old = managed(docker)[0]["Id"]
    managed(docker)[0]["State"].update(Running=False, Status="exited", ExitCode=1)
    assert wait_until(lambda: not pool._entries["fs"].slot.session.alive)

    assert wait_until(lambda: (pool.sweep() or pool.state("fs") in (WorkerState.STOPPED, WorkerState.BACKOFF)))
    assert wait_until(lambda: old not in docker.containers)
    clock.advance(10)

    assert pool.run("fs", "status").result["state"] == "running"
    assert managed(docker)[0]["Id"] != old
    assert pool.status()["workers"]["fs"]["crashes"] == 1


def test_a_container_that_keeps_failing_to_start_opens_the_breaker(docker, make_pool, clock):
    docker.exit_on_start = 1
    pool = make_pool()
    pool.register(pooled(docker))
    for _ in range(3):
        with pytest.raises(PoolUnavailable):
            pool.run("fs", "status")
        clock.advance(9)

    assert pool.state("fs") is WorkerState.BROKEN
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("fs", "status")
    assert refused.value.code == "circuit_open"
    assert not managed(docker)
    starts = len(docker.calls("POST", "/containers/create"))
    clock.advance(31)
    docker.exit_on_start = None

    assert pool.run("fs", "status").result["state"] == "running"
    assert len(docker.calls("POST", "/containers/create")) == starts + 1
    assert pool.status()["workers"]["fs"]["failures"] == 0


def test_a_host_without_docker_access_is_a_visible_start_failure_not_a_hang(docker, make_pool):
    pool = make_pool()
    pool.register(oci_worker_definition(
        "fs", spec_for(), owner_id=OWNER, client_factory=docker.client,
        host_check=lambda client: HostCheck(False, "no Docker socket or DOCKER_HOST is configured")))

    with pytest.raises(PoolUnavailable, match="no Docker socket"):
        pool.run("fs", "status")

    assert not docker.calls("POST", "/containers/create")


def test_closing_the_pool_removes_the_container(docker, make_pool):
    pool = make_pool()
    pool.register(pooled(docker))
    pool.run("fs", "status")

    pool.close()

    assert not managed(docker)


def test_unregistering_the_plugin_removes_the_container(docker, make_pool):
    pool = make_pool()
    pool.register(pooled(docker))
    pool.run("fs", "status")

    pool.unregister("fs")

    assert wait_until(lambda: not managed(docker))


def test_a_real_client_is_never_opened_by_these_tests(docker, tmp_path):
    not_ours = tmp_path / "real.sock"
    not_ours.write_text("")
    with pytest.raises(AssertionError, match="real Docker connection"):
        DockerClient(Endpoint("unix", path=str(not_ours))).ping()
    with pytest.raises(DockerApiError):
        docker.client().inspect_container("missing")
