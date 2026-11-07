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
    SandboxApplyError,
    SandboxProbe,
    launcher_failure,
    probe_sandbox,
    reset_probe_cache,
    sandbox_status,
    spawn_worker,
)

__all__ = [
    "Admission",
    "LauncherProcess",
    "Limits",
    "PolicyError",
    "RuntimeSpec",
    "SandboxApplyError",
    "SandboxPolicy",
    "SandboxProbe",
    "SandboxRefused",
    "admission",
    "clean_env",
    "launcher_failure",
    "probe_sandbox",
    "reset_probe_cache",
    "sandbox_status",
    "spawn_worker",
]
