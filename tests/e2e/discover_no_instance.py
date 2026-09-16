#!/usr/bin/env python3
"""Boot a Bazarr+ with nothing connected and drive Discover in a browser.

This exists because the case it covers cannot be seen in a unit test: an
install with no Sonarr, Radarr or Sportarr has no library half to render, and
whether the page opens on the global catalog instead is a question about the
real backend's summary and the built frontend, not about a fixture.

It boots the backend in this checkout on a throwaway configuration directory
and a free port, runs tests/e2e/discover_no_instance.mjs against it, and tears
the backend down again. Nothing outside the temporary directory is touched, so
it is safe to run beside a Bazarr+ that is already running.

    python3 tests/e2e/discover_no_instance.py

The frontend has to be built first (the backend serves frontend/build):

    cd frontend && npm install && npm run build

Playwright is not a dependency of this project. Point the walk at an install of
it with --playwright-modules, or set PLAYWRIGHT_NODE_PATH:

    npm install playwright --no-save --prefix /tmp/pw
    npx --prefix /tmp/pw playwright install chromium
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WALK = Path(__file__).with_suffix(".mjs")
BOOT_TIMEOUT_SECONDS = 180


def free_port() -> int:
    """A port the kernel just handed out, so parallel runs do not collide."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_app(base: str, process: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(
                f"the backend exited while starting ({process.returncode}); see {log}"
            )
        try:
            with urllib.request.urlopen(base, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    raise SystemExit(f"the backend did not start within {BOOT_TIMEOUT_SECONDS}s; see {log}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=None,
        help="where to write screenshots (default: a temporary directory)",
    )
    parser.add_argument(
        "--playwright-modules",
        default=os.environ.get("PLAYWRIGHT_NODE_PATH"),
        help="node_modules directory holding playwright",
    )
    parser.add_argument(
        "--keep-config",
        action="store_true",
        help="leave the throwaway configuration directory behind for inspection",
    )
    args = parser.parse_args()

    if not (ROOT / "frontend" / "build" / "index.html").is_file():
        raise SystemExit(
            "frontend/build/index.html is missing: run `npm run build` in frontend/ first"
        )

    workspace = Path(tempfile.mkdtemp(prefix="bazarr-discover-e2e-"))
    config = workspace / "config"
    config.mkdir()
    out = Path(args.out) if args.out else workspace / "screenshots"
    log = workspace / "backend.log"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    print(f"config:      {config}")
    print(f"backend log: {log}")
    print(f"screenshots: {out}")
    print(f"serving on:  {base}")

    with log.open("wb") as handle:
        backend = subprocess.Popen(
            [
                sys.executable,
                "bazarr.py",
                "--no-update",
                "--no-tasks",
                "-c",
                str(config),
                "-p",
                str(port),
            ],
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )

    try:
        wait_for_app(base, backend, log)
        environment = dict(os.environ)
        if args.playwright_modules:
            modules = Path(args.playwright_modules)
            if modules.name != "node_modules":
                modules = modules / "node_modules"
            environment["PLAYWRIGHT_NODE_PATH"] = str(modules)
        walk = subprocess.run(
            ["node", str(WALK), base, str(out)],
            cwd=str(ROOT),
            env=environment,
            check=False,
        )
        return walk.returncode
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=30)
        except subprocess.TimeoutExpired:
            backend.kill()
        if args.keep_config:
            print(f"kept {workspace}")


if __name__ == "__main__":
    raise SystemExit(main())
