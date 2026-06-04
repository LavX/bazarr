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
