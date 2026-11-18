# coding=utf-8
"""One check against a real Docker daemon, off unless asked for.

It runs only with BAZARR_REAL_DOCKER_TEST=1 and a socket (or DOCKER_HOST) to talk to, so the suite never opens a
machine's own Docker by accident. It is read-only: it pings the daemon and lists containers under a label nothing
carries. No container, network or image is created, pulled or removed.
"""
import os

import pytest

from provider_hub.runtime.docker_api import DockerClient, endpoint_from_env

ENABLED = os.environ.get("BAZARR_REAL_DOCKER_TEST") == "1"
ENDPOINT = endpoint_from_env(os.environ) if ENABLED else None

pytestmark = pytest.mark.skipif(
    ENDPOINT is None, reason="set BAZARR_REAL_DOCKER_TEST=1 with a Docker socket or DOCKER_HOST to run this")


def test_a_real_daemon_answers_the_ping_and_a_label_query_that_matches_nothing():
    client = DockerClient(ENDPOINT)

    version = client.ping()

    assert version >= "1.41"
    assert client.list_containers(labels={"bazarr-plus.real-docker-test": "matches-nothing"}) == []
    assert client.list_networks(labels={"bazarr-plus.real-docker-test": "matches-nothing"}) == []
