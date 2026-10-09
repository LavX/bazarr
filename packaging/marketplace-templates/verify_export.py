#!/usr/bin/env python3
"""Check this store tree against EXPORT.json before building or publishing it."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

COMMIT = re.compile(r"[0-9a-f]{40}\Z")
FEED = re.compile(r"https://[^\s]+\Z")


class VerificationError(Exception):
    pass


def actual_files(root):
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise VerificationError(f"symlink is not part of an export: {relative.as_posix()}")
        if path.is_file() and relative.as_posix() != "EXPORT.json":
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def verify(root, publish, repository, ref):
    try:
        record = json.loads((root / "EXPORT.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerificationError(f"cannot read EXPORT.json: {exc}") from exc
    if not isinstance(record, dict) or record.get("schema") != 1 or not isinstance(record.get("files"), dict):
        raise VerificationError("EXPORT.json is not a schema 1 store export")
    expected, actual = record["files"], actual_files(root)
    problems = [f"missing {name}" for name in sorted(set(expected) - set(actual))]
    problems += [f"unexpected {name}" for name in sorted(set(actual) - set(expected))]
    problems += [f"changed {name}" for name in sorted(set(expected) & set(actual)) if expected[name] != actual[name]]
    if problems:
        raise VerificationError("tree does not match EXPORT.json: " + ", ".join(problems))
    urls = record.get("urls") if isinstance(record.get("urls"), dict) else {}
    feed = str(urls.get("casaos_feed", ""))
    if not FEED.match(feed):
        raise VerificationError("EXPORT.json has no https CasaOS feed URL")
    if publish:
        if record.get("live_verified") is not True:
            raise VerificationError("export is not live-verified, regenerate it with --verify-live")
        provenance = record.get("packaging_git")
        if not isinstance(provenance, dict) or provenance.get("dirty") is not False \
                or not COMMIT.match(str(provenance.get("commit", ""))):
            raise VerificationError("export came from a dirty or unknown packaging commit")
        if str(record.get("repository", "")).lower() != repository.lower():
            raise VerificationError(f"export targets repository {record.get('repository')}, not {repository}")
        if record.get("ref") != ref:
            raise VerificationError(f"export targets ref {record.get('ref')}, not {ref}")
    return record, feed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", type=Path)
    parser.add_argument("--publish", action="store_true", help="also require a clean, live-verified export")
    parser.add_argument("--repository", default="", help="repository this run publishes from")
    parser.add_argument("--ref", default="", help="branch this run publishes from")
    args = parser.parse_args(argv)
    if args.publish and not (args.repository and args.ref):
        parser.error("--publish needs --repository and --ref")
    try:
        record, feed = verify(args.root, args.publish, args.repository, args.ref)
    except VerificationError as exc:
        parser.exit(1, f"export verification failed: {exc}\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"casaos-feed={feed}\n")
    print(f"EXPORT.json matches {len(record['files'])} files for {record.get('tag')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
