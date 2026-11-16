# coding=utf-8
"""The environment a plugin worker starts with.

A worker's environment is built from nothing: the host's environment is never
inherited, so tokens, config paths and proxy settings stay behind. What a worker
gets is a short fixed list, the same shape for every kind::

    PATH, LANG, PYTHONNOUSERSITE, PYTHONDONTWRITEBYTECODE   fixed
    HOME                                                    the plugin's data folder
    TMPDIR, TMP, TEMP                                       the plugin's temp folder
    TZ                                                      the host's, when it sets a valid one
    SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE       the system CA bundle, when one exists

No proxy variable is ever set, whatever the host's own proxy setting says: Bazarr+
runs no forwarder for workers, so a proxy address (and its credentials) would be
readable by plugin code and would not work through the sandbox's port rules anyway.
A name that mentions a proxy is refused here, in ``clean_env`` and in ``RuntimeSpec``.

The CA bundle is only ever a file the sandbox profile grants read access to, so
pointing the variables at it can never name a path the worker cannot open.
"""
from __future__ import annotations

import os
import re
from typing import Mapping, Sequence

from ..plugin_manifest import KIND_RE
from ..sandbox.policy import SYSTEM_READ_PATHS, PolicyError, clean_env, is_proxy_key

__all__ = [
    "CA_BUNDLE_CANDIDATES",
    "CA_BUNDLE_VARIABLES",
    "TEMP_VARIABLES",
    "find_ca_bundle",
    "host_timezone",
    "worker_environment",
]

# Names the policy fills in itself. ``extra`` may not override them.
TEMP_VARIABLES = ("TMPDIR", "TMP", "TEMP")
CA_BUNDLE_VARIABLES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_OWNED = frozenset((
    "PATH", "LANG", "HOME", "TZ", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", *TEMP_VARIABLES, *CA_BUNDLE_VARIABLES,
))

# System bundles in the order Debian, Fedora, Alpine and others use them. Only
# those inside a path the sandbox profile reads are offered.
CA_BUNDLE_CANDIDATES: tuple[str, ...] = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
    "/etc/pki/tls/cert.pem",
)

# A zone name or a POSIX rule, as TZ takes them. Anything stranger is dropped.
_TZ_RE = re.compile(r"\A[A-Za-z0-9_+\-:,./<>]{1,64}\Z")


def _granted(path: str) -> bool:
    return any(path == base or path.startswith(base.rstrip("/") + "/") for base in SYSTEM_READ_PATHS)


def _readable_bundle(path: str) -> bool:
    """A readable file the profile grants, by the name given and by where that name really leads."""
    return (
        _granted(path)
        and _granted(os.path.realpath(path))
        and os.path.isfile(path)
        and os.access(path, os.R_OK)
    )


def find_ca_bundle(candidates: Sequence[str] = CA_BUNDLE_CANDIDATES) -> str | None:
    """The first system CA bundle that is a readable file the sandbox profile grants."""
    for path in candidates:
        if _readable_bundle(path):
            return path
    return None


def host_timezone(host_env: Mapping[str, str] | None = None) -> str | None:
    """The host's ``TZ`` when it is set to something a zone name or POSIX rule can be, else None."""
    value = (os.environ if host_env is None else host_env).get("TZ")
    if not isinstance(value, str) or not _TZ_RE.match(value):
        return None
    return value


def worker_environment(
    kind: str,
    *,
    home: str | os.PathLike,
    tmpdir: str | os.PathLike,
    host_env: Mapping[str, str] | None = None,
    ca_bundle: str | None = None,
    path_prepend: Sequence[str | os.PathLike] = (),
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment for one worker of ``kind``, ready for ``RuntimeSpec(env=...)``.

    ``home`` is the plugin's data folder and ``tmpdir`` its temp folder; both
    are required, because a worker with no place to write has no business
    asking for the policy. ``host_env`` is read for ``TZ`` only (default: the
    process environment). ``ca_bundle`` overrides the discovered bundle, and must pass
    the same test (a readable file the profile grants, by name and after symlinks); leave
    it out to discover one. ``extra`` is merged last and may add names but never
    replace the ones the policy owns, and never a proxy variable.
    """
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        raise PolicyError("unknown kind syntax: %r" % (kind,))
    for key in extra or ():
        if isinstance(key, str) and (key in _OWNED or key.upper() in _OWNED):
            raise PolicyError("the environment policy owns %s" % key)
        if isinstance(key, str) and is_proxy_key(key):
            raise PolicyError("workers get no proxy variables: %s" % key)
    if home is None or tmpdir is None:
        raise PolicyError("a worker environment needs both a home and a tmpdir")
    env = clean_env(home=home, tmpdir=tmpdir, path_prepend=path_prepend, extra=extra)
    for name in TEMP_VARIABLES[1:]:
        env[name] = env["TMPDIR"]
    zone = host_timezone(host_env)
    if zone is not None:
        env["TZ"] = zone
    if ca_bundle is not None and not _readable_bundle(os.fspath(ca_bundle)):
        raise PolicyError("the CA bundle is not a readable file the sandbox profile grants: %s" % (ca_bundle,))
    bundle = ca_bundle if ca_bundle is not None else find_ca_bundle()
    if bundle:
        for name in CA_BUNDLE_VARIABLES:
            env[name] = os.fspath(bundle)
    return env
