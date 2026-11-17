# coding=utf-8
"""Sandbox launcher: runs a worker with limits and a Landlock ruleset.

The host starts this file with ``subprocess.Popen([python, "-I", "-S", "-B",
launcher.py, spec_fd, lifeline_fd, status_fd])``. It is deliberately standard library
only: no Bazarr+ import, no third-party package, no ``site`` processing (so no
``.pth`` hook can run before the ruleset is applied). The fresh interpreter has
one thread, which makes the single ``fork`` below safe and lets
``landlock_restrict_self`` cover the whole process.

What happens, in order:

1. Read the JSON spec from ``spec_fd`` (a memfd or temp file). The host keeps
   the other end of the ``lifeline_fd`` socket pair; end-of-file there means
   the host died. ``status_fd`` is one end of a socket pair the host reads: the
   launcher puts one JSON line on it when the worker could not be started, and
   nothing otherwise. Both are sockets because a same-user worker can reopen
   the host's end of a pipe through ``/proc/<host>/fd``, and cannot do that to
   a socket.
2. Become a child subreaper, then fork once. The launcher stays behind as the
   reaper. When the worker exits, when the host dies (lifeline EOF) or on
   SIGTERM, every descendant is killed, ``setsid`` children included. The tree
   is never judged empty from a ``/proc`` listing alone (``hidepid`` and
   undumpable processes hide entries): the launcher's own children list and
   ``waitpid`` returning ``ECHILD`` decide, and a launcher that can use neither
   nor a complete ``/proc`` refuses to start.
3. The child sets the parent-death signal, core size 0, open-file and
   file-size limits, nice, ``oom_score_adj``, umask, ``no_new_privs``, a
   every Linux capability dropped (see ``capabilities.py``), a seccomp filter
   that denies chmod, chown, utime and xattr calls, file attribute ioctls,
   keyrings, System V IPC, POSIX message queues, kernel-level introspection
   and the mount family, and changes to the scheduling of other processes (see
   ``seccomp.py``), changes to the working directory, applies the ruleset and
   ``execve``s the worker
   interpreter with exactly the environment in the spec.

Exit codes: the worker's own code (128 + signal when it was killed by one);
64 for a bad spec or working directory; 78 when the sandbox could not be
applied (the worker never ran); 127 when ``execve`` failed. The exit code alone
proves nothing, since a worker can exit with any code and print any line: the
authoritative record is the JSON line on ``status_fd``, written by the launcher
(``{"kind": "spec" | "sandbox" | "exec", "message": "..."}``), followed by one
cleanup record when the launcher has nothing left to clean or has given up
(``{"cleanup": "complete"}`` or ``{"cleanup": "survivors", "count": n}``; a
launcher that exits without one never finished). The child reports
over a close-on-exec pipe that the worker never inherits and that closes
silently when ``execve`` succeeds. A line starting with ``bazarr-sandbox:`` on
stderr repeats the reason for logs.

Spec layout (JSON object)::

    {
      "interpreter": "/path/to/python", "args": ["-I", "worker.py"],
      "cwd": "/path", "env": {"NAME": "value"},
      "mode": "enforce" | "declared",
      "min_abi": 1,
      "abi_cap": null,
      "read_only": [...], "read_write": [...], "devices": [...],
      "resolve_unix": [...], "connect_ports": null | [443, 80],
      "limits": {"nofile": 1024, "fsize": 536870912, "core": 0, "nproc": null,
                 "nice": 10, "oom_score_adj": 800, "umask": 63},
      "term_grace": 3.0,
      "seccomp": true
    }

``deny_connect_ports`` (default none) refuses TCP connections to those ports on kernels with network
rules and leaves every other connection alone; the provider profile uses it for Bazarr+'s own port.

``block_mptcp`` (default false) installs a small filter that keeps a worker on plain TCP (it refuses
MPTCP and SMC sockets and ``io_uring``) where the architecture has one and the port rule is in effect
(ABI 4 and above), for a spec that leaves the full metadata filter out.

``require_capabilities`` (default true) false makes dropping the capabilities best effort, for a
host whose architecture has no capability bindings; the provider profile uses it there.

``seccomp`` (default true) false leaves out the metadata filter; the provider profile does that so
``ffmpeg`` can work, and nothing else may.

In ``declared`` mode no ruleset is applied; limits, ``no_new_privs``, the
clean environment and, where the architecture is supported, the metadata
filter still are.
"""
import errno
import importlib.util
import json
import os
import resource
import select
import signal
import sys
import time

EXIT_BAD_SPEC = 64
EXIT_SANDBOX_FAILED = 78
EXIT_EXEC_FAILED = 127
MESSAGE_PREFIX = "bazarr-sandbox: "

MAX_SPEC_BYTES = 1 << 20
DEFAULT_TERM_GRACE = 3.0
_TERMINATING = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


_STATUS_FD = None


# A pipe write of at most PIPE_BUF bytes is atomic and never blocks on a pipe
# that is not already full, so a failure record is kept below it: the child
# writes its record and exits before anybody reads, and a record that did not
# fit would leave it blocked in write() with the launcher waiting for its exit.
_RECORD_LIMIT = getattr(select, "PIPE_BUF", 512)


def _record(kind, message, newline=True):
    """One failure record as ASCII JSON, at most ``_RECORD_LIMIT`` bytes in all.

    The size is measured after JSON escaping (a non-ASCII character becomes six
    bytes, so a character count says little), newline included.
    """
    kind = str(kind)[:32]
    text = str(message)[:2000]
    extra = 1 if newline else 0
    while True:
        data = json.dumps({"kind": kind, "message": text}).encode("ascii")
        if len(data) + extra <= _RECORD_LIMIT:
            return data + (b"\n" if newline else b"")
        shrunk = len(text) * (_RECORD_LIMIT - extra - 2) // len(data)
        text = text[: max(0, min(shrunk, len(text) - 1))]


def _report(kind, message):
    """Tell the host why the worker could not be started."""
    if _STATUS_FD is None:
        return
    try:
        os.write(_STATUS_FD, _record(kind, message))
    except OSError:
        pass


def _report_cleanup(state, count=None):
    """Tell the host how the tree cleanup ended: ``complete`` or ``survivors``.

    One short line (far under ``PIPE_BUF``, so the write is atomic) that is not
    a failure record: it has no ``kind``. The host treats a launcher as having
    cleaned up only when it exited by itself and wrote ``complete``; a missing
    record, from a launcher that died first, means nobody knows. ``count`` is the
    number of children still listed when the cleanup gave up, when known.
    """
    if _STATUS_FD is None:
        return
    record = {"cleanup": str(state)[:16]}
    if isinstance(count, int) and count >= 0:
        record["count"] = count
    try:
        os.write(_STATUS_FD, json.dumps(record).encode("ascii") + b"\n")
    except OSError:
        pass


def _survivor_count():
    try:
        children = _direct_children()
    except Exception:
        return None
    return None if children is None else len(children)


def _say(message):
    try:
        os.write(2, (MESSAGE_PREFIX + message + "\n").encode("utf-8", "replace"))
    except OSError:
        pass


def _load_sibling(name):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location("bazarr_sandbox_" + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_landlock():
    return _load_sibling("landlock")


def _read_spec(fd):
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_SPEC_BYTES:
            raise ValueError("spec too large")
        chunks.append(chunk)
    os.close(fd)
    spec = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("spec is not an object")
    for key in ("interpreter", "args", "cwd", "env"):
        if key not in spec:
            raise ValueError("spec lacks %r" % key)
    if not isinstance(spec["env"], dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in spec["env"].items()
    ):
        raise ValueError("env must map strings to strings")
    if not isinstance(spec["args"], list) or not all(isinstance(a, str) for a in spec["args"]):
        raise ValueError("args must be a list of strings")
    if spec.get("mode", "enforce") not in ("enforce", "declared"):
        raise ValueError("unknown mode")
    return spec


def _set_rlimit(which, value):
    if value is None:
        return
    _soft, hard = resource.getrlimit(which)
    if hard != resource.RLIM_INFINITY and value > hard:
        value = hard
    resource.setrlimit(which, (value, value))


def _apply_limits(limits):
    """Hard failures here fail closed: a worker must not run unlimited."""
    _set_rlimit(resource.RLIMIT_CORE, int(limits.get("core", 0)))
    _set_rlimit(resource.RLIMIT_NOFILE, limits.get("nofile"))
    _set_rlimit(resource.RLIMIT_FSIZE, limits.get("fsize"))
    if limits.get("nproc") is not None:
        _set_rlimit(resource.RLIMIT_NPROC, int(limits["nproc"]))
    os.umask(int(limits.get("umask", 0o077)))
    # Priority and OOM ordering only make the worker the first thing to give
    # way. They are best effort: some containers mount /proc read-only.
    try:
        nice = limits.get("nice", 10)
        if nice:
            os.nice(int(nice))
    except OSError as exc:
        _say("nice not applied: %s" % exc)
    adj = limits.get("oom_score_adj", 800)
    if adj is not None:
        try:
            with open("/proc/self/oom_score_adj", "w") as handle:
                handle.write(str(int(adj)))
        except OSError as exc:
            _say("oom_score_adj not applied: %s" % exc)


def _child(spec, landlock, seccomp, capabilities, parent_pid, close_fds, report_fd):
    """Runs in the forked child. Never returns.

    ``report_fd`` is a close-on-exec pipe: whatever is written there is a
    failure record, and a clean ``execve`` closes it without a byte.
    """
    kind = "sandbox"
    code = EXIT_SANDBOX_FAILED
    try:
        for sig in (*_TERMINATING, signal.SIGCHLD):
            signal.signal(sig, signal.SIG_DFL)
        signal.set_wakeup_fd(-1)
        for fd in close_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        # The launcher is non-dumpable and this fork inherited that, which would
        # keep it from writing its own /proc/self/oom_score_adj. The worker is
        # dumpable again after execve anyway; only the launcher stays closed.
        try:
            landlock.prctl(landlock.PR_SET_DUMPABLE, 1)
        except OSError:
            pass
        landlock.prctl(landlock.PR_SET_PDEATHSIG, int(signal.SIGKILL))
        if os.getppid() != parent_pid:
            os._exit(EXIT_SANDBOX_FAILED)
        _apply_limits(spec.get("limits") or {})
        landlock.prctl(landlock.PR_SET_NO_NEW_PRIVS, 1)
        # no_new_privs keeps an exec from gaining privilege but not from keeping
        # it: a host running as root would hand its capabilities to the worker.
        # This goes before the seccomp filter, which is then free to deny calls
        # the drop itself needs. Required when enforced; best effort otherwise.
        if spec.get("mode", "enforce") == "enforce" and spec.get("require_capabilities", True):
            capabilities.drop_all()
        else:
            try:
                capabilities.drop_all()
            except OSError as exc:
                _say("capabilities not dropped: %s" % exc)
        # chmod, chown, utime and xattr calls cannot be scoped by Landlock or by
        # a path-blind filter, so they are denied outright. Required when the
        # sandbox is enforced; best effort on a host that only declares.
        if not spec.get("seccomp", True):
            pass
        elif spec.get("mode", "enforce") == "enforce":
            seccomp.apply_filter()
        else:
            try:
                seccomp.apply_filter()
            except OSError as exc:
                _say("metadata filter not applied: %s" % exc)
        if (
            spec.get("block_mptcp")
            and spec.get("deny_connect_ports")
            and int(spec.get("min_abi", 1)) >= 4
            and seccomp.supported()
        ):
            # Landlock's TCP rules do not cover MPTCP or SMC sockets, nor io_uring, so a worker given a
            # port rule would walk around it. Only installed where that rule is in effect (ABI 4 and
            # above); a host whose architecture has no filter cannot, and the host says so.
            seccomp.apply_socket_filter()
        kind, code = "spec", EXIT_BAD_SPEC
        os.chdir(spec["cwd"])
        kind, code = "sandbox", EXIT_SANDBOX_FAILED
        if spec.get("mode", "enforce") == "enforce":
            landlock.apply_ruleset(
                read_only=spec.get("read_only", ()),
                read_write=spec.get("read_write", ()),
                devices=spec.get("devices", ()),
                resolve_unix=spec.get("resolve_unix", ()),
                connect_ports=spec.get("connect_ports"),
                deny_connect_ports=spec.get("deny_connect_ports") or (),
                abi_cap=spec.get("abi_cap"),
                min_abi=int(spec.get("min_abi", 1)),
            )
        kind, code = "exec", EXIT_EXEC_FAILED
        interpreter = spec["interpreter"]
        os.execve(interpreter, [interpreter] + spec["args"], spec["env"])
    except BaseException as exc:
        message = {
            "spec": "bad working directory: %s",
            "sandbox": "could not apply sandbox: %s",
            "exec": "exec failed: %s",
        }[kind] % exc
        _say(message)
        try:
            os.write(report_fd, _record(kind, message, newline=False))
        except OSError:
            pass
    os._exit(code)


class EnumerationError(Exception):
    """The process tree could not be listed. Never the same thing as "empty"."""


def _children_map():
    """Map pid to ppid for every process this launcher is allowed to read in /proc.

    A process that exits while it is being read is normal and skipped. So is one
    the kernel refuses to show (``hidepid`` answers EACCES or EPERM for another
    user's process, and for a descendant that cleared its dumpable flag): the
    listing is then incomplete, which is why no caller treats it as proof that a
    tree is empty. Any other failure (out of file descriptors, /proc missing or
    unreadable) raises ``EnumerationError`` and the callers fall back to the
    children list. Our own entry must be in the result.
    """
    result = {}
    try:
        names = os.listdir("/proc")
    except OSError as exc:
        raise EnumerationError("cannot list /proc: %s" % exc)
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as handle:
                data = handle.read().decode("utf-8", "replace")
            ppid = int(data.rsplit(")", 1)[1].split()[1])
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM, errno.ENOENT, errno.ESRCH):
                continue
            raise EnumerationError("cannot read /proc/%s/stat: %s" % (name, exc))
        except (ValueError, IndexError):
            raise EnumerationError("unreadable /proc/%s/stat" % name)
        result[int(name)] = ppid
    if os.getpid() not in result:
        raise EnumerationError("the launcher is missing from /proc")
    return result


def _own_children():
    """Pids of the launcher's own children, the only processes it may signal.

    The launcher is a subreaper: every descendant whose parent died becomes its
    child, and ``waitpid`` is the proof of an empty tree. A child that is not
    reaped yet (running or zombie) keeps its pid reserved, so a pid from this
    list is safe to signal until the next ``waitpid``; a pid of a grandchild is
    not, because its own parent may reap it and the kernel may hand the number
    to an unrelated process at any moment. Callers therefore reap, list and
    signal with no wait in between, and repeat: killing a child hands its
    children to the launcher, so the next pass sees them.

    Read from the kernel's children list; where there is none, from a /proc
    scan by parent id. Raises ``EnumerationError`` when neither can be used.
    """
    children = _direct_children()
    if children is not None:
        return children
    me = os.getpid()
    return [pid for pid, ppid in _children_map().items() if ppid == me]


def _proc_mount_options(mountinfo):
    """Option names of the last mount at ``/proc``, or ``None`` when there is none.

    ``mountinfo`` is the text of ``/proc/self/mountinfo``.
    """
    found = None
    for line in mountinfo.splitlines():
        head, separator, tail = line.partition(" - ")
        fields = head.split()
        if not separator or len(fields) < 6 or fields[4] != "/proc":
            continue
        super_fields = tail.split()
        if not super_fields or super_fields[0] != "proc":
            continue
        options = fields[5].split(",")
        if len(super_fields) >= 3:
            options += super_fields[2].split(",")
        found = options
    return found


def _proc_is_unfiltered(mountinfo=None):
    """True only when ``/proc`` shows every process to this launcher.

    ``hidepid`` (any value but ``0``/``off``) and ``subset=pid`` hide processes
    from an unprivileged caller, and a process that called
    ``prctl(PR_SET_DUMPABLE, 0)`` is hidden by ``hidepid`` even from its own
    user. When that cannot be ruled out, listing ``/proc`` is not proof that a
    tree is empty. An unreadable or missing mount record counts as "cannot be
    ruled out".
    """
    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
                mountinfo = handle.read()
        except OSError:
            return False
    options = _proc_mount_options(mountinfo)
    if options is None:
        return False
    for option in options:
        name, _eq, value = option.partition("=")
        if name == "hidepid" and value not in ("0", "off"):
            return False
        if name == "subset" and value == "pid":
            return False
    return True


def _direct_children():
    """Pids of the launcher's own children, whatever ``/proc`` hides.

    Read from ``/proc/self/task/<tid>/children``, which the kernel always shows
    to the process itself: ``hidepid`` and a worker's undumpable state do not
    apply. Returns ``None`` when the kernel does not provide the file. A
    subreaper's children include every orphaned descendant, so killing these
    until none are left reaches a tree no listing can see.
    """
    pids = []
    try:
        tids = os.listdir("/proc/self/task")
    except OSError:
        return None
    for tid in tids:
        try:
            with open("/proc/self/task/%s/children" % tid, "rb") as handle:
                text = handle.read().decode("ascii", "replace")
        except FileNotFoundError:
            if os.path.isdir("/proc/self/task/%s" % tid):
                return None
            continue
        except OSError:
            return None
        pids.extend(int(token) for token in text.split() if token.isdigit())
    return pids


def _tree_tracking_error():
    """Why this launcher could not stop every descendant, or ``None``.

    Needs either a complete ``/proc`` or the children file. The launcher is
    already a subreaper by the time this runs: that is what makes the children
    list complete and ``waitpid`` able to prove the tree is gone.
    """
    if _proc_is_unfiltered():
        return None
    if _direct_children() is not None:
        return None
    return (
        "/proc hides processes (hidepid or subset=pid) and the kernel does not list "
        "child processes, so descendants that left the process group could not be killed"
    )


def _kill_own_children():
    """SIGSTOP, then SIGKILL, every child the launcher has. False when the list is unavailable.

    One listing serves both signals with no wait in between, so every pid in it
    is still an unreaped child of this process when it is signalled.
    """
    children = _direct_children()
    if children is None:
        return False
    for sig in (signal.SIGSTOP, signal.SIGKILL):
        for pid in children:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
    return True


def _free_descriptors():
    """Close every descriptor but the three standard ones and the status pipe.

    The reason the tree could not be listed is often a full descriptor table,
    and the children list needs a descriptor to be read.
    """
    keep = sorted({0, 1, 2, _STATUS_FD if _STATUS_FD is not None else 2})
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        limit = 65536
    start = 0
    for fd in keep:
        if fd > start:
            os.closerange(start, fd)
        start = fd + 1
    os.closerange(start, max(limit, start))


def _emergency_kill(reason):
    """The tree cannot be listed at all: stop it through the children list, then exit.

    The launcher is a subreaper, so every descendant that lost its parent is
    one of its children: ``/proc/self/task/*/children`` names them whatever
    ``/proc`` hides, and ``waitpid`` reporting no children proves they are gone.
    The loop frees descriptors first (a full table is the usual reason to get
    here) and then kills and reaps until that proof arrives or the ceiling
    passes. Only when the kernel has no children list either is the process
    group killed as a last resort; that also ends the launcher, and a
    descendant that called ``setsid`` is then out of reach. The record sent to
    the host says which of the three happened. The exit code is always a
    sandbox failure: the launcher could not do its job the normal way.
    """
    _free_descriptors()
    outcome = None
    end = time.monotonic() + CLEANUP_SECONDS
    while time.monotonic() < end:
        if not _kill_own_children():
            break
        if not _children_remain():
            outcome = "killed and reaped every child it had"
            break
        time.sleep(0.02)
    else:
        outcome = "a descendant of the worker survived"
    if outcome is None:
        outcome = "could not list its children either and killed its process group"
    _report("sandbox", "launcher could not list its process tree (%s); %s" % (reason, outcome))
    _say("cannot list the process tree (%s); %s" % (reason, outcome))
    # Only a proven empty tree (waitpid found no children) counts as cleaned up.
    _report_cleanup("complete" if outcome.startswith("killed and reaped") else "survivors")
    if outcome.startswith("could not"):
        # Only when it really leads the group: a launcher run by hand inside a
        # shell's group must not take the shell down. The worker has a
        # parent-death signal, so it still dies when this process exits.
        if os.getpgrp() == os.getpid():
            try:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            except OSError:
                pass
    os._exit(EXIT_SANDBOX_FAILED)


def _reap_all(main_pid=None):
    """Reap every exited child. Returns ``(main child's status or None, children remain)``.

    The launcher is a subreaper, so "no children" (``ECHILD``) proves the whole
    tree is gone, however much of it ``/proc`` hid.
    """
    status = None
    while True:
        try:
            pid, st = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return status, False
        if pid == 0:
            return status, True
        if pid == main_pid:
            status = st


def _reap(main_pid):
    """Reap every exited child. Returns the main child's status or None."""
    return _reap_all(main_pid)[0]


def _kill_tree(main_pid=None):
    """SIGKILL the launcher's children until it has none. Returns ``(status, gone)``.

    ``status`` is the wait status of ``main_pid`` when this call reaped it.
    Each pass reaps first, so every pid in the list that follows is a child
    this process has not waited for; it is signalled before the next wait, so
    the number cannot belong to anyone else. Killing a child makes the
    launcher the parent of its children, which the next pass then kills. A
    SIGKILL pending also stops a fork that is in progress. ``gone`` is False
    when the ceiling passed first with a child alive.
    """
    status = None
    previous = None
    end = time.monotonic() + CLEANUP_SECONDS
    while True:
        reaped, left = _reap_all(main_pid)
        if reaped is not None:
            status = reaped
        if not left:
            return status, True
        if time.monotonic() >= end:
            return status, False
        try:
            pids = _own_children()
        except EnumerationError as exc:
            _emergency_kill(exc)
            return status, False
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if pids == previous:
            # Nothing changed since the last pass (a child that takes a moment
            # to die): do not spin.
            time.sleep(0.01)
        previous = pids


def _exit_code(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _supervise(main_pid, wake_r, lifeline_fd, grace):
    deadline = None
    status = None
    poller = select.poll()
    poller.register(wake_r, select.POLLIN)
    poller.register(lifeline_fd, select.POLLIN | select.POLLHUP)
    host_alive = True
    while status is None:
        try:
            events = poller.poll(1000)
        except InterruptedError:
            events = []
        for fd, _flag in events:
            if fd == wake_r:
                try:
                    data = os.read(wake_r, 256)
                except BlockingIOError:
                    data = b""
                if any(b in _TERMINATING for b in data) and deadline is None:
                    deadline = time.monotonic() + grace
                    try:
                        # main_pid is only reaped by the waits below, and the
                        # loop ends the moment one of them reaps it.
                        os.kill(main_pid, signal.SIGTERM)
                    except OSError:
                        pass
            elif fd == lifeline_fd and host_alive:
                try:
                    data = os.read(lifeline_fd, 64)
                except OSError:
                    data = b""
                if not data:
                    host_alive = False
                    poller.unregister(lifeline_fd)
                    # The worker is one of the children, so it dies with the rest.
                    reaped, _gone = _kill_tree(main_pid)
                    if reaped is not None:
                        status = reaped
        if status is None:
            status = _reap(main_pid)
        if status is None and deadline is not None and time.monotonic() >= deadline:
            reaped, _gone = _kill_tree(main_pid)
            if reaped is not None:
                status = reaped
            deadline = time.monotonic() + 60
    return status


CLEANUP_SECONDS = 7.0


def _children_remain():
    """Reap exited children; True while any child of the launcher is alive."""
    return _reap_all()[1]


def _cleanup_tree():
    """The worker is gone: kill whatever it left behind and reap it.

    Returns ``True`` when nothing is left, which is only established when
    ``waitpid`` reports no children: a listing of ``/proc`` can miss a process
    (``hidepid``, an undumpable descendant), so an empty listing alone never
    ends the loop. Running out of time with a descendant still alive returns
    ``False``, and the caller reports it: a launcher that exits quietly over a
    live descendant would hide exactly the leak this loop exists to prevent.
    """
    return _kill_tree()[1]


def _forward_child_report(report_r):
    """Pass the child's failure record, if it left one, on to the host."""
    chunks = []
    try:
        while True:
            data = os.read(report_r, 4096)
            if not data:
                break
            chunks.append(data)
    except OSError:
        pass
    if chunks:
        try:
            record = json.loads(b"".join(chunks).decode("utf-8", "replace"))
            _report(record.get("kind", "sandbox"), record.get("message", ""))
        except ValueError:
            _report("sandbox", "worker start failed")


def main(argv):
    global _STATUS_FD
    try:
        spec_fd = int(argv[1])
        lifeline_fd = int(argv[2])
        _STATUS_FD = int(argv[3])
        # A writer that blocks on a full pipe would keep the launcher from exiting.
        os.set_blocking(_STATUS_FD, False)
    except (IndexError, ValueError, OSError):
        _say("usage: launcher.py SPEC_FD LIFELINE_FD STATUS_FD")
        return EXIT_BAD_SPEC
    try:
        spec = _read_spec(spec_fd)
    except (OSError, ValueError) as exc:
        _say("bad spec: %s" % exc)
        _report("spec", "bad spec: %s" % exc)
        _report_cleanup("complete")  # nothing was started
        return EXIT_BAD_SPEC
    try:
        landlock = _load_landlock()
        seccomp = _load_sibling("seccomp")
        capabilities = _load_sibling("capabilities")
        landlock.prctl(landlock.PR_SET_CHILD_SUBREAPER, 1)
        # The status pipe is the launcher's word to the host. A non-dumpable
        # process is closed to its own user's other processes: /proc/<pid>/fd
        # and the descriptors in it, the pipe included, cannot be opened by the
        # worker, however it finds the number. The worker, which is exec'd
        # afterwards, is dumpable again as exec always makes it. Verified, not
        # assumed: a launcher that cannot close itself off refuses to start.
        landlock.prctl(landlock.PR_SET_DUMPABLE, 0)
        if landlock.prctl(landlock.PR_GET_DUMPABLE) != 0:
            raise OSError("the launcher could not make itself non-dumpable")
    except Exception as exc:
        _say("launcher setup failed: %s" % exc)
        _report("sandbox", "launcher setup failed: %s" % exc)
        _report_cleanup("complete")
        return EXIT_SANDBOX_FAILED

    # A launcher that cannot see its own tree cannot clean it up: refuse to
    # start rather than run a worker it could not stop.
    try:
        _children_map()
    except EnumerationError as exc:
        _say("cannot list processes: %s" % exc)
        _report("sandbox", "cannot list processes: %s" % exc)
        _report_cleanup("complete")
        return EXIT_SANDBOX_FAILED

    unsafe = _tree_tracking_error()
    if unsafe:
        _say(unsafe)
        _report("sandbox", unsafe)
        _report_cleanup("complete")
        return EXIT_SANDBOX_FAILED

    # Close-on-exec by default (os.pipe): the report's write end vanishes on a
    # good execve. A table with no room left for them is a sandbox failure the
    # host hears about, not a traceback, and nothing opened so far is leaked.
    opened = []
    try:
        wake_r, wake_w = os.pipe()
        opened += [wake_r, wake_w]
        report_r, report_w = os.pipe()
        opened += [report_r, report_w]
    except OSError as exc:
        for fd in opened:
            os.close(fd)
        _say("launcher could not create its pipes: %s" % exc)
        _report("sandbox", "launcher could not create its pipes: %s" % exc)
        _report_cleanup("complete")
        return EXIT_SANDBOX_FAILED
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
    for sig in (*_TERMINATING, signal.SIGCHLD):
        signal.signal(sig, lambda _s, _f: None)

    parent_pid = os.getpid()
    # One thread here, so this fork is safe. The host never forks itself.
    try:
        pid = os.fork()
    except OSError as exc:
        # Out of processes or memory: nothing was created, so there is nothing to
        # clean up, and the host is told so instead of being left to guess.
        _say("launcher could not fork the worker: %s" % exc)
        _report("sandbox", "launcher could not fork the worker: %s" % exc)
        _report_cleanup("complete")
        try:
            signal.set_wakeup_fd(-1)
        except (ValueError, OSError):
            pass
        for fd in (wake_r, wake_w, report_r, report_w):
            try:
                os.close(fd)
            except OSError:
                pass
        return EXIT_SANDBOX_FAILED
    if pid == 0:
        _child(spec, landlock, seccomp, capabilities, parent_pid, (lifeline_fd, wake_r, wake_w, report_r, _STATUS_FD), report_w)
    os.close(report_w)
    try:
        status = _supervise(pid, wake_r, lifeline_fd, float(spec.get("term_grace", DEFAULT_TERM_GRACE)))
        _forward_child_report(report_r)
        if not _cleanup_tree():
            _say("a descendant of the worker survived the cleanup")
            _report("sandbox", "a descendant of the worker survived the launcher's cleanup")
            _report_cleanup("survivors", _survivor_count())
            return EXIT_SANDBOX_FAILED
        _report_cleanup("complete")
    except BaseException:
        # No signal by the worker's pid here: it may be reaped already. It is
        # one of the children, so the cleanup reaches it when it is not.
        try:
            survived = not _cleanup_tree()
        except Exception:
            survived = True
        if survived:
            _report("sandbox", "a descendant of the worker may have survived the launcher's cleanup")
            _report_cleanup("survivors", _survivor_count())
        else:
            _report_cleanup("complete")
        raise
    return _exit_code(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
