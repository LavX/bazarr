#!/usr/bin/env python3
"""Generate the public sitemap with one meaningful last-modified date per page."""

from __future__ import annotations

import argparse
import datetime
import pathlib
import subprocess
import xml.etree.ElementTree as ET


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE_ROOT = REPO_ROOT / "site"
PUBLIC_ROOT = "https://lavx.github.io/bazarr/"
SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"


def pages() -> list[pathlib.Path]:
    guide_root = SITE_ROOT / "guides"
    guide_pages = sorted(
        page for page in guide_root.glob("*.html") if page.name != "index.html"
    )
    return [SITE_ROOT / "index.html", guide_root / "index.html", *guide_pages]


def public_url(page: pathlib.Path) -> str:
    relative = page.relative_to(SITE_ROOT).as_posix()
    if relative == "index.html":
        return PUBLIC_ROOT
    if relative == "guides/index.html":
        return f"{PUBLIC_ROOT}guides/"
    return f"{PUBLIC_ROOT}{relative}"


def last_modified(page: pathlib.Path) -> str:
    relative = page.relative_to(REPO_ROOT).as_posix()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", relative],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    if status.stdout.strip():
        return datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cs", "--", relative],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    date = result.stdout.strip()
    if not date:
        raise SystemExit(f"no Git history found for {relative}")
    return date


def render() -> bytes:
    ET.register_namespace("", SITEMAP_NAMESPACE)
    root = ET.Element(f"{{{SITEMAP_NAMESPACE}}}urlset")
    for page in pages():
        entry = ET.SubElement(root, f"{{{SITEMAP_NAMESPACE}}}url")
        ET.SubElement(entry, f"{{{SITEMAP_NAMESPACE}}}loc").text = public_url(page)
        ET.SubElement(entry, f"{{{SITEMAP_NAMESPACE}}}lastmod").text = last_modified(page)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=SITE_ROOT / "sitemap.xml",
        help="sitemap destination, defaults to site/sitemap.xml",
    )
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else REPO_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(render())
    try:
        print(output.relative_to(REPO_ROOT))
    except ValueError:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
