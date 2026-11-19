# coding=utf-8
"""Plugin worker runtime: the v2 worker wire protocol and the host session.

``frames`` is the wire contract, stdlib only, shared byte for byte with the
plugin SDK. ``session`` is the host half. ``worker_channel`` is the worker
half. ``pool`` supervises resident workers: states, limits, restarts. ``oci`` and ``docker_api`` run
a managed container on a Docker daemon the user opted into, as a runtime the pool supervises.
Provider workers keep speaking the v1 line protocol through
``provider_hub.worker`` and ``provider_hub.worker_runner``; nothing here
changes them.
"""
