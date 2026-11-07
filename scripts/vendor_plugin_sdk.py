#!/usr/bin/env python3
"""Copy the plugin SDK into ``bazarr/provider_hub/sdk_dist`` and check that the copy is intact.

    scripts/vendor_plugin_sdk.py --source ../bazarr-plugin-sdk --tag v0.1.0
        copies <source>/src/bazarr_plugin_sdk and rewrites VENDORED.json
    scripts/vendor_plugin_sdk.py --check [--source ../bazarr-plugin-sdk]
        fails when the files differ from VENDORED.json, and from the source when one is given

The worker protocol files inside the SDK (``_protocol/frames.py`` and
``_protocol/worker_channel.py``) must equal the host's copies in
``bazarr/provider_hub/runtime``; ``--check`` compares them whenever the host
files exist.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "bazarr" / "provider_hub" / "sdk_dist"
PACKAGE = "bazarr_plugin_sdk"
MANIFEST = DIST / "VENDORED.json"
RUNTIME = ROOT / "bazarr" / "provider_hub" / "runtime"
PROTOCOL_FILES = ("frames.py", "worker_channel.py")


def tree_hashes(base: Path) -> dict[str, str]:
    """Relative path (posix) to sha256 for every file under ``base``, caches excluded."""
    hashes = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        hashes[path.relative_to(base).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def read_version(source_package: Path) -> str:
    namespace: dict[str, object] = {}
    for line in (source_package / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            exec(line, namespace)  # a literal assignment in our own file
    return str(namespace["__version__"])


def git_commit(source: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def vendor(source: Path, tag: str | None) -> None:
    source_package = source / "src" / PACKAGE
    if not source_package.is_dir():
        sys.exit(f"{source_package} is not a directory")
    target = DIST / PACKAGE
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source_package, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    version = read_version(target)
    manifest = {
        "package": PACKAGE,
        "version": version,
        "tag": tag or f"v{version}",
        "source_commit": git_commit(source),
        "files": tree_hashes(target),
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"vendored {PACKAGE} {version} ({len(manifest['files'])} files)")


def check(source: Path | None) -> list[str]:
    problems = []
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"cannot read {MANIFEST.relative_to(ROOT)}: {error}"]
    actual = tree_hashes(DIST / PACKAGE)
    expected = manifest.get("files", {})
    for name in sorted(set(actual) | set(expected)):
        if name not in actual:
            problems.append(f"{name} is in the manifest but missing from the copy")
        elif name not in expected:
            problems.append(f"{name} is in the copy but not in the manifest")
        elif actual[name] != expected[name]:
            problems.append(f"{name} differs from the manifest")
    if source is not None:
        from_source = tree_hashes(source / "src" / PACKAGE)
        problems.extend(
            f"{name} differs from the SDK source at {source}"
            for name in sorted(set(actual) | set(from_source))
            if actual.get(name) != from_source.get(name)
        )
    for name in PROTOCOL_FILES:
        host_file = RUNTIME / name
        if host_file.is_file():
            copy = DIST / PACKAGE / "_protocol" / name
            if not copy.is_file() or copy.read_bytes() != host_file.read_bytes():
                problems.append(f"_protocol/{name} differs from the host copy runtime/{name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, help="a checkout of the SDK repository")
    parser.add_argument("--tag", help="the tag the checkout is at (default: v<version>)")
    parser.add_argument("--check", action="store_true", help="verify instead of copying")
    args = parser.parse_args(argv)
    if args.check:
        problems = check(args.source)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    if args.source is None:
        parser.error("--source is required to vendor")
    vendor(args.source, args.tag)
    problems = check(args.source)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
