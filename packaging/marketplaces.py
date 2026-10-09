#!/usr/bin/env python3
"""Export a reviewed release candidate as the root of a store repository."""

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

import release
from release import PACKAGING, ReleaseError, canonical_json, exact_replace, sha256


OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}\Z")
NAME = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
REF = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]|/(?=[A-Za-z0-9]))*\Z")
TRUENAS_APP = Path("ix-dev/community/bazarr-plus")
# Catalog fixtures that the release inventory leaves out: (directory, suffix, names that must exist).
TRUENAS_FIXTURES = (
    (Path("templates/test_values"), ".yaml", {"basic-values.yaml", "media-host-paths.yaml"}),
    (Path("templates/library/base_v2_3_14/tests"), ".py", {"__init__.py"}),
)
PLATFORM_ROOTS = {
    "runtipi": (("bazarr-plus", "apps/bazarr-plus"),),
    "casaos": (("Apps", "Apps"),),
    "unraid": (("templates", "templates"), ("ca_profile.xml", "ca_profile.xml")),
    "stack": (("compose.yaml", "stacks/bazarr-plus/compose.yaml"),
              ("compose.media.yaml.example", "stacks/bazarr-plus/compose.media.yaml.example")),
    "truenas": (("ix-dev", "submissions/truenas/ix-dev"), ("LICENSE.LGPL-3.0", "submissions/truenas/LICENSE.LGPL-3.0"),
                ("THIRD_PARTY.md", "submissions/truenas/THIRD_PARTY.md")),
}
PER_PLATFORM = {"LICENSE", "NOTICES.md", "RELEASE.md"}
TEMPLATES = PACKAGING / "marketplace-templates"
# Destination build workflow, its export check and the legacy v1 inputs: (template, store path).
SCAFFOLD = (("store.yml", ".github/workflows/store.yml"), ("verify_export.py", ".github/scripts/verify_export.py"),
            ("category-list.json", "category-list.json"), ("featured-apps.json", "featured-apps.json"),
            ("recommend-list.json", "recommend-list.json"), ("build/.gitkeep", "build/.gitkeep"))


def checked_repository(repository):
    owner, _, name = repository.partition("/")
    if not OWNER.fullmatch(owner) or not NAME.fullmatch(name) or name in {".", ".."} or name.lower().endswith(".git"):
        raise ReleaseError("Repository must be a GitHub owner/name")
    return owner, name


def checked_ref(ref):
    if len(ref) > 100 or not REF.fullmatch(ref) or ".." in ref or ref.endswith((".", ".lock")):
        raise ReleaseError("Ref must be a plain branch name")
    return ref


def store_urls(repository, ref):
    owner, name = checked_repository(repository)
    pages = f"https://{owner.lower()}.github.io"
    raw = f"https://raw.githubusercontent.com/{repository}/{checked_ref(ref)}"
    return {
        "repository": f"https://github.com/{repository}",
        "casaos_feed": pages if name.lower() == f"{owner.lower()}.github.io" else f"{pages}/{name}",
        "unraid_template": f"{raw}/templates/bazarr-plus.xml",
        "stack_compose": f"{raw}/stacks/bazarr-plus/compose.yaml",
        "stack_media_overlay": f"{raw}/stacks/bazarr-plus/compose.media.yaml.example",
    }


def copy(source, destination):
    if source.is_symlink() or not source.is_file():
        raise ReleaseError(f"Expected a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def copy_rendered(rendered, store):
    for platform, roots in PLATFORM_ROOTS.items():
        base = rendered / platform
        mapped = {name for name, _ in roots}
        unexpected = sorted(path.name for path in base.iterdir() if path.name not in mapped | PER_PLATFORM)
        if unexpected:
            raise ReleaseError(f"Rendered {platform} files have no store location: {', '.join(unexpected)}")
        for name, target in roots:
            source = base / name
            paths = sorted(path for path in source.rglob("*") if path.is_file()) if source.is_dir() else [source]
            for path in paths:
                copy(path, store / target / path.relative_to(source))


def mark_revision(path, lock, platform):
    text = path.read_text()
    if "x-bazarr-plus-package" in text or not text.endswith("\n"):
        raise ReleaseError(f"Unexpected package marker in {path}")
    # Store clients compare file content, so the revision has to be visible in the Compose file itself.
    path.write_text(text + f'\nx-bazarr-plus-package:\n  version: "{lock["version"]}"\n'
                    f'  revision: "{lock["platform_revisions"][platform]}"\n')


def adapt(store, lock, urls):
    runtipi = store / "apps/bazarr-plus/config.json"
    config = json.loads(runtipi.read_text())
    # The relative schema hint resolves only inside the Runtipi app store checkout.
    config.pop("$schema", None)
    runtipi.write_bytes(canonical_json(config))
    for relative in ("Apps/BazarrPlus/docker-compose.yml", "Apps/BazarrPlusStack/docker-compose.yml"):
        mark_revision(store / relative, lock, "casaos")
    mark_revision(store / "stacks/bazarr-plus/compose.yaml", lock, "stack")
    template = store / "templates/bazarr-plus.xml"
    if "<TemplateURL>" in template.read_text() or "<Changes>" in template.read_text():
        raise ReleaseError("Unraid template already names a TemplateURL or Changes")
    anchor = "  <Project>https://lavx.github.io/bazarr/</Project>\n"
    exact_replace(template, anchor, anchor + f"  <TemplateURL>{urls['unraid_template']}</TemplateURL>\n"
                  f"  <Changes>Bazarr+ {lock['version']}, package revision {lock['platform_revisions']['unraid']}.</Changes>\n")


def restore_fixtures(store):
    source_app = PACKAGING / "truenas" / TRUENAS_APP
    target_app = Path("submissions/truenas") / TRUENAS_APP
    restored = {}
    for directory, suffix, required in TRUENAS_FIXTURES:
        source = source_app / directory
        if any(path.is_symlink() for path in (source, *source.parents)) or not source.is_dir():
            raise ReleaseError(f"Missing or linked TrueNAS fixture directory: {directory}")
        names = set()
        for path in sorted(source.iterdir()):
            if path.is_symlink():
                raise ReleaseError(f"Symlink in TrueNAS fixtures: {path}")
            if not path.is_file() or path.suffix != suffix or path.name.startswith("."):
                continue
            target = target_app / directory / path.name
            copy(path, store / target)
            restored[target.as_posix()] = {"source": path.relative_to(PACKAGING.parent).as_posix(),
                                           "sha256": sha256(path.read_bytes())}
            names.add(path.name)
        missing = sorted(required - names)
        if missing:
            raise ReleaseError(f"Missing TrueNAS fixtures: {', '.join(missing)}")
    return restored


def readme(lock, tag, repository, ref, urls, provenance):
    revisions = lock["platform_revisions"]
    companions = lock["companions"]
    if not provenance["live_verified"]:
        status = ("This is an offline candidate. The GitHub release and registry digests were not checked, "
                  "so it is not publication evidence.")
    else:
        status = ("The GitHub release and registry digests were verified when this tree was generated. "
                  "Review and native install checks still decide publication.")
    if provenance["packaging_git"]["dirty"]:
        status += (" The package sources had uncommitted changes, so this tree is not publication evidence "
                   "until it is regenerated from a clean commit.")
    return f"""# Bazarr+ packages

Store files for Bazarr+ {lock['version']} ({tag}), prepared for {urls['repository']} on branch `{ref}`.
Nothing here is published until this tree is pushed to that repository, and no store lists Bazarr+
until that store accepts it.

{status}

## Install routes

| Ecosystem | Path in this tree | Package revision | How it is used |
| --- | --- | --- | --- |
| Runtipi | `apps/bazarr-plus/` | {revisions['runtipi']} | Add {urls['repository']} as a custom app store, branch `{ref}`. |
| CasaOS / ZimaOS | `Apps/BazarrPlus/`, `Apps/BazarrPlusStack/` | {revisions['casaos']} | Build with the official v2 store tooling and publish the result. The store URL is then {urls['casaos_feed']} |
| Unraid | `templates/bazarr-plus.xml`, `ca_profile.xml` | {revisions['unraid']} | Template repository {urls['repository']}. TemplateURL: {urls['unraid_template']} |
| Compose / Portainer | `stacks/bazarr-plus/` | {revisions['stack']} | Compose file: {urls['stack_compose']} Media overlay example: {urls['stack_media_overlay']} |
| TrueNAS | `submissions/truenas/` | {revisions['truenas']} | Copy `ix-dev/community/bazarr-plus` into a `truenas/apps` pull request. Catalog tooling generates `item.yaml`. |

## Pinned images

- `{lock['app']['image']}:{lock['version']}@{lock['app']['digest']}`
- `{companions['translator']['image']}:{companions['translator']['tag']}@{companions['translator']['digest']}`
- `{companions['flaresolverr']['image']}:{companions['flaresolverr']['tag']}@{companions['flaresolverr']['digest']}`

Runtime profile: {lock['runtime_profile']}. Source commit `{provenance['packaging_git']['commit']}` of
https://github.com/LavX/bazarr. [EXPORT.json](EXPORT.json) records the lock hash and the SHA-256 of every
file in this tree. See [LICENSE](LICENSE) and [NOTICES.md](NOTICES.md); the TrueNAS library keeps
[its own license](submissions/truenas/LICENSE.LGPL-3.0).
"""


def build(store, candidate, lock, tag, repository, ref, urls):
    provenance = json.loads((candidate / "provenance.json").read_text())
    rendered = candidate / "rendered"
    copy_rendered(rendered, store)
    for name in ("LICENSE", "NOTICES.md"):
        copy(rendered / "stack" / name, store / name)
    adapt(store, lock, urls)
    owner, name = checked_repository(repository)
    (store / "store-config.json").write_bytes(canonical_json({
        "version": 2,
        "store_id": f"{owner.lower()}.{name.lower()}",
        "name": {"en_US": "Bazarr+ packages"},
        "description": {"en_US": "Bazarr+ apps for CasaOS and ZimaOS, maintained by LavX."},
        "maintainer": owner,
        "url": urls["repository"],
    }))
    (store / "supported-languages.json").write_bytes(canonical_json(["en_US"]))
    (store / "README.md").write_text(readme(lock, tag, repository, ref, urls, provenance))
    for template, target in SCAFFOLD:
        copy(TEMPLATES / template, store / target)
    # Fixtures come last: a refusal here must still leave no partial store behind.
    fixtures = restore_fixtures(store)
    files = {path.relative_to(store).as_posix(): sha256(path.read_bytes())
             for path in sorted(store.rglob("*")) if path.is_file()}
    companions = lock["companions"]
    (store / "EXPORT.json").write_bytes(canonical_json({
        "schema": 1, "tag": tag, "repository": repository, "ref": ref, "urls": urls,
        "lock_sha256": provenance["lock_sha256"], "runtime_profile": lock["runtime_profile"],
        "platform_revisions": lock["platform_revisions"],
        "images": {
            "app": f"{lock['app']['image']}:{lock['version']}@{lock['app']['digest']}",
            **{key: f"{entry['image']}:{entry['tag']}@{entry['digest']}" for key, entry in companions.items()},
        },
        "packaging_git": provenance["packaging_git"], "live_verified": provenance["live_verified"],
        "release_provenance_sha256": sha256((candidate / "provenance.json").read_bytes()),
        "truenas_fixtures": fixtures, "files": files,
    }))


def export(tag, lock_path, repository, ref, output, live):
    urls = store_urls(repository, ref)
    output = release.checked_output(output)
    with tempfile.TemporaryDirectory(prefix=".bazarr-store-", dir=output.parent) as temporary:
        candidate = release.prepare(tag, lock_path, Path(temporary) / "release", live)
        lock = release.read_lock(lock_path, tag)
        if sha256(lock_path.read_bytes()) != json.loads((candidate / "provenance.json").read_text())["lock_sha256"]:
            raise ReleaseError("Lock changed during export")
        store = Path(temporary) / "store"
        store.mkdir()
        build(store, candidate, lock, tag, repository, ref, urls)
        if output.exists():
            raise ReleaseError("Output path appeared during export")
        store.rename(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--repository", required=True, help="destination GitHub repository, owner/name")
    parser.add_argument("--ref", required=True, help="destination branch")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-live", action="store_true")
    args = parser.parse_args(argv)
    try:
        output = export(args.tag, args.lock, args.repository, args.ref, args.output, args.verify_live)
    except (ReleaseError, OSError) as exc:
        parser.exit(1, f"store export refused: {exc}\n")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
