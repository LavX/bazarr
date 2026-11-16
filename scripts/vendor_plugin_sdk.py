#!/usr/bin/env python3
"""Copy the plugin SDK into ``bazarr/provider_hub/sdk_dist`` and check that the copy is intact.

    scripts/vendor_plugin_sdk.py --source ../bazarr-plugin-sdk
        copies the files of <source>/src/bazarr_plugin_sdk that are committed at the
        source's HEAD, and rewrites VENDORED.json. The source must be a clean git checkout,
        ignored files included. The manifest records the tag the checkout is exactly at,
        or null when it is at no tag
    scripts/vendor_plugin_sdk.py --check [--source ../bazarr-plugin-sdk]
        fails when the files differ from VENDORED.json, and from the source when one is given

The worker protocol files inside the SDK (``_protocol/frames.py`` and
``_protocol/worker_channel.py``) must equal the host's copies in
``bazarr/provider_hub/runtime``; ``--check`` compares them, and a missing host
file is a failure.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile

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


def _git(source: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def git_commit(source: Path) -> str | None:
    return _git(source, "rev-parse", "HEAD") or None


def git_tag(source: Path) -> str | None:
    """The tag ``source`` is exactly at, or None."""
    return _git(source, "describe", "--exact-match", "--tags", "HEAD") or None


def git_dirty(source: Path) -> bool:
    """True when the checkout has uncommitted, untracked or ignored files."""
    status = _git(source, "status", "--porcelain", "--ignored")
    return status is None or bool(status)


def committed_files(source: Path) -> dict[str, bytes]:
    """The package's files as they are at the source's HEAD: relative path (posix) to bytes.

    Read from the commit with ``git archive``, never from the working tree, so what is
    vendored is exactly what the manifest's ``source_commit`` names.
    """
    prefix = f"src/{PACKAGE}/"
    try:
        out = subprocess.run(
            ["git", "-C", str(source), "archive", "--format=tar", "HEAD", f"src/{PACKAGE}"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"cannot read {PACKAGE} from the commit of {source}: {error}")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(out.stdout)) as archive:
        for member in archive:
            if not member.name.startswith(prefix):
                continue
            if not member.isfile():
                if member.isdir():
                    continue
                sys.exit(f"{member.name} in the SDK commit is not a regular file or folder")
            relative = member.name[len(prefix):]
            if relative.startswith("/") or ".." in relative.split("/") or "__pycache__" in relative.split("/"):
                sys.exit(f"{member.name} is not a safe path")
            handle = archive.extractfile(member)
            assert handle is not None
            files[relative] = handle.read()
    return files


def vendor(source: Path) -> None:
    commit = git_commit(source)
    if commit is None:
        sys.exit(f"{source} is not a git checkout")
    if git_dirty(source):
        sys.exit(f"{source} has uncommitted, untracked or ignored files; clean it so the manifest names real source")
    files = committed_files(source)
    if not files:
        sys.exit(f"{PACKAGE} has no files at the commit of {source}")
    target = DIST / PACKAGE
    if target.exists():
        shutil.rmtree(target)
    for relative, content in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    version = read_version(target)
    manifest = {
        "package": PACKAGE,
        "version": version,
        "tag": git_tag(source),
        "source_commit": commit,
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
        from_source = {name: hashlib.sha256(content).hexdigest() for name, content in committed_files(source).items()}
        commit = git_commit(source)
        if commit != manifest.get("source_commit"):
            problems.append(f"the SDK source is at {commit}, the manifest names {manifest.get('source_commit')}")
        problems.extend(
            f"{name} differs from the SDK source at {source}"
            for name in sorted(set(actual) | set(from_source))
            if actual.get(name) != from_source.get(name)
        )
    for name in PROTOCOL_FILES:
        host_file = RUNTIME / name
        copy = DIST / PACKAGE / "_protocol" / name
        if not host_file.is_file():
            problems.append(f"the host copy runtime/{name} is missing, so _protocol/{name} cannot be checked")
        elif not copy.is_file() or copy.read_bytes() != host_file.read_bytes():
            problems.append(f"_protocol/{name} differs from the host copy runtime/{name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, help="a checkout of the SDK repository")
    parser.add_argument("--check", action="store_true", help="verify instead of copying")
    args = parser.parse_args(argv)
    if args.check:
        problems = check(args.source)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    if args.source is None:
        parser.error("--source is required to vendor")
    vendor(args.source)
    problems = check(args.source)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
