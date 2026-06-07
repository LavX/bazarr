"""Provider Hub venv creation must be serialized process-wide.

A large catalog update can trigger many provider installs at once (across the jobs queue /
Flask request threads). Each install builds a venv (python -m venv + pip install), and a
burst of concurrent builds can blow past a container memory limit and trigger the OOM
killer. PluginEnvironment.install() serializes the heavy subprocess work behind a
module-level lock so at most one venv builds at a time, bounding peak memory regardless of
how many callers fan out.
"""
import threading
import time
from types import SimpleNamespace

import provider_hub.venv as venvmod


def test_install_serializes_concurrent_venv_creation(tmp_path, monkeypatch):
    active = {"n": 0, "max": 0}
    track_lock = threading.Lock()
    errors = []

    def fake_run(cmd, **kwargs):
        with track_lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.05)
        with track_lock:
            active["n"] -= 1
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(venvmod.subprocess, "run", fake_run)
    # Force the venv-creation branch (treat the venv python as missing).
    monkeypatch.setattr(venvmod, "python_executable", lambda p: tmp_path / "missing-python")

    def make_manifest(pid):
        return SimpleNamespace(provider_id=pid, version="1.0.0", dependency_requirements=())

    env = venvmod.PluginEnvironment(tmp_path)

    def run_install(i):
        try:
            env.install(make_manifest(f"p{i}"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run_install, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"install raised: {errors}"
    assert active["max"] == 1, f"venv creation must be serialized (saw {active['max']} concurrent)"
