# coding=utf-8
"""The vendored plugin SDK that workers put on their path.

The copy under ``provider_hub/sdk_dist`` must match its manifest, its wire
protocol files must match the host's, it must use only the standard library,
and a plugin must run on it in an isolated interpreter the way a worker does.
"""
import ast
import importlib.util
import json
import re
import shutil
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
    assert host_file.is_file(), f"the host's runtime/{name} is missing"
    assert (PACKAGE / "_protocol" / name).read_bytes() == host_file.read_bytes()


def test_the_manifest_names_the_version_and_a_tag_only_when_there_is_one():
    manifest = json.loads((DIST / "VENDORED.json").read_text(encoding="utf-8"))
    init = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    assert manifest["package"] == "bazarr_plugin_sdk"
    assert f'__version__ = "{manifest["version"]}"' in init
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"])
    # a tag is recorded only for a source checkout that is exactly at it
    assert manifest["tag"] is None or isinstance(manifest["tag"], str) and manifest["tag"]


def test_the_check_fails_when_the_host_protocol_files_are_missing(tmp_path, monkeypatch):
    script = _load_script()
    monkeypatch.setattr(script, "RUNTIME", tmp_path)
    problems = script.check(None)
    assert any("runtime/frames.py is missing" in problem for problem in problems)
    assert any("runtime/worker_channel.py is missing" in problem for problem in problems)


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _source_checkout(tmp_path):
    source = tmp_path / "sdk"
    shutil.copytree(PACKAGE, source / "src" / "bazarr_plugin_sdk", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    _git(source, "init", "-q")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "sdk")
    return source


def _vendor_into(script, monkeypatch, tmp_path):
    dist = tmp_path / "sdk_dist"
    dist.mkdir()
    monkeypatch.setattr(script, "DIST", dist)
    monkeypatch.setattr(script, "MANIFEST", dist / "VENDORED.json")
    return dist


def test_the_manifest_tag_is_null_without_a_tag_and_set_at_one(tmp_path, monkeypatch):
    script = _load_script()
    source = _source_checkout(tmp_path)
    dist = _vendor_into(script, monkeypatch, tmp_path)
    script.vendor(source)
    manifest = json.loads((dist / "VENDORED.json").read_text(encoding="utf-8"))
    assert manifest["tag"] is None and manifest["source_commit"] == script.git_commit(source)
    _git(source, "tag", "v9.9.9")
    script.vendor(source)
    assert json.loads((dist / "VENDORED.json").read_text(encoding="utf-8"))["tag"] == "v9.9.9"


def test_vendoring_refuses_an_ignored_file_in_the_source(tmp_path, monkeypatch):
    script = _load_script()
    source = tmp_path / "sdk"
    shutil.copytree(PACKAGE, source / "src" / "bazarr_plugin_sdk", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (source / ".gitignore").write_text("local_only.py\n", encoding="utf-8")
    _git(source, "init", "-q")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "sdk")
    dist = _vendor_into(script, monkeypatch, tmp_path)
    (source / "src" / "bazarr_plugin_sdk" / "local_only.py").write_text("secret = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        script.vendor(source)
    assert not (dist / "VENDORED.json").exists()


def test_only_the_files_committed_at_the_source_commit_are_vendored(tmp_path):
    script = _load_script()
    source = _source_checkout(tmp_path)
    package = source / "src" / "bazarr_plugin_sdk"
    committed = (package / "errors.py").read_bytes()
    (package / "errors.py").write_text("# edited after the commit\n", encoding="utf-8")
    (package / "untracked.py").write_text("", encoding="utf-8")
    files = script.committed_files(source)
    assert files["errors.py"] == committed and "untracked.py" not in files
    assert set(files) == {path.relative_to(PACKAGE).as_posix() for path in PACKAGE.rglob("*") if path.is_file() and "__pycache__" not in path.parts}


def test_the_check_names_a_source_that_is_not_the_manifests_commit(tmp_path, monkeypatch):
    script = _load_script()
    source = _source_checkout(tmp_path)
    dist = _vendor_into(script, monkeypatch, tmp_path)
    script.vendor(source)
    monkeypatch.setattr(script, "RUNTIME", tmp_path / "no-runtime")
    assert not [p for p in script.check(source) if "manifest names" in p or "differs" in p]
    (source / "extra.txt").write_text("x", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "more")
    assert any("the manifest names" in problem for problem in script.check(source))
    assert dist.exists()


def test_vendoring_refuses_a_dirty_source_tree(tmp_path, monkeypatch):
    script = _load_script()
    source = _source_checkout(tmp_path)
    dist = _vendor_into(script, monkeypatch, tmp_path)
    (source / "src" / "bazarr_plugin_sdk" / "errors.py").write_text("# edited\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        script.vendor(source)
    (source / "src" / "bazarr_plugin_sdk" / "errors.py").write_bytes((PACKAGE / "errors.py").read_bytes())
    (source / "src" / "bazarr_plugin_sdk" / "stray.py").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        script.vendor(source)
    assert not (dist / "VENDORED.json").exists()


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
    assert last.startswith("OK structure") and "cancel" in last[3:].split(","), result.stdout
