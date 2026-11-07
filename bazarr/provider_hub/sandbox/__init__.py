# coding=utf-8
"""Worker sandbox: Landlock filesystem and network rules, limits, a launcher.

Entry points for the worker spawner: ``probe_sandbox`` at boot,
``SandboxPolicy.for_interpreter`` and ``RuntimeSpec`` to describe a worker,
``spawn_worker`` to start it, ``admission`` for the host-state policy.
"""
from .policy import (
    Admission,
    Limits,
    PolicyError,
    RuntimeSpec,
    SandboxPolicy,
    SandboxRefused,
    admission,
    clean_env,
)
from .spawn import (
    LauncherProcess,
    LauncherFailure,
    SandboxApplyError,
    SandboxProbe,
    WorkerProcess,
    probe_sandbox,
    reset_probe_cache,
    sandbox_limitations,
    sandbox_status,
    sandbox_status_nowait,
    spawn_worker,
    start_probe_in_background,
)

__all__ = [
    "Admission",
    "LauncherFailure",
    "LauncherProcess",
    "Limits",
    "PolicyError",
    "RuntimeSpec",
    "SandboxApplyError",
    "SandboxPolicy",
    "SandboxProbe",
    "SandboxRefused",
    "WorkerProcess",
    "admission",
    "clean_env",
    "probe_sandbox",
    "reset_probe_cache",
    "sandbox_limitations",
    "sandbox_status",
    "sandbox_status_nowait",
    "spawn_worker",
    "start_probe_in_background",
]
