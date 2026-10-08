#!/usr/bin/env python3
"""Prepare deterministic Bazarr+ platform packages from a reviewed release lock."""

import argparse
import gzip
import hashlib
import io
import json

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


PACKAGING = Path(__file__).resolve().parent
# The render anchor is the version the templates are written against; the automation lands real locks per release.
SOURCE_VERSION = "2.7.0"
SOURCE_APP_DIGEST = "sha256:90a5c184b0af41602ff78ea7286e0c5f2c4c284c9b18f71427d9ddbeb0b6531c"
IMAGE_REPOSITORIES = {
    "app": "ghcr.io/lavx/bazarr",
    "translator": "ghcr.io/lavx/ai-subtitle-translator",
    "flaresolverr": "ghcr.io/flaresolverr/flaresolverr",
}
PLATFORMS = ("stack", "casaos", "runtipi", "truenas", "unraid")
SHARED_ASSETS = ("LICENSE", "NOTICES.md")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
LOCK_NAME = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.json\Z")
EXCLUDED_NAMES = {"__pycache__", ".DS_Store", ".pytest_cache", ".mypy_cache", ".ruff_cache", "test_values", "tests"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".swp", ".tmp", ".bak", "~"}
MANIFEST_ACCEPT = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
READINESS_SECONDS = 600
RETRY_SECONDS = 20


class ReleaseError(Exception):
    pass


class RegistryNotReady(ReleaseError):
    pass


def canonical_json(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def exact_replace(path, old, new, count=1):
    text = path.read_text()
    found = text.count(old)
    if found != count:
        raise ReleaseError(f"Unexpected release field in {path}: expected {count} occurrence(s), found {found}")
    path.write_text(text.replace(old, new))


def require_keys(obj, keys, context):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise ReleaseError(f"Invalid {context} fields")


def validate_lock(lock, tag):
    match = VERSION.fullmatch(tag)
    if not match or match.group(1) != "2" or match.group(2) != "7":
        raise ReleaseError("Only stable v2.7.x tags are supported by the Atlas sidecar packages")
    require_keys(lock, ("schema", "version", "app", "companions", "runtime_profile", "platform_revisions"), "lock")
    if type(lock["schema"]) is not int or lock["schema"] != 1 or lock["version"] != tag[1:]:
        raise ReleaseError("Lock schema or app version does not match the tag")
    if lock["runtime_profile"] != "atlas-sidecar":
        raise ReleaseError("Only the atlas-sidecar runtime profile is supported for v2.7.x")
    require_keys(lock["app"], ("image", "digest"), "app")
    require_keys(lock["companions"], ("translator", "flaresolverr"), "companions")
    require_keys(lock["platform_revisions"], PLATFORMS, "platform revisions")
    if lock["app"]["image"] != IMAGE_REPOSITORIES["app"] or not isinstance(lock["app"]["digest"], str) or not DIGEST.fullmatch(lock["app"]["digest"]):
        raise ReleaseError("Invalid app image or digest")
    for name, image in IMAGE_REPOSITORIES.items():
        if name == "app":
            continue
        entry = lock["companions"][name]
        require_keys(entry, ("image", "tag", "digest"), name)
        if entry["image"] != image or not isinstance(entry["tag"], str) or not re.fullmatch(r"v\d+\.\d+\.\d+", entry["tag"]):
            raise ReleaseError(f"Invalid {name} image or tag")
        if not isinstance(entry["digest"], str) or not DIGEST.fullmatch(entry["digest"]):
            raise ReleaseError(f"Invalid {name} digest")
    for name, revision in lock["platform_revisions"].items():
        if name == "runtipi":
            if type(revision) is not int or revision < 1:
                raise ReleaseError("Runtipi revision must be a positive integer")
        elif not isinstance(revision, str) or not re.fullmatch(r"\d+\.\d+\.\d+", revision):
            raise ReleaseError(f"Invalid {name} revision")
    return lock


def read_lock(path, tag):
    if path.is_symlink() or not path.is_file():
        raise ReleaseError("Lock must be a regular file")
    try:
        return validate_lock(json.loads(path.read_text()), tag)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Invalid JSON lock: {exc}") from exc


def inventory_sources():
    files = {}
    for platform in PLATFORMS:
        root = PACKAGING / platform
        if root.is_symlink() or not root.is_dir():
            raise ReleaseError(f"Missing or linked package source: {platform}")
        paths = []
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ReleaseError(f"Symlink in package source: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in EXCLUDED_NAMES or part.startswith(".") for part in relative.parts):
                continue
            if path.suffix in EXCLUDED_SUFFIXES or path.name.endswith("~") or path.name.startswith("test_"):
                continue
            if (path.suffix == ".md" and "validation" in path.name.lower()) or path.name in {"PLAN.md", "check_connections.py"} or (path.name == "README.md" and relative.parent == Path(".")):
                continue
            paths.append(relative)
        files[platform] = sorted(paths, key=lambda p: p.as_posix())
        if not files[platform]:
            raise ReleaseError(f"No distributable assets for {platform}")
    for shared in ("LICENSE", "NOTICES.md"):
        path = PACKAGING.parent / shared
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"Missing or linked license asset: {shared}")
    return files


def package_paths(platform, files):
    return sorted((*files[platform], Path("LICENSE"), Path("NOTICES.md"), Path("RELEASE.md")), key=lambda p: p.as_posix())


def source_path(platform, relative):
    if relative.as_posix() in {"LICENSE", "NOTICES.md"}:
        return PACKAGING.parent / relative
    return PACKAGING / platform / relative


def render(out, files, lock):
    for platform, paths in files.items():
        for relative in paths:
            source = PACKAGING / platform / relative
            destination = out / "rendered" / platform / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
        for shared in ("LICENSE", "NOTICES.md"):
            shutil.copyfile(PACKAGING.parent / shared, out / "rendered" / platform / shared)
        (out / "rendered" / platform / "RELEASE.md").write_text(
            f"# Bazarr+ {lock['version']} package for {platform}\n\n"
            f"Platform package revision: {lock['platform_revisions'][platform]}\n\n"
            f"Runtime profile: {lock['runtime_profile']}. The translator and FlareSolverr pins "
            "are recorded in the reviewed release lock. Historical validation notes remain "
            "with the source packages and do not certify this rendered release.\n"
        )
    version = lock["version"]
    app = f"{IMAGE_REPOSITORIES['app']}:{version}"
    old_app = f"{IMAGE_REPOSITORIES['app']}:{SOURCE_VERSION}"
    old_pinned = f"{old_app}@{SOURCE_APP_DIGEST}"
    new_pinned = f"{app}@{lock['app']['digest']}"
    translator = lock["companions"]["translator"]
    flaresolverr = lock["companions"]["flaresolverr"]
    old_translator = "ghcr.io/lavx/ai-subtitle-translator:v2.1.1@sha256:e22abba9625b96df6727354f10933da312a084ffeae68f0eb1f9a82492f5e48d"
    old_flaresolverr = "ghcr.io/flaresolverr/flaresolverr:v3.5.2@sha256:c80ae007ce2ccdcd217a12426e4f039ef763ff90738c808d38810c3e59323767"
    for relative in ("stack/compose.yaml", "casaos/Apps/BazarrPlusStack/docker-compose.yml"):
        path = out / "rendered" / relative
        exact_replace(path, old_pinned, new_pinned, 2)
        exact_replace(path, old_translator, f"{translator['image']}:{translator['tag']}@{translator['digest']}")
        exact_replace(path, old_flaresolverr, f"{flaresolverr['image']}:{flaresolverr['tag']}@{flaresolverr['digest']}")
        exact_replace(path, f'version: "{SOURCE_VERSION}"', f'version: "{version}"')
    for relative in ("casaos/Apps/BazarrPlus/docker-compose.yml", "runtipi/bazarr-plus/docker-compose.yml"):
        exact_replace(out / "rendered" / relative, old_app, new_pinned)
    exact_replace(out / "rendered/casaos/Apps/BazarrPlus/docker-compose.yml", f'version: "{SOURCE_VERSION}"', f'version: "{version}"')
    runtipi = out / "rendered/runtipi/bazarr-plus/config.json"
    config = json.loads(runtipi.read_text())
    if config.get("version") != SOURCE_VERSION or config.get("tipi_version") != 1:
        raise ReleaseError("Unexpected Runtipi version fields")
    config["version"] = version
    config["tipi_version"] = lock["platform_revisions"]["runtipi"]
    runtipi.write_bytes(canonical_json(config))
    truenas = out / "rendered/truenas/ix-dev/community/bazarr-plus"
    exact_replace(truenas / "app.yaml", f"app_version: {SOURCE_VERSION}", f"app_version: {version}")
    exact_replace(truenas / "app.yaml", f"/releases/tag/v{SOURCE_VERSION}", f"/releases/tag/v{version}")
    exact_replace(truenas / "app.yaml", "\nversion: 1.0.0\n", f"\nversion: {lock['platform_revisions']['truenas']}\n")
    exact_replace(truenas / "ix_values.yaml", f"    tag: {SOURCE_VERSION}\n", f"    tag: {version}@{lock['app']['digest']}\n")
    exact_replace(out / "rendered/unraid/templates/bazarr-plus.xml", f"<Repository>{old_app}</Repository>", f"<Repository>{new_pinned}</Repository>")
    allowed = {new_pinned}
    for path in (out / "rendered").rglob("*"):
        if path.suffix not in {".yaml", ".yml", ".json", ".xml", ".md"}:
            continue
        references = re.findall(r"ghcr\.io/lavx/bazarr:[a-zA-Z0-9_.-]+(?:@sha256:[0-9a-f]{64})?", path.read_text())
        if any(reference not in allowed for reference in references):
            raise ReleaseError(f"Unexpected Bazarr+ image reference in {path}")


def git_provenance():
    def run(*args):
        result = subprocess.run(["git", *args], cwd=PACKAGING.parent, capture_output=True, text=True, timeout=10, check=True)
        return result.stdout.strip()
    try:
        status = run("status", "--porcelain", "--untracked-files=all", "--", "packaging", *SHARED_ASSETS)
        return {"commit": run("rev-parse", "HEAD"), "dirty": bool(status)}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"Cannot establish packaging provenance: {exc}") from exc


def create_archives(out, files, tag, lock):
    sums = {}
    for platform in PLATFORMS:
        archive = out / f"bazarr-plus-{tag}-{platform}-r{lock['platform_revisions'][platform]}.tar.gz"
        with archive.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as tar:
                    for relative in package_paths(platform, files):
                        path = out / "rendered" / platform / relative
                        data = path.read_bytes()
                        info = tarfile.TarInfo(f"{platform}/{relative.as_posix()}")
                        info.size = len(data)
                        info.mode = 0o644
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        tar.addfile(info, io.BytesIO(data))
        sums[archive.name] = sha256(archive.read_bytes())
    (out / "SHA256SUMS").write_text("".join(f"{sums[name]}  {name}\n" for name in sorted(sums)))
    return sums


def request_json(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=8) as response:
            return json.load(response), response.headers
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ReleaseError("Invalid registry JSON response") from exc


def fetch_index(image, tag):
    """Fetch the image index for tag, returning the reported digest and the parsed index."""
    repository = image.removeprefix("ghcr.io/")
    token_url = "https://ghcr.io/token?" + urllib.parse.urlencode({"scope": f"repository:{repository}:pull", "service": "ghcr.io"})
    token_data, _ = request_json(token_url)
    token = token_data.get("token") if isinstance(token_data, dict) else None
    if not isinstance(token, str) or not token:
        raise ReleaseError(f"GHCR did not supply a pull token for {image}")
    manifest_url = f"https://ghcr.io/v2/{repository}/manifests/{urllib.parse.quote(tag, safe='')}"
    index, headers = request_json(manifest_url, {"Authorization": "Bearer " + token, "Accept": MANIFEST_ACCEPT})
    return headers.get("Docker-Content-Digest", ""), index


def verify_index(image, tag, digest):
    """Check that the GHCR index for tag carries exactly this digest with linux amd64 and arm64."""
    actual_digest, index = fetch_index(image, tag)
    if actual_digest != digest:
        raise RegistryNotReady(f"GHCR digest mismatch for {image}:{tag}")
    if not isinstance(index, dict) or index.get("mediaType") not in INDEX_TYPES or not isinstance(index.get("manifests"), list):
        raise ReleaseError(f"GHCR did not return an image index for {image}:{tag}")
    arches = {entry.get("platform", {}).get("architecture") for entry in index["manifests"]
              if isinstance(entry, dict) and isinstance(entry.get("platform"), dict)
              and entry["platform"].get("os") == "linux" and DIGEST.fullmatch(str(entry.get("digest", "")))}
    if not {"amd64", "arm64"}.issubset(arches):
        raise RegistryNotReady(f"GHCR index lacks linux amd64/arm64 for {image}:{tag}")


def verify_live(lock, tag):
    try:
        result = subprocess.run(["gh", "api", f"repos/LavX/bazarr/releases/tags/{tag}"], capture_output=True, text=True, timeout=15, check=True)
        release = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Cannot verify GitHub release {tag}: {exc}") from exc
    if not isinstance(release, dict) or release.get("tag_name") != tag or release.get("draft") is not False or release.get("prerelease") is not False or not release.get("published_at"):
        raise ReleaseError(f"GitHub release {tag} is missing, draft, or prerelease")
    images = [(lock["app"]["image"], lock["version"], lock["app"]["digest"])]
    images.extend((entry["image"], entry["tag"], entry["digest"]) for entry in lock["companions"].values())
    deadline = time.monotonic() + READINESS_SECONDS
    for image, image_tag, digest in images:
        while True:
            try:
                verify_index(image, image_tag, digest)
                break
            except (RegistryNotReady, urllib.error.URLError, TimeoutError, OSError) as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReleaseError(f"GHCR image not ready for {image}:{image_tag}: {exc}") from exc
                time.sleep(min(RETRY_SECONDS, remaining))


def verify_attestation(digest, tag):
    """Refuse a digest the tagged build workflow did not attest, so a mutable-tag race cannot pin the wrong image."""
    subject = f"oci://{IMAGE_REPOSITORIES['app']}@{digest}"
    command = ["gh", "attestation", "verify", subject, "--repo", "LavX/bazarr",
               "--signer-workflow", "LavX/bazarr/.github/workflows/build-docker.yml",
               "--source-ref", f"refs/tags/{tag}"]
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=120, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"Cannot verify the image attestation for {subject}: {exc}") from exc


def resolve_app_digest(tag):
    """Resolve the published app image digest for tag with the bounded retry verify_live uses."""
    image, version = IMAGE_REPOSITORIES["app"], tag[1:]
    deadline = time.monotonic() + READINESS_SECONDS
    while True:
        try:
            digest, _ = fetch_index(image, version)
            if not DIGEST.fullmatch(digest):
                raise RegistryNotReady(f"GHCR did not return an index digest for {image}:{version}")
            verify_index(image, version, digest)
            break
        except (RegistryNotReady, urllib.error.URLError, TimeoutError, OSError) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReleaseError(f"GHCR image not ready for {image}:{version}: {exc}") from exc
            time.sleep(min(RETRY_SECONDS, remaining))
    verify_attestation(digest, tag)
    return digest


def previous_lock_path(tag, releases):
    """Return the lock of the immediately previous final release in the same major.minor line."""
    version = tuple(int(part) for part in tag[1:].split("."))
    if releases.is_symlink() or not releases.is_dir():
        raise ReleaseError("Release lock directory must be a regular directory")
    bases = []
    for path in releases.iterdir():
        match = LOCK_NAME.fullmatch(path.name)
        if not match:
            continue
        candidate = tuple(int(match.group(index)) for index in (1, 2, 3))
        if candidate[:2] == version[:2] and candidate < version:
            bases.append((candidate, path))
    if not bases:
        line = f"{version[0]}.{version[1]}"
        raise ReleaseError(f"No previous release lock exists in the {line} line; "
                           "a first release of a new major.minor line needs a hand-reviewed lock")
    return max(bases)[1]


def bumped_revisions(base):
    """Bump the platform revisions a new application version requires."""
    # Revision policy follows the platform tables in RELEASING.md: Runtipi increments
    # tipi_version on updates, TrueNAS bumps its revision, and the rest carry over.
    revisions = dict(base["platform_revisions"])
    revisions["runtipi"] = revisions["runtipi"] + 1
    major, minor, patch = revisions["truenas"].split(".")
    revisions["truenas"] = f"{major}.{minor}.{int(patch) + 1}"
    return revisions


def resolve_lock(tag, releases_dir=None):
    """Resolve the release lock for a tag after its image build published the app image.

    Reads come from releases_dir when it is provided, so the workflow resolves against
    development's reviewed locks, and otherwise from the local packaging/releases checkout.
    A newly resolved lock is always written into the local checkout, whose bytes the lane
    then prepares and proposes.
    """
    if not VERSION.fullmatch(tag):
        raise ReleaseError(f"Expected a final stable tag such as v2.7.0, got {tag}")
    version = tag[1:]
    releases = releases_dir if releases_dir is not None else PACKAGING / "releases"
    lock_path = releases / f"{version}.json"
    if lock_path.is_symlink() or lock_path.exists():
        return lock_path
    base_path = previous_lock_path(tag, releases)
    base = read_lock(base_path, "v" + base_path.name.removesuffix(".json"))
    lock = {
        "schema": 1,
        "version": version,
        "app": {"image": IMAGE_REPOSITORIES["app"], "digest": resolve_app_digest(tag)},
        "companions": base["companions"],
        "runtime_profile": base["runtime_profile"],
        "platform_revisions": bumped_revisions(base),
    }
    validate_lock(lock, tag)
    written = PACKAGING / "releases" / f"{version}.json"
    data = canonical_json(lock)
    try:
        with written.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise ReleaseError(f"Release lock appeared during resolution: {written}") from exc
    return written


def checked_output(output):
    if ".." in output.parts:
        raise ReleaseError("Output path must not contain traversal")
    if output.is_symlink() or any(parent.is_symlink() for parent in output.absolute().parents):
        raise ReleaseError("Output path must not use symlinks")
    output = output.resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise ReleaseError("Output path already exists")
    if output == PACKAGING or PACKAGING in output.parents or output in PACKAGING.parents:
        raise ReleaseError("Output must be separate from package sources")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ReleaseError("Output parent must be an existing regular directory")
    return output


def prepare(tag, lock_path, output, live):
    lock = read_lock(lock_path, tag)
    output = checked_output(output)
    files = inventory_sources()
    if live:
        verify_live(lock, tag)
    provenance = git_provenance()
    with tempfile.TemporaryDirectory(prefix=".bazarr-release-", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        render(staging, files, lock)
        source_hashes = {f"{platform}/{relative.as_posix()}": sha256(source_path(platform, relative).read_bytes())
                         for platform, paths in files.items() for relative in (*paths, Path("LICENSE"), Path("NOTICES.md"))}
        rendered_hashes = {f"{platform}/{relative.as_posix()}": sha256((staging / "rendered" / platform / relative).read_bytes())
                           for platform in PLATFORMS for relative in package_paths(platform, files)}
        archives = create_archives(staging, files, tag, lock)
        provenance_path = staging / "provenance.json"
        provenance_path.write_bytes(canonical_json({
            "tag": tag, "lock_sha256": sha256(lock_path.read_bytes()), "runtime_profile": lock["runtime_profile"],
            "platform_revisions": lock["platform_revisions"], "packaging_git": provenance,
            "source_sha256": source_hashes, "rendered_sha256": rendered_hashes, "archive_sha256": archives,
            "live_verified": live,
        }))
        bundle_id = sha256(provenance_path.read_bytes())[:16]
        shutil.copyfile(provenance_path, staging / f"bazarr-plus-{tag}-{bundle_id}-provenance.json")
        shutil.copyfile(staging / "SHA256SUMS", staging / f"bazarr-plus-{tag}-{bundle_id}-SHA256SUMS.txt")
        if output.exists():
            raise ReleaseError("Output path appeared during preparation")
        staging.rename(output)
    return output


def prepare_all(locks, output):
    """Prepare an offline candidate for every release lock, failing on any lock that cannot be packaged."""
    if locks.is_symlink() or not locks.is_dir():
        raise ReleaseError("Lock directory must be a regular directory")
    names = [path.name for path in locks.iterdir()]
    unexpected = sorted(name for name in names if not LOCK_NAME.fullmatch(name))
    if unexpected:
        raise ReleaseError(f"Lock directory holds files that are not release locks: {', '.join(unexpected)}")
    if not names:
        raise ReleaseError("Lock directory holds no release locks")
    versions = sorted(tuple(int(part) for part in name.removesuffix(".json").split(".")) for name in names)
    tags = ["v" + ".".join(str(part) for part in version) for version in versions]
    output = checked_output(output)
    with tempfile.TemporaryDirectory(prefix=".bazarr-candidates-", dir=output.parent) as temporary:
        staging = Path(temporary) / "result"
        staging.mkdir()
        for tag in tags:
            prepare(tag, locks / f"{tag[1:]}.json", staging / tag, False)
        if output.exists():
            raise ReleaseError("Output path appeared during preparation")
        staging.rename(output)
    return [output / tag for tag in tags]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--resolve-lock", action="store_true",
                        help="resolve the release lock for --tag after its image build, then exit")
    parser.add_argument("--releases-dir", type=Path, metavar="DIRECTORY",
                        help="directory of reviewed release locks --resolve-lock reads instead of the local checkout")
    parser.add_argument("--all-locks", type=Path, metavar="DIRECTORY",
                        help="prepare an offline candidate for every release lock in DIRECTORY")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-live", action="store_true")
    args = parser.parse_args(argv)
    if args.all_locks and (args.tag or args.lock or args.verify_live or args.resolve_lock):
        parser.error("--all-locks cannot be combined with --tag, --lock, --resolve-lock or --verify-live")
    if args.resolve_lock and (args.lock or args.output or args.verify_live):
        parser.error("--resolve-lock cannot be combined with --lock, --output or --verify-live")
    if args.resolve_lock and not args.tag:
        parser.error("--resolve-lock requires --tag")
    if not args.resolve_lock and not args.output:
        parser.error("the following arguments are required: --output")
    if not args.resolve_lock and not args.all_locks and not (args.tag and args.lock):
        parser.error("--tag and --lock are required unless --all-locks is used")
    try:
        if args.resolve_lock:
            outputs = [resolve_lock(args.tag, args.releases_dir)]
        elif args.all_locks:
            outputs = prepare_all(args.all_locks, args.output)
        else:
            outputs = [prepare(args.tag, args.lock, args.output, args.verify_live)]
    except ReleaseError as exc:
        parser.exit(1, f"release preparation refused: {exc}\n")
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
