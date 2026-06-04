import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "bazarr_supervisor", ROOT / "docker" / "supervisor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installing_providers_stage_present_and_ordered():
    sup = _load_supervisor()
    stages = sup.BackendManager.STAGES
    markers = sup.BackendManager._STAGE_MARKERS

    assert "Installing providers" in stages
    install_idx = stages.index("Installing providers")
    # It runs after launch, before checking for updates.
    assert stages.index("Launching process") < install_idx < stages.index("Checking for updates")

    # Marker indices stay within range and increase in declaration order (forward-only).
    idxs = [idx for _, idx in markers]
    assert idxs == sorted(idxs)
    assert all(0 <= idx < len(stages) for _, idx in markers)
    # The auto-install marker maps to the new stage.
    assert any(marker == "Provider Hub startup auto-install" and idx == install_idx
               for marker, idx in markers)


def test_wait_for_ready_returns_when_process_not_alive():
    # The readiness poll waits as long as the backend process is alive (so a long
    # first-boot migration can't leave the startup screen stuck), but must end
    # promptly when there is no live process rather than looping forever.
    sup = _load_supervisor()
    mgr = sup.BackendManager([])
    mgr.state = mgr.STATE_STARTING
    mgr.process = None  # no live backend process

    asyncio.run(asyncio.wait_for(mgr._wait_for_ready(), timeout=5))

    # It never falsely marks RUNNING without a responding backend.
    assert mgr.state == mgr.STATE_STARTING
