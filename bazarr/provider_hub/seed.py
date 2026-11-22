# coding=utf-8
"""Build and read the catalog floor's immutable, target-specific runtime."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import venv

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import revocation
from .bundle import BundleValidationError, verify_bundle_tree
from .bundled_wheels import verified_wheels_dir
from .kinds.translator import TranslatorKind
from .manifest import (
    BUNDLE_MANIFEST_FILE,
    ManifestValidationError,
    ValidatedManifest,
    manifest_provider_id,
    validate_manifest,
    validate_provider_bundle,
)
from .plugin_manifest import PYTHON_BUNDLE, STDIO_WORKER, parse_plugin_manifest
from .venv import (
    BUNDLE_DIR,
    COMPLETE_MARKER,
    SHARED_ENV_DIR,
    environment_fingerprint,
    has_parent_reference,
    python_executable,
    validated_bundle_path,
    validated_interpreter,
)
from .versions import version_key

logger = logging.getLogger(__name__)

INDEX_FILE = "seed.json"
RUNTIME_DIR = "runtime"
GOOGLE_PLUGIN_ID = "google_translate"
INDEX_VERSION = 1


class SeedError(ValueError):
    """The runtime seed cannot be built or verified."""


@dataclass(frozen=True)
class Release:
    manifest: ValidatedManifest
    bundle: Path
    interpreter: Path
    seeded: bool


def _bundle_path(root: Path, manifest: ValidatedManifest) -> Path:
    return root / BUNDLE_DIR / manifest.provider_id / manifest.version / manifest.source_commit


def _catalog_manifests(
    document: dict[str, Any], repo: str, commit: str, *, signed: bool,
) -> Iterable[tuple[dict[str, Any], bool]]:
    """The catalog manifests admitted to the image runtime."""
    # Prefer the signed bundled wheel closure if the dependency set is
    # shared with a legacy provider that would otherwise go to the index.
    lists = [("plugins", True), ("providers", False)] if signed else [("providers", False)]
    for name, plugin in lists:
        for entry in document.get(name, []):
            raw = entry["manifest"]
            plugin_id = manifest_provider_id(raw)
            if plugin and plugin_id != GOOGLE_PLUGIN_ID:
                continue
            if not plugin and plugin_id == GOOGLE_PLUGIN_ID:
                raise SeedError("the Google translator must be in the signed plugins list")
            origin = raw.get("source", {})
            if origin.get("repo") != repo:
                raise SeedError(f"{plugin_id} is outside the pinned catalog repository")
            yield {**raw, "source": {**origin, "type": origin.get("type", "github"),
                                     "commit": commit, "trusted": True}}, plugin


def _validate(raw: dict[str, Any], plugin: bool, *, building: bool) -> ValidatedManifest:
    if plugin:
        validated = TranslatorKind().validate_manifest(raw, trusted=True)
        envelope = parse_plugin_manifest(raw)
        if (envelope.artifact, envelope.process) != (PYTHON_BUNDLE, STDIO_WORKER):
            raise SeedError(f"{validated.provider_id} does not declare a Python worker")
        return validated
    if raw.get("schema_version") != 1:
        raise SeedError("a provider in the seed must use the legacy provider catalog schema")
    return validate_provider_bundle(raw) if building else validate_manifest(raw)


def _file_sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _interpreter_facts() -> dict[str, Any]:
    return {
        "version": list(sys.version_info[:3]),
        "platform": sys.platform,
        "machine": platform.machine().lower(),
        "cache_tag": sys.implementation.cache_tag,
        "abi": sysconfig.get_config_var("SOABI"),
        "interpreter_sha256": _file_sha(Path(sys._base_executable)),
    }


def _tree_digest(root: Path) -> str:
    """Fingerprint a read-only environment including file modes and names."""
    digest = hashlib.sha256()
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode) and mode == 0o555:
            content_hash = ""
        elif stat.S_ISREG(info.st_mode) and mode in (0o444, 0o555):
            content_hash = _file_sha(path)
        else:
            raise SeedError(f"unexpected seeded environment entry: {path}")
        entry = json.dumps((path.relative_to(root).as_posix(), mode, content_hash), separators=(",", ":"))
        digest.update(entry.encode("utf-8") + b"\n")
    return digest.hexdigest()


def _make_read_only(root: Path, *, executable: bool = False) -> None:
    for path in [*sorted(root.rglob("*"), reverse=True), root]:
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(0o555)
        elif stat.S_ISREG(mode):
            path.chmod(0o555 if executable and mode & 0o111 else 0o444)
        else:
            raise SeedError(f"unexpected entry in the seed build: {path}")


def _install_environment(
    manifest: ValidatedManifest, path: Path, wheelhouse: Path | None, *, offline: bool,
) -> None:
    # Install from the target platform's interpreter. A copied interpreter keeps
    # the image free of symlinks and a pip-less environment has no installer at boot.
    venv.EnvBuilder(with_pip=False, symlinks=False).create(path)
    lib64 = path / "lib64"
    if lib64.is_symlink() and os.readlink(lib64) == "lib":
        lib64.unlink()
    if manifest.dependency_requirements:
        with tempfile.TemporaryDirectory(prefix="catalog-wheels-") as temp:
            requirements = Path(temp) / "requirements.txt"
            requirements.write_text(
                "".join(f"{line}\n" for line in sorted(r.pip_line for r in manifest.dependency_requirements)),
                encoding="utf-8",
            )
            pip = [
                sys.executable, "-I", "-m", "pip", "--python", str(python_executable(path)),
                "--disable-pip-version-check", "install", "--no-compile", "--only-binary=:all:",
                "--require-hashes", "-r", str(requirements),
            ]
            if wheelhouse is not None:
                # Bundled metadata was already checked for a complete closure.
                pip.extend(("--no-index", "--no-deps", "--find-links", str(wheelhouse)))
            elif offline:
                pip.append("--no-index")
            subprocess.run(
                pip, check=True, env={"PATH": os.environ.get("PATH", ""), "PIP_CONFIG_FILE": os.devnull},
            )
    (path / COMPLETE_MARKER).touch()
    _make_read_only(path, executable=True)


def build(
    root: Path, *, repo: str, commit: str, catalog: bytes, signed_catalog: bytes | None,
    files: Mapping[str, bytes], offline: bool = False,
) -> bytes:
    """Derive an index and runtime tree from a snapshot the lock builder verified."""
    if not root.is_dir() or root.is_symlink() or any(root.iterdir()):
        raise SeedError("the runtime seed destination must be a new empty directory")
    document = json.loads(signed_catalog if signed_catalog is not None else catalog)
    revocations = revocation.parse(document.get(revocation.FIELD_NAME)) if signed_catalog is not None else ()
    manifests = [
        (raw, plugin)
        for raw, plugin in _catalog_manifests(document, repo, commit, signed=signed_catalog is not None)
        if revocation.lookup(revocations, manifest_provider_id(raw), raw.get("version")) is None
    ]
    if signed_catalog is not None and not any(plugin for _raw, plugin in manifests):
        raise SeedError(f"the signed catalog has no usable {GOOGLE_PLUGIN_ID} release")
    environments: dict[str, str] = {}
    releases = []
    skipped = []
    skipped_envs = set()
    seen = set()
    for raw, plugin in manifests:
        manifest = _validate(raw, plugin, building=True)
        release_key = manifest.provider_id, manifest.version
        if release_key in seen:
            raise SeedError(f"duplicate seed release: {manifest.provider_id} {manifest.version}")
        seen.add(release_key)
        key = environment_fingerprint(manifest)
        if key in skipped_envs:
            skipped.append(manifest.provider_id)
            continue
        bundle = _bundle_path(root, manifest)
        bundle.mkdir(parents=True)
        for name in manifest.files:
            source = "/".join(filter(None, (manifest.source_path, name)))
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[source])
        verify_bundle_tree(manifest, bundle)
        # The copy is for parity with config installations. Its contents are
        # never used as authority for starting a seeded worker.
        (bundle / BUNDLE_MANIFEST_FILE).write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
        wheelhouse = verified_wheels_dir(manifest, bundle)
        if plugin and manifest.dependency_requirements and wheelhouse is None:
            raise SeedError(f"{manifest.provider_id} has no bundled wheels")
        if key not in environments:
            env = root / SHARED_ENV_DIR / key
            try:
                _install_environment(manifest, env, wheelhouse, offline=offline)
            except subprocess.CalledProcessError as error:
                if signed_catalog is not None:
                    raise SeedError(f"could not install wheels for {manifest.provider_id}") from error
                # The unsigned transitional pin still advertises these on the
                # legacy catalog. The signed release requires every wheel set.
                skipped_envs.add(key)
                skipped.append(manifest.provider_id)
                shutil.rmtree(bundle)
                shutil.rmtree(env)
                continue
            environments[key] = _tree_digest(env)
        _make_read_only(bundle)
        releases.append({"manifest": raw, "bundle_sha256": manifest.bundle_sha256, "environment": key})
    # The inner trees already have their final modes. Seal their ancestors.
    for path in [*root.rglob("*"), root]:
        if not path.is_symlink() and path.is_dir():
            path.chmod(0o555)
    if skipped:
        logger.warning("The unsigned floor has no runnable wheel closure for: %s", ", ".join(sorted(set(skipped))))
    index = {
        "index_version": INDEX_VERSION,
        "source_commit": commit,
        "interpreter": _interpreter_facts(),
        "environments": environments,
        "releases": releases,
    }
    return (json.dumps(index, sort_keys=True) + "\n").encode("utf-8")


def _index(snapshot) -> dict[str, Any]:
    try:
        index = json.loads(snapshot.read(INDEX_FILE))
    except (UnicodeError, ValueError) as error:
        raise SeedError("the runtime seed index is not valid JSON") from error
    if not isinstance(index, dict) or index.get("index_version") != INDEX_VERSION:
        raise SeedError("the runtime seed index version is unsupported")
    if index.get("source_commit") != snapshot.commit:
        raise SeedError("the runtime seed is for another catalog commit")
    if index.get("interpreter") != _interpreter_facts():
        raise SeedError("the runtime seed is for another Python interpreter or architecture")
    if not isinstance(index.get("environments"), dict) or not isinstance(index.get("releases"), list):
        raise SeedError("the runtime seed index has no environment or release inventory")
    return index


def check_image_interpreter(root: Path) -> None:
    """Check the final image's host interpreter against the build stage."""
    from .floor import load_floor

    snapshot = load_floor(root)
    if snapshot is None:
        raise SeedError("the image has no catalog floor")
    _index(snapshot)


def _same_manifest(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Refresh may supply catalog_url from the official source's configuration.
    # Repo, commit, source path and every executable declaration must match.
    def comparable(raw):
        return {**raw, "source": {k: v for k, v in raw["source"].items() if k != "catalog_url"}}

    try:
        return comparable(left) == comparable(right)
    except (AttributeError, KeyError, TypeError):
        return False


def _seed_release(snapshot, index: dict[str, Any], entry: dict[str, Any], raw: dict[str, Any], plugin: bool) -> Release:
    from .compatibility import AVAILABLE_RUNTIMES, HostFacts, evaluate, host_platform

    manifest = _validate(raw, plugin, building=False)
    host = HostFacts(os.environ.get("BAZARR_VERSION"), host_platform(), AVAILABLE_RUNTIMES)
    compatible, reason = evaluate(parse_plugin_manifest(raw), host)
    if not compatible:
        raise SeedError(reason)
    runtime_root = snapshot.root / RUNTIME_DIR
    if not runtime_root.is_dir() or runtime_root.is_symlink():
        raise SeedError("the runtime seed root is missing or is a symlink")
    bundle = validated_bundle_path(runtime_root, _bundle_path(runtime_root, manifest))
    key = environment_fingerprint(manifest)
    environment = runtime_root / SHARED_ENV_DIR / key
    interpreter = validated_interpreter(runtime_root, python_executable(environment))
    if (bundle is None or interpreter is None or not interpreter.is_file()
            or interpreter.is_symlink() or not os.access(interpreter, os.X_OK)):
        raise SeedError("the runtime seed has no bundle or interpreter")
    if entry.get("bundle_sha256") != manifest.bundle_sha256 or entry.get("environment") != key:
        raise SeedError("the runtime seed entry does not match the catalog manifest")
    verify_bundle_tree(manifest, bundle)
    if _tree_digest(environment) != index["environments"].get(key):
        raise SeedError("the runtime seed environment does not match its build digest")
    return Release(manifest, bundle, interpreter, True)


def _seed_releases(plugin_id: str, exact_version: str | None = None) -> Iterable[Release]:
    from .floor import FloorError, _usable_floor

    snapshot = _usable_floor()
    if snapshot is None or INDEX_FILE not in snapshot.files:
        return
    try:
        index = _index(snapshot)
        signed = snapshot.signed_catalog()
        document = signed if signed is not None else snapshot.catalog()
        revocations = revocation.parse(signed.get(revocation.FIELD_NAME)) if signed is not None else ()
        manifests = {
            raw["version"]: (raw, plugin)
            for raw, plugin in _catalog_manifests(document, snapshot.repo, snapshot.commit, signed=signed is not None)
            if manifest_provider_id(raw) == plugin_id
            and (exact_version is None or raw["version"] == exact_version)
            and revocation.lookup(revocations, plugin_id, raw["version"]) is None
        }
        matching = sorted(
            (entry for entry in index["releases"] if isinstance(entry, dict)
             and manifest_provider_id(entry.get("manifest")) == plugin_id
             and entry["manifest"].get("version") in manifests),
            key=lambda entry: version_key(entry["manifest"]["version"]),
            reverse=True,
        )
    except (FloorError, KeyError, OSError, TypeError, ValueError) as error:
        logger.warning("The catalog runtime seed is not used: %s", error)
        return
    for entry in matching:
        raw, plugin = manifests[entry["manifest"]["version"]]
        try:
            if not _same_manifest(entry["manifest"], raw):
                raise SeedError("the runtime seed manifest does not match the catalog")
            yield _seed_release(snapshot, index, entry, raw, plugin)
        except (BundleValidationError, ManifestValidationError, OSError, SeedError) as error:
            logger.warning("The catalog runtime seed for %s %s is not used: %s", plugin_id, raw["version"], error)


def select_release(
    config_root: Path,
    plugin_id: str,
    *,
    config_candidate: tuple[ValidatedManifest, str, str] | None = None,
) -> Release | None:
    """Choose the highest valid release, preferring the config root on ties."""
    from .env_registry import _adoptable

    config = None
    if config_candidate is not None:
        manifest, bundle_path, python_path = config_candidate
        if manifest.provider_id == plugin_id:
            bundle = validated_bundle_path(config_root, bundle_path)
            interpreter = validated_interpreter(config_root, python_path)
            if (bundle is not None and interpreter is not None and interpreter.is_file()
                    and os.access(interpreter, os.X_OK) and _adoptable(interpreter.parent.parent, manifest)):
                try:
                    verify_bundle_tree(manifest, bundle)
                except (BundleValidationError, OSError) as error:
                    logger.warning("The config release for %s is not usable: %s", plugin_id, error)
                else:
                    config = Release(manifest, bundle, interpreter, False)
    seed = next(_seed_releases(plugin_id), None)
    if config is not None and (seed is None or version_key(config.manifest.version) >= version_key(seed.manifest.version)):
        return config
    return seed


def resolve_paths(
    config_root: Path,
    manifest: dict[str, Any],
    bundle_path: str | Path,
    python_path: str | Path,
) -> tuple[Path, Path] | None:
    """Resolve a stored bundle/interpreter pair for a worker start.

    A seed release is reverified against the same catalog trust and environment
    identity on every start. Config releases use the existing config path gate.
    """
    bundle = validated_bundle_path(config_root, bundle_path)
    interpreter = validated_interpreter(config_root, python_path)
    if bundle is not None and interpreter is not None:
        return bundle, interpreter
    if has_parent_reference(bundle_path) or has_parent_reference(python_path):
        return None
    plugin_id = manifest_provider_id(manifest)
    version = manifest.get("version")
    if not plugin_id or not isinstance(version, str):
        return None
    for release in _seed_releases(plugin_id, version):
        if (_same_manifest(manifest, release.manifest.raw)
                and Path(bundle_path) == release.bundle and Path(python_path) == release.interpreter):
            return release.bundle, release.interpreter
    return None
