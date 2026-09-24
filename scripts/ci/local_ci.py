#!/usr/bin/env python3
# coding=utf-8
"""Run the pull request pipeline from .github/workflows/ci.yml on this machine.

Started by scripts/ci/run-local.sh, which prepares the virtualenv, node_modules
and a throwaway PostgreSQL first. The suites are read out of ci.yml rather than
listed here, so the two cannot drift apart: every `run:` step of every backend
job is run as written, except the setup steps the virtualenv already covers,
and the per-file isolation loops are unrolled so their files run in parallel,
each in its own pytest process and against its own database.

The frontend runs the steps of the Frontend build and Frontend checks jobs as
written, and the unit tests as one `vitest run --coverage`, which is the same
tests and the same coverage floor the sharded CI jobs check after merging.
"""

import argparse
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# Rough first-run estimates in seconds, so the longest work starts first. Every
# run records what each task really took and later runs use that instead.
DEFAULT_ESTIMATES = {"loop": 30.0, "step": 120.0, "vitest": 300.0}
# The step every backend task waits for, as CI's backend jobs wait for the job.
UI_BUILD = "Frontend build: Build"


@dataclass
class Task:
    group: str
    label: str
    argv: list
    cwd: Path = ROOT
    env: dict = field(default_factory=dict)
    weight: int = 1
    needs_db: bool = False
    after: str = ""
    kind: str = "step"
    key: str = ""
    # Filled in while running.
    process: object = None
    log: Path = None
    database: str = ""
    started: float = 0.0
    finished: float = 0.0
    returncode: int = None

    @property
    def passed(self):
        return self.returncode == 0


def _job_name(job_id: str, job: dict, python: str) -> str:
    name = str(job.get("name") or job_id)
    name = name.replace("${{ matrix.python-version }}", python)
    return re.sub(r"\s*\(Python [^)]*\)$", "", name).strip()


def _logical_lines(script: str) -> list:
    lines, pending = [], ""
    for raw in script.splitlines():
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        lines.append(pending + stripped)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def _unroll_loop(script: str) -> tuple:
    """(files, pytest argv template) of a `for f in ...; do pytest "$f" ...; done` step."""
    files, template, variable = [], None, None
    for line in _logical_lines(script):
        tokens = shlex.split(line)
        if tokens and tokens[0] == "for":
            variable = tokens[1]
            files = [token for token in tokens[3:] if token not in {";", "do"}]
            files = [token.rstrip(";") for token in files if token.rstrip(";")]
        elif tokens and tokens[0] == "pytest" and variable:
            template = tokens
    if not files or not template:
        raise SystemExit(f"cannot unroll the loop in:\n{script}")
    return files, template, "$" + variable


def _is_setup(script: str) -> bool:
    """Steps the virtualenv and the system packages already stand for."""
    first = _logical_lines(script)[0] if _logical_lines(script) else ""
    return first.startswith(("sudo apt-get", "pip install -r", "npm install", "npm ci"))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def build_tasks(args, workflow: dict, work: Path) -> list:
    jobs = workflow["jobs"]
    tasks = []
    ui = UI_BUILD
    frontend_dir = ROOT / "frontend"

    for job_id in ("frontend-build", "frontend-checks"):
        if job_id == "frontend-checks" and args.mode == "backend":
            continue
        job = jobs[job_id]
        group = _job_name(job_id, job, args.python)
        for step in job.get("steps") or []:
            script = step.get("run")
            if not isinstance(script, str) or _is_setup(script):
                continue
            name = step.get("name") or "step"
            tasks.append(Task(group, name, ["bash", "-e", "-c", script],
                              cwd=frontend_dir, key=f"{group}: {name}"))
    if not any(task.key == ui for task in tasks):
        raise SystemExit(f"ci.yml has no step called {ui!r} any more; update {__file__}")

    if args.mode != "backend":
        tasks.append(Task(
            "Frontend tests and coverage",
            "vitest run --coverage",
            ["npx", "vitest", "run", "--coverage", f"--maxWorkers={args.vitest_workers}"],
            cwd=frontend_dir, weight=args.vitest_workers, kind="vitest",
            key="vitest",
        ))

    if args.mode != "frontend":
        venv_bin = str(Path(args.venv) / "bin")
        base_env = {
            "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
            "VIRTUAL_ENV": args.venv,
            "ROOT_DIRECTORY": str(ROOT),
            "UI_DIRECTORY": str(frontend_dir),
            "SCRIPTS_DIRECTORY": str(ROOT / ".github" / "scripts"),
        }
        for job_id, job in jobs.items():
            if not job_id.startswith("backend-"):
                continue
            group = _job_name(job_id, job, args.python)
            for step in job.get("steps") or []:
                script = step.get("run")
                if not isinstance(script, str) or _is_setup(script):
                    continue
                env = dict(base_env)
                env.update({str(k): str(v) for k, v in (step.get("env") or {}).items()})
                needs_db = "BAZARR_PG_TEST_URL" in env
                name = step.get("name") or "step"
                if "build_test.sh" in script:
                    env["BAZARR_SMOKE_PORT"] = str(_free_port())
                    env["BAZARR_SMOKE_CONFIG"] = str(work / "startup-config")
                    tasks.append(Task(group, name, ["bash", str(ROOT / ".github/scripts/build_test.sh")],
                                      env=env, after=ui, key=f"{group}: {name}"))
                elif re.search(r"^\s*for\s", script, flags=re.MULTILINE):
                    files, template, reference = _unroll_loop(script)
                    for path in files:
                        argv = [path if token == reference else token for token in template]
                        tasks.append(Task(group, path, argv, env=env, needs_db=needs_db,
                                          after=ui, kind="loop", key=path))
                else:
                    tasks.append(Task(group, name, ["bash", "-e", "-c", script], env=env,
                                      needs_db=needs_db, after=ui, key=f"{group}: {name}"))

    if args.mode == "all":
        docs_env = {"PATH": os.environ.get("PATH", "")}
        base = args.docs_base
        tasks.append(Task("Docs", "docs_check.py",
                          [sys.executable, str(ROOT / ".github/scripts/docs_check.py"), "--base", base],
                          env=docs_env, key="docs"))
    return tasks


def _estimate(task: Task, history: dict) -> float:
    if task.key == UI_BUILD:
        return float("inf")  # everything in the backend waits for it
    return history.get(task.key, DEFAULT_ESTIMATES.get(task.kind, 60.0))


def run(tasks: list, args, logs: Path, history: dict) -> None:
    capacity = args.jobs
    databases = [f"bazarr_test_{index}" for index in range(1, args.databases + 1)]
    pending = sorted(tasks, key=lambda task: -_estimate(task, history))
    running = []
    done = set()
    failed_prerequisite = set()
    counter = 0

    def launch(task):
        nonlocal counter
        counter += 1
        env = dict(os.environ)
        env.update(task.env)
        if task.needs_db:
            task.database = databases.pop()
            env["BAZARR_PG_TEST_URL"] = (
                f"postgresql+psycopg://postgres:postgres@127.0.0.1:{args.pg_port}/{task.database}"
            )
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{task.group}-{task.label}")[:150]
        task.log = logs / f"{counter:03d}-{safe}.log"
        handle = open(task.log, "w")
        handle.write(f"$ {shlex.join(task.argv)}\n(cwd {task.cwd})\n\n")
        handle.flush()
        task.started = time.monotonic()
        task.process = subprocess.Popen(
            task.argv, cwd=task.cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        task.process._log_handle = handle
        running.append(task)

    def stop_all(signum=None, frame=None):
        for task in running:
            try:
                os.killpg(task.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_all)
    try:
        while pending or running:
            used = sum(task.weight for task in running)
            for task in list(pending):
                if task.after and task.after in failed_prerequisite:
                    pending.remove(task)
                    task.returncode = -1
                    task.log = None
                    continue
                if task.after and task.after not in done:
                    continue
                weight = min(task.weight, capacity)
                if used + weight > capacity:
                    continue
                pending.remove(task)
                launch(task)
                used += weight
            time.sleep(0.2)
            for task in list(running):
                code = task.process.poll()
                if code is None:
                    if time.monotonic() - task.started > args.timeout:
                        os.killpg(task.process.pid, signal.SIGKILL)
                    continue
                task.returncode = code
                task.finished = time.monotonic()
                task.process._log_handle.close()
                running.remove(task)
                if task.database:
                    databases.append(task.database)
                if code == 0:
                    done.add(task.key)
                else:
                    failed_prerequisite.add(task.key)
                mark = "ok  " if code == 0 else "FAIL"
                print(f"  {mark} {task.finished - task.started:6.1f}s  {task.group}: {task.label}",
                      flush=True)
    except KeyboardInterrupt:
        for task in running:
            try:
                os.killpg(task.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise


def summarise(tasks: list, total: float) -> bool:
    groups = {}
    for task in tasks:
        groups.setdefault(task.group, []).append(task)
    rows = []
    for group, members in groups.items():
        started = [task.started for task in members if task.started]
        finished = [task.finished for task in members if task.finished]
        wall = (max(finished) - min(started)) if started and finished else 0.0
        failed = [task for task in members if not task.passed]
        detail = f"{len(members)} {'files' if members[0].kind == 'loop' else 'steps'}"
        if failed:
            names = ", ".join(task.label if task.log else task.label + " (not started)"
                              for task in failed)
            detail += f", failed: {names}"
        rows.append((group, "FAIL" if failed else "pass", f"{wall:6.1f}s", detail))
    width = max(len(row[0]) for row in rows)
    print("\n" + "Group".ljust(width) + "  Result  Time     Detail")
    print("-" * (width + 40))
    for group, result, wall, detail in rows:
        print(f"{group.ljust(width)}  {result:6}  {wall}  {detail}")
    minutes, seconds = divmod(int(round(total)), 60)
    print("-" * (width + 40))
    ok = all(task.passed for task in tasks)
    print(f"{'PASS' if ok else 'FAIL'}: {len(tasks)} tasks in {minutes}m {seconds:02d}s wall time")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["all", "backend", "frontend"], default="all")
    parser.add_argument("--python", required=True)
    parser.add_argument("--venv", default="")
    parser.add_argument("--pg-port", default="")
    parser.add_argument("--databases", type=int, default=0)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--vitest-workers", type=int, default=max(2, (os.cpu_count() or 4) // 3))
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--logs", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--history", default="")
    parser.add_argument("--docs-base", default="origin/development")
    args = parser.parse_args()

    workflow = yaml.safe_load(WORKFLOW.read_text())
    logs = Path(args.logs)
    logs.mkdir(parents=True, exist_ok=True)
    history = {}
    if args.history:
        try:
            history = json.loads(Path(args.history).read_text())
        except (OSError, ValueError):
            history = {}

    tasks = build_tasks(args, workflow, Path(args.work))
    if args.mode != "frontend" and args.databases < 1:
        raise SystemExit("the backend needs --databases and --pg-port")
    print(f"Running {len(tasks)} tasks, {args.jobs} at a time, Python {args.python}. "
          f"Logs: {logs}", flush=True)
    began = time.monotonic()
    try:
        run(tasks, args, logs, history)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    total = time.monotonic() - began

    if args.history:
        for task in tasks:
            if task.passed and task.finished:
                history[task.key] = round(task.finished - task.started, 1)
        try:
            Path(args.history).write_text(json.dumps(history, indent=1, sort_keys=True))
        except OSError:
            pass

    ok = summarise(tasks, total)
    for task in tasks:
        if not task.passed and task.log:
            print(f"\n==== {task.group}: {task.label} (exit {task.returncode}), {task.log}")
            lines = task.log.read_text(errors="replace").splitlines()
            print("\n".join(lines[-40:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
