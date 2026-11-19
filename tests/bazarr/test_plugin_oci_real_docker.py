# coding=utf-8
"""Checks against a real Docker daemon, off unless asked for, so the fake daemon can be compared with the real one.

Nothing here runs by default and nothing here ran on the machine that wrote it. To run it on a box:

    BAZARR_REAL_DOCKER_TEST=1 pytest tests/bazarr/test_plugin_oci_real_docker.py

with a Docker socket (or DOCKER_HOST) to talk to. The lifecycle test also needs BAZARR_REAL_DOCKER_IMAGE: a
digest-pinned reference to a tiny public image that has ``sleep``, written as ``name@sha256:<index digest>`` (for
example ``docker.io/library/busybox@sha256:...`` with the digest of the multi-arch index the registry serves; read it
from the registry's own page, never from a tag).

The first test is read-only. The lifecycle test pulls that image if it is missing, then creates a labelled
network and one container on it, starts it and removes both again, even when an assert fails. Every name carries
a random id, so it can only ever touch what it made.
"""
import os
import secrets

import pytest

from provider_hub.runtime.docker_api import DockerClient, DockerNotFound, endpoint_from_env
from provider_hub.runtime.oci import ContainerSpec, container_config

ENABLED = os.environ.get("BAZARR_REAL_DOCKER_TEST") == "1"
ENDPOINT = endpoint_from_env(os.environ) if ENABLED else None
IMAGE_REF = os.environ.get("BAZARR_REAL_DOCKER_IMAGE", "")

needs_daemon = pytest.mark.skipif(
    ENDPOINT is None, reason="set BAZARR_REAL_DOCKER_TEST=1 with a Docker socket or DOCKER_HOST to run this")
needs_image = pytest.mark.skipif(
    "@sha256:" not in IMAGE_REF, reason="set BAZARR_REAL_DOCKER_IMAGE to name@sha256:<index digest> to run this")


def lifecycle_spec(image, digest):
    """The spec the lifecycle test runs: small enough for a 64 MiB container."""
    return ContainerSpec(plugin_id="realtest", image=image, index_digest=digest, port=8191,
                         memory_bytes=64 * 1024 * 1024, shm_bytes=8 * 1024 * 1024,
                         tmpfs=(("/tmp", 8 * 1024 * 1024),))


def test_the_lifecycle_test_builds_a_valid_spec_and_a_body_the_client_accepts():
    """Plain unit test, no daemon: the spec the opt-in test uses must stay valid."""
    from provider_hub.runtime.docker_api import check_create_body

    spec = lifecycle_spec("docker.io/library/busybox", "sha256:" + "ab" * 32)
    body = container_config(spec, owner_id="0123456789abcdef", network="bazarr-plus-realtest-x", network_id="n" * 64)
    body["Cmd"] = ["sleep", "30"]

    check_create_body(body)
    assert body["HostConfig"]["Memory"] == 64 * 1024 * 1024 and body["HostConfig"]["ShmSize"] == 8 * 1024 * 1024


@needs_daemon
def test_a_real_daemon_answers_the_ping_and_a_label_query_that_matches_nothing():
    client = DockerClient(ENDPOINT)

    version = client.ping()

    assert version >= "1.41"
    assert client.list_containers(labels={"bazarr-plus.real-docker-test": "matches-nothing"}) == []
    assert client.list_networks(labels={"bazarr-plus.real-docker-test": "matches-nothing"}) == []


@needs_daemon
@needs_image
def test_a_real_daemon_stores_the_network_id_as_sent_and_fills_it_in_at_start():
    """The behaviour the fake daemon copies: before the start the endpoint holds what was sent, after it the real id."""
    client = DockerClient(ENDPOINT)
    client.ping()
    image, digest = IMAGE_REF.split("@", 1)
    suffix = secrets.token_hex(4)
    owner = secrets.token_hex(8)
    labels = {"bazarr-plus.real-docker-test": suffix}
    spec = lifecycle_spec(image, digest)
    network_name = f"bazarr-plus-realtest-{suffix}"
    container_name = f"bazarr-plus-realtest-{suffix}-c"
    network_id = container_id = None
    try:
        client.pull_image(image, digest)
        network_id = client.create_network(network_name, labels=labels)
        body = container_config(spec, owner_id=owner, network=network_name, network_id=network_id)
        body["Cmd"] = ["sleep", "30"]
        body["Labels"].update(labels)
        container_id, _warnings = client.create_container(container_name, body)

        before = client.inspect_container(container_id)
        entry = before["NetworkSettings"]["Networks"][network_name]
        # Stored as sent at create: the id we sent (or empty), never another network's, and no address yet.
        assert entry["NetworkID"] in ("", network_id)
        assert not entry.get("IPAddress")
        assert before["HostConfig"]["Privileged"] is False and not before["HostConfig"]["PortBindings"]

        client.start_container(container_id)
        after = client.inspect_container(container_id)
        attached = after["NetworkSettings"]["Networks"]
        assert set(attached) == {network_name}
        assert attached[network_name]["NetworkID"] == network_id
        assert attached[network_name]["IPAddress"]
        assert after["State"]["Running"] is True
    finally:
        if container_id is not None:
            try:
                client.stop_container(container_id, 0)
            except DockerNotFound:
                pass
            client.remove_container(container_id, force=True)
        if network_id is not None:
            client.remove_network(network_id)
