# coding=utf-8
"""Version ordering shared by plugin updates and host compatibility.

Semantic versions sort by semantic-version precedence: the numeric core
first, then a pre-release below its release, with numeric identifiers below
alphanumeric ones. Build metadata is ignored. Anything else falls back to a
token-wise order and always sorts below a semantic version.
"""
from __future__ import annotations

import re

from typing import Any

_VERSION_TOKEN_RE = re.compile(r"\d+|[A-Za-z]+")
_SEMVER_RE = re.compile(
    r"^\s*v?(?P<core>\d+(?:\.\d+)*)(?:-(?P<prerelease>[0-9A-Za-z.-]+))?\s*$"
)
# What `git describe` appends to a tag on a development build: commits since
# the tag, then the abbreviated commit. Such a build counts as its tag.
_GIT_DESCRIBE_RE = re.compile(r"^\d+-g[0-9a-f]{4,40}$")


def version_key(version: Any) -> tuple[Any, ...] | None:
    raw_version = str(version or "").strip()
    if not raw_version:
        return None
    comparable_version = raw_version.split("+", 1)[0]
    match = _SEMVER_RE.match(comparable_version)
    if match:
        core_parts = [int(part) for part in match.group("core").split(".")]
        while len(core_parts) < 3:
            core_parts.append(0)
        prerelease = match.group("prerelease")
        if prerelease is None:
            prerelease_key = (1,)
        else:
            prerelease_tokens: list[tuple[int, int | str]] = []
            for token in prerelease.split("."):
                if token.isdigit():
                    prerelease_tokens.append((0, int(token)))
                else:
                    prerelease_tokens.append((1, token.lower()))
            prerelease_key = (0, tuple(prerelease_tokens))
        return (1, tuple(core_parts), prerelease_key)

    tokens: list[tuple[int, int | str]] = []
    for token in _VERSION_TOKEN_RE.findall(comparable_version):
        if token.isdigit():
            tokens.append((0, int(token)))
        else:
            tokens.append((1, token.lower()))
    return (0, tuple(tokens))


def host_version_key(version: Any) -> tuple[Any, ...] | None:
    """The host's own version, where a `git describe` build counts as its tag.

    ``2.7.0-12-gabc1234`` is a development build twelve commits after 2.7.0,
    not a pre-release of 2.7.0. None when the host has no usable version,
    which only happens when running from a source tree without a VERSION file.
    """
    raw_version = str(version or "").strip().split("+", 1)[0]
    match = _SEMVER_RE.match(raw_version)
    if not match:
        return None
    prerelease = match.group("prerelease")
    if prerelease is not None and _GIT_DESCRIBE_RE.match(prerelease):
        raw_version = match.group("core")
    return version_key(raw_version)
