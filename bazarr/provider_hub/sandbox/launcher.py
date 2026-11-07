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
   the write end of the ``lifeline_fd`` pipe; end-of-file there means the host
   died. ``status_fd`` is the write end of a pipe the host reads: the launcher
   puts one JSON line on it when the worker could not be started, and nothing
   otherwise.
2. Become a child subreaper, then fork once. The launcher stays behind as the
   reaper. When the worker exits, when the host dies (lifeline EOF) or on
   SIGTERM, every descendant is killed, ``setsid`` children included.
3. The child sets the parent-death signal, core size 0, open-file and
   file-size limits, nice, ``oom_score_adj``, umask, ``no_new_privs``, a
   every Linux capability dropped (see ``capabilities.py``), a seccomp filter
   that denies chmod, chown, utime and xattr calls, file attribute ioctls,
   keyrings and changes to the scheduling of other processes (see
   ``seccomp.py``), changes to the working directory, applies the ruleset and
   ``execve``s the worker
   interpreter with exactly the environment in the spec.

Exit codes: the worker's own code (128 + signal when it was killed by one);
64 for a bad spec or working directory; 78 when the sandbox could not be
applied (the worker never ran); 127 when ``execve`` failed. The exit code alone
proves nothing, since a worker can exit with any code and print any line: the
authoritative record is the JSON line on ``status_fd``, written by the launcher
(``{"kind": "spec" | "sandbox" | "exec", "message": "..."}``). The child reports
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
      "term_grace": 3.0
    }

In ``declared`` mode no ruleset is applied; limits, ``no_new_privs``, the
clean environment and, where the architecture is supported, the metadata
filter still are.
"""
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


def _report(kind, message):
    """Tell the host why the worker could not be started."""
    if _STATUS_FD is None:
        return
    line = json.dumps({"kind": kind, "message": str(message)[:2000]}) + "\n"
    try:
        os.write(_STATUS_FD, line.encode("utf-8", "replace"))
    except OSError:
        pass


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
        landlock.prctl(landlock.PR_SET_PDEATHSIG, int(signal.SIGKILL))
        if os.getppid() != parent_pid:
            os._exit(EXIT_SANDBOX_FAILED)
        _apply_limits(spec.get("limits") or {})
        landlock.prctl(landlock.PR_SET_NO_NEW_PRIVS, 1)
        # no_new_privs keeps an exec from gaining privilege but not from keeping
        # it: a host running as root would hand its capabilities to the worker.
        # This goes before the seccomp filter, which is then free to deny calls
        # the drop itself needs. Required when enforced; best effort otherwise.
        if spec.get("mode", "enforce") == "enforce":
            capabilities.drop_all()
        else:
            try:
                capabilities.drop_all()
            except OSError as exc:
                _say("capabilities not dropped: %s" % exc)
        # chmod, chown, utime and xattr calls cannot be scoped by Landlock or by
        # a path-blind filter, so they are denied outright. Required when the
        # sandbox is enforced; best effort on a host that only declares.
        if spec.get("mode", "enforce") == "enforce":
            seccomp.apply_filter()
        else:
            try:
                seccomp.apply_filter()
            except OSError as exc:
                _say("metadata filter not applied: %s" % exc)
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
            os.write(report_fd, json.dumps({"kind": kind, "message": message[:2000]}).encode("utf-8", "replace"))
        except OSError:
            pass
    os._exit(code)


class EnumerationError(Exception):
    """The process tree could not be listed. Never the same thing as "empty"."""


def _children_map():
    """Map pid to ppid for every process visible in /proc.

    A process that exits while it is being read is normal and skipped. Any
    other failure (out of file descriptors, /proc missing or unreadable) raises
    ``EnumerationError``: a scan that silently saw nothing would let detached
    children outlive their worker. Our own entry must be in the result.
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
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as exc:
            raise EnumerationError("cannot read /proc/%s/stat: %s" % (name, exc))
        except (ValueError, IndexError):
            raise EnumerationError("unreadable /proc/%s/stat" % name)
        result[int(name)] = ppid
    if os.getpid() not in result:
        raise EnumerationError("the launcher is missing from /proc")
    return result


def _descendants(root):
    parents = _children_map()
    found = []
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == current and pid not in found:
                found.append(pid)
                frontier.append(pid)
    return found


def _emergency_kill(reason):
    """Last resort when the tree cannot be listed: kill the whole process group.

    The launcher leads its own group and session (the host starts it with
    ``start_new_session``), so this reaches everything that did not leave it
    and also ends the launcher, which then reports the reason. A descendant that
    called ``setsid`` is out of reach without a listing; that is the cost of
    failing safe, and the reason says so.
    """
    _report("sandbox", "launcher could not list its process tree (%s); killed its process group" % reason)
    _say("cannot list the process tree (%s); killing the process group" % reason)
    # Only when it really leads the group: a launcher run by hand inside a
    # shell's group must not take the shell down. The worker has a parent-death
    # signal, so it still dies when this process exits.
    if os.getpgrp() == os.getpid():
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except OSError:
            pass
    os._exit(EXIT_SANDBOX_FAILED)


def _freeze_descendants(root):
    """SIGSTOP the tree until a scan finds nobody new; return the final list.

    A stopped process cannot fork, so a worker that forks while the launcher
    scans is outrun after a few passes instead of being chased forever. The
    caller kills what this returns.
    """
    seen = []
    for _ in range(10):
        pids = _descendants(root)
        fresh = [pid for pid in pids if pid not in seen]
        for pid in fresh:
            try:
                os.kill(pid, signal.SIGSTOP)
            except OSError:
                pass
        seen.extend(fresh)
        if not fresh:
            break
    return seen


def _kill_descendants(sig=signal.SIGKILL):
    try:
        pids = _freeze_descendants(os.getpid()) if sig == signal.SIGKILL else _descendants(os.getpid())
        if sig == signal.SIGKILL:
            # Anything that appeared after the last freeze pass dies here too.
            pids = list(dict.fromkeys([*pids, *_descendants(os.getpid())]))
    except EnumerationError as exc:
        _emergency_kill(exc)
        return
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _reap(main_pid):
    """Reap every exited child. Returns the main child's status or None."""
    status = None
    while True:
        try:
            pid, st = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return status
        if pid == 0:
            return status
        if pid == main_pid:
            status = st


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
                    _kill_descendants()
                    try:
                        os.kill(main_pid, signal.SIGKILL)
                    except OSError:
                        pass
        status = _reap(main_pid)
        if status is None and deadline is not None and time.monotonic() >= deadline:
            _kill_descendants()
            try:
                os.kill(main_pid, signal.SIGKILL)
            except OSError:
                pass
            deadline = time.monotonic() + 60
    return status


CLEANUP_SECONDS = 7.0


def _cleanup_tree():
    """The worker is gone: kill whatever it left behind and reap it.

    Returns ``True`` when nothing is left. Running out of time with a
    descendant still alive returns ``False``, and the caller reports it: a
    launcher that exits quietly over a live descendant would hide exactly the
    leak this loop exists to prevent.
    """
    end = time.monotonic() + CLEANUP_SECONDS
    while time.monotonic() < end:
        try:
            left = _descendants(os.getpid())
        except EnumerationError as exc:
            _emergency_kill(exc)
            return False
        if not left:
            return True
        _kill_descendants()
        time.sleep(0.02)
        _reap(-1)
    try:
        return not _descendants(os.getpid())
    except EnumerationError as exc:
        _emergency_kill(exc)
        return False


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
    except (IndexError, ValueError):
        _say("usage: launcher.py SPEC_FD LIFELINE_FD STATUS_FD")
        return EXIT_BAD_SPEC
    try:
        spec = _read_spec(spec_fd)
    except (OSError, ValueError) as exc:
        _say("bad spec: %s" % exc)
        _report("spec", "bad spec: %s" % exc)
        return EXIT_BAD_SPEC
    try:
        landlock = _load_landlock()
        seccomp = _load_sibling("seccomp")
        capabilities = _load_sibling("capabilities")
        landlock.prctl(landlock.PR_SET_CHILD_SUBREAPER, 1)
    except Exception as exc:
        _say("launcher setup failed: %s" % exc)
        _report("sandbox", "launcher setup failed: %s" % exc)
        return EXIT_SANDBOX_FAILED

    # A launcher that cannot see its own tree cannot clean it up: refuse to
    # start rather than run a worker it could not stop.
    try:
        _children_map()
    except EnumerationError as exc:
        _say("cannot list processes: %s" % exc)
        _report("sandbox", "cannot list processes: %s" % exc)
        return EXIT_SANDBOX_FAILED

    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
    for sig in (*_TERMINATING, signal.SIGCHLD):
        signal.signal(sig, lambda _s, _f: None)

    # Close-on-exec by default (os.pipe): the write end vanishes on a good execve.
    report_r, report_w = os.pipe()
    parent_pid = os.getpid()
    # One thread here, so this fork is safe. The host never forks itself.
    pid = os.fork()
    if pid == 0:
        _child(spec, landlock, seccomp, capabilities, parent_pid, (lifeline_fd, wake_r, wake_w, report_r, _STATUS_FD), report_w)
    os.close(report_w)
    try:
        status = _supervise(pid, wake_r, lifeline_fd, float(spec.get("term_grace", DEFAULT_TERM_GRACE)))
        _forward_child_report(report_r)
        if not _cleanup_tree():
            _say("a descendant of the worker survived the cleanup")
            _report("sandbox", "a descendant of the worker survived the launcher's cleanup")
            return EXIT_SANDBOX_FAILED
    except BaseException:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        _kill_descendants()
        raise
    return _exit_code(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
