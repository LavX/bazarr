# coding=utf-8
"""The vendored plugin SDK that workers put on their path.

The copy under ``provider_hub/sdk_dist`` must match its manifest, its wire
protocol files must match the host's, it must use only the standard library,
and a plugin must run on it in an isolated interpreter the way a worker does.
"""
import ast
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "bazarr" / "provider_hub" / "sdk_dist"
PACKAGE = DIST / "bazarr_plugin_sdk"
RUNTIME = ROOT / "bazarr" / "provider_hub" / "runtime"


def _load_script():
    spec = importlib.util.spec_from_file_location("vendor_plugin_sdk", ROOT / "scripts" / "vendor_plugin_sdk.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_copy_matches_its_manifest():
    assert _load_script().check(None) == []


def test_the_check_notices_a_changed_added_and_removed_file(tmp_path, monkeypatch):
    script = _load_script()
    copy = tmp_path / "sdk_dist"
    import shutil

    shutil.copytree(DIST, copy)
    monkeypatch.setattr(script, "DIST", copy)
    monkeypatch.setattr(script, "MANIFEST", copy / "VENDORED.json")
    assert script.check(None) == []
    (copy / "bazarr_plugin_sdk" / "errors.py").write_text("# edited\n", encoding="utf-8")
    (copy / "bazarr_plugin_sdk" / "extra.py").write_text("", encoding="utf-8")
    (copy / "bazarr_plugin_sdk" / "kinds.py").unlink()
    problems = script.check(None)
    assert "errors.py differs from the manifest" in problems
    assert "extra.py is in the copy but not in the manifest" in problems
    assert "kinds.py is in the manifest but missing from the copy" in problems


@pytest.mark.parametrize("name", ["frames.py", "worker_channel.py"])
def test_the_wire_protocol_files_equal_the_host_copies(name):
    host_file = RUNTIME / name
    if not host_file.is_file():
        pytest.skip("the host's worker protocol is not on this branch yet")
    assert (PACKAGE / "_protocol" / name).read_bytes() == host_file.read_bytes()


def test_the_manifest_names_the_tag_and_version():
    manifest = json.loads((DIST / "VENDORED.json").read_text(encoding="utf-8"))
    init = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    assert manifest["package"] == "bazarr_plugin_sdk"
    assert f'__version__ = "{manifest["version"]}"' in init
    assert manifest["tag"] == f"v{manifest['version']}"


def test_the_sdk_uses_only_the_standard_library():
    stdlib = set(sys.stdlib_module_names)
    # worker_channel.py falls back to a plain `import frames` when it is run as a script
    # beside frames.py; inside the package the relative import is the one that applies.
    allowed = stdlib | {"bazarr_plugin_sdk", "frames"}
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                assert name in allowed, f"{path.name} imports {name}"


PLUGIN = '''
import asyncio

from bazarr_plugin_sdk.translator.v1 import TranslatorPlugin


class Plugin(TranslatorPlugin):
    def describe(self):
        return {"backend_types": ["probe"]}

    async def test(self, config, secrets, ctx=None):
        return {"ok": True}

    async def translate(self, request, ctx):
        hold = await ctx.reserve(0.1, "m")
        for step in range(20):
            await asyncio.sleep(0.05)
            ctx.progress(step, 20)
        await ctx.usage(1, 0.1, "m", [hold])
        print("stdout noise that must not reach the frames")
        return {"lines": request["lines"]}
'''

PROBE = '''
import sys

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from bazarr_plugin_sdk.testing import ProcessHost, run_conformance
import probe_plugin

passed = run_conformance(
    probe_plugin.Plugin,
    kind_api="bazarr.translator.v1",
    samples={"translate": {"args": {"request": {"lines": ["a"]}}}},
    entry="probe_plugin:Plugin",
    paths=(sys.argv[2],),
)
with ProcessHost("probe_plugin:Plugin", paths=[sys.argv[2]], sdk_root=sys.argv[1]) as host:
    result = host.request("translate", {"request": {"lines": ["x", "y"]}}).value
    assert result == {"lines": ["x", "y"]}, result
print("OK", ",".join(passed))
'''


def test_a_plugin_runs_on_the_vendored_copy_in_an_isolated_interpreter(tmp_path):
    (tmp_path / "probe_plugin.py").write_text(textwrap.dedent(PLUGIN), encoding="utf-8")
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent(PROBE), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(probe), str(DIST), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    last = result.stdout.strip().splitlines()[-1]
    assert last.startswith("OK structure") and "cancel" in last, result.stdout
