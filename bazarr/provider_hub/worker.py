# coding=utf-8
from __future__ import annotations

import collections
import errno
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import weakref
import time
import uuid

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import WORKER_ABI_VERSION
from . import runtime_status
from .provider_roots import SourceMediaFile
from .sandbox import activity
from secret_store.scrub import scrub_text

logger = logging.getLogger(__name__)

# Cap a single worker->host NDJSON response line. ``readline()`` buffers a whole
# line before ``json.loads`` runs, and the protocol-level archive cap only fires
# after the line is already in memory, so a runaway or malicious worker could OOM
# the host with one giant line. Sized comfortably above the 32 MB archive cap
# (base64 is ~43 MB, plus the JSON envelope) so legitimate responses still pass.
_MAX_RESPONSE_LINE_BYTES = 48 * 1024 * 1024
# Read granularity for the bounded readline loop, so the cap is enforced before a
# whole oversized line accumulates.
_READ_CHUNK_CHARS = 1024 * 1024
# What a worker may send the host as a call in the middle of a request. A call carries a captcha site
# address and key, so every part of it is bounded far below the reply cap. A call outside the bounds,
# and a request that makes more calls than the limit, is a protocol violation and ends the worker.
_MAX_HOST_CALL_LINE_BYTES = 128 * 1024
_MAX_HOST_CALL_ID_CHARS = 64
_MAX_HOST_CALL_CAPABILITY_CHARS = 128
_MAX_HOST_CALL_PAYLOAD_BYTES = 64 * 1024
_MAX_HOST_CALLS_PER_REQUEST = 16
# What the host sends back. Bounded so that answers cannot fill a pipe the worker does not read, and
# writing one is bounded in time as well: a worker that does not read within this long is ended.
_MAX_HOST_REPLY_PAYLOAD_BYTES = 32 * 1024
_HOST_REPLY_WRITE_SECONDS = 10.0
# What the stdout reader may hold ahead of the request thread, by bytes and by pieces. Past either it
# stops reading, so the pipe fills and the worker's own writes wait: a worker cannot grow the host's
# memory by writing while nothing is consuming (during a host call, or between requests). A reply is
# consumed as it arrives, so this never slows one down.
_STDOUT_QUEUE_MAX_BYTES = 8 * 1024 * 1024
_STDOUT_QUEUE_MAX_CHUNKS = 64
# Queued by the reader thread when a response line exceeds the cap, so the
# consumer kills the worker at the transport layer instead of assembling it.
_OVERSIZE_RESPONSE = object()
_CANCELLED_EOF = object()


class _BoundedChunks:
    """The reader's queue of worker output: ``put`` waits while it holds too much, ``get`` is Queue's.

    Markers (the end of output, an oversized line) are never held back. A waiting ``put`` gives up when
    ``alive`` says the process is gone, so a killed worker never leaves a reader thread stuck.
    """

    def __init__(self, max_bytes: int = _STDOUT_QUEUE_MAX_BYTES, max_chunks: int = _STDOUT_QUEUE_MAX_CHUNKS):
        self._items: collections.deque = collections.deque()
        self._bytes = 0
        self._max_bytes = max_bytes
        self._max_chunks = max_chunks
        self._cond = threading.Condition()

    @property
    def queued_bytes(self) -> int:
        with self._cond:
            return self._bytes

    def put(self, item, alive=lambda: True) -> None:
        size = len(item) if isinstance(item, str) else 0
        with self._cond:
            while size and self._items and (
                    len(self._items) >= self._max_chunks or self._bytes + size > self._max_bytes):
                if not alive():
                    return
                self._cond.wait(0.2)
            self._items.append(item)
            self._bytes += size
            self._cond.notify_all()

    def get(self, timeout: float | None = None):
        with self._cond:
            if not self._items:
                self._cond.wait(timeout)
            if not self._items:
                raise queue.Empty
            item = self._items.popleft()
            if isinstance(item, str):
                self._bytes -= len(item)
            self._cond.notify_all()
            return item


def _json_default(obj):
    """Coerce values not natively JSON-serializable into safe representations.

    Subliminal's Video objects carry numeric fields (notably ``fps``) as
    ``decimal.Decimal``, which the stdlib JSON encoder rejects. We convert
    Decimals to float (lossy precision is acceptable for transport payloads,
    the provider only uses these for matching, not for arithmetic). Other
    surprise types fall back to ``str(obj)`` so the worker call surfaces a
    debuggable payload instead of a hard crash.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


class WorkerError(RuntimeError):
    """Raised when a provider worker fails or returns an error.

    ``remote_traceback`` is the plugin's own traceback from the error reply,
    so a log can name the plugin's cause instead of the host transport's;
    ``remote_errno`` is the errno of an OSError the operating system raised
    inside the plugin, which distinguishes a real file fault from the
    PermissionError the plugins raise themselves for a refused login."""

    def __init__(self, message, *, remote_class_name=None, code=None, retryable=False,
                 remote_traceback=None, remote_errno=None):
        super().__init__(message)
        self.remote_class_name = remote_class_name
        self.code = code
        self.retryable = retryable
        self.remote_traceback = remote_traceback
        self.remote_errno = remote_errno


class WorkerCancelled(WorkerError):
    """The host cancelled this worker and interrupted the request transport."""


class RequestNotAdmitted(Exception):
    """A gated request whose admission check said no once it got its turn.

    Deliberately not a WorkerError: nothing that classifies worker failures
    may take a request that was never sent for one.
    """


class SourceMediaRefused(Exception):
    """This item cannot be given a confined local-media read."""


class WorkerBusy(Exception):
    """A gated request that could not get its turn within its own timeout, or
    whose client was stopped while it waited. Not a WorkerError either."""


def _raise_worker_error(payload):
    if not isinstance(payload, dict):
        raise WorkerError("worker request failed")

    remote_name = payload.get("class_name")
    remote_name = remote_name if isinstance(remote_name, str) else None
    code = payload.get("code")
    code = code if isinstance(code, str) else None
    retryable = payload.get("retryable", False)
    retryable = retryable if isinstance(retryable, bool) else False
    message = payload.get("message")
    message = message if isinstance(message, str) and message else code or "worker request failed"
    # The worker cannot scrub: its runner deliberately imports no Bazarr
    # module so it can run under a provider venv. The host does it, and both
    # the message and the plugin's traceback are log-bound and API-bound.
    message = scrub_text(message)
    # The plugin's own traceback and, for an OSError, its errno. Both optional:
    # a reply without them, or a transport failure, is logged as before, with
    # the class and message alone.
    remote_traceback = payload.get("traceback")
    remote_traceback = remote_traceback if isinstance(remote_traceback, str) and remote_traceback else None
    if remote_traceback is not None:
        remote_traceback = scrub_text(remote_traceback)
    remote_errno = payload.get("errno")
    remote_errno = (remote_errno if isinstance(remote_errno, int)
                    and not isinstance(remote_errno, bool) else None)
    error = WorkerError(message, remote_class_name=remote_name, code=code, retryable=retryable,
                       remote_traceback=remote_traceback, remote_errno=remote_errno)
    if code != "provider":
        raise error

    # Keep host exception imports out of worker startup. Only these fixed
    # semantic names can cross the boundary as host provider exceptions.
    from subliminal.exceptions import (
        AuthenticationError,
        ConfigurationError,
        DownloadLimitExceeded,
        ServiceUnavailable,
    )
    from subliminal_patch.exceptions import APIThrottled, TooManyRequests

    exception_type = {
        "DownloadLimitExceeded": DownloadLimitExceeded,
        "TooManyRequests": TooManyRequests,
        "RateLimited": TooManyRequests,
        "ServiceUnavailable": ServiceUnavailable,
        "APIThrottled": APIThrottled,
        "AuthenticationError": AuthenticationError,
        "AuthenticationRequired": AuthenticationError,
        "ConfigurationError": ConfigurationError,
    }.get(remote_name)
    if exception_type is None:
        raise error
    # Rebuild the provider's own Retry-After too, where the exception takes
    # one. The class and message alone lose it at the boundary, and it is what
    # decides how long the backoff keeps the provider out.
    retry_after = payload.get("retry_after")
    retry_after = (retry_after if isinstance(retry_after, (int, float))
                   and not isinstance(retry_after, bool)
                   and 0 < retry_after < float("inf") else None)
    if retry_after is not None:
        try:
            raise exception_type(message, retry_after=min(86400.0, float(retry_after))) from error
        except TypeError:
            pass
    raise exception_type(message) from error


@dataclass
class WorkerResult:
    ok: bool
    payload: dict[str, Any]
    events: list[dict[str, Any]]
    status_generation: int | None = None
    status_configuration_generation: int | None = None
    worker_started: bool = False


# Every started worker registers here so idle ones can be reclaimed. A weak set,
# so a client the pool has dropped does not stay alive just by being listed.
#
# The fleet needs reclaiming because nothing else does it: a worker is spawned
# lazily the first time a pool searches with that provider, and the pool's own
# recycle only fires the next time that same pool is searched. On a quiet install
# the processes simply accumulate, at 17.6 to 44.6 MiB each.
_live_clients: "weakref.WeakSet[ProviderWorkerClient]" = weakref.WeakSet()
_live_clients_lock = threading.Lock()

# Clients whose process survived a kill. The weak set alone would let garbage
# collection drop such a client once its pool does, and with it the only handle
# to the still-running subprocess; held strongly until a later sweep wins.
_unreaped_survivors: "set[ProviderWorkerClient]" = set()

# Resolved once: signal.SIGKILL does not exist on Windows, and referencing it
# at a call site raises AttributeError before _signal_group's own guards can
# report False, breaking the process.kill() fallback.
_SIGKILL = getattr(signal, "SIGKILL", None)


def live_workers(provider_id: str | None = None, release: str | None = None) -> "list[ProviderWorkerClient]":
    """The started workers of one plugin, or of one release of it, so a release that is being replaced or
    stopped can be found by its key. A client without a release key matches only a lookup that names none."""
    with _live_clients_lock:
        found = list(_live_clients)
    return [client for client in found
            if (provider_id is None or client.provider_id == provider_id)
            and (release is None or client.release == release)]


def reap_idle_workers(idle_seconds: float) -> int:
    """Stop workers that have served no request for ``idle_seconds``.

    The decision and the shutdown happen together inside each client, under the
    same lock a request holds. Deciding here and acting afterwards is a
    check-then-act race: a search can pass ``start()`` and be queued on that
    lock in the window between the two, and would then write into a process
    this sweep had already ended.
    """
    with _live_clients_lock:
        candidates = list(_live_clients)

    reaped = 0
    for client in candidates:
        try:
            if client.stop_if_idle(idle_seconds):
                reaped += 1
        except Exception:
            # One plugin refusing to shut down must not strand the rest.
            logger.warning("Provider Hub worker did not stop cleanly", exc_info=True)

    if reaped:
        logger.debug("Provider Hub reclaimed %d idle worker(s)", reaped)
    return reaped


# Guards every client's list of process groups: a respawn appends from the request
# thread while a release check prunes from another.
_GROUPS_LOCK = threading.RLock()
_unverifiable_lock = threading.Lock()
_unverifiable_logged: set[str] = set()


def _client_gone(client, budget: float) -> bool:
    """Whether a client's worker has exited and nothing it started is left, waiting at most ``budget`` seconds."""
    process = getattr(client, "process", None)
    if process is None or not hasattr(client, "_has_exited"):
        return True
    deadline = time.monotonic() + budget
    while not client._has_exited(process):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    if not client.group_gone(max(0.0, deadline - time.monotonic())):
        return False
    # A worker started through the launcher has descendants that left its process group, which only the
    # launcher's own cleanup reaches. Its word that the cleanup finished is what lets the hold go.
    state = getattr(client, "launcher_cleanup_state", None)
    if state is not None and state() != "confirmed":
        return False
    activity.forget(process)
    return True


# How the wait for a leftover process paces itself. It starts at the cadence a quick exit is
# noticed at and doubles after every round that finds the survivor still running, up to a
# ceiling, so a process that lives for hours is looked at every few seconds, not fifty times a
# second. The hold is never given up: only the looking slows down.
LEFTOVER_POLL_START = 0.05
LEFTOVER_POLL_CEILING = 5.0


def _wait_until_gone(client, release, sleep=time.sleep) -> None:
    """Keep the hold until the client's worker and everything it started are gone, then call ``release``.

    Each round is one check with no wait of its own, followed by a pause that grows from
    ``LEFTOVER_POLL_START`` to ``LEFTOVER_POLL_CEILING``. ``sleep`` is a parameter for tests. A
    launcher that ended without confirming its cleanup ends the wait with the hold kept.
    """
    delay = LEFTOVER_POLL_START
    while not _client_gone(client, 0.0):
        state = getattr(client, "launcher_cleanup_state", None)
        if state is not None and state() == "unconfirmed":
            # A launcher that is gone without having said its cleanup finished (it was killed, or it gave
            # up): nothing can show that a detached descendant is not still running from the environment.
            # The hold stays for the rest of this process, as where process groups cannot be checked.
            logger.error(
                "Provider Hub: a worker's launcher ended without confirming its cleanup; "
                "its environment stays in place until the next restart"
            )
            return
        sleep(delay)
        delay = min(delay * 2, LEFTOVER_POLL_CEILING)
    release()


def release_when_gone(client, release, label: str, budget: float = 3.0) -> bool:
    """Call ``release`` once the client's worker and its process group are confirmed gone.

    For the environment hold of a temporary client (a probe, a connection test):
    the hold must outlive a stop that did not end everything. Waits briefly in
    the caller; if something is still running it logs, keeps the hold, and a
    daemon thread releases it when the survivors are gone. True when released at
    once.
    """
    if _client_gone(client, budget):
        release()
        return True
    if getattr(os, "killpg", None) is None and getattr(client, "_groups", None):
        # Where there are no process groups, nothing can show that the worker's
        # descendants are gone. The hold is kept for the rest of this process:
        # a cleanup on such a host never removes an environment a worker ran
        # from this session.
        with _unverifiable_lock:
            first = label not in _unverifiable_logged
            _unverifiable_logged.add(label)
        if first:
            logger.info(
                "Provider Hub: the processes of a worker for %s cannot be checked on this platform; "
                "its environment is kept until the next restart", label,
            )
        return False
    logger.error("Provider Hub: a temporary worker for %s left processes running; its environment stays in place", label)

    threading.Thread(
        target=_wait_until_gone, args=(client, release), name="hub-release-when-gone", daemon=True
    ).start()
    return False


# The runner every provider worker starts. A client is a provider worker, and so gets the provider
# sandbox profile, only when it starts this file from an interpreter of the hub's own.
_RUNNER_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "worker_runner.py"))

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")

# The launcher's own words for a ruleset or limit that could not be applied before the worker ran
# (``launcher._child``). Its other ``sandbox`` reports (a failed fork, survivors of the cleanup) come
# from a launcher that is past that point and never start an unsandboxed retry.
_APPLY_FAILURE_PREFIX = "could not apply sandbox: "
_ERRNO_IN_MESSAGE = re.compile(r"\[Errno (\d+)\]")
# What a kernel without Landlock, or with a lower ABI than the probe saw, answers with.
_KERNEL_CANNOT_APPLY = frozenset({errno.ENOSYS, errno.EOPNOTSUPP})


@dataclass(frozen=True)
class ProviderLayout:
    """Where one provider worker lives, enough to build its sandbox profile."""

    key: str
    interpreter: Path
    runner: str
    bundle: str
    hub_dir: Path
    config_dir: Path


def provider_layout(command, cwd, env, provider_id) -> ProviderLayout | None:
    """The layout of a hub provider worker, or None for any other process this client starts.

    Only the real runner, started by an interpreter the hub built, with a bundle named in its
    environment, is a provider worker. Anything else (a fixture, a script a test writes) runs as
    it always did.
    """
    try:
        if not command or len(command) < 2 or os.path.abspath(str(command[-1])) != _RUNNER_PATH:
            return None
        bundle = (env or {}).get("BAZARR_PROVIDER_HUB_BUNDLE") or cwd
        if not bundle:
            return None
        from app.get_args import args

        from .state import provider_hub_dir
        from .venv import validated_interpreter

        hub_dir = Path(provider_hub_dir())
        interpreter = validated_interpreter(hub_dir, command[0])
        if interpreter is None:
            return None
        key = _SAFE_KEY.sub("_", provider_id or "temporary").lstrip(".") or "temporary"
        return ProviderLayout(
            key=key[:64],
            interpreter=interpreter,
            runner=_RUNNER_PATH,
            bundle=os.fspath(bundle),
            hub_dir=hub_dir,
            config_dir=Path(args.config_dir),
        )
    except Exception:
        logger.debug("Provider Hub: could not work out the sandbox layout of a worker", exc_info=True)
        return None


def _secret_files(config_dir: Path) -> list[Path]:
    """The files in the configuration and database folders, so a link to one cannot put it in a granted tree."""
    files: list[Path] = []
    for folder in ("config", "db"):
        try:
            files.extend(entry for entry in (config_dir / folder).iterdir() if entry.is_file() or entry.is_symlink())
        except OSError:
            continue
    return files


_PROBE_AGAIN_SECONDS = 60.0
_probe_again_lock = threading.Lock()
_probe_again_at = 0.0


def _probe_again(probe):
    """The host check once more, when the last try to confirm it was over a minute ago."""
    global _probe_again_at
    with _probe_again_lock:
        if time.monotonic() - _probe_again_at < _PROBE_AGAIN_SECONDS:
            return probe
        _probe_again_at = time.monotonic()
    from .sandbox import probe_sandbox

    return probe_sandbox(refresh=True)


def _is_provider_runner(command, env) -> bool:
    """Whether ``command`` starts the real provider runner for a bundle, whatever interpreter it uses."""
    try:
        return bool(
            command
            and len(command) >= 2
            and os.path.abspath(str(command[-1])) == _RUNNER_PATH
            and (env or {}).get("BAZARR_PROVIDER_HUB_BUNDLE")
        )
    except Exception:
        return False


def provider_spec(layout: ProviderLayout, command, cwd, env, *, source_media: SourceMediaFile | None = None):
    """The launch specification of one provider worker: the softer provider profile.

    Both profiles retain their system, interpreter and bundle access. The ordinary worker also
    receives Arr roots and program directories. A scoped worker gets individual media tools and
    one source file, with the metadata filter enabled.
    Both can write to their own data and temp folders. The configuration, database and backups
    remain protected. The limits allow ffmpeg to write large temporary files and run its threads.
    """
    from .sandbox import capabilities
    from .provider_roots import (
        backup_folders, binary_folders, media_overlaps_runtime, media_root_folders, own_ports, program_folders,
        source_file_is_shielded, source_tool_paths,
    )
    from .runtime.environment import worker_environment
    from .sandbox.policy import (
        PROVIDER_EXTRA_READ_PATHS,
        PROVIDER_LIMITS,
        PolicyError,
        RuntimeSpec,
        SandboxPolicy,
        provider_tree_grants,
    )

    base = layout.hub_dir / "run" / layout.key
    data, temp = Path((env or {}).get("BAZARR_PLUGIN_DATA_DIR") or base / "data"), base / "tmp"
    for folder in (base, data, temp):
        if folder.is_symlink() or folder.is_junction():
            raise WorkerError("A worker folder is a link")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The worker can change the mode of its own folders; the next start puts it back.
        os.chmod(folder, 0o700)
    backups = backup_folders()
    shields = [layout.config_dir, *backups]
    managed_binaries = layout.config_dir / "managed" / "bin"
    secrets = _secret_files(layout.config_dir)
    source_paths = ()
    tool_paths = ()
    if source_media is None:
        runtime_paths = (managed_binaries, *provider_tree_grants(
            [*program_folders(), *binary_folders(), *media_root_folders()], shields))
    else:
        if source_file_is_shielded(source_media.path, shields, secrets):
            raise PolicyError("the source media file is in a protected location")
        source_paths = (source_media.path,)
        managed = managed_binaries.resolve()
        runtime_paths = ()
        tool_paths = tuple(tool for tool in source_tool_paths(layout.config_dir)
                           if not source_file_is_shielded(
                               tool, backups if tool.is_relative_to(managed) else shields, secrets))
    policy = SandboxPolicy.for_interpreter(
        layout.interpreter,
        read_only=[
            layout.bundle,
            layout.runner,
            *PROVIDER_EXTRA_READ_PATHS,
            *runtime_paths,
        ],
        read_files=source_paths,
        execute_files=tool_paths,
        read_write=[data, temp],
        # The folders, and the files in them: a file that is a link into a granted tree is refused too.
        protected_paths=[layout.config_dir / "config", layout.config_dir / "db", *backups, *secrets],
        # Bazarr+'s own API answers on these ports (the one the server bound, --port, the setting). Where
        # the kernel has network rules (ABI 4 and above) the worker cannot connect to them; every other
        # TCP connection stays open. MPTCP sockets, which those rules do not cover, are refused by the filter.
        deny_connect_ports=own_ports(),
    )
    if source_media is not None:
        if policy.identities.get(str(source_media.path)) != source_media.identity:
            raise PolicyError("the source media file changed while preparing its read grant")
        runtime_grants = (*policy.read_only, *policy.read_write, *policy.execute_files)
        if media_overlaps_runtime(source_media, runtime_grants, policy.identities):
            raise PolicyError("a configured media library overlaps a provider runtime location")
    environment = worker_environment(
        "subtitle_provider",
        home=data,
        tmpdir=temp,
        extra={key: value for key, value in (env or {}).items() if key not in {"HOME", "PATH"}},
    )
    # The host's own search path stays: the plugins find ffmpeg and ffprobe through it.
    environment["PATH"] = os.environ.get("PATH") or environment["PATH"]
    return RuntimeSpec(
        interpreter=os.fspath(layout.interpreter),
        args=tuple(str(part) for part in command[1:]),
        cwd=os.fspath(cwd or layout.bundle),
        env=environment,
        policy=policy,
        limits=PROVIDER_LIMITS,
        seccomp=source_media is not None,
        block_mptcp=True,
        require_capabilities=source_media is not None or capabilities.supported(),
    )


# The real process class, taken at import so a patched ``subprocess.Popen`` does not change
# which objects are treated as real processes.
_POPEN = subprocess.Popen


class ProviderWorkerClient:
    """Small NDJSON client for a single provider worker process."""

    def __init__(
        self,
        command: list[str],
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        provider_id: str | None = None,
        before_start=None,
        host_calls: dict[str, Any] | None = None,
        release: str | None = None,
    ):
        # The calls this worker may make to the host while it serves a request: capability id to
        # a function (payload, seconds_left, cancelled_event) -> dict. Anything else a worker asks for is refused.
        self._host_calls = dict(host_calls or {})
        # Called before every start of the worker process, the first one and each
        # respawn: whatever it raises keeps the process from starting.
        self._before_start = before_start
        self.command = command
        self.cwd = str(cwd) if cwd else None
        self.env = env
        self.provider_id = provider_id if isinstance(provider_id, str) and provider_id else None
        # The release this worker runs, by the content address of its bundle: a release that is replaced or
        # stopped is told apart from the one that replaces it.
        self.release = release if isinstance(release, str) and release else None
        self.process: subprocess.Popen | None = None
        # A one-shot health probe can share its host's absolute deadline. Its
        # owner bounds the caller's wait and stops this client after the request
        # unwinds, including when an OS launch or pipe write outlives that wait.
        self.request_deadline: float | None = None
        # monotonic timestamp of the last request, read by reap_idle_workers
        self.last_used: float = time.monotonic()
        self._lock = threading.Lock()
        # Close and scoped launch publication are mutually exclusive. Policy
        # preparation and worker shutdown stay outside this lock.
        self._launch_lock = threading.Lock()
        # Set while no gated request on this worker is waiting for its outcome
        # to be recorded. A gated request clears it right before it writes and
        # the pool sets it again, through outcome_recorded(), once the backoff
        # and discard for that call are in place, so a queued request never
        # runs its admission check against the state from before a failure.
        # Waited on without the lock: the call that cleared it may still need
        # the lock for its archive member selection.
        self._outcome_latch = threading.Event()
        self._outcome_latch.set()
        # Which thread cleared the latch; only that thread may set it again.
        self._latch_owner = threading.local()
        # Set by stop(): a retired client admits no gated request, so a waiter
        # cannot respawn its worker with the configuration that was replaced.
        self._closed = False
        self._cancelled = False
        self._cut_process = None
        self._stdout_queue: _BoundedChunks | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        # Set when the sandbox ruleset could not be applied to this client's worker: it starts
        # without it from then on. ``_sandbox_layout`` is set while the live worker is sandboxed.
        self._sandbox_failed_open = False
        self._sandbox_layout: ProviderLayout | None = None

    def _closed_worker_error(self, message, *, bound_selector=False):
        if getattr(self, "_cancelled", False):
            return WorkerCancelled(message)
        if bound_selector:
            return SourceMediaRefused(message)
        return WorkerError(message)

    def start(self, *, source_media: SourceMediaFile | None = None) -> bool:
        if self.process:
            if not self._has_exited(self.process):
                return False
            if activity.is_registered(self.process):
                self._kill_tree(self.process, 5.0)
                if not _client_gone(self, 0):
                    raise WorkerError("The plugin worker is still shutting down")
            elif getattr(self.process, "returncode", 0) is None:
                # The previous worker is gone but not collected: end what it
                # left running before its slot is reused.
                self._kill_tree(self.process, 5.0)

        if getattr(self, "_closed", False):
            # Stopped or cancelled: nothing starts on this client again. A request that passed its own check just
            # before a cancel reaches here, and must not respawn the worker the cancel killed.
            raise self._closed_worker_error("worker client is stopped")

        before_start = getattr(self, "_before_start", None)
        if before_start is not None:
            before_start()
        if source_media is not None and getattr(self, "_closed", False):
            raise self._closed_worker_error("worker client is stopped")

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if self.env:
            env.update(self.env)
        if data_dir := env.get("BAZARR_PLUGIN_DATA_DIR"):
            folder = Path(data_dir)
            if folder.is_symlink() or folder.is_junction():
                raise WorkerError("The plugin data folder is a link")
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            env["HOME"] = data_dir

        self.last_used = time.monotonic()
        if source_media is None:
            self.process = self._spawn(env)
        else:
            self._spawn(env, source_media=source_media)  # Published at launch under _launch_lock.
        if self._is_launcher(self.process):
            # Every launcher this client started stays on the list until it has confirmed its cleanup:
            # a respawn must not make the previous launcher's descendants forgotten.
            self.launcher_cleanup_state()  # forgets the launchers that have confirmed their cleanup
            with _GROUPS_LOCK:
                self._launchers = [*getattr(self, "_launchers", ()), self.process]
        # Every group this client started stays on the list until it is seen
        # empty: a respawn must not make the previous worker's survivors
        # forgotten while they still run from the environment.
        if not activity.is_windows():
            with _GROUPS_LOCK:
                group = getattr(self.process, "activity_group", self.process.pid)
                self._groups = [*self._live_groups(), group]
        self._stdout_queue = _BoundedChunks()
        client_ref = weakref.ref(self)
        process = self.process
        self._stdout_thread = threading.Thread(
            target=self._enqueue_stdout,
            args=(process, self._stdout_queue, lambda: getattr(client_ref(), "_cut_process", None) is process),
            daemon=True,
        )
        self._stdout_thread.start()
        # Continuously drain stderr too: the worker writes tracebacks there, and an
        # undrained pipe can fill before the worker writes its JSON error to stdout,
        # blocking the worker until the request times out. Drain + log instead.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self.process,),
            daemon=True,
        )
        self._stderr_thread.start()

        with _live_clients_lock:
            _live_clients.add(self)
        provider_id = getattr(self, "provider_id", None)
        if provider_id is not None:
            runtime_status.worker_started(provider_id)
        return True

    def _spawn(self, env: dict[str, str], *, source_media: SourceMediaFile | None = None):
        """Start the worker process: through the provider sandbox when it applies, else directly.

        A provider worker is sandboxed unless the user switched that off or the kernel has no Landlock
        at all: then it starts without it and the status page says so. Where the host may enforce, a
        failed or unfinished host check refuses the start, and so does a start that cannot prepare its
        ruleset: a worker that
        can damage the files the ruleset is built from must not be able to turn the sandbox off
        for its next start. Everything that is not a hub provider worker starts as before.
        """
        self._sandbox_layout = None
        if source_media is not None:
            try:
                profile = self._sandbox_profile(source_media=source_media)
                if profile is None:
                    raise SourceMediaRefused("source media requires the enforcing Provider Hub sandbox")
                return profile()
            except (SourceMediaRefused, WorkerCancelled):
                raise
            except Exception as error:
                raise SourceMediaRefused(f"the source media ruleset could not be prepared: {error}") from error

        profile = self._sandbox_profile()
        if profile is not None:
            try:
                return profile()
            except Exception as error:
                self._blocked(f"the ruleset could not be prepared: {error}")
                raise WorkerError(f"the sandbox could not be prepared, so the provider was not started: {error}") from error
        from .sandbox.spawn import OWNER_VARIABLE, _start_plain, owner_marker

        if marker := owner_marker():
            env = {**env, OWNER_VARIABLE: marker}
        options = dict(cwd=self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
        if sys.platform != "linux" and (
                getattr(self, "provider_id", None) or _is_provider_runner(self.command, env)):
            return _start_plain(self.command, **options)
        return subprocess.Popen(self.command, start_new_session=True, **options)

    def _sandbox_profile(self, *, source_media: SourceMediaFile | None = None):
        """A callable that starts the sandboxed worker, or None when this start is unsandboxed."""
        if source_media is None and getattr(self, "_sandbox_failed_open", False):
            return None
        provider_id = getattr(self, "provider_id", None)
        layout = provider_layout(self.command, self.cwd, self.env, provider_id)
        if layout is None:
            if source_media is None and _is_provider_runner(self.command, self.env):
                from .sandbox import note_provider_unsandboxed, provider_profile_enabled

                if provider_profile_enabled():
                    note_provider_unsandboxed(
                        provider_id or "temporary",
                        "the worker does not run from one of the hub's own environments",
                    )
            return None
        from .sandbox import provider_profile_enabled

        if not provider_profile_enabled():
            return None
        from .sandbox import PROVIDER_MIN_ABI, note_provider_unsandboxed, probe_sandbox, spawn_worker
        from .sandbox.spawn import MIN_ENFORCED_ABI

        probe = probe_sandbox()
        minimum_abi = MIN_ENFORCED_ABI if source_media is not None else PROVIDER_MIN_ABI
        if not probe.enforces(minimum_abi) and not probe.absent:
            # A host check that failed or did not finish may have hit a passing condition. It is asked
            # again, at most once a minute, before a provider is refused on its word.
            probe = _probe_again(probe)
        if not probe.enforces(minimum_abi):
            if source_media is not None:
                raise SourceMediaRefused("source media requires verified Landlock ABI 3 or later")
            if probe.absent:
                # The kernel genuinely has no Landlock: nothing can be applied, and the provider runs without it.
                note_provider_unsandboxed(layout.key, probe.reason or "this host has no Landlock")
                return None
            # The kernel may well enforce, but the host check failed or could not finish (a timeout, a file
            # error, a canary that escaped). Nothing here says the ruleset would hold, so no worker starts.
            cause = f"the host check could not confirm the ruleset: {probe.reason or 'no result'}"
            self._blocked(cause)
            raise WorkerError(
                f"the sandbox could not be confirmed, so the provider was not started: {probe.reason}. "
                "It is checked again on the next start; restarting Bazarr+ or switching the sandbox off "
                "in the Subtitle Hub settings ends the refusal."
            )

        def start():
            options = {"source_media": source_media} if source_media is not None else {}
            spec = provider_spec(layout, self.command, self.cwd, self.env, **options)
            with self._launch_lock if source_media is not None else nullcontext():
                if source_media is not None and self._closed:
                    raise self._closed_worker_error("worker client is stopped")
                process = spawn_worker(
                    spec,
                    probe=probe,
                    minimum_abi=minimum_abi,
                    require_sandbox=source_media is not None,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self._sandbox_layout = layout
                if source_media is not None:
                    self.process = process
            return process

        return start

    def _notice_key(self) -> str:
        layout = provider_layout(self.command, self.cwd, self.env, getattr(self, "provider_id", None))
        return layout.key if layout else (getattr(self, "provider_id", None) or "temporary")

    def _fail_open(self, cause: str) -> None:
        """The kernel cannot apply a ruleset at all: this client's workers start without it from now on, and say why."""
        from .sandbox import note_provider_unsandboxed

        self._sandbox_failed_open = True
        self._sandbox_layout = None
        note_provider_unsandboxed(self._notice_key(), cause)

    def _blocked(self, cause: str) -> None:
        """Record that a start was refused because its ruleset could not be applied."""
        from .sandbox import note_provider_blocked

        self._sandbox_layout = None
        note_provider_blocked(self._notice_key(), cause)

    def _opened_after_apply_failure(self, *, allow_fallback=True) -> bool:
        """After a worker that said nothing: handle a launcher report that the ruleset was not applied.

        Only a report that the ruleset failed to apply before the worker ran counts; a report about
        the launcher's cleanup, or its fork, after the worker ran is not one (``_sandbox_apply_failure``).
        When the kernel itself cannot apply a ruleset (no Landlock, or a lower ABI than the probe
        saw) the client starts its workers without it from now on and True is returned, so the caller
        starts one and sends the request again. Any other failure (a grant that could not be opened,
        a capability that could not be dropped) refuses this start with an error: it is what a worker
        that damaged its own folders would cause, and it must not switch the sandbox off.
        """
        process = self.process
        if process is None or not getattr(self, "_sandbox_layout", None):
            return False
        deadline = time.monotonic() + 2.0
        while True:
            exited = self._has_exited(process)
            cause = self._sandbox_apply_failure()
            if cause or exited or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if not cause:
            return False
        self._kill_tree(process, 5.0)
        if not allow_fallback:
            raise SourceMediaRefused(f"the source media ruleset could not be applied: {cause}")
        match = _ERRNO_IN_MESSAGE.search(cause)
        if match and int(match.group(1)) in _KERNEL_CANNOT_APPLY:
            self._fail_open(f"the kernel could not apply the ruleset: {cause}")
            return True
        self._blocked(f"the ruleset could not be applied: {cause}")
        raise WorkerError(f"the sandbox could not be applied, so the provider was not started: {cause}")

    def _sandboxed_answer(self, line: str) -> None:
        """A sandboxed worker answered, so its ruleset is in force: any earlier notice for it is out of date."""
        layout = getattr(self, "_sandbox_layout", None)
        if line and layout is not None and getattr(self.process, "enforcement", None) == "enforced":
            from .sandbox import clear_provider_notice

            clear_provider_notice(layout.key)

    def _sandbox_apply_failure(self) -> str | None:
        """The launcher's own report that the ruleset could not be applied, when this worker never ran."""
        process = self.process
        report = getattr(process, "launcher_failure", None)
        if process is None or report is None or not getattr(self, "_sandbox_layout", None):
            return None
        try:
            failure = report()
        except Exception:
            return None
        if failure is not None and failure.kind == "sandbox" and failure.message.startswith(_APPLY_FAILURE_PREFIX):
            return failure.message
        return None

    @staticmethod
    def _enqueue_stdout(process: subprocess.Popen, stdout_queue: _BoundedChunks,
                        cancelled: Callable[[], bool] | None = None) -> None:
        stdout = process.stdout
        if stdout is None:
            stdout_queue.put(None)
            return
        eof = None
        try:
            # Read in bounded chunks (readline(size) stops at a newline or after
            # ``size`` chars) and track the current line's length, so an oversized
            # response is rejected here instead of buffering in full before
            # json.loads. A normal multi-chunk line is reassembled downstream.
            line_len = 0
            while True:
                chunk = stdout.readline(_READ_CHUNK_CHARS)
                if not chunk:
                    # Freeze the cause where stdout closes. The request can
                    # consume this marker after a later cancellation.
                    if cancelled is not None and cancelled():
                        eof = _CANCELLED_EOF
                    break
                line_len += len(chunk)
                if line_len > _MAX_RESPONSE_LINE_BYTES:
                    stdout_queue.put(_OVERSIZE_RESPONSE)
                    break
                if isinstance(stdout_queue, _BoundedChunks):
                    # Checked without reaping: collecting the exit status here would free the group id
                    # before the code that ends the worker's descendants has signalled it.
                    stdout_queue.put(chunk, alive=lambda: not ProviderWorkerClient._has_exited(process))
                else:
                    stdout_queue.put(chunk)
                if chunk.endswith("\n"):
                    line_len = 0
        finally:
            stdout_queue.put(eof)

    @staticmethod
    def _drain_stderr(process: subprocess.Popen) -> None:
        """Drain the worker's stderr so a large or repeated traceback can never
        fill the pipe buffer and block the worker before it writes its JSON error
        to stdout. Surfaces the diagnostics in the host log (per-line capped)."""
        stderr = process.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                line = line.rstrip("\n")
                if line:
                    logging.debug("provider-worker stderr: %s", line[:2000])
        except Exception:
            pass

    def _deregister(self) -> bool:
        # A provider's child processes can still have its data files open.
        # Keep the client visible to the next backup, removal and idle sweep.
        process = getattr(self, "process", None)
        env = getattr(self, "env", None) or {}
        writes_data = getattr(self, "provider_id", None) and (
            env.get("BAZARR_PLUGIN_DATA_DIR") or _is_provider_runner(getattr(self, "command", ()), env))
        if (writes_data or activity.is_registered(process)) and not _client_gone(self, 0):
            self._retain_survivor()
            return False
        with _live_clients_lock:
            if getattr(self, "process", None) is not process:
                return False
            _live_clients.discard(self)
            _unreaped_survivors.discard(self)
        return True

    def _retain_survivor(self) -> None:
        with _live_clients_lock:
            _unreaped_survivors.add(self)

    @staticmethod
    def _signal_group(process, sig) -> bool:
        """Send ``sig`` to the worker's whole process group. False if it could not.

        ``start_new_session=True`` makes the worker its own session and group
        leader, so its group id is its pid. Signalling only that pid leaves
        anything the plugin forked running and reparented to init, where nothing
        counts or reclaims it, and a reap would report success having freed
        nothing. Plugin code is third party, so that is a mechanism to close,
        not an unlikely accident.

        ``os.killpg`` is POSIX only, so this reports failure rather than raising
        on Windows and the caller falls back to signalling the worker alone.
        ``AttributeError`` is not an ``OSError``, so catching it is not enough:
        it has to be checked before the call.
        """
        killpg = getattr(os, "killpg", None)
        if killpg is None or sig is None:
            return False
        try:
            killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    @staticmethod
    def _is_launcher(process) -> bool:
        """Whether ``process`` is a sandbox launcher, which ends its own tree through ``stop``."""
        from .sandbox import LauncherProcess

        return isinstance(process, LauncherProcess)

    @staticmethod
    def _has_exited(process) -> bool:
        """Whether the worker has exited, without reaping it.

        ``poll()`` collects the exit status, after which the worker's pid, and
        with it the id of the process group it led, can be handed to an
        unrelated process: signalling the group would then hit a stranger.
        Seeing the exit with ``waitid(..., WNOWAIT)`` leaves the worker a
        zombie that still reserves both, so whatever it forked can be signalled
        before the caller reaps it.
        """
        returncode = getattr(process, "returncode", None)
        if isinstance(returncode, int):
            return True
        waitid = getattr(os, "waitid", None)
        if waitid is None or not isinstance(process, _POPEN):
            return process.poll() is not None
        try:
            return waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True

    @classmethod
    def _wait_exit(cls, process, timeout: float) -> None:
        """Wait for the worker to exit without reaping it; ``TimeoutExpired`` if it does not in time."""
        if getattr(os, "waitid", None) is None or not isinstance(process, _POPEN):
            process.wait(timeout=timeout)
            return
        deadline = time.monotonic() + timeout
        while not cls._has_exited(process):
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args if hasattr(process, "args") else "worker", timeout)
            time.sleep(0.01)

    @staticmethod
    def _group_alive(group: int) -> bool:
        """Whether anything is left in the process group ``group`` (signal 0 sends nothing)."""
        killpg = getattr(os, "killpg", None)
        if killpg is None:
            # No way to look: whatever the worker started may still be running.
            return True
        try:
            killpg(group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _live_groups(self) -> list[int]:
        """The groups this client started that still have members; the empty ones are forgotten."""
        with _GROUPS_LOCK:
            groups = [group for group in getattr(self, "_groups", ()) if self._group_alive(group)]
            self._groups = groups
            return groups

    def launcher_cleanup_state(self) -> str:
        """Whether every launcher this client started has confirmed that its tree is gone.

        ``confirmed``: none left, or each exited by itself and said ``complete``. ``pending``: one is
        still running. ``unconfirmed``: one is gone without that word (killed from outside, or it gave
        up on survivors), which nothing can make up for. A launcher is forgotten once confirmed.
        """
        with _GROUPS_LOCK:
            launchers = list(getattr(self, "_launchers", ()))
        state = "confirmed"
        kept = []
        for launcher in launchers:
            if not self._has_exited(launcher):
                kept.append(launcher)
                state = "pending" if state != "unconfirmed" else state
                continue
            try:
                if launcher.returncode is None:
                    launcher.poll()
                said_complete = launcher.returncode is not None and launcher.returncode >= 0 and (
                    launcher.launcher_cleanup() == "complete"
                )
            except Exception:
                said_complete = False
            if not said_complete:
                kept.append(launcher)
                state = "unconfirmed"
        with _GROUPS_LOCK:
            self._launchers = kept
        return state

    def group_gone(self, ceiling: float = 10.0) -> bool:
        """True once nothing is left in any process group this client has started.

        A worker's children can outlive it, and they run from the same
        environment, so the environment is not free until every group is empty,
        including the groups of workers an earlier respawn replaced. Checked for
        at most ``ceiling`` seconds. On Windows the worker and the other
        members of its job must have exited.
        """
        if activity.is_windows():
            return self.process is None or (self.process.poll() is not None
                                            and activity.tree_gone(self.process, ceiling))
        deadline = time.monotonic() + ceiling
        while True:
            if not self._live_groups():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    @classmethod
    def _terminate_tree(cls, process, grace_seconds: float) -> None:
        """Ask the group to stop, wait for the worker, reclaim what stayed.

        Raises if the worker itself outlives the grace period, which is the
        caller's signal to escalate to ``_kill_tree``.

        The trailing SIGKILL is what makes the graceful path complete. A worker
        that exits promptly never reaches ``_kill_tree``, so without it a child
        that ignored SIGTERM would survive a reap that reported success. It
        addresses the group by the id the exited worker led; a pid freed
        microseconds earlier could in principle be reused, but only a process
        that had made itself a group leader in that window would be reachable
        by it.
        """
        if activity.is_registered(process):
            process.stop(grace_seconds)
            if process.poll() is None or not activity.tree_gone(process):
                raise subprocess.TimeoutExpired(getattr(process, "args", "worker"), grace_seconds)
            return
        if cls._is_launcher(process):
            # The launcher passes the signal on and ends the whole tree, the detached
            # processes a plugin started included, which a group signal cannot reach.
            process.stop(grace_seconds)
            if process.poll() is None:
                raise subprocess.TimeoutExpired(getattr(process, "args", "worker"), grace_seconds)
            return
        if not cls._signal_group(process, signal.SIGTERM):
            process.terminate()
        cls._wait_exit(process, grace_seconds)
        # The leader is still unreaped here, so the group id is still its own.
        cls._signal_group(process, _SIGKILL)
        process.wait(timeout=grace_seconds)

    @classmethod
    def _kill_tree(cls, process, grace_seconds: float) -> bool:
        """SIGKILL the worker's whole process group, not just the worker.

        True only when the worker is confirmed gone; the caller must not
        deregister a process this could not end.
        """
        if activity.is_registered(process):
            process.stop(0.0)
            return process.poll() is not None and activity.tree_gone(process)
        if cls._is_launcher(process):
            # Closing the launcher's lifeline makes it kill everything it started at once; a
            # group kill alone would leave the launcher unable to reach a detached descendant.
            process.stop(0.0)
            if process.returncode is not None:
                # Reaped by the stop, so its pid may already be somebody else's: nothing is signalled.
                return True
        if not cls._signal_group(process, _SIGKILL):
            try:
                process.kill()
            except Exception:
                logger.warning("failed to kill provider worker", exc_info=True)
        try:
            process.wait(timeout=grace_seconds)
        except Exception:
            return False
        return True

    def stop_if_idle(self, idle_seconds: float, grace_seconds: float = 5.0) -> bool:
        """End this worker only if it is genuinely idle. True if it was stopped.

        Never waits. A worker serving a request holds ``_lock``, so the
        non-blocking acquire fails and it is left alone. That covers both halves
        of the problem: the sweep cannot end a request in flight, and it cannot
        pin the scheduler thread behind one. The second half matters because a
        request may run for hours, and a blocking acquire would hold the sweep
        for all of it and then kill the worker the instant it succeeded.

        Idleness is checked again once the lock is held, because the sweep chose
        this client without it.

        The process is ended directly rather than through ``stop()``, which
        sends a shutdown request and so would take the lock this already holds.
        An idle worker has nothing in progress to unwind.
        """
        process = self.process
        if process is None or self._has_exited(process):
            if process is not None and (getattr(process, "returncode", 0) is None
                                        or activity.is_registered(process)):
                # Exited but not collected: end what it left running first.
                self._kill_tree(process, grace_seconds)
            # Nothing to reap. Mirrors stop(): a held survivor whose process
            # finally exited on its own must not stay pinned in the strong
            # set; start() re-registers on the next spawn either way.
            self._deregister()
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if time.monotonic() - self.last_used < idle_seconds:
                return False
            if self._has_exited(process):
                return False
            ended = True
            try:
                self._terminate_tree(process, grace_seconds)
            except Exception:
                ended = self._kill_tree(process, grace_seconds)
        finally:
            self._lock.release()

        if not ended:
            # Both attempts left it running. Deregistering now would make it
            # permanently unreapable; keep it listed, and strongly, so a later
            # sweep retries even after the pool drops its own reference.
            logger.warning("idle provider worker survived termination; keeping it registered")
            self._retain_survivor()
            return False

        return self._deregister()

    def stop(self, grace_seconds: float = 5.0) -> None:
        # Closed first, on every path, then the latch is set so that a gated
        # request waiting on it wakes, sees the mark and gives up.
        with self._launch_lock:
            self._closed = True
            process = self.process
        latch = getattr(self, "_outcome_latch", None)
        if latch is not None:
            latch.set()
        if not process or self._has_exited(process):
            if process is not None and (getattr(process, "returncode", 0) is None
                                        or activity.is_registered(process)):
                # Exited but not yet reaped: its group id is still reserved, so
                # what it left running can be ended before it is collected.
                self._kill_tree(process, grace_seconds)
            # Nothing left to reap, so this is the one safe unconditional
            # discard.
            self._deregister()
            return
        try:
            self.request("shutdown", {"reason": "app_shutdown", "grace_ms": int(grace_seconds * 1000)}, grace_seconds)
            self._wait_exit(process, grace_seconds)
            # A clean shutdown ends the worker, not necessarily what it forked;
            # the worker is unreaped, so its group id is still its own.
            if activity.is_registered(process):
                self._kill_tree(process, grace_seconds)
            else:
                self._signal_group(process, _SIGKILL)
                process.wait(timeout=grace_seconds)
        except Exception:
            # A failed or refused shutdown does not excuse what the worker
            # forked. Unless it was already collected, end the whole group (a
            # worker that died meanwhile is still a zombie holding its id).
            if getattr(process, "returncode", 0) is None or activity.is_registered(process):
                self._kill_tree(process, grace_seconds)

        # Leave the registry only once this process is confirmed dead AND is
        # still the one this client owns. Two ways to get that wrong:
        #
        # A worker can outlive its own stop. start() registers only on the spawn
        # path, so a client discarded while its process still runs is never
        # re-added by a later request(), and that subprocess becomes permanently
        # unreapable: precisely the leak this reaper exists to prevent.
        #
        # And request() calls start(), which respawns if the process died
        # between the check above and the shutdown. self.process is then a NEW
        # worker that registered itself, and discarding here would strand it.
        if self.process is process and process.poll() is not None:
            self._deregister()
        elif self.process is process:
            # stop() could not end it. The pool is about to drop this client,
            # and the weak registry alone would let it be collected with the
            # process still running; hold it until a sweep succeeds.
            self._retain_survivor()

    def _read_line_with_deadline(self, timeout: float) -> str:
        """Read one NDJSON line from the worker, honoring ``timeout`` seconds.

        ``subprocess.PIPE.readline()`` blocks forever when a plugin hangs.
        Read stdout on a daemon thread and wait on a queue so the timeout path
        works on platforms where ``select`` cannot poll subprocess pipes.
        """
        process = self.process
        if process is None or process.stdout is None:
            raise WorkerError("worker process did not start")
        stdout_queue = self._stdout_queue
        if stdout_queue is None:
            raise WorkerError("worker stdout reader did not start")
        deadline = time.monotonic() + max(0.0, float(timeout))
        chunks: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # One-shot probes let their owner perform ordinary shutdown
                # after the request unwinds. Other requests keep the hard stop.
                if not getattr(self, "_defer_probe_timeout_stop", False):
                    self._kill_worker()
                # Carry the cause. Without a code this arrives at
                # provider_search_failure as a bare RuntimeError and is
                # classified "error", so a plugin that hung until the hard kill
                # was reported as "Provider search failed" rather than as the
                # timeout it was.
                raise WorkerError(
                    f"worker exceeded {timeout:.1f}s deadline",
                    code="timeout",
                )
            try:
                chunk = stdout_queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if chunk is _CANCELLED_EOF:
                raise WorkerCancelled("worker closed stdout")
            if chunk is None:
                return "".join(chunks)
            if chunk is _OVERSIZE_RESPONSE:
                self._kill_worker()
                raise WorkerError(
                    f"worker response exceeded {_MAX_RESPONSE_LINE_BYTES} bytes"
                )
            chunks.append(chunk)
            if chunk.endswith("\n"):
                return "".join(chunks)

    def _kill_worker(self) -> None:
        process = self.process
        if process is None:
            return
        self._kill_tree(process, 5.0)

    def revoke_host_calls(self) -> None:
        """Take back every host capability this worker was given, for the calls that follow."""
        self._host_calls = {}

    def cancel(self) -> None:
        """End this worker now, without waiting for the request it is serving.

        ``stop`` asks the worker to shut down through the same lock a request holds, so it waits for the request
        to end. This is for a release that must not run another second (a revoked one): the client is closed, so
        nothing is sent or respawned on it, and the process and everything it started are killed. The request in
        flight reports cancellation when the host can attribute the interruption to the cut.
        Never starts a process and never raises.
        """
        with self._launch_lock:
            self._cancelled = True
            self._closed = True
            self._host_calls = {}
            process = self.process
        latch = getattr(self, "_outcome_latch", None)
        if latch is not None:
            latch.set()
        try:
            # A directly observed worker must still be alive when the cut
            # starts. A live launcher can already be cleaning up an exited
            # child, so its liveness cannot explain the worker's pipe failure.
            if process is not None and not self._is_launcher(process) and not self._has_exited(process):
                self._cut_process = process
            self._kill_worker()
        except Exception:
            if process is not None:
                self._retain_survivor()
            logger.warning("Provider Hub: could not kill the worker of %s", self.provider_id, exc_info=True)
            return
        if process is not None and self._has_exited(process):
            self._deregister()
        elif process is not None:
            # Still there: held, so a later sweep tries again once the pool lets go of the client.
            self._retain_survivor()

    def _read_reply(self, request_id: str, timeout: float) -> str:
        """Read the line that answers the request, serving the worker's host calls on the way.

        A worker that needs something only the host has (a captcha solved with the user's key)
        sends a call frame on stdout before its reply and waits for the answer on stdin. This
        carries no socket and no address, so it works the same inside the sandbox, which keeps
        a worker off Bazarr+'s own port. The whole exchange stays inside the request's deadline.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        served = 0
        while True:
            line = self._read_line_with_deadline(max(0.0, deadline - time.monotonic()))
            call = self._host_call_of(line, request_id)
            if call is None:
                return line
            served += 1
            if served > _MAX_HOST_CALLS_PER_REQUEST:
                self._kill_worker()
                raise WorkerError("worker made too many host calls in one request")
            answer = self._answer_host_call(call, deadline)
            reply = {"abi": WORKER_ABI_VERSION, "id": request_id, "call_reply": answer}
            self._send_reply(json.dumps(reply, separators=(",", ":")) + "\n", deadline)

    def _violation(self, why: str):
        self._kill_worker()
        raise WorkerError(f"worker sent a malformed host call: {why}")

    def _host_call_of(self, line: str, request_id: str) -> dict | None:
        """The call a line carries, or None when it is the reply (or anything else) for the caller to judge.

        A call frame starts with exactly the head the runner writes, so a reply of any size is told apart
        by its first bytes and never parsed here. A frame outside the bounds ends the worker.
        """
        if not line.startswith(f'{{"abi":"{WORKER_ABI_VERSION}","id":{json.dumps(request_id)},"call":'):
            return None
        if len(line) > _MAX_HOST_CALL_LINE_BYTES:
            self._violation("the frame is too large")
        try:
            frame = json.loads(line)
        except ValueError:
            self._violation("the frame is not JSON")
        call = frame.get("call") if isinstance(frame, dict) else None
        if not isinstance(call, dict):
            self._violation("the call is not an object")
        call_id, capability, payload = call.get("id"), call.get("capability"), call.get("payload", {})
        if not isinstance(call_id, str) or not 0 < len(call_id) <= _MAX_HOST_CALL_ID_CHARS:
            self._violation("the call id is missing or too long")
        if not isinstance(capability, str) or not 0 < len(capability) <= _MAX_HOST_CALL_CAPABILITY_CHARS:
            self._violation("the capability is missing or too long")
        if not isinstance(payload, dict):
            self._violation("the payload is not an object")
        if len(json.dumps(payload, separators=(",", ":"))) > _MAX_HOST_CALL_PAYLOAD_BYTES:
            self._violation("the payload is too large")
        return {"id": call_id, "capability": capability, "payload": payload}

    def _send_reply(self, text: str, deadline: float) -> None:
        """Write the answer to a host call, without ever waiting on a worker that does not read.

        The write runs on a thread of its own and is waited for only briefly. A worker that has stopped
        reading fills the pipe and the write blocks, so past the wait the worker is ended, which also
        ends the write. The request's lock is therefore never held for longer than that wait.
        """
        process = self.process
        failures: list[tuple[BaseException, bool]] = []

        def write():
            try:
                process.stdin.write(text)
                process.stdin.flush()
            except BaseException as error:  # reported by the caller
                cut = isinstance(error, BrokenPipeError) and getattr(self, "_cut_process", None) is process
                failures.append((error, cut))

        thread = threading.Thread(target=write, name="provider-host-reply", daemon=True)
        thread.start()
        thread.join(max(0.0, min(_HOST_REPLY_WRITE_SECONDS, deadline - time.monotonic())))
        if thread.is_alive():
            self._kill_worker()
            thread.join(5.0)
            raise WorkerError("worker did not read the host's answer to its call")
        if failures:
            error, cut = failures[0]
            error_type = WorkerCancelled if cut else WorkerError
            raise error_type("worker closed while the host answered its call") from error

    def _answer_host_call(self, call: dict, deadline: float) -> dict:
        """Run one call and shape the outcome. Never raises, and says nothing the plugin may not see."""
        call_id, capability = call["id"], call["capability"]

        def failure(code: str, message: str) -> dict:
            return {"id": call_id, "ok": False, "error": {"code": code, "message": message}}

        handler = self._host_calls.get(capability)
        if handler is None:
            return failure("denied", "This plugin may not use that host capability.")
        payload = call.get("payload")
        if not isinstance(payload, dict):
            return failure("invalid", "A call carries an object.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return failure("timeout", "The request ran out of time.")
        outcome: dict[str, Any] = {}
        # Set when the request's time runs out, so a handler stops what it is doing and starts nothing new.
        cancelled = threading.Event()

        def run():
            try:
                outcome["payload"] = handler(payload, remaining, cancelled)
            except BaseException as error:  # reported to the plugin, never raised
                outcome["error"] = error

        # On a thread of its own so a vendor that never answers cannot hold the request past its
        # deadline; the reply is then a timeout and the read after it ends the worker.
        thread = threading.Thread(target=run, name="provider-host-call", daemon=True)
        thread.start()
        thread.join(remaining)
        if thread.is_alive() or time.monotonic() >= deadline:
            # Whatever the handler produces from here on is dropped: the request has ended.
            cancelled.set()
            return failure("timeout", "The request ran out of time.")
        error = outcome.get("error")
        if error is not None:
            code, message = getattr(error, "code", None), getattr(error, "message", None)
            if isinstance(code, str) and isinstance(message, str):
                return failure(code, scrub_text(message))
            logger.warning("A host call from %s failed", self.provider_id or "a worker", exc_info=error)
            return failure("failed", "The host could not do that.")
        result = outcome.get("payload")
        if not isinstance(result, dict):
            return failure("failed", "The host could not do that.")
        try:
            too_large = len(json.dumps(result, separators=(",", ":"))) > _MAX_HOST_REPLY_PAYLOAD_BYTES
        except (TypeError, ValueError):
            too_large = True
        if too_large:
            return failure("failed", "The host could not do that.")
        return {"id": call_id, "ok": True, "payload": result}

    def select_archive_member(
        self, payload: dict[str, Any] | None = None, timeout: float | None = None
    ) -> WorkerResult:
        """Ask the worker to language-pin a member from a host-listed archive."""
        return self.request(
            "select_archive_member", payload, timeout=30.0 if timeout is None else timeout
        )

    def outcome_recorded(self) -> None:
        """Let the next gated request run its admission check.

        Only the thread whose gated request cleared the latch sets it again. A
        call that never cleared it (refused, busy, or never sent) leaves it
        alone, or a queued request could pass its check while the failure it
        should see is still being recorded.
        """
        owner = getattr(self, "_latch_owner", None)
        if owner is None or not getattr(owner, "cleared", False):
            return
        owner.cleared = False
        self._outcome_latch.set()

    def _acquire_gated(self, timeout: float, admit) -> None:
        """Take the request lock for a gated request, or raise.

        Waits for the outcome latch without the lock, then for the lock,
        within the request's own timeout, and runs ``admit`` once it holds
        both. Returns with the lock held and the latch cleared by this thread.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            # A timeout of zero or less would make acquire() raise ValueError,
            # or wait forever for -1, so a spent budget is busy right here.
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._outcome_latch.wait(remaining):
                raise WorkerBusy(f"worker busy for {timeout:.1f}s")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._lock.acquire(timeout=remaining):
                raise WorkerBusy(f"worker busy for {timeout:.1f}s")
            if self._closed:
                self._lock.release()
                raise WorkerBusy("worker client is stopped")
            if not self._outcome_latch.is_set():
                # Another gated request took its turn between the wait and the
                # lock; wait for its outcome instead.
                self._lock.release()
                continue
            try:
                admitted = admit()
            except BaseException:
                self._lock.release()
                raise
            if not admitted:
                self._lock.release()
                raise RequestNotAdmitted("provider is excluded")
            self._outcome_latch.clear()
            self._latch_owner.cleared = True
            return

    def _end_worker_for_source_scope(self) -> None:
        """Retire a process and its descendants while leaving the client open for the next request."""
        process = self.process
        if process is None:
            return
        try:
            if getattr(process, "returncode", None) is None:
                try:
                    self._terminate_tree(process, 5.0)
                except Exception:
                    self._kill_tree(process, 5.0)
            ended = _client_gone(self, 0.0)
        except Exception:
            logger.warning("Provider Hub could not check a source media worker's exit", exc_info=True)
            ended = False
        if not ended:
            self._closed = True
            self._retain_survivor()
            raise WorkerError("the worker could not be stopped for the source media scope")
        self._deregister()

    def request(self, op: str, payload: dict[str, Any] | None = None, timeout: float = 30.0,
                admit=None, *, source_media: Callable[[], SourceMediaFile | None] | None = None,
                consume_source_reply=None) -> WorkerResult:
        """Send one request and read its reply.

        With ``admit``, a callable answering whether the provider may still be
        called, the request is gated: see _acquire_gated. The caller must then
        report the outcome through outcome_recorded(). Without it the request
        queues on the lock as it always has, which shutdown and the archive
        member selection rely on.

        ``source_media`` is a host-only lookup after admission. A worker with that
        grant ends before the next request gets the lock. A download can consume
        its content and select an archive member on the same worker before that.
        """
        if (op != "shutdown" and getattr(self, "provider_id", None)
                and (self.env or {}).get("BAZARR_PLUGIN_DATA_DIR")):
            from .datadir import dispatch

            with dispatch(self.provider_id):
                return self._request(op, payload, timeout, admit, source_media, consume_source_reply)
        return self._request(op, payload, timeout, admit, source_media, consume_source_reply)

    def _request(self, op, payload, timeout, admit, source_media, consume_source_reply) -> WorkerResult:
        if admit is None:
            if op == "health" and getattr(self, "request_deadline", None) is not None:
                if not self._lock.acquire(timeout=self._request_time_left(op, timeout)):
                    raise WorkerBusy(f"worker busy for {timeout:.1f}s")
            else:
                self._lock.acquire()
        else:
            self._acquire_gated(self._request_time_left(op, timeout), admit)
        grant = None
        completed = False
        try:
            if getattr(self, "_closed", False) and op != "shutdown":
                raise self._closed_worker_error("worker client is stopped")
            if source_media is not None:
                try:
                    authorized = source_media()
                except WorkerCancelled:
                    raise
                except Exception as error:
                    raise SourceMediaRefused("the source file could not be checked for local media access") from error
                if getattr(self, "_closed", False):
                    raise self._closed_worker_error("worker client is stopped")
                if authorized is None:
                    raise SourceMediaRefused("the source file is no longer eligible for local media access")
                # A warm worker has a different ruleset. Retire it before
                # starting a launcher that can read only this source file.
                self._end_worker_for_source_scope()
                grant = authorized
            result = self._request_locked(op, payload, timeout, source_media=grant)
            if consume_source_reply is not None:
                process = self.process

                def select_member(payload, timeout):
                    return self._request_locked("select_archive_member", payload, timeout, process=process)

                consume_source_reply(result, select_member)
            completed = True
            return result
        finally:
            try:
                if grant is not None:
                    self._end_worker_for_source_scope()
            except WorkerError:
                if completed:
                    raise
                logger.warning("Provider Hub could not clean up a failed source media request", exc_info=True)
            finally:
                self._lock.release()

    def _request_time_left(self, op, timeout):
        deadline = getattr(self, "request_deadline", None)
        if op != "health" or deadline is None:
            return timeout
        remaining = min(float(timeout), deadline - time.monotonic())
        if remaining <= 0:
            raise WorkerError("worker exceeded the probe deadline", code="timeout")
        return remaining

    def _request_locked(self, op, payload, timeout, *, source_media=None, process=None) -> WorkerResult:
        self._request_time_left(op, timeout)
        if getattr(self, "_closed", False) and op != "shutdown":
            raise self._closed_worker_error("worker client is stopped", bound_selector=process is not None)
        # A bound archive selector may speak only to the worker that downloaded
        # its archive. It cannot launch or fall back to a different process.
        if process is not None and (self.process is not process or self._has_exited(process)):
            raise self._closed_worker_error("the source media worker closed before archive selection", bound_selector=True)
        if op == "shutdown" and not (self.process is not None and not self._has_exited(self.process)):
            raise WorkerError("worker is not running")
        request_id = str(uuid.uuid4())
        message = {
            "abi": WORKER_ABI_VERSION,
            "id": request_id,
            "op": op,
            "deadline_ms": int(timeout * 1000),
            "payload": payload or {},
        }

        encoded = json.dumps(message, separators=(",", ":"), default=_json_default) + "\n"
        launch_options = {"source_media": source_media} if source_media is not None else {}
        fallback = {"allow_fallback": False} if source_media is not None else {}
        # Only an ordinary request may retry if the kernel cannot apply its
        # ruleset. Source media is never sent to an unsandboxed worker.
        for attempt in (0, 1):
            self._request_time_left(op, timeout)
            worker_started = process is None and self.start(**launch_options) is True
            remaining = self._request_time_left(op, timeout)
            if op == "health" and getattr(self, "request_deadline", None) is not None:
                message["deadline_ms"] = int(remaining * 1000)
                encoded = json.dumps(message, separators=(",", ":"), default=_json_default) + "\n"
            if (source_media is not None or process is not None) and self._closed:
                raise self._closed_worker_error("worker client is stopped", bound_selector=process is not None)
            provider_id = getattr(self, "provider_id", None)
            status_generation = runtime_status.generation(provider_id) if provider_id is not None else None
            status_configuration_generation = (
                runtime_status.configuration_generation(provider_id) if provider_id is not None else None
            )
            self.last_used = time.monotonic()
            if self.process is None or self.process.stdin is None or self.process.stdout is None:
                raise WorkerError("worker process did not start")
            self._request_time_left(op, timeout)
            try:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
            except OSError as error:
                if isinstance(error, BrokenPipeError) and getattr(self, "_cut_process", None) is self.process:
                    raise WorkerCancelled("worker closed stdin") from error
                if process is None and attempt == 0 and self._opened_after_apply_failure(**fallback):
                    continue
                raise
            self._defer_probe_timeout_stop = op == "health" and getattr(self, "request_deadline", None) is not None
            try:
                line = self._read_reply(request_id, self._request_time_left(op, timeout))
            finally:
                self._defer_probe_timeout_stop = False
            self._request_time_left(op, timeout)
            if not line and process is None and attempt == 0 and self._opened_after_apply_failure(**fallback):
                continue
            break
        self._sandboxed_answer(line)
        # A long request is fresh as it finishes, regardless of when it started.
        self.last_used = time.monotonic()

        if not line:
            raise WorkerError("worker closed stdout")

        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise WorkerError("worker returned malformed JSON") from error

        if not isinstance(response, dict):
            raise WorkerError("worker response must be an object")
        if response.get("abi") != WORKER_ABI_VERSION:
            raise WorkerError("worker returned unsupported ABI")
        if response.get("id") != request_id:
            raise WorkerError("worker returned mismatched request id")

        if not response.get("ok", False):
            _raise_worker_error(response.get("error"))

        payload = response.get("payload") or {}
        events = response.get("events") or []
        if not isinstance(payload, dict):
            raise WorkerError("worker payload must be an object")
        if not isinstance(events, list):
            events = []
        self._request_time_left(op, timeout)
        return WorkerResult(
            ok=True,
            payload=payload,
            events=events,
            status_generation=status_generation,
            status_configuration_generation=status_configuration_generation,
            worker_started=worker_started,
        )


def worker_command(python_exe: str | os.PathLike[str], runner: str | os.PathLike[str]) -> list[str]:
    return [str(python_exe), "-I", "-B", str(Path(runner))]
