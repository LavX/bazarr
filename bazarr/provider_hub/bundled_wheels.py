# coding=utf-8
"""Wheels a plugin bundle carries for its own dependencies.

A bundle may hold the wheels of its locked dependency closure under ``wheels/``. When it does, its
environment is built from exactly those files and never from an index: the bundle's signature already
vouches for them, and a host with no route to PyPI (a seeded image, an offline box) can still install.

The lock in ``dependencies.requirements`` stays the authority. Before anything is installed, each wheel is
matched to a locked requirement by name and version, and its digest must be one the lock lists. A wheel the
lock does not name, a locked dependency with no wheel, a file that is not a wheel and a file on disk that the
manifest does not declare each refuse the install, with the reason. pip then runs with ``--require-hashes``,
so it checks the same digests a second time.

pip is told not to resolve anything (``--no-deps``): the lock is the whole closure. So that nothing a wheel asks
for goes unchecked, every ``Requires-Dist`` of every bundled wheel that applies to this interpreter must be met by
another bundled wheel. A requirement that names a URL, or a package the bundle does not carry, refuses the install.

A bundle without a ``wheels/`` directory is not affected: its dependencies come from the index as before.
"""
from __future__ import annotations

import email.parser
import hashlib
import re
import zipfile

from pathlib import Path, PurePosixPath
from typing import Any

WHEELS_DIR = "wheels"

# {name}-{version}(-{build})?-{python}-{abi}-{platform}.whl. Name and version never contain a dash in a
# wheel filename (a dash in either is written as an underscore), so the first two fields are unambiguous.
_WHEEL_RE = re.compile(r"^(?P<name>[^-]+)-(?P<version>[^-]+)(?:-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$")


class BundledWheelsError(ValueError):
    """The wheels in a bundle do not match its lock."""


def normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _same_version(locked: str, found: str) -> bool:
    locked, found = locked.replace("_", "-").lower(), found.replace("_", "-").lower()
    if locked == found:
        return True
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return False
    try:
        return Version(locked) == Version(found)
    except InvalidVersion:
        return False


_METADATA_MAX_BYTES = 4 * 1024 * 1024


def _packaging():
    """``(Requirement, Version, InvalidRequirement)`` from packaging, or from the copy pip vendors."""
    try:
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.version import Version
    except ImportError:
        try:
            from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
            from pip._vendor.packaging.version import Version
        except ImportError as error:
            raise BundledWheelsError(
                "the requirements of the bundled wheels cannot be checked: neither packaging nor pip is available"
            ) from error
    return Requirement, Version, InvalidRequirement


def _requires_dist(wheel: Path) -> list[str]:
    """The ``Requires-Dist`` lines of a wheel's metadata."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.count("/") == 1 and name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise BundledWheelsError(f"{wheel.name} has no single dist-info/METADATA")
            if archive.getinfo(names[0]).file_size > _METADATA_MAX_BYTES:
                raise BundledWheelsError(f"{wheel.name} has oversized metadata")
            raw = archive.read(names[0])
    except (zipfile.BadZipFile, OSError) as error:
        raise BundledWheelsError(f"{wheel.name} is not a readable wheel: {error}") from error
    message = email.parser.BytesParser().parsebytes(raw, headersonly=True)
    return [str(value) for value in message.get_all("Requires-Dist") or []]


def _check_requirements(wheels: list[tuple[str, str, str]], root: Path) -> None:
    """Every requirement of every bundled wheel that applies here is met by a bundled wheel.

    ``wheels`` lists ``(normalized name, version, path)`` for every wheel file in the bundle, so each variant of
    a package is checked, whichever one pip ends up choosing. Markers are judged for this interpreter, which is
    the one the environment is built with. An extra asked of a package (``a`` needs ``b[socks]``) is carried
    through: the package's wheels are checked again with that extra, and what it adds must be in the bundle too.
    """
    Requirement, Version, InvalidRequirement = _packaging()
    versions = {key: version for key, version, _path in wheels}
    parsed: list[tuple[str, str, Any]] = []
    for key, _version, path in wheels:
        for line in _requires_dist(root / path):
            try:
                requirement = Requirement(line)
            except InvalidRequirement as error:
                raise BundledWheelsError(f"{path} asks for {line!r}, which is not a valid requirement") from error
            if requirement.url:
                raise BundledWheelsError(f"{path} asks for {requirement.name} from a URL ({requirement.url}), which is not allowed")
            parsed.append((key, path, requirement))

    # The extras requested of each package, grown until nothing new is asked. The locked requirements name none.
    requested: dict[str, set[str]] = {key: set() for key in versions}
    applying: set[tuple[str, str, int]] = set()
    changed = True
    while changed:
        changed = False
        for index, (key, path, requirement) in enumerate(parsed):
            for extra in {""} | requested[key]:
                try:
                    applies = requirement.marker is None or requirement.marker.evaluate({"extra": extra})
                except Exception as error:
                    raise BundledWheelsError(f"{path} has a marker that cannot be judged here: {requirement}") from error
                if not applies:
                    continue
                applying.add((key, path, index))
                target = normalized_name(requirement.name)
                if target not in versions:
                    continue
                wanted = {normalized_name(item) for item in requirement.extras} - requested[target]
                if wanted:
                    requested[target] |= wanted
                    changed = True

    for _key, path, index in sorted(applying, key=lambda item: (item[1], item[2])):
        requirement = parsed[index][2]
        found = versions.get(normalized_name(requirement.name))
        if found is None:
            raise BundledWheelsError(f"{path} needs {requirement.name}, which the bundle carries no wheel for")
        try:
            satisfied = requirement.specifier.contains(Version(found), prereleases=True)
        except Exception:
            satisfied = False
        if not satisfied:
            raise BundledWheelsError(f"{path} needs {requirement.name}{requirement.specifier}, but the bundle carries {found}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def declared_wheels(manifest: Any) -> dict[str, str]:
    """The bundle files under ``wheels/`` the manifest declares, as ``{path: sha256}``."""
    prefix = WHEELS_DIR + "/"
    return {path: digest for path, digest in (getattr(manifest, "files", None) or {}).items() if path.startswith(prefix)}


def verified_wheels_dir(manifest: Any, bundle_path: str | Path) -> Path | None:
    """The directory of the bundle's baked wheels, checked against the lock, or None when it has none.

    Raises ``BundledWheelsError`` when the wheels and the lock disagree.
    """
    declared = declared_wheels(manifest)
    if not declared:
        return None
    provider_id = getattr(manifest, "provider_id", "the plugin")
    requirements = tuple(getattr(manifest, "dependency_requirements", ()) or ())
    if not requirements:
        raise BundledWheelsError(f"{provider_id} carries wheels but its lock names no dependency")
    root = Path(bundle_path).resolve()
    wheels_dir = root / WHEELS_DIR
    if wheels_dir.is_symlink() or not wheels_dir.is_dir():
        raise BundledWheelsError(f"{provider_id} declares wheels but its bundle has no {WHEELS_DIR} directory")

    by_name = {normalized_name(requirement.name): requirement for requirement in requirements}
    covered: set[str] = set()
    wheels: list[tuple[str, str, str]] = []
    for path, declared_digest in sorted(declared.items()):
        parts = PurePosixPath(path).parts
        match = _WHEEL_RE.match(parts[-1]) if len(parts) == 2 else None
        if match is None:
            raise BundledWheelsError(f"{path} is not a wheel file placed directly in {WHEELS_DIR}/")
        key = normalized_name(match.group("name"))
        requirement = by_name.get(key)
        if requirement is None:
            raise BundledWheelsError(f"{path} is not in the lock: {match.group('name')} is not a locked dependency")
        if not _same_version(requirement.version, match.group("version")):
            raise BundledWheelsError(
                f"{path} is version {match.group('version')} but the lock pins {requirement.name} {requirement.version}"
            )
        if "sha256:" + declared_digest not in requirement.hashes:
            raise BundledWheelsError(f"{path} has a digest the lock does not list for {requirement.name} {requirement.version}")
        wheel = root / path
        if wheel.is_symlink() or not wheel.is_file():
            raise BundledWheelsError(f"{path} is missing from the bundle")
        if _sha256_file(wheel) != declared_digest:
            raise BundledWheelsError(f"{path} does not match its declared digest")
        covered.add(key)
        wheels.append((key, requirement.version.replace("_", "-"), path))

    for key, requirement in sorted(by_name.items()):
        if key not in covered:
            raise BundledWheelsError(
                f"the lock names {requirement.name} {requirement.version} but the bundle carries no wheel for it"
            )
    on_disk = {entry.relative_to(root).as_posix() for entry in wheels_dir.rglob("*") if entry.is_file() or entry.is_symlink()}
    extra = sorted(on_disk - set(declared))
    if extra:
        raise BundledWheelsError(f"{extra[0]} sits among the bundle's wheels but the manifest does not declare it")
    _check_requirements(wheels, root)
    return wheels_dir
