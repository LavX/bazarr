# coding=utf-8
from __future__ import annotations

import contextlib
import contextvars
import copy
import io
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from hashlib import sha256

import requests

from collections import Counter
import threading
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from subliminal_patch.extensions import provider_registry

from secret_store.registry import SECRET_DESCRIPTOR_KEY as SECRET_DESCRIPTORS_KEY
from secret_store.scrub import mask_credential_refusal, scrub_job_secrets, scrub_text

from .manifest import ManifestValidationError, manifest_provider_id, validate_manifest
from .kinds import (ATTACH_LIVE, ATTACH_ON_RESTART, DETACH_LIVE, SUBTITLE_PROVIDER, UnknownKindError, catalog_kind_of, get_kind, kind_for_api,
                    kind_of, known_kinds)
from .kinds import subtitle_provider as provider_kind
from . import catalog_feed, closure, consent, detach, dev_source, floor, releases, revocation
from .bundle import verify_bundle_tree
from .compatibility import (
    CATALOG_LIST_PLUGINS, CATALOG_LIST_PROVIDERS, HostFacts, current_host, entry_compatibility, refusal_text,
    select_entry,
)
from .migration import (
    MIGRATED_BUILT_IN_PROVIDER_IDS,
    OFFICIAL_CATALOG_PROVIDER_IDS,
    RETIRED_BUILT_IN_PROVIDER_IDS,
    validation_built_in_provider_ids,
)
from .plugin_manifest import parse_plugin_manifest
from .state import (
    OFFICIAL_CATALOG_REPO,
    OFFICIAL_CATALOG_SOURCE_ID,
    OFFICIAL_CATALOG_URL,
    ORIGIN_BUNDLED,
    catalog_source_for_entry,
    load_state,
    mutate_state,
    provider_hub_dir,
    running_installations,
    spoofs_official_catalog_source,
)
from . import env_registry, transport
from .bundled_wheels import declared_wheels
from .venv import (
    _ENV_LOCK, PluginEnvironment, environment_fingerprint, hold_environment, pin_scope, python_executable,
    release_pins, validated_bundle_path, validated_interpreter,
)
from .worker import ProviderWorkerClient, release_when_gone, worker_command


class CatalogSourceError(ValueError):
    """Raised when a Provider Hub catalog source is not allowed."""


class ProviderHubInstallError(RuntimeError):
    """Raised when a Provider Hub install could not be staged."""


class ProviderHubConsentError(ProviderHubInstallError):
    """An install or update asked for consent that was not given, or given for a set that has changed."""

    def __init__(self, message: str, *, changed: bool = False):
        super().__init__(message)
        self.changed = changed


class ProviderHubStopped(Exception):
    """Stop was pressed, and the action ended at a step boundary with nothing recorded."""


class ProviderHubSettingsError(RuntimeError):
    """Raised when Bazarr's enabled_providers could not be saved for a Provider Hub action."""


ENABLED_PROVIDERS_NOT_SAVED = "the enabled providers list could not be saved to the configuration file"


SECRET_PLACEHOLDER = "********"
_JOB_LOG_LIMIT = 200
# A job that has not reported for this long is taken as dead. An install or a repair has time limits far below it.
JOB_TIME_LIMIT_SECONDS = 2 * 60 * 60
# Which run of Bazarr+ wrote a job row, so a row left open by a process that is gone can be told from a live one.
_PROCESS_SESSION = uuid.uuid4().hex
JOB_STOPPED_MESSAGE = "Bazarr+ stopped before this finished."
JOB_TIMED_OUT_MESSAGE = "This did not finish in time and was given up on."
PLUGIN_MANIFEST_UNREADABLE_REASON = "This plugin manifest could not be read, so it was not stored."
PLUGIN_KIND_CONFLICT_REASON = (
    "Another plugin in this catalog claims this id with a different kind, so this claim was not stored."
)
PLUGIN_VERSION_REPEATED_REASON = (
    "This version is listed more than once in this catalog, so none of its copies was stored."
)
PLUGIN_KIND_UNKNOWN_REASON = "The kind of this plugin is not defined by Bazarr+, so it was not stored."
PLUGIN_KIND_REFUSED_REASON = "The kind of this plugin refused its manifest, so it was not stored."
PLUGIN_KIND_MISMATCH_REASON = (
    "The kind this plugin declares does not match the kind its kind_api belongs to, so it was not stored."
)
PLUGIN_IN_PROVIDERS_LIST_REASON = (
    "A schema 2 manifest belongs in the plugins list, so it was not stored from the providers list."
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _persist_job(job: dict[str, Any]) -> None:
    # One row's message, error and details come from whatever raised, which
    # for a plugin failure is the plugin's own words. The row is read by the
    # API and, after a restart, by the boot that loads it, so it is scrubbed
    # once here: no writer of a row has to remember to. Every field takes an
    # exact credential value except the structural ones (the action, the ids,
    # the versions, the state and the timings): a password that happens to read
    # "install" must not rewrite those. The credential shapes are masked
    # everywhere.
    stored = copy.deepcopy(job)
    scrub_job_secrets(stored)

    def persist(state: dict[str, Any]) -> None:
        jobs = state.setdefault("jobs", [])
        if not isinstance(jobs, list):
            jobs = []
            state["jobs"] = jobs
        job_id = job.get("id")
        replaced = False
        for index, existing in enumerate(jobs):
            if isinstance(existing, dict) and existing.get("id") == job_id:
                jobs[index] = dict(stored)
                replaced = True
                break
        if not replaced:
            jobs.append(dict(stored))
        if len(jobs) > _JOB_LOG_LIMIT:
            del jobs[: len(jobs) - _JOB_LOG_LIMIT]

    mutate_state(persist)


class _JobHandle:
    """Mutable wrapper for the dict callers receive inside ``record_job``."""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    def update(self, **fields: Any) -> None:
        """Merge ``fields`` into the job payload and persist immediately.

        ``details`` is shallow-merged so callers can extend the metadata bag
        across multiple stages without clobbering earlier keys.
        """
        details = fields.pop("details", None)
        if details is not None:
            current = self.data.get("details") or {}
            if not isinstance(current, dict):
                current = {}
            current.update(details)
            self.data["details"] = current
        for key, value in fields.items():
            self.data[key] = value
        self.data["updated_at"] = utcnow_iso()
        _persist_job(self.data)


@contextlib.contextmanager
def record_job(
    action: str,
    *,
    target_kind: str = "system",
    target_id: str | None = None,
    target_name: str | None = None,
    source_id: str | None = None,
    source_name: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
):
    """Record the full lifecycle of a Provider Hub action.

    Writes one job row that transitions pending -> running -> completed/failed,
    capturing duration and any exception trace. The yielded handle exposes
    ``data`` (the live job dict) and ``update(**fields)`` so callers can fill
    in fields they only learn mid-flight (e.g. resolved ``to_version``).
    """
    job_id = str(uuid.uuid4())
    now = utcnow_iso()
    job = {
        "id": job_id,
        "action": action,
        "state": "pending",
        "target_kind": target_kind,
        "target_id": target_id,
        "target_name": target_name,
        "source_id": source_id,
        "source_name": source_name,
        "from_version": from_version,
        "to_version": to_version,
        "message": message,
        "error": None,
        "details": dict(details) if isinstance(details, dict) else {},
        "duration_ms": None,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
        "updated_at": now,
        "session": _PROCESS_SESSION,
    }
    handle = _JobHandle(job)
    _persist_job(job)
    started_perf = time.perf_counter()
    job["state"] = "running"
    job["started_at"] = utcnow_iso()
    job["updated_at"] = job["started_at"]
    _persist_job(job)
    try:
        yield handle
    except BaseException as error:
        job["state"] = "failed"
        job["error"] = str(error) or error.__class__.__name__
        job["completed_at"] = utcnow_iso()
        job["updated_at"] = job["completed_at"]
        job["duration_ms"] = int((time.perf_counter() - started_perf) * 1000)
        _persist_job(job)
        raise
    else:
        if job.get("state") != "failed":
            job["state"] = "completed"
        job["completed_at"] = utcnow_iso()
        job["updated_at"] = job["completed_at"]
        job["duration_ms"] = int((time.perf_counter() - started_perf) * 1000)
        _persist_job(job)


_DEV_REF_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,200}$")


def _validate_dev_ref(dev_ref):
    if dev_ref is None:
        return None
    if not isinstance(dev_ref, str):
        raise CatalogSourceError("dev_ref must be a string or null")
    trimmed = dev_ref.strip()
    if not trimmed:
        return None
    if not _DEV_REF_RE.match(trimmed):
        raise CatalogSourceError(
            "dev_ref contains characters not allowed in a git ref"
        )
    return trimmed


def _validate_github_catalog_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise CatalogSourceError("Provider Hub V1 only supports GitHub.com HTTPS catalog sources")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[2] not in ("blob", "raw"):
        raise CatalogSourceError("Catalog source must be a GitHub file URL")
    return url


def _parse_github_file_url(url: str) -> tuple[str, str, str, str]:
    parsed = urlparse(_validate_github_catalog_url(url))
    parts = [part for part in parsed.path.split("/") if part]
    owner, repo, _kind, ref, *path_parts = parts
    if not path_parts:
        raise CatalogSourceError("Catalog source must include a file path")
    return owner, repo, ref, "/".join(path_parts)


# The git smart-HTTP ref advertisement the catalog refresh resolves refs
# against: protocol v0, one GET, answered unauthenticated and outside the
# GitHub REST quota. The bounds are constants, not settings.
_GIT_ADVERTISEMENT_TIMEOUT = 20
_GIT_ADVERTISEMENT_MAX_BYTES = 10 * 1024 * 1024
_GIT_ADVERTISEMENT_CHUNK_BYTES = 64 * 1024
_PKT_LINE_MAX_PAYLOAD = 65516
_GIT_ADVERTISEMENT_CONTENT_TYPE = "application/x-git-upload-pack-advertisement"
_PKT_LINE_PREFIX_RE = re.compile(b"^[0-9a-f]{4}$")


_STREAMED_ERROR_BODY_LIMIT = 8193


def _retain_streamed_error_body(response) -> None:
    """Keep a bounded, decoded slice of a streamed error body, then close.

    The refresh's error mapper reads the body to tell a secondary rate limit
    from a plain access denial, but a streamed response that is closed unread
    has none, and the raw stream is content-encoded, so its undecoded bytes
    are not the body yet. Retain up to one byte over the mapper's 8,192-byte
    size boundary through iter_content, which decodes, so both the size
    distinction and the message survive the close. A chunked transfer yields
    one HTTP chunk at a time, so the decoded chunks are accumulated until the
    limit or end of stream, whichever comes first."""
    try:
        read = getattr(response, "iter_content", None)
        if callable(read):
            retained = bytearray()
            for chunk in read(_STREAMED_ERROR_BODY_LIMIT):
                retained += chunk
                if len(retained) >= _STREAMED_ERROR_BODY_LIMIT:
                    break
            response._content = bytes(retained[:_STREAMED_ERROR_BODY_LIMIT])
        else:
            response._content = b""
    except Exception:
        response._content = b""
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _fetch_git_ref_advertisement(owner: str, repo: str) -> bytes:
    """The v0 ref advertisement body of ``owner/repo``, fail-closed and
    size-capped while it is read, so an oversized body stops the download."""
    response = requests.get(
        f"https://github.com/{owner}/{repo}/info/refs?service=git-upload-pack",
        timeout=_GIT_ADVERTISEMENT_TIMEOUT,
        stream=True,
    )
    try:
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            _retain_streamed_error_body(response)
            raise
        headers = getattr(response, "headers", None) or {}
        media_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        if media_type != _GIT_ADVERTISEMENT_CONTENT_TYPE:
            raise CatalogSourceError("GitHub did not return a git ref advertisement")
        content_length = str(headers.get("Content-Length", "")).strip()
        if content_length.isdigit() and int(content_length) > _GIT_ADVERTISEMENT_MAX_BYTES:
            raise CatalogSourceError("GitHub's ref advertisement is too large to read")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=_GIT_ADVERTISEMENT_CHUNK_BYTES):
            total += len(chunk)
            if total > _GIT_ADVERTISEMENT_MAX_BYTES:
                raise CatalogSourceError("GitHub's ref advertisement is too large to read")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _parse_git_ref_advertisement(body: bytes, peel_tags: bool = True) -> dict[str, str]:
    """The branch and tag refs a v0 ref advertisement lists, by full name.

    A pkt-line stream in this exact shape: the service banner line, a flush,
    zero or more ``<sha> <ref>`` lines (the first carrying the capability
    list after a NUL), and one closing flush with nothing after it. Each
    line's 4-hex length prefix is four literal hex digits. An annotated tag
    line is followed by the line of its peeled commit. Only ``refs/heads/``
    and ``refs/tags/`` are read; every other namespace a real repository
    advertises, such as ``refs/pull/``, is skipped. The one line an empty
    repository advertises, the all-zero sha with the capabilities peel, is
    skipped, so the resolver reports the missing branch. Anything else fails
    closed.

    An annotated tag maps to its peeled commit, which is what resolving a ref
    needs. With ``peel_tags=False`` it maps to the tag object's own sha, so a
    tag recreated on the same commit still reads as a change.
    """
    if len(body) > _GIT_ADVERTISEMENT_MAX_BYTES:
        raise CatalogSourceError("GitHub's ref advertisement is too large to read")
    refs: dict[str, str] = {}
    peeled: dict[str, str] = {}
    empty_repository = False
    # banner: before the service line; banner_flush: between the service line
    # and its flush; refs: ref lines; terminated: after the closing flush.
    stage = "banner"
    previous_name: str | None = None
    offset = 0
    while offset < len(body):
        if len(body) - offset < 4:
            raise CatalogSourceError("GitHub sent a truncated ref advertisement")
        prefix = body[offset:offset + 4]
        if not _PKT_LINE_PREFIX_RE.match(prefix):
            raise CatalogSourceError("GitHub sent a malformed ref advertisement")
        length = int(prefix, 16)
        offset += 4
        if length == 0:  # A flush packet.
            if stage == "banner" or stage == "terminated":
                raise CatalogSourceError("GitHub sent a malformed ref advertisement")
            stage = "refs" if stage == "banner_flush" else "terminated"
            continue
        if stage == "terminated":
            raise CatalogSourceError("GitHub sent a malformed ref advertisement")
        if length < 4 or length - 4 > _PKT_LINE_MAX_PAYLOAD:
            raise CatalogSourceError("GitHub sent a malformed ref advertisement")
        if offset + length - 4 > len(body):
            raise CatalogSourceError("GitHub sent a truncated ref advertisement")
        line = body[offset:offset + length - 4].rstrip(b"\n")
        offset += length - 4
        if empty_repository:
            # The empty-repository marker must be the only ref line.
            raise CatalogSourceError("GitHub sent a malformed ref advertisement")
        if line.startswith(b"# service="):
            if stage != "banner" or line != b"# service=git-upload-pack":
                raise CatalogSourceError("GitHub sent a malformed ref advertisement")
            stage = "banner_flush"
            continue
        if stage != "refs":
            # An ordinary ref line belongs after the service banner and its
            # flush; the protocol's first pkt-line is the service banner.
            raise CatalogSourceError("GitHub sent a malformed ref advertisement")
        ref_part, _separator, _capabilities = line.partition(b"\0")
        sha, _space, name = ref_part.partition(b" ")
        try:
            sha_text = sha.decode("ascii")
            name_text = name.decode("utf-8")
        except UnicodeDecodeError:
            raise CatalogSourceError("GitHub sent a malformed ref advertisement") from None
        if not _is_commit_sha(sha_text) or not name_text:
            raise CatalogSourceError("GitHub sent a malformed ref advertisement")
        if name_text.endswith("^{}"):
            if (sha_text == "0" * 40 and name_text == "capabilities^{}"
                    and previous_name is None and not refs and not peeled):
                # git's documented advertisement of a repository with no
                # refs: one line, the all-zero sha and the capabilities
                # peel. No branch is advertised, so the resolver reports the
                # missing ref instead of blaming GitHub's framing.
                empty_repository = True
                continue
            # The peeled commit of the annotated tag on the line before it.
            target = name_text[:-3]
            if (previous_name != target or not target.startswith("refs/tags/")
                    or target not in refs):
                raise CatalogSourceError("GitHub sent a malformed ref advertisement")
            if target in peeled and peeled[target] != sha_text.lower():
                raise CatalogSourceError(
                    f"GitHub advertised '{target}' peeled at two different commits")
            peeled[target] = sha_text.lower()
            continue
        previous_name = name_text
        if not (name_text.startswith("refs/heads/") or name_text.startswith("refs/tags/")):
            continue
        if name_text in refs and refs[name_text] != sha_text.lower():
            raise CatalogSourceError(f"GitHub advertised '{name_text}' at two different commits")
        refs[name_text] = sha_text.lower()
    if stage != "terminated":
        raise CatalogSourceError("GitHub sent a truncated ref advertisement")
    if not peel_tags:
        return refs
    return {name: peeled.get(name, sha) for name, sha in refs.items()}


def _resolve_github_ref(
    owner: str, repo: str, ref: str, advertisement_sink=None
) -> str:
    if len(ref) == 40 and all(char in "0123456789abcdefABCDEF" for char in ref):
        return ref.lower()
    refs = _parse_git_ref_advertisement(_fetch_git_ref_advertisement(owner, repo))
    if advertisement_sink is not None:
        advertisement_sink(owner, repo, refs)
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}"):
        if name in refs:
            return refs[name]
    raise CatalogSourceError(
        f"GitHub did not advertise '{ref}' as a branch or tag of {owner}/{repo}. "
        "Use a full commit SHA, a branch name or a tag name."
    )


def _fetch_github_catalog(
    url: str, override_ref: str | None = None, unchanged_commit: str | None = None,
    advertisement_sink=None,
) -> tuple[dict[str, Any] | None, str]:
    """The catalog ``url`` serves and the commit it was resolved to. The
    catalog is None when the resolved commit equals ``unchanged_commit``:
    raw at a pinned commit is immutable, so there is nothing to fetch.
    ``advertisement_sink`` is told the parsed advertisement when one was
    read, so a caller that needs the same advertisement does not fetch it
    again."""
    owner, repo, ref, path = _parse_github_file_url(url)
    if override_ref:
        ref = override_ref
    commit = _resolve_github_ref(owner, repo, ref, advertisement_sink=advertisement_sink)
    if (unchanged_commit is not None and commit == unchanged_commit.lower()
            and not _is_commit_sha(ref)):
        # The skip is the ADVERTISED sha equaling the stored commit. A 40-hex
        # ref resolves through the short-circuit without an advertisement, so
        # a source pinned to a commit still fetches raw, and a deleted,
        # renamed or private repository keeps showing at refresh.
        return None, commit
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{path}"
    response = requests.get(raw_url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise CatalogSourceError("Catalog payload must be a JSON object")
    return payload, commit


def _fetch_raw_file(owner: str, repo: str, commit: str, path: str) -> bytes | None:
    """One file of the catalog repository at ``commit``, or None when the commit has no such file."""
    response = requests.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{path}", timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.content


def _fallback_setting_on() -> bool:
    """Whether the user lets the official catalog be read from its mirror when GitHub refuses."""
    from app.config import settings
    return bool(getattr(settings.general, "provider_hub_allow_fallback", True))


def _iso_from_epoch(moment: float | None) -> str | None:
    if moment is None:
        return None
    try:
        return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


class _Served:
    """Which origin served one fetch, why it was not the first, and the mirror pointer it relied on.

    ``failure`` is the first trigger failure the fetch met: ``surface``, ``origin``, ``kind``, ``status``,
    ``at`` and, when GitHub gave a reset time, ``retry_at``.
    """

    def __init__(self) -> None:
        self.origin: str | None = None
        self.failure: dict[str, Any] | None = None
        self.pointer: dict[str, str] | None = None
        self.unchanged = False
        # The signed file could not be read and the legacy catalog stood in for it: the next refresh asks again.
        self.signed_unreadable = False

    def observe(self, origin: str, surface: str, failure: "transport.Failure") -> None:
        if self.failure is not None:
            return
        self.failure = {
            "surface": surface, "origin": origin, "kind": failure.kind.value, "status": failure.status,
            "at": utcnow_iso(), "retry_at": _iso_from_epoch(failure.retry_at),
        }

    def note_origin(self, origin: str) -> None:
        # One mirror file in a fetch makes the fetch a mirror one.
        if self.origin != transport.PAGES_ORIGIN:
            self.origin = origin


def _note_served(source: dict[str, Any], served: "_Served | None") -> None:
    """Record on the official source where its catalog came from and what made it move."""
    if served is not None and served.unchanged:
        if served.pointer is not None:
            source["mirror_pointer"] = served.pointer
        return
    if served is None or served.origin is None and served.failure is None:
        source.pop("last_origin", None)
        source.pop("last_failure", None)
        return
    source["last_origin"] = served.origin
    source["last_failure"] = served.failure
    if served.pointer is not None:
        source["mirror_pointer"] = served.pointer


def _mirror_allowed_for(source: dict[str, Any], owner: str, repo: str, ref: str) -> bool:
    """Whether the official source may read the catalog from its mirror.

    Only the official source on the default branch qualifies, with the user's setting on. A source on a
    ``dev_ref`` reads the repository itself, so testing is never served from a copy. The signed feed has to
    be readable too: the unsigned legacy catalog is never taken from the mirror.
    """
    return (
        not source.get("dev_ref") and ref == "main"
        and f"{owner}/{repo}".lower() == transport.OFFICIAL_REPO.lower()
        and catalog_feed.signed_feed_enabled() and _fallback_setting_on()
    )


def _primary_has_no_file(error: BaseException, route: "transport.JobRoute") -> bool:
    """Whether ``error`` is the primary origin saying the commit has no such file.

    A 404 from the mirror, or from a primary that was skipped, says nothing about the repository:
    the route is on the mirror by then, and the file is not known to be missing.
    """
    return route.sticky is None and transport.classify_failure(error).kind is transport.FailureKind.NOT_FOUND


def _fetch_catalog_file(
    owner: str, repo: str, commit: str, channel: str, path: str, route: "transport.JobRoute | None",
    served: "_Served | None",
) -> bytes | None:
    """One file of the signed feed at ``commit``, or None when the primary origin says it does not exist."""
    if route is None:
        return _fetch_raw_file(owner, repo, commit, path)
    key = transport.FetchKey(f"{owner}/{repo}", channel, commit, path)
    try:
        result = transport.fetch_with_fallback(
            transport.default_origins(), key, surface=transport.SURFACE_CATALOG, official=True,
            allow_fallback=True, route=route, observer=served.observe if served is not None else None)
    except Exception as error:
        if _primary_has_no_file(error, route):
            return None
        if served is not None and isinstance(error, transport.NoOriginAvailable) and error.failure is not None:
            served.observe(transport.RAW_ORIGIN, transport.SURFACE_CATALOG, error.failure)
        raise
    if served is not None:
        served.note_origin(result.origin)
    return result.content


def _fetch_signed_feed(
    owner: str, repo: str, commit: str, channel: str, route: "transport.JobRoute | None" = None,
    served: "_Served | None" = None,
):
    """The signed files of ``channel`` at ``commit``, or None when the commit has no signed catalog.

    With a ``route`` the files are read through the origins, raw first and the mirror when raw fails. The
    absence of a signed catalog is only ever the primary's 404.
    """
    body = _fetch_catalog_file(
        owner, repo, commit, channel, catalog_feed.SIGNED_CATALOG_PATH.format(channel=channel), route, served)
    if body is None:
        return None
    try:
        return catalog_feed.FetchedFeed(
            catalog=body,
            envelope=_fetch_catalog_file(
                owner, repo, commit, channel, catalog_feed.SIGNED_ENVELOPE_PATH.format(channel=channel), route,
                served),
            key_file=_fetch_catalog_file(owner, repo, commit, channel, catalog_feed.KEY_FILE_PATH, route, served),
        )
    except Exception as error:
        # The signed catalog itself was served, so this commit is signed: whatever else it needs and
        # cannot be read is a failed refresh, never a reason to read the legacy catalog.
        error.signed_body_read = True
        raise


def _head_from_mirror(
    source: dict[str, Any], channel: str, advertisement_error: BaseException, served: "_Served | None",
) -> "transport.MirrorPointer":
    """The channel head the mirror names, for a refresh whose ref advertisement failed.

    Only called after a failure that is a trigger. The pointer is taken only when it is the stored commit or
    strictly ahead of it, so a stale mirror cannot roll the catalog back. When the mirror cannot help, the
    failure of GitHub is the one raised, so it is the one shown.
    """
    if served is not None:
        served.observe("github", transport.SURFACE_POINTER, transport.classify_failure(advertisement_error))
    try:
        pointer = transport.fetch_pointer(channel, observer=served.observe if served is not None else None)
        stored = source.get("resolved_commit")
        accepted = source.get("mirror_pointer")
        accepted_at = accepted.get("published_at") if isinstance(accepted, dict) else None
        transport.check_pointer_advance(
            pointer, stored if isinstance(stored, str) and stored else None,
            accepted_at if isinstance(accepted_at, str) else None)
    except transport.TransportError as refusal:
        if refusal.kind is transport.FailureKind.INTEGRITY:
            raise CatalogSourceError(
                f"GitHub did not answer, and the mirror was not used: {refusal}. "
                "Your cached catalog is unchanged.") from refusal
        raise advertisement_error from refusal
    except Exception as mirror_error:
        raise advertisement_error from mirror_error
    return pointer


def _fetch_official_catalog(
    source: dict[str, Any], state: dict[str, Any], ancestry, unchanged_commit: str | None = None,
    advertisement_sink=None, served: "_Served | None" = None,
):
    """The official catalog, read through the signature check.

    Returns ``(catalog, head, feed)``: the catalog dictionary (None when the head is unchanged), the
    commit the source's ref resolved to, and the ``VerifiedFeed`` when the catalog came from the signed
    feed (None for the legacy catalog). The head is the commit trust is judged on; the manifests of a
    verified catalog are pinned to the signed body's ``source_commit`` instead. The legacy catalog never
    carries a plugins list. Raises ``catalog_feed.FeedRefused`` when a signed catalog fails its check.

    When GitHub refuses the ref lookup or the files, and the setting allows it, the mirror stands in:
    it names the head, and the signed files are read from it when raw fails. What it serves is judged
    exactly as raw's bytes are, against the counters this host stored for the channel. ``served`` is told
    where the catalog came from.
    """
    channel = catalog_feed.feed_channel(source.get("dev_ref"))
    host_latched = any(catalog_feed.stored_record(state, name).latched
                       for name in (catalog_feed.CHANNEL_MAIN, catalog_feed.CHANNEL_BETA))
    if channel is not None and not catalog_feed.signed_feed_enabled() and host_latched:
        # A downgrade: this host accepted a signed catalog, and this release cannot verify one. The
        # legacy catalog is not a substitute, so say why and what to do instead of reading it.
        raise catalog_feed.FeedRefused(
            catalog_feed.Status.UNSIGNED, catalog_feed.NO_ROOT_REASON.format(channel=channel))
    if channel is None or not catalog_feed.signed_feed_enabled():
        payload, head = _fetch_github_catalog(
            source["url"], override_ref=source.get("dev_ref"), unchanged_commit=unchanged_commit,
            advertisement_sink=advertisement_sink,
        )
        if payload is not None:
            payload.pop("plugins", None)
        return payload, head, None
    owner, repo, ref, _path = _parse_github_file_url(source["url"])
    ref = source.get("dev_ref") or ref
    mirror = _mirror_allowed_for(source, owner, repo, ref)
    route = transport.JobRoute() if mirror else None
    pointer = None
    try:
        head = _resolve_github_ref(owner, repo, ref, advertisement_sink=advertisement_sink)
    except Exception as error:
        if not mirror or not transport.classify_failure(error).trigger:
            raise
        pointer = _head_from_mirror(source, channel, error, served)
        head = pointer.commit
    if unchanged_commit is not None and head == unchanged_commit.lower() and not _is_commit_sha(ref):
        if served is not None and mirror:
            # Nothing was read, so where the held catalog came from is unchanged.
            served.unchanged = True
            served.pointer = _pointer_record(pointer, source)
        return None, head, None
    stored = catalog_feed.stored_record(state, channel)
    try:
        fetched = _fetch_signed_feed(owner, repo, head, channel, route, served)
    except (requests.exceptions.RequestException, transport.TransportError) as error:
        if stored.latched or host_latched or pointer is not None or getattr(error, "signed_body_read", False):
            raise
        # Nothing has ever been accepted as signed here, so a signed file that cannot be read is
        # the same as one that is not there: the legacy catalog, as before a root was embedded.
        logging.getLogger(__name__).info(
            "The signed catalog could not be read (%s); reading the legacy catalog instead.", error)
        if served is not None:
            served.signed_unreadable = True
        fetched = None
    feed = catalog_feed.verify_fetched(
        fetched,
        channel=channel,
        stored=stored, now=catalog_feed.current_time(),
        host_build_time=catalog_feed.build_time(), host_latched=host_latched,
    )
    if feed is None:
        if pointer is not None:
            # The commit was named by the mirror, so nothing anchors an unsigned catalog at it.
            raise CatalogSourceError(
                "GitHub did not answer, and the mirror's catalog has no signed file, so it was not used. "
                "Your cached catalog is unchanged.")
        # No signed file at this commit and the channel has never latched: the legacy catalog.
        payload, _same = _fetch_github_catalog(source["url"], override_ref=head)
        payload.pop("plugins", None)
        if served is not None and mirror:
            served.note_origin(transport.RAW_ORIGIN)
        return payload, head, None
    if feed.source_commit != head:
        verdict, _reason = ancestry.check(feed.source_commit)
        if verdict is False:
            raise catalog_feed.FeedRefused(
                catalog_feed.Status.MALFORMED,
                "The signed catalog names a commit that is not on the official catalog's main or beta.",
                key_file_version=feed.result.key_file_version)
    try:
        revocation.parse(feed.catalog.get(revocation.FIELD_NAME))
    except revocation.RevocationFormatError as error:
        # The list is signed with the rest, but a host that cannot read an entry cannot tell what it revokes:
        # the catalog is refused whole and the last good one, list included, stays in force.
        raise catalog_feed.FeedRefused(
            catalog_feed.Status.MALFORMED,
            f"The signed catalog's revocation list could not be read ({error}).",
            key_file_version=feed.result.key_file_version) from error
    if served is not None:
        served.pointer = _pointer_record(pointer, source)
    return feed.catalog, head, feed


def _pointer_record(pointer: "transport.MirrorPointer | None", source: dict[str, Any]) -> dict[str, str] | None:
    """What to remember of the mirror pointer a refresh relied on: the commit and when it was published.

    The stamp is a high-water mark: a pointer for the stored commit with an older stamp never lowers it,
    so a later pointer cannot slip in under it. A refresh that did not use the pointer keeps the last one.
    """
    held = source.get("mirror_pointer")
    held = held if isinstance(held, dict) else None
    if pointer is None:
        return held
    held_at = held.get("published_at") if held else None
    stamp = max(pointer.published_at, held_at) if isinstance(held_at, str) else pointer.published_at
    return {"commit": pointer.commit, "published_at": stamp}


# The official source is trusted only on these branches of the official catalog
# repository, or on a commit that is an ancestor of one of them.
_TRUSTED_BRANCHES = ("main", "beta")
# The reasons a refresh records when it trusts the official source.
_TRUST_REASONS = ("default branch", "beta", *(f"ancestor of {branch}" for branch in _TRUSTED_BRANCHES))


class _NoAnswer(Exception):
    """GitHub could not be asked (rate limit, network, an error status).

    Not an answer either way: no trust is granted or taken on it."""


def _github_verification_failure_reason(status: Any) -> str:
    """The reason a trust-verification question went unanswered, for the HTTP
    ``status`` GitHub answered with (None when there was no answer to read,
    or the body could not be parsed). One mapping serves the REST compare
    calls and the advertisement read, so the two cannot drift apart."""
    if status in (403, 429):
        return "could not verify: rate limited"
    if status:
        return f"could not verify: GitHub returned HTTP {status}"
    return "could not verify: GitHub could not be reached"


def _github_api(path: str) -> Any:
    """GET ``api.github.com/repos/<path>``. None is GitHub's 404: it does not know
    the commit, which is an answer, unlike any other failure."""
    try:
        response = requests.get(f"https://api.github.com/repos/{path}", timeout=20)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status == 404:
            return None
        raise _NoAnswer(_github_verification_failure_reason(status)) from error
    except (requests.exceptions.RequestException, ValueError) as error:
        raise _NoAnswer(_github_verification_failure_reason(None)) from error


def _is_commit_sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def _compare_status(repo: str, base: str, head: str) -> str | None:
    """GitHub's compare status of ``head`` against ``base``, or None for a 404."""
    comparison = _github_api(f"{repo}/compare/{base}...{head}")
    if comparison is None:
        return None
    status = comparison.get("status") if isinstance(comparison, dict) else None
    if not isinstance(status, str):
        raise _NoAnswer("could not verify: GitHub returned no comparison")
    return status


class _OfficialAncestry:
    """Whether a commit is on ``main`` or ``beta`` of the official catalog.

    One instance serves one refresh, which reads both branch heads in a single
    call. Answers are kept in state per commit and branch, and hold only for the
    heads they were computed against. When a head moved by fast-forward, its yes
    answers carry over, because an ancestor of the old head is one of the new head
    too; its no answers are asked again. A head that moved any other way is asked
    again in full. A failure is never stored.
    """

    def __init__(self, state: dict[str, Any]):
        cache = state.get("trust_cache")
        if not isinstance(cache, dict):
            cache = {}
            state["trust_cache"] = cache
        self._cache = cache
        self._heads_read = False
        self._heads_failure: str | None = None
        self._noted_refs: dict[str, str] | None = None

    def note_advertisement(self, owner: str, repo: str, refs: dict[str, str]) -> None:
        """Keep a successfully parsed advertisement of the official catalog
        repository for this refresh, so the installed-trust recheck does not
        fetch the same advertisement again. Any other repository's refs are
        ignored."""
        if f"{owner}/{repo}" == OFFICIAL_CATALOG_REPO and not self._heads_read:
            self._noted_refs = refs

    def _read_heads(self) -> None:
        if self._heads_read:
            return
        self._heads_read = True
        refs = self._noted_refs
        if refs is None:
            try:
                owner, repo = OFFICIAL_CATALOG_REPO.split("/", 1)
                refs = _parse_git_ref_advertisement(_fetch_git_ref_advertisement(owner, repo))
            except requests.exceptions.HTTPError as error:
                status = getattr(getattr(error, "response", None), "status_code", None)
                if status in (401, 404):
                    # github.com answers an anonymous request for a missing or
                    # private repository with 401, so it lists no branches,
                    # like an advertisement without them.
                    pass
                else:
                    self._heads_failure = _github_verification_failure_reason(status)
                    return
            except (requests.exceptions.RequestException, CatalogSourceError):
                # A body that could not be read or parsed maps to the same
                # unreachable wording the REST path gave a body it could not
                # parse, so the reason strings keep their base meaning.
                self._heads_failure = _github_verification_failure_reason(None)
                return
            if refs is None:
                refs = {}
        heads: dict[str, str] = {}
        for branch in _TRUSTED_BRANCHES:
            sha = refs.get(f"refs/heads/{branch}")
            if _is_commit_sha(sha):
                heads[branch] = sha.lower()
        if set(heads) != set(_TRUSTED_BRANCHES):
            # Both branches always exist, so an answer without them is not one.
            self._heads_failure = "could not verify: GitHub did not list main and beta"
            return
        previous = self._cache.get("heads") if isinstance(self._cache.get("heads"), dict) else {}
        answers = self._cache.get("answers") if isinstance(self._cache.get("answers"), dict) else {}
        for branch in _TRUSTED_BRANCHES:
            old, new = previous.get(branch), heads.get(branch)
            if old == new:
                continue
            fast_forward = False
            if old and new:
                try:
                    fast_forward = _compare_status(OFFICIAL_CATALOG_REPO, old, new) == "ahead"
                except _NoAnswer:
                    fast_forward = False
            for commit_answers in answers.values():
                if not (fast_forward and commit_answers.get(branch) is True):
                    commit_answers.pop(branch, None)
        self._cache["heads"] = heads
        self._cache["answers"] = {commit: value for commit, value in answers.items() if value}

    def check(self, commit: Any) -> tuple[bool | None, str]:
        """(True, reason), (False, reason), or (None, why it could not be asked)."""
        if not _is_commit_sha(commit):
            return False, "unknown commit"
        commit = commit.lower()
        self._read_heads()
        if self._heads_failure:
            return None, self._heads_failure
        heads = self._cache["heads"]
        answers = self._cache["answers"].setdefault(commit, {})
        failure = None
        for branch in _TRUSTED_BRANCHES:
            if branch not in answers:
                try:
                    status = _compare_status(OFFICIAL_CATALOG_REPO, heads[branch], commit)
                except _NoAnswer as error:
                    failure = str(error)
                    continue
                # "unknown" is GitHub's 404: the commit is not in the repository.
                answers[branch] = status in ("identical", "behind") or (
                    "unknown" if status is None else False
                )
            if answers[branch] is True:
                return True, f"ancestor of {branch}"
        if failure:
            if not answers:
                del self._cache["answers"][commit]
            return None, failure
        if "unknown" in answers.values():
            return False, "unknown commit"
        return False, "outside main and beta"


def _catalog_source_error_message(error: Exception) -> str:
    failure = transport.classify_failure(error)
    if failure.kind in transport.UNREACHABLE_KINDS:
        return (
            "Could not reach GitHub while refreshing this catalog source. "
            "Check network or DNS and try again."
        )
    if failure.http:
        if failure.kind is transport.FailureKind.RATE_LIMITED:
            reset = None
            if failure.retry_at is not None:
                try:
                    reset = datetime.fromtimestamp(failure.retry_at, timezone.utc)
                except (ValueError, TypeError, OverflowError, OSError):
                    reset = None
            recovery = (f"Retry after {reset:%Y-%m-%d %H:%M:%S} UTC."
                        if reset is not None else "GitHub did not provide a reset time. Retry later.")
            return f"GitHub temporarily limited catalog requests. {recovery} Your cached catalog is unchanged."
        if failure.kind is transport.FailureKind.FORBIDDEN:
            return "GitHub denied access to this catalog source. Check the repository URL and access permissions."
        if failure.kind in (transport.FailureKind.UNAUTHORIZED, transport.FailureKind.NOT_FOUND):
            # github.com answers an anonymous request for a missing or private
            # repository with 401, so it means the same as a 404 here.
            return "GitHub does not have this catalog source. Check the repository URL and that its catalog file exists."
        if failure.status:
            return f"GitHub returned HTTP {failure.status} while refreshing this catalog source."
        return "GitHub returned an error while refreshing this catalog source."
    return str(error)


def add_catalog_source(
    name: str, url: str, trusted: bool = False, dev_ref: str | None = None
) -> dict[str, Any]:
    with record_job(
        "add_source",
        target_kind="source",
        target_id=name,
        target_name=name,
    ) as job:
        if not name or not isinstance(name, str):
            raise CatalogSourceError("Catalog source name is required")
        name = name.strip()
        if not name:
            raise CatalogSourceError("Catalog source name is required")
        url = _validate_github_catalog_url(url)
        if spoofs_official_catalog_source(name):
            raise CatalogSourceError("The official catalog source name is reserved")
        is_official = name == OFFICIAL_CATALOG_SOURCE_ID and url == OFFICIAL_CATALOG_URL
        dev_ref_value = _validate_dev_ref(dev_ref)

        source = {
            "id": name,
            "name": name,
            "type": "github",
            "url": url,
            "enabled": True,
            "official": is_official,
            "trusted": is_official,
            "dev_ref": dev_ref_value,
            "registration_id": uuid.uuid4().hex,
            "last_checked_at": None,
            "last_error": None,
        }
        def store_source(state: dict[str, Any]) -> dict[str, Any]:
            state.setdefault("catalog_sources", {})[name] = source
            return dict(source)

        source = mutate_state(store_source)
        job.update(
            source_id=name,
            source_name=name,
            message=f"Added catalog source '{name}'",
            details={"url": url, "trusted": is_official, "dev_ref": dev_ref_value},
        )
        return source


def remove_catalog_source(name: str) -> bool:
    with record_job(
        "remove_source",
        target_kind="source",
        target_id=name,
        target_name=name,
    ) as job:
        if name == OFFICIAL_CATALOG_SOURCE_ID:
            job.update(message="Refused to remove the official catalog source")
            return False

        def remove_source(state: dict[str, Any]) -> tuple[bool, str]:
            sources = state.setdefault("catalog_sources", {})
            if name not in sources:
                return False, name
            removed_name = sources[name].get("name", name) if isinstance(sources[name], dict) else name
            del sources[name]
            # Drop catalog_entries that came from this source. Without this purge
            # the marketplace keeps offering stale entries and update checks may
            # match them as available upgrades, which surprises the user.
            entries = state.setdefault("catalog_entries", {})
            for key, entry in list(entries.items()):
                if not isinstance(entry, dict):
                    continue
                if entry.get("source") == name or entry.get("source_name") == removed_name:
                    del entries[key]
            # Its installs stay, with no source to update from. A source added
            # later under the same name is a different catalog and must not
            # inherit them.
            for installation in (state.get("installations") or {}).values():
                if (isinstance(installation, dict) and installation.get("source_bound")
                        and installation.get("source_id") == name):
                    installation["source_id"] = None
                    installation["trust_note"] = _SOURCE_REMOVED_NOTE
            return True, removed_name

        removed, _removed_name = mutate_state(remove_source)
        if not removed:
            job.update(message=f"Catalog source '{name}' not found")
            return False
        job.update(message=f"Removed catalog source '{name}'")
        return True


_UNSET = object()


def _find_catalog_source(state: dict[str, Any], identifier: str):
    """Return ``(key, source_dict)`` matching ``identifier`` by id or name.

    Catalog sources are stored under their ``id`` (or the explicit name the
    caller passed to add_catalog_source). The official source keys on
    ``"official"`` but exposes the long display name to the UI, so callers
    can hit either; we look up by exact key first and fall back to matching
    the ``name`` field.
    """
    sources = state.get("catalog_sources") or {}
    source = sources.get(identifier)
    if isinstance(source, dict):
        return identifier, source
    for key, candidate in sources.items():
        if isinstance(candidate, dict) and candidate.get("name") == identifier:
            return key, candidate
    return None, None


def update_catalog_source(name: str, dev_ref=_UNSET) -> dict[str, Any] | None:
    """Update a catalog source in place.

    Currently only ``dev_ref`` is mutable. Leave it unset to no-op; pass
    ``None`` to clear it; pass a string to set it. The validated ref is
    stored on the source dict. Returns the updated source, or ``None`` if no
    source matches the identifier (by id or by display name).
    """
    with record_job(
        "update_source",
        target_kind="source",
        target_id=name,
        target_name=name,
    ) as job:
        validated_dev_ref = _validate_dev_ref(dev_ref) if dev_ref is not _UNSET else _UNSET
        binding_revision = uuid.uuid4().hex

        def update_source(state: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
            _key, source = _find_catalog_source(state, name)
            if source is None:
                return None, None
            previous_dev_ref = source.get("dev_ref")
            if dev_ref is not _UNSET:
                # The stored commit stays: update_catalog_source only takes a
                # dev_ref, so the URL cannot change, and raw content at a
                # fixed commit never changes, so an unchanged-commit skip
                # stays correct for a new ref that resolves to the same one.
                # The official trust input (previous_commit) keeps its value.
                source["dev_ref"] = validated_dev_ref
                if previous_dev_ref != validated_dev_ref:
                    source["binding_revision"] = binding_revision
            return dict(source), previous_dev_ref

        source, previous_dev_ref = mutate_state(update_source)
        if source is None:
            job.update(message=f"Catalog source '{name}' not found")
            return None
        job.update(
            source_id=source.get("id"),
            source_name=source.get("name"),
            from_version=previous_dev_ref,
            to_version=source.get("dev_ref"),
            message=f"Updated catalog source '{source.get('name') or name}'",
            details={"dev_ref": source.get("dev_ref")},
        )
        return source


def get_catalog_source(name: str) -> dict[str, Any] | None:
    state = load_state()
    _key, source = _find_catalog_source(state, name)
    return source


def _catalog_needs_auto_refresh(state: dict[str, Any]) -> bool:
    for source in (state.get("catalog_sources") or {}).values():
        if not isinstance(source, dict):
            continue
        if (source.get("enabled", True)
                and source.get("last_checked_at") is None
                and source.get("last_attempted_at") is None):
            return True
    return False


def list_catalog(auto_refresh: bool = False) -> dict[str, Any]:
    state = load_state()
    if auto_refresh and _catalog_needs_auto_refresh(state):
        refresh_catalog()
        state = load_state()
    sources = state.get("catalog_sources") or {}
    host = current_host()
    entries = []
    for entry in (state.get("catalog_entries") or {}).values():
        if isinstance(entry, dict):
            # Named from the source record as it is now. Marked when its source
            # may not install it, or when this Bazarr+ cannot run it.
            source = catalog_source_for_entry(sources, entry)
            entry = {**entry, "source_name": source.get("name") or entry.get("source"),
                     "development_source": dev_source.is_dev_source(entry.get("source"), source)}
            refusal = (_install_refusal(state, entry.get("source"), entry.get("provider_id"),
                                        catalog_kind_of(entry), entry.get("version"), entry.get("name"))
                       or _host_refusal(entry, host)
                       or _closure_refusal(state, entry, host))
            if refusal:
                entry["blocked_reason"] = refusal
        entries.append(entry)
    return {
        "sources": [
            {**source, "development_source": dev_source.is_dev_source(source.get("id"), source)}
            if isinstance(source, dict) else source
            for source in (state.get("catalog_sources") or {}).values()
        ],
        "entries": entries,
    }


def _stored_manifest_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The stored catalog entries whose manifest is an object."""
    return [
        entry for entry in (state.get("catalog_entries") or {}).values()
        if isinstance(entry, dict) and isinstance(entry.get("manifest"), dict)
    ]


def _latest_official_catalog_manifest(state: dict[str, Any], provider_id: str) -> dict[str, Any] | None:
    """Latest manifest for ``provider_id`` from the OFFICIAL (trusted) catalog source."""
    host = current_host()

    def usable(entry: dict[str, Any]) -> bool:
        # Entries stored before the plugins list existed carry no catalog_list and came from providers.
        if entry.get("catalog_list", CATALOG_LIST_PROVIDERS) != CATALOG_LIST_PROVIDERS:
            return False
        if _revocation_refusal(state, entry.get("provider_id"), entry.get("version")):
            return False
        return bool(entry.get("trusted", False)) and _host_refusal(entry, host) is None

    entry = select_entry(
        _stored_manifest_entries(state), provider_id, usable=usable, source_id=OFFICIAL_CATALOG_SOURCE_ID)
    return entry["manifest"] if entry else None


def autoinstall_enabled_builtins() -> list[str]:
    """Stage-install official-catalog versions of enabled providers not yet installed.

    For each provider in Bazarr's ``enabled_providers`` that (a) has no Provider Hub
    installation row yet and (b) has a matching entry in the OFFICIAL catalog, stage
    the latest official manifest. The caller's ``activate_staged_installations()`` +
    ``register_active_provider_classes()`` then activate and shadow the built-in in the
    same boot (catalog plugins reuse the built-in id). Idempotent and best-effort:
    never raises, never enables/disables/uninstalls anything.
    """
    import logging
    import os

    logger = logging.getLogger(__name__)
    if os.environ.get("BAZARR_DISABLE_PROVIDER_AUTOINSTALL"):
        return []

    # Opt-in (default off): replacing built-in providers with their catalog versions at
    # startup is automatic only when the user enables it. Manual Marketplace install/replace
    # is unaffected, and turning this off never uninstalls already-active plugins.
    from app.config import settings
    if not getattr(settings.general, "provider_hub_auto_install", False):
        logger.debug("Provider Hub startup auto-install disabled (opt-in off); skipping")
        return []

    logger.info("Provider Hub startup auto-install: reconciling enabled providers against the official catalog")

    try:
        state = load_state()
        # Skip rows that are live or mid-lifecycle, but NOT "failed": a transient
        # bundle/venv error should be retried on a later boot rather than blocking
        # the migration forever.
        existing = {
            provider_id
            for provider_id, row in (state.get("installations") or {}).items()
            if isinstance(row, dict) and row.get("state") != "failed"
        }
        enabled = provider_kind.bazarr_enabled_providers()
    except Exception:
        logger.exception("Provider Hub startup auto-install: could not load state; skipping")
        return []

    candidates = [provider_id for provider_id in enabled if provider_id not in existing]
    if not candidates:
        # Everything enabled is already installed: no migration work, so don't touch
        # the network at all. Keeps the common boot fast and independent of catalog
        # source health.
        return []

    # There is potential work, so force a fresh OFFICIAL catalog (only) to pick up a
    # newly-merged provider id. The refresh runs during startup, before the web
    # server starts. Its network calls time out after 20 seconds (git ref
    # advertisement and each GitHub comparison) or 30 seconds (raw catalog fetch). A
    # failure falls back to the cached catalog. The 180 second budget below bounds
    # only the staging loop. Third-party sources are not fetched on the boot path.
    try:
        refresh_catalog(source_ids={OFFICIAL_CATALOG_SOURCE_ID})
        state = load_state()
    except Exception:
        logger.exception("Provider Hub startup auto-install: catalog refresh failed; using cached catalog")

    budget_seconds = 180.0
    started = time.monotonic()
    staged: list[str] = []
    for provider_id in candidates:
        manifest = _latest_official_catalog_manifest(state, provider_id)
        if manifest is None:
            continue
        remaining = budget_seconds - (time.monotonic() - started)
        if remaining <= 0:
            logger.warning(
                "Provider Hub startup auto-install: time budget exceeded at %s; deferring remaining to next start",
                provider_id,
            )
            break
        try:
            # Bound the install itself by the remaining budget so one stuck pip/venv
            # step can't block boot indefinitely; failures are swallowed and the
            # built-in keeps serving until the next start.
            stage_install(manifest, install_timeout=remaining)
            staged.append(provider_id)
            logger.info("Provider Hub startup auto-install: staged %s from the official catalog", provider_id)
        except Exception:
            logger.exception("Provider Hub startup auto-install: failed to stage %s; keeping built-in", provider_id)
    return staged


def _set_official_trust(
    source: dict[str, Any], commit: str, previous_commit: Any, ancestry: _OfficialAncestry
) -> None:
    """Trust the official source only on main or beta, or a commit on either."""
    ref = source.get("dev_ref") or "main"
    if ref in _TRUSTED_BRANCHES:
        # Resolved by name in the official repository.
        trusted, reason = True, ("default branch" if ref == "main" else "beta")
    else:
        trusted, reason = ancestry.check(commit)
        if trusted is None:
            # No answer keeps the verdict a refresh already gave this same
            # commit, so a commit without an earlier yes stays untrusted.
            earlier = source.get("trust_reason")
            trusted = bool(source.get("trusted")) and previous_commit == commit and earlier in _TRUST_REASONS
            if trusted:
                reason = earlier
    source["trusted"] = trusted
    source["trust_reason"] = reason


_UNKNOWN_SOURCE_NOTE = (
    "No configured catalog source is known to have published this version, so it "
    "gets no updates. Reinstall it from a catalog source to get updates again."
)
_SOURCE_REMOVED_NOTE = (
    "Its catalog source was removed, so it gets no updates. Reinstall it from a "
    "catalog source to get updates again."
)
_FOREIGN_REPO_NOTE = (
    "No longer trusted: its code does not come from the official catalog repository. "
    "Reinstall it from the official catalog to trust it again."
)
_SHADOWS_BUILT_IN_NOTE = (
    " It replaces a built-in provider, so it leaves provider searches at the next restart."
)


def _commit_demotion_note(reason: str) -> str:
    where = "unknown to GitHub" if reason == "unknown commit" else "outside main and beta"
    return (
        f"No longer trusted: the official catalog commit it was installed from is {where}. "
        "It is trusted again once that commit is on main or beta, or when it is "
        "reinstalled from the official catalog."
    )


def _demote_installation(row: dict[str, Any], note: str, shadows: bool, for_commit: bool) -> None:
    row["trusted"] = False
    row["source_id"] = None
    row["source_bound"] = True
    row["demoted_commit"] = for_commit
    row["trust_note"] = note + (_SHADOWS_BUILT_IN_NOTE if shadows else "")


def _bind_by_evidence(row: dict[str, Any], manifest: Any, origin: dict[str, Any], state: dict[str, Any]) -> None:
    """Bind an untrusted install to the community source its code came from.

    The evidence is its manifest equalling an entry that source holds, or its
    catalog URL naming that source at the source's commit or an ancestor of it.
    A matching id alone is none: an unrelated catalog can publish the same id. An
    unanswered comparison leaves the install unbound, to be asked again.
    """
    sources = state.get("catalog_sources") or {}
    manifest = manifest if isinstance(manifest, dict) else {}
    bound = None
    for entry in (state.get("catalog_entries") or {}).values():
        if not isinstance(entry, dict) or entry.get("source") == OFFICIAL_CATALOG_SOURCE_ID:
            continue
        if entry.get("source") not in sources or not isinstance(entry.get("manifest"), dict):
            continue
        if (entry.get("provider_id") == manifest_provider_id(manifest)
                and entry.get("version") == manifest.get("version")
                and _trust_manifest(entry["manifest"]) == _trust_manifest(manifest)):
            bound = entry["source"]
            break
    unanswered = False
    commit = origin.get("commit")
    for key, source in sources.items():
        if bound or key == OFFICIAL_CATALOG_SOURCE_ID or not isinstance(source, dict):
            continue
        resolved = source.get("resolved_commit")
        if origin.get("catalog_url") != source.get("url") or not (
                _is_commit_sha(resolved) and _is_commit_sha(commit)):
            continue
        if resolved.lower() == commit.lower():
            bound = key
            continue
        try:
            owner, repo, _ref, _path = _parse_github_file_url(source["url"])
            status = _compare_status(f"{owner}/{repo}", resolved, commit)
        except CatalogSourceError:
            continue
        except _NoAnswer:
            unanswered = True
            continue
        if status in ("identical", "behind"):
            bound = key
    if bound:
        row.update(source_bound=True, source_id=bound, trust_note=None)
    elif not unanswered:
        row.update(source_bound=True, source_id=None, trust_note=_UNKNOWN_SOURCE_NOTE)


def _recheck_installed_trust(state: dict[str, Any], ancestry: _OfficialAncestry) -> None:
    """Bind, demote or restore installations by where their code came from.

    Runs with every refresh of the official source, and its first run binds the
    installs made before an install recorded its source. A trusted install stays
    trusted only while its official commit is on main or beta, and one demoted for
    its commit is trusted again once a later check says yes. Only a definite
    answer changes an install; a check that fails is repeated next time.
    """
    built_in_ids = None
    for provider_id, row in (state.get("installations") or {}).items():
        if not isinstance(row, dict):
            continue
        if row.get("origin") == "local":
            row["source_bound"] = True
            continue
        manifest = row.get("staged_manifest")
        if not isinstance(manifest, dict):
            manifest = row.get("manifest")
        origin = manifest.get("source") if isinstance(manifest, dict) else None
        origin = origin if isinstance(origin, dict) else {}
        if not row.get("source_bound"):
            if not row.get("trusted"):
                _bind_by_evidence(row, manifest, origin, state)
                continue
            # Only the official source ever made an install trusted.
            row["source_bound"] = True
            row["source_id"] = OFFICIAL_CATALOG_SOURCE_ID
        if not (row.get("trusted") or row.get("demoted_commit")):
            continue
        if built_in_ids is None:
            built_in_ids = _built_in_provider_ids()
        shadows = str(provider_id) in built_in_ids
        if origin.get("repo") != OFFICIAL_CATALOG_REPO:
            if row.get("trusted"):
                _demote_installation(row, _FOREIGN_REPO_NOTE, shadows, for_commit=False)
            continue
        verdict, reason = ancestry.check(origin.get("commit"))
        if verdict is None:
            continue
        if verdict and row.get("demoted_commit"):
            row.update(trusted=True, source_id=OFFICIAL_CATALOG_SOURCE_ID,
                       demoted_commit=False, trust_note=None)
        elif not verdict and row.get("trusted"):
            _demote_installation(row, _commit_demotion_note(reason), shadows, for_commit=True)


def _normalize_catalog_manifest(manifest: dict[str, Any], source: dict[str, Any], commit: str) -> dict[str, Any]:
    normalized = dict(manifest)
    manifest_source = dict(normalized.get("source") or {})
    manifest_source.setdefault("type", "github")
    manifest_source.setdefault("catalog_url", source.get("url"))
    manifest_source["commit"] = commit
    manifest_source["trusted"] = bool(source.get("trusted", False))
    normalized["source"] = manifest_source
    return normalized


def _rejected_plugin(source: dict[str, Any], manifest: dict[str, Any], reason: str) -> dict[str, Any]:
    """The job-details record of a plugin manifest the refresh did not store."""
    provider_id = manifest.get("id")
    version = manifest.get("version")
    return {
        "source": source["name"],
        "provider_id": provider_id if isinstance(provider_id, str) else None,
        "version": version if isinstance(version, str) else None,
        "reason": reason,
    }


def _hold_unsigned_ref_to_the_latch(state: dict[str, Any], source: dict[str, Any]) -> None:
    """Once a signed channel has latched, a ref that reads no signed channel is not trusted.

    A tag, a pinned commit or a spelling of main that resolves to a commit on main or beta is trusted by
    where that commit sits, yet it reads the unsigned legacy catalog. On a host that has accepted a signed
    catalog, that would be a way around the signature, so such a ref stays untrusted.
    """
    if catalog_feed.feed_channel(source.get("dev_ref")) is not None:
        return
    if any(catalog_feed.stored_record(state, channel).latched
           for channel in (catalog_feed.CHANNEL_MAIN, catalog_feed.CHANNEL_BETA)):
        source["trusted"] = False
        source["trust_reason"] = "unsigned ref on a host that requires signed catalogs"


def _note_catalog_signature(
    state: dict[str, Any], source: dict[str, Any], feed, accepted: dict[str, catalog_feed.ChannelRecord],
    revocation_lists: dict[str, revocation.ChannelList] | None = None,
) -> None:
    """Record on the official source and in the state document what its signature check came to.

    A verified catalog latches its channel and raises the stored counters. The legacy catalog is
    shown as "unsigned (legacy)" and changes nothing a channel remembers.
    """
    if feed is None:
        source["catalog_signature"] = catalog_feed.SIGNATURE_LEGACY
        source["signature_verified"] = False
        source["catalog_channel"] = catalog_feed.feed_channel(source.get("dev_ref"))
        source["catalog_expires_at"] = None
        source.pop("catalog_signed_version", None)
        source.pop("catalog_key_file_version", None)
        source.pop("catalog_signed_commit", None)
        return
    source["catalog_signature"] = catalog_feed.SIGNATURE_VERIFIED
    source["signature_verified"] = True
    source["catalog_channel"] = feed.channel
    source["catalog_signed_version"] = feed.result.signed_version
    source["catalog_key_file_version"] = feed.result.key_file_version
    source["catalog_expires_at"] = catalog_feed._iso(feed.result.expires)
    source["catalog_signed_commit"] = feed.source_commit
    # The list of the catalog just verified replaces the last one of its channel, so an entry that channel's new
    # catalog drops is lifted and one it adds holds from now on. An unsigned catalog (the branch above) never
    # touches it.
    entries = revocation.parse(feed.catalog.get(revocation.FIELD_NAME))
    source[revocation.RECORD_KEY] = revocation.to_record(
        source.get(revocation.RECORD_KEY), feed.channel, feed.result.signed_version, entries)
    if revocation_lists is not None:
        revocation_lists[feed.channel] = revocation.ChannelList(feed.result.signed_version, entries)
    if feed.may_advance_state:
        accepted[feed.channel] = catalog_feed.record_accepted(state, feed, catalog_feed.current_time())
        catalog_feed.clear_refusal(state, feed.channel)


def _store_transport(state: dict[str, Any]) -> None:
    """Put what the transport has learnt about each origin into ``state``, inside the write that holds it.

    The process record is authoritative once it has been restored, so it is stored as it is: a restriction
    lifted by a success stays lifted after a restart. Nothing is written while the transport has learnt
    nothing and the state holds nothing.
    """
    record = transport.export_state()
    if any(record.values()) or "transport" in state:
        state["transport"] = record


def _persist_transport() -> None:
    """The same, for a caller that is not inside a state write (an install): one write, only when the
    stored section differs. A failure is logged and goes no further."""
    try:
        if not _fallback_setting_on():
            return
        record = transport.export_state()
        held = load_state().get("transport")
        if (not any(record.values()) and held is None) or record == held:
            return
        mutate_state(lambda state: _store_transport(state))
    except Exception:
        # What the transport learnt is kept in memory too, so a failed write costs a restart's memory only.
        logging.getLogger(__name__).warning(
            "The Provider Hub could not store what it learnt about its catalog origins", exc_info=True)


def refresh_catalog(source_ids: set[str] | None = None, checkpoint=None) -> dict[str, Any]:
    """Refresh catalog sources. When ``source_ids`` is given, only those source ids
    are fetched (e.g. the startup migration refreshes only the official source so an
    unrelated slow/broken third-party source can't delay boot); otherwise all are.

    ``checkpoint(message)`` runs before each source is fetched and raises
    ProviderHubStopped when the job was stopped. The whole refresh is one state
    write, so a stop leaves the catalog exactly as it was.
    """
    with record_job("refresh_catalog", target_kind="system") as job:
        def refresh(state: dict[str, Any]) -> dict[str, Any]:
            now = utcnow_iso()
            entries = state.setdefault("catalog_entries", {})
            refreshed_sources = set()
            refreshed_entry_keys = set()
            entries_count = 0
            sources_count = 0
            failed_sources: list[str] = []
            rejected_plugins: list[dict[str, Any]] = []
            accepted_feeds: dict[str, catalog_feed.ChannelRecord] = {}
            revocation_lists: dict[str, revocation.ChannelList] = {}
            ancestry = _OfficialAncestry(state)
            if _fallback_setting_on():
                transport.restore_state(state.get("transport"))
            for source_key, source in (state.get("catalog_sources") or {}).items():
                if not isinstance(source, dict):
                    continue
                if source_ids is not None and source.get("id") not in source_ids:
                    continue
                if checkpoint is not None:
                    checkpoint(f"Fetching {source.get('name') or source.get('id') or 'a catalog source'}")
                sources_count += 1
                source["last_attempted_at"] = now
                previous_commit = source.get("resolved_commit")
                source_id = source.get("id") or source["name"]
                is_official = source_key == OFFICIAL_CATALOG_SOURCE_ID
                # An unchanged commit means an unchanged catalog: the refresh
                # can end after the advertisement. A skip needs a stored commit
                # and stored entries for the source, and every one of those
                # entries must carry a catalog_list. Entries stored without one
                # predate the plugins list, so that source is fetched once and
                # its entries are rewritten in the current key format.
                # A source other than the official one skips on those
                # conditions alone. The official source also skips only when
                # its stored trusted flag is True, it has no dev_ref, and its
                # effective ref (the ref in its URL) is in _TRUSTED_BRANCHES.
                # A dev_ref verdict depends on where the commit sits in the
                # graph, so it is re-derived on every refresh. The stored
                # trusted flag must be True because the reserved provider-id
                # list is written only for a trusted official source, so a
                # stored False means that list may be stale.
                skip_commit = None
                stored_entries = [
                    entry for entry in entries.values()
                    if isinstance(entry, dict) and entry.get("source") == source_id
                ]
                if isinstance(previous_commit, str) and stored_entries and all(
                    "catalog_list" in entry for entry in stored_entries
                ):
                    if not is_official:
                        # The development source is always read: whether it may carry a new kind
                        # depends on the variable, not on the commit.
                        if not dev_source.is_dev_source(source_key, source):
                            skip_commit = previous_commit
                    elif (not source.get("dev_ref") and source.get("trusted") is True
                          and (not catalog_feed.signed_feed_enabled() or (
                              source.get("catalog_signature")
                              and not source.get("signed_feed_retry")
                              and not _revocations_stale(state, (catalog_feed.CHANNEL_MAIN,))
                              and source.get("catalog_channel") == catalog_feed.CHANNEL_MAIN
                              and catalog_feed.supply_refusal(state, source) is None))):
                        # A source refreshed before the signature check existed, on another channel, or
                        # whose cached catalog no longer meets what this host requires of one (a counter,
                        # a key file version it lacks the record of) is fetched once more. So is one whose
                        # legacy catalog stood in for a signed file that could not be read.
                        # So is one whose revocation list is older than the catalog it accepted last.
                        try:
                            url_ref = _parse_github_file_url(source["url"])[2]
                        except CatalogSourceError:
                            url_ref = None
                        if url_ref in _TRUSTED_BRANCHES:
                            skip_commit = previous_commit
                from_floor = False
                feed = None
                served = _Served() if is_official else None
                try:
                    if is_official:
                        catalog, commit, feed = _fetch_official_catalog(
                            source, state, ancestry, unchanged_commit=skip_commit,
                            advertisement_sink=ancestry.note_advertisement, served=served,
                        )
                    else:
                        catalog, commit = _fetch_github_catalog(
                            source["url"], override_ref=source.get("dev_ref"),
                            unchanged_commit=skip_commit,
                            advertisement_sink=ancestry.note_advertisement,
                        )
                    source["resolved_commit"] = commit
                    source["last_checked_at"] = now
                    source["last_error"] = None
                    floor.clear_seed(source)
                    if is_official:
                        _note_served(source, served)
                except Exception as error:
                    if served is not None and served.failure is not None:
                        source["last_failure"] = served.failure
                    if isinstance(error, catalog_feed.FeedRefused):
                        refused_channel = catalog_feed.feed_channel(source.get("dev_ref"))
                        if refused_channel is not None:
                            mirrored = catalog_feed.record_refusal(
                                state, refused_channel, error, catalog_feed.current_time())
                            if mirrored is not None:
                                accepted_feeds[refused_channel] = catalog_feed._larger(
                                    accepted_feeds.get(refused_channel), mirrored)
                    source["last_error"] = refusal_text(_catalog_source_error_message(error))
                    failed_sources.append(source.get("name") or source.get("id") or "?")
                    # An official source that has never refreshed falls back to
                    # the catalog bundled in the image; any other failure keeps
                    # what is stored.
                    seeded = floor.seed_candidate(source, entries) if is_official else None
                    if seeded is None:
                        continue
                    catalog, commit = seeded
                    from_floor = True
                if catalog is None:
                    # Unchanged: keep the stored entries, provider ids and
                    # trust, and stay out of refreshed_sources so the prune
                    # below does not run against a catalog not fetched.
                    # Everything the skip bypasses for this source (entry
                    # building and the official published-ids list) reads
                    # only the fetched catalog and the source's own stored
                    # fields; the installed-trust recheck runs after the
                    # loop on every refresh. Nothing the skip skips reads
                    # the app version or installed state. The one stored
                    # input that can change is "trusted", which the trust
                    # update below rewrites in this same refresh, so the
                    # skip requires it to be true before it is taken.
                    if is_official:
                        _set_official_trust(source, commit, previous_commit, ancestry)
                    # The kept entries still count toward the refresh summary.
                    entries_count += sum(
                        1 for entry in entries.values()
                        if isinstance(entry, dict) and entry.get("source") == source_id
                    )
                    continue
                refreshed_sources.add(source_id)
                # The commit the manifests and their bundles are pinned to: the signed body names it,
                # and it can sit behind the head the source resolved to.
                entry_commit = feed.source_commit if feed is not None else commit

                if is_official:
                    if from_floor:
                        floor.trust_source(source)
                    else:
                        _set_official_trust(source, commit, previous_commit, ancestry)
                        _hold_unsigned_ref_to_the_latch(state, source)
                        _note_catalog_signature(state, source, feed, accepted_feeds, revocation_lists)
                        if served is not None and served.signed_unreadable:
                            source["signed_feed_retry"] = True
                        else:
                            source.pop("signed_feed_retry", None)
                    published_ids = set()
                claimed_kinds: dict[str, set[str]] = {}
                for item in catalog.get("providers", []):
                    if not isinstance(item, dict):
                        continue
                    manifest = item.get("manifest") if isinstance(item.get("manifest"), dict) else item
                    if not isinstance(manifest, dict):
                        continue
                    manifest = _normalize_catalog_manifest(manifest, source, entry_commit)
                    provider_id = manifest_provider_id(manifest)
                    version = manifest.get("version") or item.get("version")
                    if not provider_id or not version:
                        continue
                    # A schema 2 manifest is a plugin, so it is listed in the plugins list only.
                    if manifest.get("schema_version") == 2:
                        rejected_plugins.append(_rejected_plugin(
                            source, manifest, PLUGIN_IN_PROVIDERS_LIST_REASON))
                        continue
                    key = f"{source['name']}:{CATALOG_LIST_PROVIDERS}:{provider_id}:{version}"
                    source_id = source.get("id") or source["name"]
                    entries[key] = {
                        "source": source_id,
                        "source_name": source["name"],
                        "provider_id": provider_id,
                        "name": manifest.get("name") or item.get("name") or provider_id,
                        "version": version,
                        "trusted": bool(source.get("trusted", False)),
                        "manifest": manifest,
                        "resolved_commit": entry_commit,
                        "catalog_list": CATALOG_LIST_PROVIDERS,
                    }
                    refreshed_entry_keys.add(key)
                    entries_count += 1
                    claimed_kinds.setdefault(provider_id, set()).add(catalog_kind_of(entries[key]))
                    if is_official:
                        published_ids.add(str(provider_id))
                plugins = catalog.get("plugins")
                plugin_claims: list[dict[str, Any]] = []
                for item in plugins if isinstance(plugins, list) else []:
                    if not isinstance(item, dict):
                        continue
                    manifest = item.get("manifest") if isinstance(item.get("manifest"), dict) else item
                    if not isinstance(manifest, dict):
                        continue
                    try:
                        manifest = _normalize_catalog_manifest(manifest, source, entry_commit)
                        parsed = parse_plugin_manifest(manifest)
                    except ManifestValidationError as error:
                        rejected_plugins.append(_rejected_plugin(
                            source, manifest, refusal_text(error)))
                        continue
                    except Exception:
                        rejected_plugins.append(_rejected_plugin(
                            source, manifest, PLUGIN_MANIFEST_UNREADABLE_REASON))
                        continue
                    if parsed.schema_version != 2:
                        continue
                    provider_id = manifest_provider_id(manifest)
                    if is_official:
                        published_ids.add(provider_id)
                    claimed_kinds.setdefault(provider_id, set()).add(parsed.kind)
                    # The kind checks run after the id and kind are recorded, so a refused
                    # plugin still reserves its id and counts toward the kind conflict rule.
                    kind = kind_for_api(parsed.kind_api)
                    if kind is None:
                        rejected_plugins.append(_rejected_plugin(
                            source, manifest, PLUGIN_KIND_UNKNOWN_REASON))
                        continue
                    if kind.name != parsed.kind:
                        rejected_plugins.append(_rejected_plugin(
                            source, manifest, PLUGIN_KIND_MISMATCH_REASON))
                        continue
                    if kind.name != SUBTITLE_PROVIDER and not (
                            (is_official and source.get("trusted") is True)
                            or dev_source.is_dev_source(source_key, source)):
                        rejected_plugins.append(_rejected_plugin(
                            source, manifest, NEW_KIND_OFFICIAL_ONLY_REASON))
                        continue
                    try:
                        kind.validate_manifest(manifest, trusted=bool(source.get("trusted", False)))
                    except ManifestValidationError:
                        rejected_plugins.append(_rejected_plugin(
                            source, manifest, PLUGIN_KIND_REFUSED_REASON))
                        continue
                    plugin_claims.append({
                        "manifest": manifest,
                        "provider_id": provider_id,
                        "version": manifest.get("version"),
                    })
                # One id names one plugin of one kind. When the two lists claim an id with
                # different kinds, every plugin claim for that id is rejected.
                accepted = []
                for claim in plugin_claims:
                    if len(claimed_kinds[claim["provider_id"]]) > 1:
                        rejected_plugins.append(_rejected_plugin(
                            source, claim["manifest"], PLUGIN_KIND_CONFLICT_REASON))
                    else:
                        accepted.append(claim)
                listed = Counter((claim["provider_id"], claim["version"]) for claim in accepted)
                for claim in accepted:
                    provider_id, version = claim["provider_id"], claim["version"]
                    if listed[(provider_id, version)] > 1:
                        rejected_plugins.append(_rejected_plugin(
                            source, claim["manifest"], PLUGIN_VERSION_REPEATED_REASON))
                        continue
                    key = f"{source['name']}:{CATALOG_LIST_PLUGINS}:{provider_id}:{version}"
                    entries[key] = {
                        "source": source_id,
                        "source_name": source["name"],
                        "provider_id": provider_id,
                        "name": claim["manifest"].get("name") or provider_id,
                        "version": version,
                        "trusted": bool(source.get("trusted", False)),
                        "manifest": claim["manifest"],
                        "resolved_commit": entry_commit,
                        "catalog_list": CATALOG_LIST_PLUGINS,
                    }
                    refreshed_entry_keys.add(key)
                    entries_count += 1
                if from_floor:
                    floor.mark_seeded(source, entries, refreshed_entry_keys, commit, now)
                if is_official and source["trusted"]:
                    source["provider_ids"] = sorted(published_ids)
            for key, entry in list(entries.items()):
                if not isinstance(entry, dict):
                    continue
                if entry.get("source") in refreshed_sources:
                    if key not in refreshed_entry_keys:
                        del entries[key]
            if source_ids is None or OFFICIAL_CATALOG_SOURCE_ID in source_ids:
                _recheck_installed_trust(state, ancestry)
                _hold_revoked_installations(state)
            if _fallback_setting_on():
                _store_transport(state)
            ok_sources = sources_count - len(failed_sources)
            return {
                "refreshed_at": now,
                "sources": sources_count,
                "entries": entries_count,
                "sources_ok": ok_sources,
                "failed_sources": failed_sources,
                "rejected_plugins": rejected_plugins,
                "accepted_feeds": accepted_feeds,
                "revocation_lists": revocation_lists,
            }

        # The fetches above run inside this mutation, so a lost race must not
        # run it again: it fails with a conflict and the refresh is retried.
        result = mutate_state(refresh, retry_on_conflict=False)
        try:
            from .registry import revoke_untrusted_host_calls

            revoke_untrusted_host_calls()
        except Exception:
            logging.getLogger(__name__).exception("Provider Hub: could not revoke host capabilities after the refresh")
        try:
            enforce_revocations_live()
        except Exception:
            logging.getLogger(__name__).exception("Provider Hub: could not stop the releases the refresh revoked")
            try:
                ask_restart_for_unstopped()
            except Exception:
                logging.getLogger(__name__).exception("Provider Hub: could not ask for the restart that stops them")
        if result["accepted_feeds"]:
            catalog_feed.write_counter_file(result["accepted_feeds"])
        if result["revocation_lists"]:
            revocation.write_mirror(result["revocation_lists"])
        sources_count = result["sources"]
        entries_count = result["entries"]
        ok_sources = result["sources_ok"]
        failed_sources = result["failed_sources"]
        if failed_sources:
            summary = (
                f"Refreshed {ok_sources}/{sources_count} sources, "
                f"{entries_count} entries. Failed: {', '.join(failed_sources)}"
            )
        else:
            summary = f"Refreshed {sources_count} source(s), {entries_count} entries"
        job.update(
            message=summary,
            details={
                "sources_total": sources_count,
                "sources_ok": ok_sources,
                "sources_failed": failed_sources,
                "entries": entries_count,
                "rejected_plugins": result["rejected_plugins"],
            },
        )
        return {
            "refreshed_at": result["refreshed_at"],
            "sources": sources_count,
            "entries": entries_count,
        }


def _manifest_secret_fields(installation: dict[str, Any]) -> set[str]:
    """The secret fields of an installation's current manifest. The field
    list itself comes from the one shared helper the settings registry uses
    (`secret_fields` and `secret: true` by the same rule the frontend
    renders a password input by), so the three copies cannot drift."""
    manifest = installation.get("manifest")
    if not isinstance(manifest, dict):
        manifest = installation.get("staged_manifest")
    if not isinstance(manifest, dict):
        return set()
    from secret_store.registry import manifest_secret_fields
    return manifest_secret_fields(manifest)


def _retained_secret_fields(provider_id: Any, state: dict[str, Any] | None = None) -> set[str]:
    """Fields the persisted registry still calls secrets for this provider,
    on top of the current manifest's own.

    A newer version can drop a field from its schema while the stored
    value survives in state.json; the persisted registration keeps that
    value covered (redacted in the API, kept out of the worker config)
    rather than letting a schema change expose it.

    `state`, when the caller already holds the loaded state.json, is used
    instead of reading the file again: the admission gate walks every
    provider on every call, and re-reading the file per installation was
    most of its cost."""
    fields = set()
    provider_id = str(provider_id or "").lower()
    if not provider_id:
        return fields
    prefix = f"{provider_id}."
    try:
        from secret_store.registry import plugin_secret_paths
        for path in plugin_secret_paths():
            if path.startswith(prefix) and "." not in path[len(prefix):]:
                fields.add(path[len(prefix):])
    except Exception:
        logging.getLogger(__name__).exception(
            "Unable to read the registered plugin secret paths for %s", provider_id)
    try:
        if state is None:
            from .state import load_state
            state = load_state()
        # The persisted set is read through the shared persisted-path
        # validator, the same one the boot loader uses: a malformed value
        # is skipped until the state is repaired rather than costing the
        # retained coverage of the valid entries, and an entry that never
        # matched the persisted path shapes never reaches a field name.
        from secret_store.registry import _valid_persisted_paths
        for path in _valid_persisted_paths(state.get("plugin_secret_paths")):
            if path.startswith(prefix) and "." not in path[len(prefix):]:
                fields.add(path[len(prefix):])
    except Exception:
        logging.getLogger(__name__).exception(
            "Unable to read the persisted plugin secret paths for %s", provider_id)
    return fields


def _encrypt_secret_value(value: Any) -> Any:
    """Encrypt a secret for storage, or raise: a credential that cannot be
    encrypted is not stored. Failing open here would put plaintext into
    state.json whenever the master key is broken."""
    if not isinstance(value, str) or not value:
        return value
    from secret_store import encrypt_secret
    from secret_store.crypto import persist_master_key
    # The master key is durable before any ciphertext is committed. Without
    # this the runtime update path could store ciphertext under a key that
    # exists only in memory, and a failing settings write would leave a
    # credential no restart can ever decrypt. A persistence failure raises
    # and becomes the caller's could-not-be-encrypted refusal.
    persist_master_key()
    return encrypt_secret(value)


def _decrypt_secret_value(value: Any) -> Any:
    """Decrypt a stored secret, or raise: a value that will not decrypt is
    never handed out as the credential. Failing open here sent the stored
    ciphertext to the provider as if it were the key. An empty collection
    reads as an empty string: a leftover [] or {} is an absent credential,
    not a value the plugin's setup logic can treat as present."""
    if isinstance(value, (list, dict)) and not value:
        return ""
    if not isinstance(value, str) or not value:
        return value
    from secret_store import decrypt_secret
    return decrypt_secret(value)


# The installation last_error for a stored credential that will not decrypt.
# The provider is left out of searches until the credential is re-entered.
DECRYPT_FAILED_MESSAGE = "Stored credential could not be decrypted; re-enter it"


def _plugin_secret_paths(provider_id, *manifests) -> set[str]:
    """The `provider_id.field` registry paths for a plugin's manifests."""
    paths = set()
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        for field in _manifest_secret_fields({"manifest": manifest}):
            paths.add(f"{str(provider_id).lower()}.{field.lower()}")
    return paths


def _persist_plugin_secret_paths(state: dict[str, Any], provider: dict[str, Any]) -> set[str]:
    """Add the plugin's secret paths to the persisted set in `state` and
    register them in the live secret store.

    Called inside the state mutation on install, update and configuration
    change, so the paths survive the removal of the plugin that owns them,
    and in the live registry so the running process covers the plugin's
    secrets without a restart."""
    provider_id = provider.get("provider_id") if isinstance(provider, dict) else None
    if not provider_id:
        return set()
    paths = _plugin_secret_paths(
        provider_id, provider.get("manifest"), provider.get("staged_manifest"))
    if not paths:
        return set()
    # The persisted set is read through the shared persisted-path
    # validator, the same one the boot loader uses: a value that is not a
    # list (a number a broken state left behind, which a boot that
    # discovers nothing never repairs) must neither break the
    # registration nor ride along, and the write repairs the set to the
    # valid union.
    from secret_store.registry import _valid_persisted_paths
    known = _valid_persisted_paths(state.get("plugin_secret_paths"))
    state["plugin_secret_paths"] = sorted(known | paths)
    try:
        from secret_store.registry import register_plugin_secret_paths
        register_plugin_secret_paths(paths)
    except Exception:
        logging.getLogger(__name__).exception(
            "Unable to register the plugin secret paths for %s", provider_id)
    return paths


def _secret_descriptor(stored_value: Any) -> dict[str, Any]:
    """The write-only descriptor for one stored secret value."""
    descriptor: dict[str, Any] = {"configured": False}
    if not isinstance(stored_value, str) or not stored_value:
        return descriptor
    descriptor["configured"] = True
    try:
        plaintext = _decrypt_secret_value(stored_value)
    except Exception:
        descriptor["decrypt_failed"] = True
        return descriptor
    from secret_store.migration import LAST_FOUR_MIN_LENGTH
    if isinstance(plaintext, str) and len(plaintext) >= LAST_FOUR_MIN_LENGTH:
        descriptor["last_four"] = plaintext[-4:]
    return descriptor


def _capability_report(installation: dict[str, Any]) -> list[dict[str, Any]]:
    """Which host capabilities the plugin declares and whether each is ready, for its card. Never raises."""
    try:
        from .capabilities import binding_report

        return binding_report(installation)
    except Exception:
        logging.getLogger(__name__).exception("Unable to report the capabilities of %s", installation.get("provider_id"))
        return []


def needs_restart(installation: dict[str, Any]) -> bool:
    """Whether the change waiting on this installation takes effect only once Bazarr+ restarts.

    A change is waiting while ``pending_restart`` is set; it needs a restart when its kind attaches on restart. A
    kind that attaches live never reports one. This is the one field every restart prompt in the interface reads,
    so a kind that starts attaching live changes no screen."""
    if not installation.get("pending_restart"):
        return False
    try:
        return get_kind(kind_of(installation)).attach_policy == ATTACH_ON_RESTART
    except UnknownKindError:
        return True


def _redact_installation(installation: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(installation)
    if redacted.pop(releases.STAGED_ROLLBACK, None) and redacted.get("staged_version"):
        redacted["staged_action"] = "rollback"
    provider_id = redacted.get("provider_id")
    if provider_id:
        # enabled_providers in config.yaml is the source of truth; the stored
        # field on the hub row is a stale shadow we don't trust on read.
        redacted["enabled"] = get_kind(kind_of(redacted)).is_enabled(redacted)
        redacted["needs_restart"] = needs_restart(redacted)
    needs = _capability_report(redacted)
    if needs:
        redacted["capabilities"] = needs
    config = redacted.get("config")
    if not isinstance(config, dict):
        return redacted
    # The current manifest's secret fields plus every field the persisted
    # registry still calls a secret: a value retained after a schema change
    # stays redacted instead of becoming visible again.
    secret_fields = _manifest_secret_fields(redacted) | _retained_secret_fields(provider_id)
    redacted_config = dict(config)
    descriptors = {}
    for field in sorted(secret_fields):
        # Write-only: an empty value plus the descriptor, never the stored
        # value and never a placeholder a caller might echo back as real.
        redacted_config[field] = ""
        descriptors[field] = _secret_descriptor(config.get(field))
    for stored_key in sorted(key for key in config if isinstance(key, str)):
        if stored_key != stored_key.lower() and stored_key.lower() in secret_fields:
            # A mixed-case alias of a secret field never comes back either:
            # the value is the credential's, whatever spelling an earlier
            # write left it under, until the next boot or update
            # canonicalizes it away.
            redacted_config[stored_key] = ""
    if descriptors:
        redacted_config[SECRET_DESCRIPTORS_KEY] = descriptors
    redacted["config"] = redacted_config
    return redacted


def _effective_installation_config(installation: dict[str, Any],
                                    state: dict[str, Any] | None = None) -> tuple[dict[str, Any], set[str]]:
    """The decrypted config for the worker, plus the set of secret fields
    whose stored value would not decrypt.

    Fail closed: an undecryptable field is omitted from the config (never
    handed out as the credential), and its name is returned so the caller
    can leave the provider out of searches and say why. A registered
    secret the ACTIVE schema no longer declares is left out entirely: the
    plugin stopped reading it, a retained credential does not travel to
    the worker as ordinary config, and a staged schema that still
    declares it cannot put it there ahead of the activation. A retained
    field counts as undecryptable only when the active schema declares
    it: ciphertext of a field the plugin no longer reads withholds
    nothing the plugin needs, so it must not block the provider either.

    `state` spares the caller's own load_state pass-through; see
    _retained_secret_fields."""
    config = installation.get("config")
    if not isinstance(config, dict):
        return {}, set()
    retained = _retained_secret_fields(installation.get("provider_id"), state)
    active_manifest = installation.get("manifest")
    if not isinstance(active_manifest, dict):
        active_manifest = {}
    from secret_store.registry import _manifest_field_names, manifest_secret_fields
    secret_fields = manifest_secret_fields(active_manifest) | retained
    effective = dict(config)
    undecryptable = set()
    for field in secret_fields:
        if field in effective:
            try:
                effective[field] = _decrypt_secret_value(effective[field])
            except Exception:
                del effective[field]
                undecryptable.add(field)
    declared = _manifest_field_names(active_manifest)
    if retained:
        for field in sorted(retained):
            if field not in declared:
                effective.pop(field, None)
                undecryptable.discard(field)
    return effective, undecryptable


_ABSENT = object()


def _restore_after_refused_update(provider_id: str, changes, restore) -> None:
    try:
        left = restore(changes)
    except Exception:
        logging.getLogger(__name__).exception("Provider Hub: could not put %s back after a refused update", provider_id)
        return
    if left:
        logging.getLogger(__name__).warning(
            "Provider Hub: %s changed again after a refused update, so %s was left as the newer write set it",
            provider_id, ", ".join(sorted(left)),
        )


def note_switched_providers(switched_on: set[str], switched_off: set[str]) -> None:
    """Update what held-back providers show, once a settings save has switched some on or off.

    Called after the save has released its settings locks, because it takes the state lock, which the Hub's
    own switch takes first. A held-back provider that is now on shows its reason; one that is now off loses
    it. Any other error is left alone."""
    if not switched_on and not switched_off:
        return

    def apply(state: dict[str, Any]) -> None:
        for provider_id, row in (state.get("installations") or {}).items():
            if not isinstance(row, dict) or not row.get("blocked") or not row.get("blocked_reason"):
                continue
            if provider_id in switched_on:
                row["last_error"] = _held_error(state, str(provider_id), row)
            elif (provider_id in switched_off and row.get("last_error") == row["blocked_reason"]
                  and not row.get("revoked")):
                # A revoked release keeps saying why, switched on or off.
                row["last_error"] = None

    mutate_state(apply)


def switch_on_refusal(name: str, needs: list[str]) -> str:
    """Why a plugin cannot be switched on: what it needs is not running."""
    return f"{name} needs {', '.join(needs)}, which is not running, so it cannot be switched on."


def _held_error(state: dict[str, Any], provider_id: str, row: dict[str, Any]) -> Any:
    """The error a held-back plugin shows once it is switched on: why it is held, or, for a revoked release
    whose safe update is staged, that the update takes over at the next restart."""
    found = None
    if row.get("revoked") and row.get("staged_version"):
        found = revocation.lookup(_revocations(state), provider_id, row.get("active_version"))
    if found is None:
        return row.get("blocked_reason")
    return _revocation_text(revocation.held_back, found, row.get("name") or provider_id,
                            row.get("active_version"), revocation.STAGED)


def revoked_switch_on_refusal(provider_id: str, state: dict[str, Any] | None = None) -> str | None:
    """Why a plugin cannot be switched on: the official catalog revoked the release it would run, or None.

    A revoked plugin whose staged update is not revoked may be switched on only once the boot has held the
    revoked release back, because from then on it cannot run and the staged update takes over at the next
    restart. Until then the revoked release is still registered and unblocked, and switching it on would let
    it run, so the person is told to restart first.
    """
    state = state if state is not None else load_state()
    row = (state.get("installations") or {}).get(provider_id)
    if not isinstance(row, dict) or not row.get("active_version") or row.get("state") == "removed":
        return None
    revocations = _revocations(state)
    found = revocation.lookup(revocations, provider_id, row.get("active_version"))
    if found is None:
        return None
    name = row.get("name") or provider_id
    staged = row.get("staged_version")
    if staged and revocation.lookup(revocations, provider_id, staged) is None:
        if _held_for_revocation(row):
            return None
        return _revocation_text(revocation.held_back, found, name, row.get("active_version"), revocation.STAGED)
    return _revocation_text(revocation.refusal, found, name, row.get("active_version"), "switched on")


def update_provider(provider_id: str, enabled: bool | None = None, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if config is not None:
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
    if enabled is not None and not isinstance(enabled, bool):
        # A value that is not a boolean is neither on nor off, and the checks below look for exactly True.
        raise ValueError("enabled must be true or false")
    if enabled is False:
        refusal = dependent_refusal(provider_id, "switched off", enabled_only=True)
        if refusal:
            raise ValueError(refusal)
    if enabled is True:
        revoked = revoked_switch_on_refusal(provider_id)
        if revoked:
            raise ValueError(revoked)
        # What a plugin relies on has to be running before it is switched on, as at the next boot.
        rows = load_state().get("installations") or {}
        needs = closure.unmet_needs(rows).get(provider_id)
        if needs:
            name = (rows.get(provider_id) or {}).get("name") or provider_id
            raise ValueError(switch_on_refusal(name, needs))

    def _encrypt_provider_config(provider: dict[str, Any],
                                  config: dict[str, Any]) -> dict[str, Any]:
        """The submitted config with its secrets encrypted, computed before
        anything persists. A credential that cannot be encrypted raises, and
        no part of the request survives it: not the configuration, and not
        the enabled toggle, which writes Bazarr's own settings store and
        must not stay behind on a refused update.

        The covered set is the current manifest's secret fields plus every
        field the persisted registry still calls a secret: a PATCH to a
        retained field (one a newer schema dropped) is stored encrypted
        exactly like a declared one, not written verbatim in the clear. A
        new value that is not text is refused outright: a number or a
        boolean in a secret slot is the shape an older save coerced typed
        tokens into, and accepting it would store a credential that
        neither encrypts at rest nor masks in the API.

        A key no manifest declares is refused too. The boot rule covers
        undeclared credentials by discovering and registering them, so
        accepting one here would leave it sitting in plaintext in state,
        the API response and the worker config until the next boot
        disagrees with what this write did. The route is API-only and the
        UI edits declared fields, so refusing costs no interface.

        Every submitted key is canonicalized first, to the one lowercase
        spelling manifest validation admits and the reader walks: a
        mixed-case name (API_KEY for api_key) is classified, encrypted
        and stored under the canonical name, never parked beside the
        declared field in the clear under the mixed-case spelling, where
        the redaction answering for the declared name would hand it back
        as it stood. The stored copy is canonicalized the same way, so a
        mixed-case alias an earlier version persisted never outlives the
        next update either, and a non-string scalar the alias holds moves
        as the typed text it stands for, never verbatim."""
        from secret_store.migration import _coerced_secret_text
        from secret_store.registry import (_manifest_field_names,
                                           manifest_boolean_secret_paths)

        secret_fields = _manifest_secret_fields(provider) | _retained_secret_fields(provider_id)
        declared_fields = set()
        for manifest in (provider.get("manifest"), provider.get("staged_manifest")):
            if isinstance(manifest, dict):
                declared_fields |= {str(field).lower() for field in _manifest_field_names(manifest)}
        next_config = dict(provider.get("config") if isinstance(provider.get("config"), dict) else {})
        # The stored case variants of a credential key resolve the same
        # way the submitted ones do: the value at the canonical name is
        # the one the canonical writer exposes, so an alias beside it
        # drops as a stale duplicate, while an alias with no canonical
        # name beside it moves under the canonical name, encrypted the
        # way a submitted credential is, so the name the reader walks
        # never holds plaintext. Whatever order the config holds them
        # in, the alias never survives the update it rode in on.
        for stored_key in sorted(key for key in next_config if isinstance(key, str)):
            canonical = stored_key.lower()
            if canonical == stored_key or canonical not in secret_fields:
                continue
            if canonical in next_config:
                del next_config[stored_key]
                continue
            try:
                stored_value = next_config.pop(stored_key)
                if (isinstance(stored_value, bool)
                        and f"{provider_id}.{canonical}" in manifest_boolean_secret_paths()):
                    # A stored toggle under a mixed-case alias keeps its
                    # value: the manifest declares the field boolean, and
                    # stringifying its False would hand the plugin the
                    # truthy text.
                    next_config[canonical] = stored_value
                elif not isinstance(stored_value, str):
                    # A non-string scalar at the alias (a typed token an
                    # older save coerced into a number or a boolean) moves
                    # as the typed text it stands for, encrypted like a
                    # submitted credential, so the canonical secret name
                    # never holds plaintext after a write.
                    next_config[canonical] = _encrypt_secret_value(
                        _coerced_secret_text(stored_value))
                else:
                    next_config[canonical] = _encrypt_secret_value(stored_value)
            except Exception as error:
                raise ValueError(
                    "The credential could not be encrypted, so the configuration "
                    "was not saved. Check the encryption key and try again."
                ) from error
        for submitted_key, value in config.items():
            key = str(submitted_key).lower()
            if key == SECRET_DESCRIPTORS_KEY:
                # The write-only descriptor the API echoes back, never a
                # config field. Manifest validation rejects a field name
                # with a leading underscore, so this can only be an echo.
                continue
            if key in secret_fields:
                if value == SECRET_PLACEHOLDER:
                    # Kept for callers that still echo the placeholder:
                    # absent means unchanged, so this does too.
                    continue
                if value is not None and not isinstance(value, str):
                    raise ValueError(
                        f"The credential {key} must be text; the configuration "
                        "was not saved."
                    )
                # The mask text is refused, so it never replaces a stored
                # credential, the same rule the settings save applies. A
                # value equal to the stored one is not new. A short value is
                # allowed, and the scrub layer masks it as a whole token.
                try:
                    stored_value = _decrypt_secret_value(next_config.get(key))
                except Exception:
                    stored_value = None
                refusal = mask_credential_refusal(
                    f"{provider_id}.{key}", value, stored_value or None)
                if refusal:
                    raise ValueError(f"{refusal}; the configuration was not saved.")
                try:
                    next_config[key] = _encrypt_secret_value(value)
                except Exception as error:
                    raise ValueError(
                        "The credential could not be encrypted, so the configuration "
                        "was not saved. Check the encryption key and try again."
                    ) from error
            elif key not in declared_fields:
                raise ValueError(
                    f"The configuration field {key} is not declared by the "
                    "provider's manifest, so the configuration was not saved."
                )
            else:
                next_config[key] = value
        return next_config

    def update_installation(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        # A pure change to the document: a lost race reruns it, so it must not
        # touch Bazarr's own settings or anything else outside the state. The
        # enabled toggle's settings write happens after the commit, below.
        provider = (state.get("installations") or {}).get(provider_id)
        if not isinstance(provider, dict):
            return None

        # Encrypt and validate before anything is written, so a refused
        # configuration refuses the whole update.
        next_config = _encrypt_provider_config(provider, config) if config is not None else None

        previous: dict[str, Any] = {}

        def remember(key: str) -> None:
            previous[key] = copy.deepcopy(provider[key]) if key in provider else _ABSENT

        if enabled is not None:
            remember("enabled")
            provider["enabled"] = bool(enabled)
            # A plugin held back at boot says why once it is switched on, and stops saying it once it is off.
            if enabled and provider.get("blocked") and provider.get("blocked_reason"):
                remember("last_error")
                provider["last_error"] = _held_error(state, provider_id, provider)
            elif not enabled and provider.get("blocked") and not provider.get("revoked") and provider.get(
                    "last_error") == provider.get("blocked_reason"):
                remember("last_error")
                provider["last_error"] = None

        if next_config is not None:
            remember("config")
            provider["config"] = next_config
            # A re-entered credential ends the decrypt-failure note: the
            # installation stops asking for the key once its stored config
            # decrypts again.
            if provider.get("last_error") == DECRYPT_FAILED_MESSAGE:
                _effective, undecryptable = _effective_installation_config(provider)
                if not undecryptable:
                    remember("last_error")
                    provider["last_error"] = None

        state.setdefault("installations", {})[provider_id] = provider
        _persist_plugin_secret_paths(state, provider)
        written = {key: copy.deepcopy(provider.get(key, _ABSENT)) for key in previous}
        return dict(provider), (previous, written)

    def restore_previous(changes: tuple[dict[str, Any], dict[str, Any]]) -> list[str]:
        """Put back each field, and each config key, this update changed that still holds what it wrote.

        A value another writer changed since is theirs and stays. Returns what
        was left.
        """
        previous, written = changes
        left: list[str] = []

        def put_back(container: dict[str, Any], key: str, before: Any, after: Any, name: str) -> None:
            if before is after or before == after:
                return  # this update did not change it
            if container.get(key, _ABSENT) != after:
                left.append(name)
            elif before is _ABSENT:
                container.pop(key, None)
            else:
                container[key] = copy.deepcopy(before)

        def restore(state: dict[str, Any]) -> None:
            left.clear()
            row = (state.get("installations") or {}).get(provider_id)
            if not isinstance(row, dict):
                return
            for key in previous:
                if key != "config":
                    put_back(row, key, previous[key], written[key], key)
            if "config" in previous:
                before = previous["config"] if isinstance(previous["config"], dict) else {}
                after = written["config"] if isinstance(written["config"], dict) else {}
                current = row.get("config")
                if not isinstance(current, dict):
                    left.append("config")
                    return
                for name in sorted(set(before) | set(after)):
                    put_back(current, name, before.get(name, _ABSENT), after.get(name, _ABSENT), f"config.{name}")

        mutate_state(restore)
        return left

    # The state lock is taken first, because a settings write reaches the
    # enabled-providers sync, which takes the config lock itself: the other
    # order would let a config save and an enable toggle wait on each other's
    # locks.
    from .state import state_write_lock
    with state_write_lock():
        if config is None:
            outcome = mutate_state(update_installation)
        else:
            # A config save follows the same destination rule the settings save
            # follows, and its check and its write share the boundary the
            # settings save commits under, the config lock: the check re-reads
            # the two stores inside it, so a settings save cannot land a token
            # between this check and this write, and this save cannot land a move
            # between the settings save's check and its write either.
            from app.config import refuse_unverified_provider_hub_config_move
            from discover.metadata import CONFIG_LOCK

            with CONFIG_LOCK:
                refuse_unverified_provider_hub_config_move(provider_id, config)
                outcome = mutate_state(update_installation)
        if outcome is None:
            return None
        provider, changes = outcome
        if enabled is not None:
            # The toggle writes Bazarr's own settings store. It runs once, after
            # the state commit is known, and a refusal puts the row back so the
            # whole update is refused, configuration included.
            try:
                saved = get_kind(kind_of(provider)).set_enabled(provider, bool(enabled))
            except Exception:
                _restore_after_refused_update(provider_id, changes, restore_previous)
                raise
            if not saved:
                _restore_after_refused_update(provider_id, changes, restore_previous)
                raise ProviderHubSettingsError(
                    f"Could not {'enable' if enabled else 'disable'} {provider_id}: "
                    f"{ENABLED_PROVIDERS_NOT_SAVED}"
                )
    if enabled is False:
        _stop_serving(provider_id, provider)
    if config is not None:
        get_kind(kind_of(provider)).on_config_saved(provider_id)
    if config is not None or enabled is not None:
        from . import runtime_status
        runtime_status.clear(provider_id)
    if config is not None:
        # The save just stored a credential (encrypted) and registered its
        # path, so the scrub set is rebuilt now: whatever prints the value
        # next, in this process, prints the mask.
        from secret_store.scrub import refresh_secret_values
        refresh_secret_values()
    return _redact_installation(provider)


def _stop_serving(provider_id: str, row: dict[str, Any]) -> None:
    """A plugin was just switched off: for a kind that stops while Bazarr+ runs, its pools let go of it now and its
    worker ends once the calls it holds are done. The release stays the active one, so switching it on again
    needs no restart. A failure is logged and changes nothing: the switch has taken effect in the settings, and
    the next search finds the provider excluded."""
    if not _detaches_live(row):
        return
    try:
        workers = detach.snapshot_workers(provider_id)
        drains = detach.detach(provider_id, kind_of(row), withdraw=False)
        detach.when_drained(drains, lambda: detach.stop_stragglers(workers), name=provider_id)
    except Exception:
        logging.getLogger(__name__).exception("Provider Hub: could not release the pools of %s after it was switched "
                                              "off", provider_id)


def runtime_provider_configs() -> dict[str, dict[str, Any]]:
    state = load_state()
    configs = {}
    for provider_id, installation in (state.get("installations") or {}).items():
        if not isinstance(installation, dict) or kind_of(installation) != SUBTITLE_PROVIDER:
            continue
        # The active provider class is keyed off ``active_version`` and stays
        # registered until restart, even while an update or removal is staged.
        # Filtering on ``state == active`` would strip the config from a
        # pending-restart row and the live search would run without
        # credentials. Use ``active_version`` as the gate instead.
        if not installation.get("active_version"):
            continue
        # A release that was held back or detached live runs nothing, so what it was configured with is not handed
        # to the class that took its id back (a built-in a plugin had replaced). The registry is what says whose
        # class holds the id: a row that still names a release (a revocation lifted without a restart, an install
        # over a removal) is not enough.
        if installation.get("blocked") or (
                installation.get("state") == "removed" and not installation.get("pending_restart")):
            continue
        from .registry import is_plugin_class

        if not is_plugin_class(provider_id):
            continue
        # Fail closed: a stored credential that will not decrypt is left
        # out of the worker's config (never sent as the credential), and
        # the provider is left out of searches until it is fixed. A value
        # in the plugin's own settings section satisfies the credential
        # (the worker config is laid over it), so only a field neither
        # source supplies keeps the provider out. Say why on the
        # installation once, not on every pool build.
        if _unsatisfied_secret_fields(provider_id, installation, state):
            if installation.get("last_error") != DECRYPT_FAILED_MESSAGE:
                _record_provider_last_error(provider_id, DECRYPT_FAILED_MESSAGE)
            continue
        if installation.get("last_error") == DECRYPT_FAILED_MESSAGE:
            # The block lifted on its own: a settings-section value now
            # covers the field, or the stored credential decrypts again.
            # The note was only ever the echo of the block, so leaving it
            # would keep asking for a re-entry that already happened.
            _record_provider_last_error(provider_id, None)
        configs[provider_id] = _effective_installation_config(installation, state)[0]
    return configs


def _settings_section_supplies(provider_id: str, field: str) -> bool:
    """Whether the plugin's own settings section holds a non-empty live
    value for `field` - the same overlay get_providers_auth applies, so
    the block agrees with what the worker would actually receive."""
    try:
        from app.config import settings
        section = settings.get(str(provider_id), {}) if hasattr(settings, "get") else {}
        if not hasattr(section, "get"):
            return False
        return section.get(field) not in (None, "", [], {})
    except Exception:
        return False


def _unsatisfied_secret_fields(provider_id: str, installation: dict[str, Any],
                               state: dict[str, Any] | None = None) -> set[str]:
    """The installation's secret fields whose stored value will not
    decrypt and whose settings section supplies no live value either:
    exactly the ones that leave the provider without a credential."""
    _effective, undecryptable = _effective_installation_config(installation, state)
    return {
        field
        for field in undecryptable
        if not _settings_section_supplies(provider_id, field)
    }


def missing_required_fields(installation: dict[str, Any]) -> list[str]:
    """The `config_schema.required` fields of the installation's manifest that
    hold no usable value yet.

    A field counts as set when the stored provider config holds a non-empty
    value (a secret is stored encrypted, so it is only checked for presence,
    never decrypted), when the plugin's own settings section supplies one, or
    when the schema declares a default for it. The staged manifest stands in
    for the active one on a first install, which has no active manifest yet.
    """
    manifest = installation.get("manifest")
    if not isinstance(manifest, dict):
        manifest = installation.get("staged_manifest")
    if not isinstance(manifest, dict):
        return []
    schema = manifest.get("config_schema")
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    config = installation.get("config")
    config = config if isinstance(config, dict) else {}
    provider_id = str(installation.get("provider_id") or "")
    missing = []
    for field in required:
        if not isinstance(field, str):
            continue
        if config.get(field) not in (None, "", [], {}):
            continue
        # The runtime applies a schema default only when the key is absent, so
        # a field the user saved empty does not fall back to its default.
        declared = properties.get(field)
        if (field not in config and isinstance(declared, dict)
                and declared.get("default") not in (None, "", [], {})):
            continue
        if _settings_section_supplies(provider_id, field):
            continue
        missing.append(field)
    return missing


# The state-derived inputs of the decrypt block, cached per state.json
# version: the admission gate runs before every provider call, and loading
# and re-walking state.json each time was most of its cost. The cache key is
# the state file's mtime and size plus the write version (mutate_state bumps
# it, so a rewritten file never reads as unchanged), so anything that changes
# state.json invalidates it; the live failure paths and settings-section
# values are applied per call, outside the cache.
_BLOCK_CACHE: dict[str, Any] = {"key": None, "state": None}


def _block_cache_key() -> tuple:
    """The identity of state.json as this process sees it right now."""
    from .state import state_file, state_version
    try:
        path = state_file()
        stat = path.stat()
        disk_key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        path = None
        disk_key = None
    return (str(path), disk_key, state_version())


def _decrypt_block_state(cache_key: tuple) -> dict[str, Any]:
    """The blocked-set inputs that come from state.json alone: per provider,
    the active manifest's declared fields, the ones whose stored value will
    not decrypt, and the ones with a working stored value.

    The key is read by the caller, BEFORE the state is loaded, so a cache
    hit pays neither the file read nor the parse. On a miss the state is
    loaded and the block computed under that same key, and the answer is
    stored only when the state did not change while it was computed: a
    save landing in that window would otherwise pin the pre-save answer
    under the post-save key, and a provider whose credential was just
    re-entered would stay out of searches until some other write."""
    if _BLOCK_CACHE["key"] == cache_key:
        return _BLOCK_CACHE["state"]
    state = load_state()
    block: dict[str, Any] = {
        "declared": {},      # provider_id -> fields the active manifest declares
        "starved": {},       # provider_id -> declared fields with no working stored value
        "working": {},       # provider_id -> secret fields with a working stored value
    }
    for provider_id, installation in (state.get("installations") or {}).items():
        if not isinstance(installation, dict) or not installation.get("active_version"):
            continue
        # Keyed by the lowercased id: decrypt_failure_paths() carries the
        # registry's lowercase form, whatever case state.json was written in.
        key = str(provider_id).lower()
        active_manifest = installation.get("manifest")
        if not isinstance(active_manifest, dict):
            active_manifest = {}
        from secret_store.registry import _manifest_field_names, manifest_secret_fields
        block["declared"][key] = _manifest_field_names(active_manifest)
        effective, undecryptable = _effective_installation_config(installation, state)
        block["starved"][key] = set(undecryptable)
        working = set()
        for field in manifest_secret_fields(active_manifest) | _retained_secret_fields(key, state):
            value = effective.get(field)
            if value not in (None, "") and field not in undecryptable:
                working.add(field)
        block["working"][key] = working
    if _block_cache_key() == cache_key:
        _BLOCK_CACHE["key"] = cache_key
        _BLOCK_CACHE["state"] = block
    return block


def decrypt_blocked_provider_ids() -> set[str]:
    """Provider ids with no usable credential, from either place one lives.

    Two sources block a provider. A stored installation credential that
    will not decrypt blocks it unless the plugin's settings section
    supplies a live value for that field (the worker config is laid over
    that section, so the plugin runs on the section's value). And a
    settings-section credential that failed to decrypt at boot blocks it
    unless the stored config supplies a working value: the withheld empty
    string is what the overlay would apply. In both cases only fields the
    active manifest declares count - a retained field the plugin no longer
    reads withholds nothing it needs.

    The ids are published so the search path keeps these providers out of
    selection and admission too, not just out of the worker's
    configuration. A blocked provider is not searched until its
    credential is re-entered."""
    from secret_store.migration import decrypt_failure_paths

    block = _decrypt_block_state(_block_cache_key())
    blocked: set[str] = set()
    for provider_id, starved in block["starved"].items():
        if any(not _settings_section_supplies(provider_id, field) for field in starved):
            blocked.add(str(provider_id))
    # A settings-section credential that failed to decrypt is withheld from
    # the overlay, and the stored config is the only other source for the
    # field.
    for path in decrypt_failure_paths():
        provider_id, _, field = path.partition(".")
        declared = block["declared"].get(provider_id)
        if not declared or field not in declared:
            continue
        if field in block["working"].get(provider_id, ()):
            continue
        blocked.add(str(provider_id))
    return blocked


def list_installations(redact: bool = True, *, kind: str | None = None) -> list[dict[str, Any]]:
    """Installations of one kind, or of every kind when ``kind`` is None."""
    from . import runtime_status

    state = load_state()
    enabled_provider_ids = set(provider_kind.bazarr_enabled_providers())
    installations = []
    for provider in (state.get("installations") or {}).values():
        if not isinstance(provider, dict):
            installations.append(provider)
            continue
        if kind is not None and kind_of(provider) != kind:
            continue
        if kind_of(provider) not in known_kinds():
            logging.getLogger(__name__).warning(
                "Skipping installation %s: kind %s is not defined by this build",
                provider.get("provider_id"), kind_of(provider))
            continue
        item = dict(_with_origin(provider, state))
        status = runtime_status.get(item.get("provider_id"))
        status_is_current = (
            item.get("provider_id") in enabled_provider_ids
            and item.get("state") != "removed"
        )
        if status is not None and status_is_current:
            item["runtime_status"] = status
        installations.append(_redact_installation(item) if redact else item)
    return installations


def dismiss_review_notice(provider_id: str) -> bool:
    """Record on the installation that the person has read its review notice.

    An installation made for the person, rather than by them, carries ``review_pending`` until they have seen
    what it may do. Dismissing it clears the flag on the record itself, so it stays dismissed from any browser.
    False when there is no such installation.
    """
    def clear(state: dict[str, Any]) -> bool:
        provider = (state.get("installations") or {}).get(provider_id)
        if not isinstance(provider, dict):
            return False
        provider["review_pending"] = False
        return True

    return bool(mutate_state(clear))


def list_providers(redact: bool = True) -> list[dict[str, Any]]:
    """The Subtitle Hub's listing: subtitle providers only."""
    return list_installations(redact, kind=SUBTITLE_PROVIDER)


def get_provider(provider_id: str, redact: bool = True) -> dict[str, Any] | None:
    state = load_state()
    provider = (state.get("installations") or {}).get(provider_id)
    if not isinstance(provider, dict):
        return None
    provider = _with_origin(provider, state)
    return _redact_installation(provider) if redact else provider


def _record_provider_last_error(provider_id: str, message: str | None, python_path: str | None = None) -> None:
    # The message is an exception's text, which for a worker failure is the
    # plugin's own words: it is stored in state, shown by the API and read
    # by the next boot, so it is masked and cut at this one write.
    if message is not None:
        message = refusal_text(message)

    def update_error(state: dict[str, Any]) -> None:
        provider = state.setdefault("installations", {}).get(provider_id)
        if not isinstance(provider, dict):
            return
        if python_path is not None and provider.get("python_path") != python_path:
            # The result belongs to an interpreter the installation no longer runs from.
            return
        provider["last_error"] = message

    mutate_state(update_error)


def _remove_bundle_runtime_artifacts(bundle_path: Path) -> None:
    # Only inside the hub's own bundles, reached without a ``..`` or a symlink:
    # the path often comes out of the state, and a stored value must not point
    # this at someone else's directory tree.
    bundle = validated_bundle_path(provider_hub_dir(), bundle_path)
    if bundle is None:
        return
    for cache_dir in list(bundle.rglob("__pycache__")):
        shutil.rmtree(cache_dir, ignore_errors=True)


def _blocked_connection_result(provider_id: str, provider: dict[str, Any], job) -> dict[str, Any]:
    """The answer for a plugin that was not started because what it relies on is not running."""
    message = provider.get("blocked_reason") or provider.get("last_error") or (
        "This plugin was not started because what it relies on is not running.")
    job.data["state"] = "failed"
    job.update(error=message, message=message, details={"status": "blocked"})
    return {"provider_id": provider_id, "ok": False, "status": "blocked", "message": message}


def test_provider_connection(provider_id: str) -> dict[str, Any] | None:
    provider = get_provider(provider_id, redact=False)
    if not provider:
        return None

    target_name = provider.get("name") or provider_id

    with record_job(
        "test_connection",
        target_kind="provider",
        target_id=provider_id,
        target_name=target_name,
    ) as job:
        if provider.get("pending_restart") or provider.get("state") != "active":
            job.update(
                message="Restart Bazarr+ before testing this staged plugin",
                details={"status": "pending_restart"},
            )
            return {
                "provider_id": provider_id,
                "ok": False,
                "status": "pending_restart",
                "message": "Restart Bazarr+ before testing this staged plugin",
            }
        if provider.get("blocked"):
            return _blocked_connection_result(provider_id, provider, job)

        # The row is read again under the lock a cleanup decides under, and the
        # environment is held in the same step: a repair and a cleanup that ran
        # since the first read cannot take it away while the test runs.
        with _ENV_LOCK:
            provider = get_provider(provider_id, redact=False) or provider
            blocked_now = bool(provider.get("blocked"))
            manifest_data = provider.get("manifest")
            active_path = provider.get("active_path")
            python_path = provider.get("python_path")
            release_environment = (
                hold_environment(env_registry.env_dir_of(python_path)) if python_path else (lambda: None)
            )

        if blocked_now:
            # Held back since the first read: nothing of it is started to be tested.
            release_environment()
            return _blocked_connection_result(provider_id, provider, job)

        client = None
        try:
            if not isinstance(manifest_data, dict):
                raise ProviderHubInstallError("active manifest is missing")
            if not active_path:
                raise ProviderHubInstallError("active bundle path is missing")
            if not python_path:
                raise ProviderHubInstallError("provider Python path is missing")
            interpreter = validated_interpreter(provider_hub_dir(), python_path)
            if interpreter is None:
                raise ProviderHubInstallError("provider Python path is not one of the hub's environments")

            trusted = bool(provider.get("trusted", False))
            manifest = validate_manifest(
                manifest_data,
                built_in_provider_ids=_validation_built_in_provider_ids(
                    manifest_data,
                    trusted,
                ),
            )
            bundle_path = validated_bundle_path(provider_hub_dir(), active_path)
            if bundle_path is None:
                env_registry.mark_needs_repair(provider_id, python_path)
                raise ProviderHubInstallError("active bundle path is not one of the hub's bundles")
            _remove_bundle_runtime_artifacts(bundle_path)
            verify_bundle_tree(manifest, bundle_path)

            runner = Path(__file__).with_name("worker_runner.py")
            client = ProviderWorkerClient(
                worker_command(interpreter, runner),
                cwd=bundle_path,
                env={
                    "BAZARR_PROVIDER_HUB_BUNDLE": str(bundle_path),
                    "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps(manifest.raw),
                },
            )
            try:
                result = client.request("health", {}, timeout=10)
            except Exception:
                # The worker could not start or answer here, which is what a
                # missing, stale or foreign environment looks like.
                env_registry.mark_needs_repair(provider_id, python_path)
                raise
            finally:
                client.stop()
                _remove_bundle_runtime_artifacts(bundle_path)

            _record_provider_last_error(provider_id, None, python_path)
            env_registry.clear_needs_repair(provider_id, python_path)
            job.update(
                message="Worker health check passed",
                details={"status": "ready"},
            )
            return {
                "provider_id": provider_id,
                "ok": True,
                "status": "ready",
                "message": "Worker health check passed",
                "details": result.payload,
            }
        except Exception as error:
            message = scrub_text(str(error) or error.__class__.__name__)
            _record_provider_last_error(provider_id, message, python_path)
            job.data["state"] = "failed"
            job.update(
                error=message,
                message=message,
                details={"status": "failed"},
            )
            return {
                "provider_id": provider_id,
                "ok": False,
                "status": "failed",
                "message": message,
            }
        finally:
            if client is None:
                release_environment()
            else:
                # Held until the worker and anything it started are gone: a
                # stop that leaves a survivor keeps the environment in place.
                release_when_gone(client, release_environment, f"the connection test of {provider_id}")


def _built_in_provider_ids() -> set[str]:
    provider_ids = set(provider_registry.names())
    try:
        from .registry import _REGISTERED_PROVIDER_HUB_IDS
        provider_ids = provider_ids - _REGISTERED_PROVIDER_HUB_IDS
    except Exception:
        pass
    # Keep migrated built-in ids in the denylist even after a hub provider has
    # registered one, so an untrusted install of the same id can never shadow it.
    # Retired built-in ids stay on the denylist forever: their module is gone, so
    # nothing else keeps the id claimed.
    return provider_ids | MIGRATED_BUILT_IN_PROVIDER_IDS | RETIRED_BUILT_IN_PROVIDER_IDS


def _manifest_provider_id(manifest: dict[str, Any]) -> str:
    return manifest_provider_id(manifest)


PLUGIN_KIND_NOT_INSTALLED_REASON = "This Bazarr+ does not install this kind of plugin."
NEW_KIND_OFFICIAL_ONLY_REASON = "Only the official catalog can install this kind of plugin."
LOCAL_PACKAGE_KIND_REASON = (
    "A local package can only be a subtitle provider. Only the official catalog can install this kind of plugin."
)


def _kind_for_manifest(manifest: Any):
    """The registered kind that stages ``manifest``, or ManifestValidationError.

    A schema 1 manifest, and anything that is not an object, belongs to the subtitle provider kind,
    which gives the refusal it always has for a manifest it cannot read. A schema 2 manifest names
    its kind and the contract it follows, and both must be defined by this Bazarr+.
    """
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        return get_kind(SUBTITLE_PROVIDER)
    plugin = parse_plugin_manifest(manifest)
    kind = kind_for_api(plugin.kind_api)
    if kind is None:
        raise ManifestValidationError(PLUGIN_KIND_NOT_INSTALLED_REASON)
    if kind.name != plugin.kind:
        raise ManifestValidationError(PLUGIN_KIND_MISMATCH_REASON)
    return kind


def _validation_built_in_provider_ids(manifest: dict[str, Any], trusted: bool) -> set[str]:
    return validation_built_in_provider_ids(
        _manifest_provider_id(manifest),
        _built_in_provider_ids(),
        trusted,
    )


def _bundle_path_for(manifest) -> Path:
    return (
        provider_hub_dir()
        / "bundles"
        / manifest.provider_id
        / manifest.version
        / manifest.source_commit
    )


def _github_raw_url(manifest, relative_path: str) -> str:
    raw_path = "/".join(part for part in (manifest.source_path, relative_path) if part)
    return f"https://raw.githubusercontent.com/{manifest.source_repo}/{manifest.source_commit}/{raw_path}"


def _deadline_request_timeout(deadline: float | None, default: float = 30.0) -> float:
    """Per-request timeout bounded by an optional wall-clock deadline (monotonic).

    Raises ``ProviderHubInstallError`` when the deadline has already passed so a
    slow bundle fetch can't run the startup auto-install past its time budget.
    """
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderHubInstallError("bundle fetch exceeded the install time budget")
    return max(1.0, min(default, remaining))


# The channel an install may read its bundle from the mirror for, set while the install of an official
# source's entry runs. None for every other install, which reads GitHub only.
_BUNDLE_MIRROR_CHANNEL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "provider_hub_bundle_mirror_channel", default=None)


# The largest single bundle file read through the origins. The old fetch had no limit, and no file of the
# catalog comes near this.
_BUNDLE_FILE_MAX_BYTES = 64 * 1024 * 1024


def _bundle_mirror_channel(state: dict[str, Any], source_id: str | None) -> str | None:
    """The mirror channel the bundle of an entry of ``source_id`` may be read from, or None.

    Only the official source with no ``dev_ref`` qualifies, with the user's setting on. A community source
    or a source on a development ref reads GitHub only.
    """
    if source_id != OFFICIAL_CATALOG_SOURCE_ID or not _fallback_setting_on():
        return None
    source = (state.get("catalog_sources") or {}).get(OFFICIAL_CATALOG_SOURCE_ID)
    if not isinstance(source, dict) or source.get("dev_ref") or source.get("official") is False:
        return None
    return catalog_feed.CHANNEL_MAIN


def _fetch_github_bundle_file(
    manifest, relative_path: str, deadline: float | None = None, route: "transport.JobRoute | None" = None,
) -> bytes:
    bundled = floor.bundle_file(manifest, relative_path)
    if bundled is not None:
        return bundled
    channel = _BUNDLE_MIRROR_CHANNEL.get()
    raw_path = "/".join(part for part in (manifest.source_path, relative_path) if part)
    key = transport.FetchKey(str(manifest.source_repo), channel or "", manifest.source_commit, raw_path)
    # A file whose path the origins cannot address is read from GitHub the old way, mirror or not.
    if (channel is not None and str(manifest.source_repo).lower() == transport.OFFICIAL_REPO.lower()
            and transport.key_is_addressable(key)):
        content = transport.fetch_with_fallback(
            transport.default_origins(), key, surface=transport.SURFACE_BUNDLE, official=True,
            allow_fallback=True, route=route if route is not None else transport.JobRoute(),
            timeout=_deadline_request_timeout(deadline), max_bytes=_BUNDLE_FILE_MAX_BYTES).content
    else:
        response = requests.get(
            _github_raw_url(manifest, relative_path),
            timeout=_deadline_request_timeout(deadline),
        )
        response.raise_for_status()
        content = response.content
    if not isinstance(content, bytes):
        raise ProviderHubInstallError(f"GitHub returned invalid content for {relative_path}")
    return content


def _write_manifest_file(manifest, root: Path) -> None:
    (root / "provider.json").write_text(
        json.dumps(manifest.raw, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _fetch_bundle(manifest, deadline: float | None = None) -> Path:
    target = _bundle_path_for(manifest)
    if target.exists():
        _remove_bundle_runtime_artifacts(target)
        verify_bundle_tree(manifest, target)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=str(target.parent)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        # One install is one job: it moves to the mirror at most once, whichever file meets the failure.
        route = transport.JobRoute() if _BUNDLE_MIRROR_CHANNEL.get() is not None else None
        for relative_path in manifest.files:
            destination = tmp_path / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            fetched = (
                _fetch_github_bundle_file(manifest, relative_path, deadline=deadline) if route is None
                else _fetch_github_bundle_file(manifest, relative_path, deadline=deadline, route=route))
            destination.write_bytes(fetched)

        _write_manifest_file(manifest, tmp_path)
        verify_bundle_tree(manifest, tmp_path)
        shutil.move(str(tmp_path), str(target))

    verify_bundle_tree(manifest, target)
    return target


_LOCAL_PACKAGE_MAX_MEMBERS = 5000
_LOCAL_PACKAGE_MAX_TOTAL_BYTES = 100 * 1024 * 1024  # 100 MB uncompressed

# The uploaded .zip itself, bounded by the upload route before it is spooled.
MAX_LOCAL_PACKAGE_SIZE = 100 * 1024 * 1024  # 100 MiB


def local_package_uploads_dir() -> Path:
    """Where an uploaded package waits on disk for its install job."""
    return provider_hub_dir() / "uploads"


def discard_local_package(path: str | Path) -> None:
    with contextlib.suppress(OSError):
        Path(path).unlink()


def discard_local_package_uploads() -> None:
    """Remove every spooled package. Run at startup: pending jobs do not survive
    a restart, so nothing is waiting for these files any more."""
    uploads = local_package_uploads_dir()
    if not uploads.is_dir():
        return
    for leftover in uploads.iterdir():
        if leftover.is_file():
            discard_local_package(leftover)


def _safe_extract_zip(package: bytes | str | Path, dest: Path) -> None:
    """Extract a zip into ``dest`` with zip-slip, size and read-error guards.

    ``package`` is the path of the spooled upload, or the package's bytes.
    """
    dest_root = dest.resolve()
    source = io.BytesIO(package) if isinstance(package, (bytes, bytearray)) else package
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as error:
        raise ProviderHubInstallError("uploaded package is not a valid .zip archive") from error
    except OSError as error:
        raise ProviderHubInstallError("uploaded package could not be read") from error
    with archive:
        infos = archive.infolist()
        # Bound the work before extracting so an oversized or zip-bomb upload
        # cannot fill the disk or stall the install.
        if len(infos) > _LOCAL_PACKAGE_MAX_MEMBERS:
            raise ProviderHubInstallError("package contains too many files")
        if sum(max(0, info.file_size) for info in infos) > _LOCAL_PACKAGE_MAX_TOTAL_BYTES:
            raise ProviderHubInstallError("package is too large")
        for member in infos:
            name = member.filename
            if not name or name.endswith("/"):
                continue
            # Reject absolute paths and any traversal before joining, then confirm
            # the resolved target stays under dest_root (zip-slip containment that
            # CodeQL recognizes as a barrier on the member name).
            member_path = PurePosixPath(name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ProviderHubInstallError(f"unsafe path in package: {name}")
            target = dest_root.joinpath(*member_path.parts).resolve()
            try:
                target.relative_to(dest_root)
            except ValueError:
                raise ProviderHubInstallError(f"unsafe path in package: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(member) as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)
            except (RuntimeError, zipfile.BadZipFile, OSError) as error:
                # Encrypted/corrupt members raise here, outside the open() guard
                # above; surface them as a 400 invalid-package, not a 500.
                raise ProviderHubInstallError(
                    f"could not read package entry {name}: {error}"
                ) from error


def _find_local_manifest(extracted: Path) -> tuple[dict[str, Any], Path]:
    """Locate the manifest inside an extracted local package and its bundle root."""
    candidates = sorted(
        list(extracted.rglob("provider.json")) + list(extracted.rglob("manifest.json")),
        key=lambda path: len(path.parts),
    )
    if not candidates:
        raise ProviderHubInstallError("package must contain a provider.json manifest")
    manifest_file = candidates[0]
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProviderHubInstallError(f"invalid manifest in package: {error}") from error
    if not isinstance(manifest, dict):
        raise ProviderHubInstallError("package manifest must be a JSON object")
    return manifest, manifest_file.parent


def _stage_local_bundle(validated, source_dir: Path) -> Path:
    """Stage a bundle from already-extracted local files (no network fetch)."""
    target = _bundle_path_for(validated)
    if target.exists():
        _remove_bundle_runtime_artifacts(target)
        verify_bundle_tree(validated, target)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=str(target.parent)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        for relative_path in validated.files:
            source_file = source_dir / relative_path
            if not source_file.is_file():
                raise ProviderHubInstallError(f"package is missing declared file: {relative_path}")
            destination = tmp_path / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source_file.read_bytes())

        _write_manifest_file(validated, tmp_path)
        verify_bundle_tree(validated, tmp_path)
        shutil.move(str(tmp_path), str(target))

    verify_bundle_tree(validated, target)
    return target


def _trust_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(manifest)
    if isinstance(normalized.get("source"), dict):
        source = dict(normalized["source"])
        source.pop("trusted", None)
        normalized["source"] = source
    return normalized


def _catalog_binding(
    manifest: dict[str, Any], state: dict[str, Any], source_id: str | None = None
) -> tuple[str | None, bool]:
    """The catalog source an install of ``manifest`` binds to, and its trust.

    Only an entry Bazarr+ holds binds an install: one whose manifest equals this
    one apart from ``source.trusted``, from ``source_id`` when it is given, else a
    trusted source first. A forged manifest matches no trusted entry and installs
    untrusted. The manifest's own source fields are data, never its identity.
    """
    provider_id = manifest_provider_id(manifest)
    version = manifest.get("version")
    sources = state.get("catalog_sources") or {}
    binding: tuple[str | None, bool] | None = None
    for entry in (state.get("catalog_entries") or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("provider_id") != provider_id or entry.get("version") != version:
            continue
        if source_id is not None and entry.get("source") != source_id:
            continue
        entry_manifest = entry.get("manifest") if isinstance(entry.get("manifest"), dict) else {}
        if _trust_manifest(entry_manifest) != _trust_manifest(manifest):
            continue
        trusted = bool(catalog_source_for_entry(sources, entry).get("trusted", False)) and bool(
            entry.get("trusted", False))
        if binding is None or (trusted and not binding[1]):
            binding = (entry.get("source"), trusted)
    return binding or (None, False)


def _manifest_catalog_list(manifest: dict[str, Any], state: dict[str, Any], source_id: str | None) -> str:
    """The catalog list the entry ``manifest`` equals is stored under.

    A manifest no entry holds installs untrusted with no source and gets no updates,
    so the list only needs a value: a schema 2 manifest is a plugin, anything else a provider.
    """
    provider_id = manifest_provider_id(manifest)
    for entry in (state.get("catalog_entries") or {}).values():
        if (isinstance(entry, dict) and isinstance(entry.get("manifest"), dict)
                and entry.get("provider_id") == provider_id and entry.get("version") == manifest.get("version")
                and (source_id is None or entry.get("source") == source_id)
                and _trust_manifest(entry["manifest"]) == _trust_manifest(manifest)):
            return entry.get("catalog_list") or CATALOG_LIST_PROVIDERS
    return CATALOG_LIST_PLUGINS if manifest.get("schema_version") == 2 else CATALOG_LIST_PROVIDERS


RESERVED_ID_REASON = (
    "The official catalog publishes this provider, so only the official catalog can "
    "install or update it."
)


def _reserved_provider_ids(state: dict[str, Any]) -> set[str]:
    """Ids only the official catalog may install: its last trusted refresh, the
    snapshot shipped with this release, and the built-in ids plugins take over."""
    official = (state.get("catalog_sources") or {}).get(OFFICIAL_CATALOG_SOURCE_ID)
    published = official.get("provider_ids") if isinstance(official, dict) else None
    return (
        {str(item) for item in published or [] if isinstance(item, str)}
        | OFFICIAL_CATALOG_PROVIDER_IDS
        | MIGRATED_BUILT_IN_PROVIDER_IDS
        | RETIRED_BUILT_IN_PROVIDER_IDS
    )


def _supply_refusal(state: dict[str, Any]) -> str | None:
    """Why nothing installs from the official source's cached catalog right now (expired, or older than a
    counter or latch this host holds), or None. Running plugins are never touched."""
    official = (state.get("catalog_sources") or {}).get(OFFICIAL_CATALOG_SOURCE_ID)
    return catalog_feed.supply_refusal(state, official) if isinstance(official, dict) else None


def _source_may_carry_new_kinds(state: dict[str, Any], source_id: str | None) -> bool:
    """Whether ``source_id`` may supply plugins of a kind other than the subtitle provider.

    Only the official source may, and only while Bazarr+ trusts it. A community source never may, except
    the one source the development install path names, which stays untrusted all the same.
    """
    source = (state.get("catalog_sources") or {}).get(source_id) if source_id else None
    if source_id == OFFICIAL_CATALOG_SOURCE_ID:
        return isinstance(source, dict) and source.get("trusted") is True
    return isinstance(source, dict) and dev_source.is_dev_source(source_id, source)


def _revocation_lists(state: dict[str, Any]) -> dict[str, revocation.ChannelList]:
    """The lists this host holds, by channel: of the database copy and the file copy, the newer one."""
    official = (state.get("catalog_sources") or {}).get(OFFICIAL_CATALOG_SOURCE_ID)
    return revocation.effective(revocation.from_source(official), revocation.read_mirror())


def _revocations(state: dict[str, Any]) -> tuple[revocation.Revocation, ...]:
    """What the official catalog has revoked, as this host last verified it: every channel's list together.
    Nothing verified means none."""
    return revocation.entries_of(_revocation_lists(state))


def _revocations_stale(state: dict[str, Any], channels: Iterable[str] = revocation.CHANNELS) -> list[str]:
    """The given channels whose revocation list is older than the catalog this host last accepted for them.

    The counter file survives a restored database, and the list the database held does not, so a list that
    is behind the counter cannot be trusted to be the latest. Such a list is kept (dropping it would lift
    what it revokes) and the channel is read again at the next refresh. Only the channel a host reads is
    judged by the caller: another channel it once used is read again only when the host goes back to it.
    """
    lists = _revocation_lists(state)
    stale = []
    for channel in channels:
        accepted = catalog_feed.stored_record(state, channel).signed_version
        held = lists.get(channel)
        if accepted is not None and (held is None or (held.signed_version or 0) < accepted):
            stale.append(channel)
    return stale


def _revocation_text(build, found: revocation.Revocation, name: Any, version: Any, *extra: Any) -> str:
    """A revocation text with the reason masked of anything that looks like a credential, within the message limit."""
    if found.reason:
        found = revocation.with_reason(found, scrub_text(found.reason))
    return build(found, str(name), version, *extra)


def _revocation_refusal(
    state: dict[str, Any], provider_id: Any, version: Any = None, name: Any = None, action: str = "installed",
) -> str | None:
    """Why the official catalog's revocation list keeps ``version`` of ``provider_id`` from being ``action``, or None.

    Without a version only a revocation of every release of the plugin counts. The list is the one a
    verified catalog carried, whichever source the plugin comes from: an id the official catalog revokes
    is revoked here.
    """
    found = revocation.lookup(_revocations(state), provider_id, version)
    if found is None:
        return None
    return _revocation_text(revocation.refusal, found, name or provider_id, version, action)


def _install_refusal(
    state: dict[str, Any], source_id: str | None, provider_id: Any, kind: str = SUBTITLE_PROVIDER,
    version: Any = None, name: Any = None,
) -> str | None:
    revoked = _revocation_refusal(state, provider_id, version, name)
    if revoked:
        return revoked
    if kind != SUBTITLE_PROVIDER and not _source_may_carry_new_kinds(state, source_id):
        return NEW_KIND_OFFICIAL_ONLY_REASON
    if source_id != OFFICIAL_CATALOG_SOURCE_ID and str(provider_id) in _reserved_provider_ids(state):
        return RESERVED_ID_REASON
    if source_id == OFFICIAL_CATALOG_SOURCE_ID:
        return _supply_refusal(state)
    return None


def _host_refusal(entry: dict[str, Any], host: HostFacts) -> str | None:
    """Why this Bazarr+ cannot run a catalog entry, or None when it can."""
    compatible, reason = entry_compatibility(entry, host)
    return None if compatible else reason


def _entry_usable(state: dict[str, Any], host: HostFacts):
    """Whether a stored catalog entry may be installed here: its source may supply its kind, this host
    can run it and its kind accepts its manifest. The rule a dependency is chosen by, as the plugin is."""

    def usable(entry: dict[str, Any]) -> bool:
        if _install_refusal(state, entry.get("source"), entry.get("provider_id"), catalog_kind_of(entry),
                            entry.get("version")):
            return False
        if _host_refusal(entry, host) is not None:
            return False
        try:
            get_kind(catalog_kind_of(entry)).validate_manifest(entry["manifest"], bool(entry.get("trusted", False)))
        except Exception:
            return False
        return True

    return usable


def _closure_of(state: dict[str, Any], entry: dict[str, Any], host: HostFacts | None = None):
    host = host or current_host()
    return closure.resolve_closure(state, entry, _entry_usable(state, host))


def _closure_refusal(state: dict[str, Any], entry: dict[str, Any], host: HostFacts | None = None) -> str | None:
    """Why the dependencies of ``entry`` do not resolve here, or None when they do or it has none."""
    if not closure.has_needs(entry.get("manifest")):
        return None
    return _closure_of(state, entry, host).refusal


def _with_origin(provider: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """A serialized installation with its origin and the source Bazarr+ bound it to.

    The stored ``source_id`` is authoritative once the install is bound; the name
    comes from Bazarr+'s own source record, never from the manifest. An install
    that is not bound, or whose source is gone, has no source name.
    """
    result = dict(provider)
    if result.get("origin") != "local":
        result["origin"] = "catalog"
    source_id = result.get("source_id") if result.get("source_bound") else None
    source = (state.get("catalog_sources") or {}).get(source_id) if source_id else None
    result["source_id"] = source_id
    result["source_name"] = source.get("name") if isinstance(source, dict) else None
    # An install from the development source says so, and is never shown as official.
    result["development_source"] = dev_source.is_dev_source(source_id, source)
    return result


def _verify_bundle_for_run(manifest, bundle_path: Path) -> None:
    """The check every run of a plugin's code starts with: the caches go, then the tree must match its manifest."""
    _remove_bundle_runtime_artifacts(bundle_path)
    verify_bundle_tree(manifest, bundle_path)


def _smoke_validate_worker(manifest, bundle_path: Path, python_path: Path, timeout: float = 10.0) -> None:
    _verify_bundle_for_run(manifest, bundle_path)
    runner = Path(__file__).with_name("worker_runner.py")
    # The environment is held for as long as the worker may run, which can be
    # past this call when the stop leaves something running.
    release = hold_environment(env_registry.env_dir_of(python_path))
    client = None
    try:
        client = ProviderWorkerClient(
            worker_command(python_path, runner),
            cwd=bundle_path,
            env={
                "BAZARR_PROVIDER_HUB_BUNDLE": str(bundle_path),
                "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps(manifest.raw),
            },
        )
        client.request("health", {}, timeout=timeout)
    finally:
        try:
            if client is not None:
                client.stop()
        finally:
            _remove_bundle_runtime_artifacts(bundle_path)
            if client is None:
                release()
            else:
                release_when_gone(client, release, "a health check")


def _catalog_list_field(catalog_list: str | None) -> dict[str, str]:
    """The ``catalog_list`` key an installation row records, or nothing for a local package."""
    return {"catalog_list": catalog_list} if catalog_list else {}


def _installed_catalog_list(installation: dict[str, Any]) -> str:
    """The catalog list an installation came from, which its updates are taken from.

    New installations record it. A row stored before it was recorded has none, so it
    is inferred from the manifest: a schema 2 manifest is listed in the plugins list
    only and every other manifest in the providers list only, which makes the
    inference exact. A row with no readable manifest defaults to providers, the list
    every installation came from before the plugins list existed.
    """
    recorded = installation.get("catalog_list")
    if recorded in (CATALOG_LIST_PROVIDERS, CATALOG_LIST_PLUGINS):
        return recorded
    for key in ("manifest", "staged_manifest"):
        manifest = installation.get(key)
        if isinstance(manifest, dict):
            return CATALOG_LIST_PLUGINS if manifest.get("schema_version") == 2 else CATALOG_LIST_PROVIDERS
    return CATALOG_LIST_PROVIDERS


def _abandon_staged_catalog_list(installation: dict[str, Any]) -> None:
    """Put back the catalog list of the active version when its staged update is dropped.

    A staged update records the new version's list and remembers the one the active
    version came from. Dropping the staged update puts that list back, or removes the
    field when none was remembered so it is worked out from the active manifest. A row
    with no active version has nothing to go back to and keeps what it records.
    """
    previous_list = installation.pop("previous_catalog_list", None)
    if not installation.get("active_version"):
        return
    if previous_list:
        installation["catalog_list"] = previous_list
    else:
        installation.pop("catalog_list", None)


def _abandon_staged_release(installation: dict[str, Any], message: str) -> None:
    """Drop a staged release that will not activate and record why. The active release, if any, stays."""
    installation["last_error"] = message
    if installation.pop(releases.STAGED_ROLLBACK, None) is None:
        _abandon_staged_catalog_list(installation)
    installation["staged_version"] = None
    installation["staged_path"] = None
    installation["staged_python_path"] = None
    installation["staged_manifest"] = None
    installation.pop("staged_supplied_by", None)
    # The restart a refresh asked for, to stop a revoked release, is still to come.
    installation["pending_restart"] = bool(installation.get("revoked_restart"))
    installation["state"] = "active" if installation.get("active_version") else "failed"
    # The release tables keep the release that did not activate, with this reason.
    installation[releases.FAIL_STAGED] = message


def _drop_revoked_staged(installations: dict[str, Any], revocations: tuple[revocation.Revocation, ...]) -> bool:
    """Drop each staged release the revocation list names, so it never activates. Returns whether any row changed."""
    changed = False
    for plugin_id, row in installations.items():
        if not isinstance(row, dict) or row.get("state") != "staged" or not row.get("staged_version"):
            continue
        found = revocation.lookup(revocations, plugin_id, row.get("staged_version"))
        if found is not None:
            _abandon_staged_release(row, _revocation_text(
                revocation.refusal, found, row.get("name") or plugin_id, row["staged_version"], "installed"))
            changed = True
    return changed


def _hold_revoked_installations(state: dict[str, Any]) -> None:
    """After a refresh: mark each installed release the list now revokes, release one it no longer does, and
    drop a staged release it names. Nothing else about the installations is judged here, and nothing is stopped
    here: a release of a kind that cannot stop live keeps running, with its settings and its place in the
    registry, until the next restart holds it back, and the restart cue it raises is the Hub's ordinary one, so
    while it is up a connection test asks for the restart first and Discover leaves the plugin out: both are the
    safe direction. A release of a kind that stops live is stopped right after this commits
    (``enforce_revocations_live``)."""
    installations = state.setdefault("installations", {})
    revocations = _revocations(state)
    # A revoked staged release goes first, so the text and the restart cue are decided on what is left.
    _drop_revoked_staged(installations, revocations)
    _mark_revoked_installations(installations, revocations)


def _staged_installation(
    validated,
    existing,
    bundle_path: Path,
    staged_python_path: Path,
    source_trusted: bool,
    origin: str = "catalog",
    source_id: str | None = None,
    catalog_list: str | None = None,
):
    existing = existing if isinstance(existing, dict) else {}
    previous = {}
    if existing.get("active_version"):
        # A staged update replaces the recorded list with the new version's. The
        # list the active version came from is kept so a failed activation can
        # put it back. Staging over a staged update keeps the first one.
        restaged = existing.get("pending_restart") and existing.get("state") == "staged"
        previous_list = (existing.get("previous_catalog_list")
                         if restaged and not existing.get(releases.STAGED_ROLLBACK) else existing.get("catalog_list"))
        if previous_list:
            previous = {"previous_catalog_list": previous_list}
    return {
        **_catalog_list_field(catalog_list),
        **previous,
        "provider_id": validated.provider_id,
        "kind": validated.kind,
        "name": validated.name,
        "active_version": existing.get("active_version"),
        "staged_version": validated.version,
        "active_path": existing.get("active_path"),
        "python_path": existing.get("python_path"),
        "staged_path": str(bundle_path),
        "staged_python_path": str(staged_python_path),
        "staged_manifest": validated.raw,
        "state": "staged",
        "pending_restart": True,
        "installed_at": utcnow_iso(),
        "activated_at": existing.get("activated_at"),
        "last_error": None,
        "trusted": bool(source_trusted),
        "origin": origin,
        "source_id": source_id,
        "source_bound": True,
        "manifest": existing.get("manifest") if existing.get("active_version") else validated.raw,
        "enabled": existing.get("enabled", True),
        "config": existing.get("config", {}),
        # What the running release was met by, and whether it was held back, stay with it until the update
        # takes over.
        **({"supplied_by": existing["supplied_by"]} if existing.get("supplied_by") else {}),
        **({"blocked": True, "blocked_reason": existing.get("blocked_reason"),
            **({"blocked_cause": existing["blocked_cause"]} if existing.get("blocked_cause") else {})}
           if existing.get("blocked") else {}),
        # A release the catalog revoked stays revoked, and is no supplier, until the update takes over.
        **({"revoked": True, "revoked_reason": existing.get("revoked_reason")} if existing.get("revoked") else {}),
    }


def _failed_installation(
    validated,
    existing,
    error: Exception,
    source_trusted: bool,
    origin: str = "catalog",
    source_id: str | None = None,
    catalog_list: str | None = None,
    state: dict[str, Any] | None = None,
):
    existing = existing if isinstance(existing, dict) else {}
    message = refusal_text(error)
    if (state is not None and existing.get("state") == "staged"
            and isinstance(existing.get("staged_manifest"), dict)
            and closure.broken_by(state, validated.provider_id, None)):
        # The staged release, whether it updates an active one or is a first install still waiting for the
        # restart, is what another plugin will rely on after it, so a restage that failed does not take it
        # away.
        installation = dict(existing)
        installation["last_error"] = message
        return installation
    if existing.get("active_version"):
        installation = dict(existing)
        if installation.get(releases.STAGED_ROLLBACK):
            _abandon_staged_release(installation, message)
            return installation
        if installation.get("state") == "staged":
            # A restage that fails drops the staged update the row still holds.
            _abandon_staged_catalog_list(installation)
        installation["last_error"] = message
        installation["staged_version"] = None
        installation["staged_path"] = None
        installation["staged_python_path"] = None
        installation["staged_manifest"] = None
        # The restart a refresh asked for, to stop a revoked release, is still to come after a failed update.
        installation["pending_restart"] = bool(installation.get("revoked_restart"))
        return installation

    return {
        **_catalog_list_field(catalog_list),
        "provider_id": validated.provider_id,
        "kind": validated.kind,
        "name": validated.name,
        "active_version": None,
        "staged_version": None,
        "active_path": None,
        "python_path": None,
        "staged_path": None,
        "staged_python_path": None,
        "staged_manifest": None,
        "state": "failed",
        "pending_restart": False,
        "installed_at": utcnow_iso(),
        "activated_at": None,
        "last_error": message,
        "trusted": bool(source_trusted),
        "origin": origin,
        "source_id": source_id,
        "source_bound": True,
        "manifest": validated.raw,
        "enabled": existing.get("enabled", True),
        "config": existing.get("config", {}),
    }


def _refuse_a_release_that_breaks_dependents(
    state: dict[str, Any], provider_id: str, version: str | None, manifest: dict[str, Any],
) -> None:
    """Refuse a release of an installed plugin that drops a capability something installed or staged relies on."""
    row = (state.get("installations") or {}).get(provider_id)
    if isinstance(row, dict) and (row.get("active_version") or row.get("staged_version")):
        broken = closure.broken_by(state, provider_id, manifest)
        if broken:
            raise ProviderHubInstallError(closure.breaks_text(provider_id, version, broken))


def _bundled_wheels_argument(validated, bundle_path: Path) -> dict[str, Path]:
    """The staged bundle, for an environment build that installs from the wheels the bundle carries.

    A bundle without wheels builds from the index and needs nothing from its files.
    """
    return {"bundle_path": bundle_path} if declared_wheels(validated) else {}


def _stage_validated(
    validated,
    source_trusted: bool,
    origin: str,
    source_id: str | None,
    bundle_stager,
    catalog_url: str | None = None,
    install_timeout: float | None = None,
    checkpoint=None,
    catalog_list: str | None = None,
) -> dict[str, Any]:
    """Shared install core: stage the bundle, build the venv, smoke-test, record.

    ``bundle_stager(validated, deadline) -> Path`` decides where the bundle comes
    from (catalog fetch vs already-extracted local files). ``install_timeout``, when
    set, is a single wall-clock budget for the whole stage (bundle fetch + venv
    build) shared via ``deadline``, so the startup auto-install can't run past it;
    ``None`` (manual installs) leaves it unbounded.

    ``checkpoint(message)`` runs before each step and raises ProviderHubStopped
    when the job was stopped. A stop records nothing, so a fresh install leaves no
    row and an update leaves the active version as it was, and it removes the
    bundle and venv this run staged. Recording is the last step it can stop.
    """
    step = checkpoint or (lambda message: None)
    staged = []
    env_path = None
    state = load_state()
    # Every route that installs or updates ends here, a local package included, so a revoked release is
    # refused whichever way it was asked for.
    revoked = _revocation_refusal(
        state, validated.provider_id, validated.version, validated.raw.get("name"), "installed")
    if revoked:
        raise ProviderHubInstallError(revoked)
    existing = (state.get("installations") or {}).get(validated.provider_id)
    if isinstance(existing, dict) and kind_of(existing) != validated.kind:
        # An installation's kind never changes: the id already belongs to a
        # plugin of another kind, and replacing it would hand that plugin's
        # settings, bindings and dependents to something else.
        raise ProviderHubInstallError(
            f"{validated.provider_id} is installed as a {kind_of(existing)} plugin and cannot "
            f"become a {validated.kind} plugin"
        )
    existing_version = (
        existing.get("active_version") if isinstance(existing, dict) else None
    )
    is_update = bool(existing_version)
    # A plugin removed in this run keeps its active release until the restart, so installing it again before then
    # reads as an update. It is a fresh install for everything that follows a removal: whose data it may take, and
    # what the kind makes ready.
    is_fresh = not is_update or (isinstance(existing, dict) and existing.get("state") == "removed")
    action = "stage_update" if is_update else "install"

    with record_job(
        action,
        target_kind="provider",
        target_id=validated.provider_id,
        target_name=validated.raw.get("name") or validated.provider_id,
        from_version=existing_version,
        to_version=validated.version,
        details={"catalog_url": catalog_url, "trusted": source_trusted, "origin": origin},
    ) as job, pin_scope():
        try:
            deadline = None if install_timeout is None else time.monotonic() + max(0.0, install_timeout)
            step("Downloading the provider")
            bundle_path = bundle_stager(validated, deadline)
            staged.append(bundle_path)
            step("Installing the provider's dependencies")
            install_remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            env_path = PluginEnvironment(provider_hub_dir()).install(
                validated, timeout=install_remaining, **_bundled_wheels_argument(validated, bundle_path))
            staged.append(env_path)
            staged_python_path = python_executable(env_path)
            step("Checking that the provider starts")
            smoke_timeout = None
            if deadline is not None:
                # The check shares the install's budget instead of adding its own.
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ProviderHubInstallError(
                        f"install for {validated.provider_id} exceeded its time budget"
                    )
                smoke_timeout = min(10.0, left)
            get_kind(validated.kind).smoke_test(
                validated, bundle_path, staged_python_path, timeout=smoke_timeout)
            step("Recording the installation")
        except ProviderHubStopped:
            _discard_stopped_install(staged, existing)
            raise
        except Exception as error:
            install_error = error
            if env_path is not None:
                # An environment this install built and nothing records or uses
                # would otherwise stay on disk for good.
                release_pins()
                env_registry.discard_if_unused(provider_hub_dir(), env_path)

            def record_failed_install(state: dict[str, Any]) -> dict[str, Any]:
                installations = state.setdefault("installations", {})
                current = installations.get(validated.provider_id, existing)
                installation = _failed_installation(
                    validated, current, install_error, source_trusted, origin, source_id, catalog_list, state
                )
                installations[validated.provider_id] = installation
                return dict(installation)

            mutate_state(record_failed_install)
            raise ProviderHubInstallError(str(install_error)) from install_error

        before_staging: dict[str, Any] = {}

        def record_staged_install(state: dict[str, Any]) -> dict[str, Any]:
            installations = state.setdefault("installations", {})
            # What the row was before this write, to put back if the install is refused after it.
            before_staging.clear()
            before_staging["row"] = copy.deepcopy(installations.get(validated.provider_id))
            current = installations.get(validated.provider_id, existing)
            if is_fresh and isinstance(current, dict) and current.get("state") == "removed":
                # The row of a plugin removed in this run still carries its saved settings and keys. They go to the
                # same plugin from the same source and to nobody else, so a package from another source, or an
                # upload over a catalog install, starts with none, before anything is copied from the row.
                if current.get("source_id") != source_id or (current.get("origin") or origin) != origin:
                    current = {**current, "config": {}}
            _refuse_a_release_that_breaks_dependents(state, validated.provider_id, validated.version, validated.raw)
            installation = _staged_installation(
                validated,
                current,
                bundle_path,
                staged_python_path,
                source_trusted,
                origin,
                source_id,
                catalog_list,
            )
            plugin = parse_plugin_manifest(validated.raw) if validated.raw.get("schema_version") == 2 else None
            if plugin is not None:
                # Which installed plugin supplies each capability it needs, kept with the release.
                installation["staged_supplied_by"] = closure.supplier_map(state, plugin, validated.provider_id)
            installations[validated.provider_id] = installation
            # The environment passed the worker check, so it is now the one
            # recorded for this dependency set (a no-op when it already is).
            env_registry.register_environment(state, environment_fingerprint(validated), env_path)
            # The plugin's secret paths join the persisted set now, so a
            # credential configured through the settings card is encrypted
            # at rest from the first save and stays covered after removal.
            _persist_plugin_secret_paths(state, installation)
            return dict(installation)

        try:
            with env_registry.recording_environment(env_path):
                installation = mutate_state(record_staged_install)
        except env_registry.EnvironmentGone as error:
            raise ProviderHubInstallError(str(error)) from error
        except ProviderHubInstallError:
            # Refused in the write: another plugin's need would no longer be met. Nothing was recorded.
            _discard_stopped_install(staged, existing)
            raise
        if is_fresh:
            try:
                claim_kept_data(validated.provider_id, source_id, validated.kind, origin)
            except ProviderHubInstallError:
                # Refused, so nothing of this install may be left to activate at the next restart.
                _undo_staging(validated.provider_id, before_staging.get("row"))
                _discard_stopped_install(staged, existing)
                raise
            # The saved settings may have come back with the kept data, so the row is read again before the
            # required fields are judged.
            installation = dict((load_state().get("installations") or {}).get(validated.provider_id) or installation)
        missing_required = missing_required_fields(installation) if is_fresh else []
        if is_fresh and not get_kind(validated.kind).on_installed(
                validated.provider_id, missing_required=missing_required):
            # First install: opt the provider into Bazarr's enabled_providers so
            # the new plugin is search-eligible without a separate UI toggle.
            # When that cannot be saved the install is staged but disabled,
            # and saying so beats reporting a provider that will not search.
            raise ProviderHubSettingsError(
                f"it was staged but could not be enabled: {ENABLED_PROVIDERS_NOT_SAVED}. "
                "Enable it once the configuration can be written"
            )
        if is_update:
            message = (
                f"Staged update {existing_version} -> {validated.version} "
                f"(restart Bazarr+ to activate)"
            )
        else:
            message = f"Staged install of v{validated.version} (restart Bazarr+ to activate)"
            if (validated.kind == SUBTITLE_PROVIDER and missing_required
                    and not get_kind(validated.kind).is_enabled(installation)):
                message += (
                    ". It is not enabled yet because it still needs its required settings: "
                    "add it under Enabled search providers and fill them in"
                )
        job.update(message=message)
        return _redact_installation(installation)


def _undo_staging(provider_id: str, previous: Any) -> None:
    """Put the installation row back as it was before an install staged over it, or take the new row out when
    there was none."""
    def restore(state: dict[str, Any]) -> None:
        installations = state.setdefault("installations", {})
        if isinstance(previous, dict):
            installations[provider_id] = previous
        else:
            installations.pop(provider_id, None)

    mutate_state(restore)


def _discard_stopped_install(paths, existing) -> None:
    """Remove the bundle and venv a stopped install staged, unless they are in use.

    The bundle lives at a path keyed by the version, so reinstalling the version
    that is already active or staged lands on that installation's own files,
    which stay. A venv can be shared, so any venv an installation references, the
    environment record names, or another install is using right now stays too.
    """
    existing = existing if isinstance(existing, dict) else {}
    in_use = {existing.get(key) for key in ("active_path", "staged_path", "python_path", "staged_python_path")}
    # The files of the previous release, kept to go back to, are as much in use as the active ones.
    for release in getattr(load_state(), "retained_releases", ()):
        in_use.update((release.get("bundle_path"), release.get("python_path")))
    in_use.discard(None)
    # This install's own hold ends here, so only other installs' holds count.
    release_pins()
    hub = provider_hub_dir()
    with _ENV_LOCK:
        for path in paths:
            # A bundle is recorded by its directory, a venv by its interpreter.
            if {str(path), str(python_executable(path))} & in_use:
                continue
            # Both removals stay inside the hub and never cross a symlink; a
            # venv is also kept while anything references, records or pins it.
            if env_registry.discard_if_unused(hub, path):
                continue
            if env_registry.is_environment_path(hub, path):
                continue
            env_registry.remove_inside_hub(hub, path)


def stage_install(
    manifest: dict[str, Any], install_timeout: float | None = None, checkpoint=None,
    source_id: str | None = None, catalog_list: str | None = None,
) -> dict[str, Any]:
    """Install ``manifest``, bound to the catalog entry Bazarr+ holds for it.

    ``source_id`` names the source whose entry it is; without it, the entry whose
    manifest equals this one binds it. A manifest no entry holds installs
    untrusted with no source, so it gets no updates. ``catalog_list`` is the list
    the caller already resolved the entry from; without it the list is found by
    matching the manifest against the stored entries.
    """
    state = load_state()
    if isinstance(manifest, dict):
        source_id, source_trusted = _catalog_binding(manifest, state, source_id)
        if catalog_list not in (CATALOG_LIST_PROVIDERS, CATALOG_LIST_PLUGINS):
            catalog_list = _manifest_catalog_list(manifest, state, source_id)
    else:
        source_id, source_trusted = None, False
        catalog_list = CATALOG_LIST_PROVIDERS
    # The entry's own kind validates it: a translator is not a subtitle provider.
    kind = _kind_for_manifest(manifest)
    validated = kind.validate_manifest(manifest, source_trusted)
    refusal = (_install_refusal(state, source_id, validated.provider_id, kind.name, validated.version,
                                validated.raw.get("name"))
               or _host_refusal({"manifest": manifest}, current_host()))
    if refusal:
        raise ProviderHubInstallError(refusal)
    _refuse_a_release_that_breaks_dependents(state, validated.provider_id, validated.version, validated.raw)
    origin = "catalog"
    manifest_source = validated.raw.get("source") if isinstance(validated.raw, dict) else None
    catalog_url = manifest_source.get("catalog_url") if isinstance(manifest_source, dict) else None
    mirror_channel = _bundle_mirror_channel(state, source_id)
    token = _BUNDLE_MIRROR_CHANNEL.set(mirror_channel)
    try:
        if mirror_channel is not None:
            transport.restore_state(state.get("transport"))
        return _stage_validated(
            validated,
            source_trusted,
            origin,
            source_id,
            _fetch_bundle,
            catalog_url=catalog_url,
            install_timeout=install_timeout,
            checkpoint=checkpoint,
            catalog_list=catalog_list,
        )
    finally:
        _BUNDLE_MIRROR_CHANNEL.reset(token)
        if mirror_channel is not None:
            _persist_transport()


def _find_catalog_entry(
    state: dict[str, Any], source_id: Any, provider_id: Any, version: Any, *,
    entry_key: Any = None, catalog_list: Any = None, manifest: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    """The stored key and entry a request names, or None when the source lists none.

    One source can list the same id and version in both the providers and the
    plugins list, with different manifests, so the triple alone is not an identity.
    A stored key names one entry exactly. Otherwise a manifest picks the entry it
    equals, and failing that the list the caller prefers wins (providers when none
    is given), so a request never lands on a copy it did not ask for.
    """
    wanted = _trust_manifest(manifest) if isinstance(manifest, dict) else None
    preferred = catalog_list or CATALOG_LIST_PROVIDERS
    best: tuple[int, str, dict[str, Any]] | None = None
    for key, entry in (state.get("catalog_entries") or {}).items():
        if not (isinstance(entry, dict) and entry.get("source") == source_id
                and entry.get("provider_id") == provider_id and entry.get("version") == version
                and isinstance(entry.get("manifest"), dict)):
            continue
        if entry_key is not None and key != entry_key:
            continue
        if wanted is not None and _trust_manifest(entry["manifest"]) != wanted:
            continue
        # Entries stored before the plugins list existed carry no catalog_list and came from providers.
        rank = 0 if entry.get("catalog_list", CATALOG_LIST_PROVIDERS) == preferred else 1
        if best is None or rank < best[0]:
            best = (rank, key, entry)
    return (best[1], best[2]) if best else None


def _held_catalog_entry(
    state: dict[str, Any], source_id: Any, provider_id: Any, version: Any, entry_key: Any = None,
) -> dict[str, Any] | None:
    found = _find_catalog_entry(state, source_id, provider_id, version, entry_key=entry_key)
    return found[1] if found else None


def _requested_entry(
    state: dict[str, Any], source_id: Any, provider_id: Any, version: Any, manifest: Any = None,
    *, entry_key: Any = None, catalog_list: Any = None,
) -> tuple[str, dict[str, Any]]:
    """The key and entry an install request names, or ProviderHubInstallError."""
    if manifest is not None:
        if not isinstance(manifest, dict):
            raise ProviderHubInstallError("manifest must be an object")
        source_id, _trusted = _catalog_binding(manifest, state)
        if source_id is None:
            raise ProviderHubInstallError(
                "the manifest matches no catalog entry; install it by source, provider_id and version"
            )
        provider_id, version = manifest_provider_id(manifest), manifest.get("version")
    elif not all(isinstance(value, str) and value for value in (source_id, provider_id, version)):
        raise ProviderHubInstallError("source, provider_id and version are required")
    found = _find_catalog_entry(state, source_id, provider_id, version,
                                entry_key=entry_key, catalog_list=catalog_list, manifest=manifest)
    if found is None:
        raise ProviderHubInstallError(
            f"{source_id} does not list {provider_id} {version}; refresh the catalog and try again"
        )
    key, entry = found
    refusal = (_install_refusal(state, source_id, provider_id, catalog_kind_of(entry), version, entry.get("name"))
               or _host_refusal(entry, current_host())
               or _closure_refusal(state, entry))
    if refusal:
        raise ProviderHubInstallError(refusal)
    return key, entry


def resolve_catalog_install(
    source_id: Any = None, provider_id: Any = None, version: Any = None, manifest: Any = None,
    catalog_list: Any = None,
) -> dict[str, Any]:
    """The catalog entry an install request names, as ``{source, provider_id,
    version, name, entry_key, kind}``, or ProviderHubInstallError with the reason it is refused.

    A request names an entry by source, provider id and version. A raw manifest
    is taken only when it equals an entry Bazarr+ holds, and then names that one.
    ``catalog_list`` says which list to prefer when the source lists the same id
    and version in both. The ``entry_key`` returned pins the one entry, so the
    install that follows reads the entry that was checked here.
    """
    key, entry = _requested_entry(
        load_state(), source_id, provider_id, version, manifest, catalog_list=catalog_list)
    return {
        "source": entry["source"],
        "provider_id": entry["provider_id"],
        "version": entry["version"],
        "name": entry.get("name") or entry["provider_id"],
        "entry_key": key,
        "kind": catalog_kind_of(entry),
    }


def _source_description(state: dict[str, Any]):
    """How a consent summary names the source of a plugin: from Bazarr+'s own record, never the manifest."""
    sources = state.get("catalog_sources") or {}

    def describe(source_id: Any) -> dict[str, Any]:
        source = sources.get(source_id) if isinstance(sources, dict) else None
        source = source if isinstance(source, dict) else {}
        official = source_id == OFFICIAL_CATALOG_SOURCE_ID
        return {
            "id": source_id,
            "name": source.get("name") or source_id,
            "official": official,
            "trusted": bool(source.get("trusted")),
            "development": dev_source.is_dev_source(source_id, source),
            "signature": source.get("catalog_signature") if official else None,
        }

    return describe


CONSENT_MISSING_REASON = "This kind of plugin needs your consent before it is installed."
CONSENT_CHANGED_REASON = (
    "What this install would add has changed since it was shown to you. Review it again before you agree."
)


class _StagedMembers(list):
    """The ids of the plugins a call staged, and the kind of each one that was switched on before it started.

    Removing a plugin switches it off, so a discard puts back the ones the person already had on."""

    def __init__(self, ids=(), was_enabled=None):
        super().__init__(ids)
        self.was_enabled: dict[str, str] = dict(was_enabled or {})

    @staticmethod
    def snapshot_enabled(resolved) -> dict[str, str]:
        found: dict[str, str] = {}
        for member in resolved.members:
            if member.reason == "root":
                continue
            try:
                if get_kind(member.plugin.kind).is_enabled({"provider_id": member.plugin_id}):
                    found[member.plugin_id] = member.plugin.kind
            except Exception:
                continue
        return found


def _stage_closure_members(resolved, source_id: str, checkpoint=None) -> list[str]:
    """Stage the plugins a closure adds, dependencies first, before the plugin that needs them.

    Returns the ids of the rows this call wrote, which is what to discard if the plugin they were staged
    for then fails. A row counts as soon as ``stage_install`` wrote it, including a failed one, so a member
    that raises after recording its row is not left behind. When a member fails, the rows written before it
    in this call are discarded and the failure goes on.
    """
    def installed_ids() -> set[str]:
        # A row that only records an earlier failure is not an installation: a plugin this call installs over
        # one is new, and is discarded with the rest when the plugin being installed fails.
        return {str(pid) for pid, row in (load_state().get("installations") or {}).items()
                if isinstance(row, dict) and (row.get("active_version") or row.get("staged_version"))}

    before = installed_ids()
    # Only members this call has started to stage can have a row it wrote: a later member it never reached keeps
    # whatever row it had, a record of an earlier failure included.
    attempted: set[str] = set()
    was_enabled = _StagedMembers.snapshot_enabled(resolved)

    def written() -> "_StagedMembers":
        rows = load_state().get("installations") or {}
        return _StagedMembers(
            [member.plugin_id for member in resolved.members
             if member.reason != "root" and member.plugin_id in attempted and member.plugin_id not in before
             and member.plugin_id in rows],
            was_enabled)

    current = None
    try:
        for member in resolved.members:
            if member.reason == "root":
                continue
            current = member
            attempted.add(member.plugin_id)
            stage_install(
                member.entry["manifest"], checkpoint=checkpoint, source_id=source_id,
                catalog_list=member.entry.get("catalog_list", CATALOG_LIST_PROVIDERS),
            )
    except Exception as error:
        _discard_staged_members(written(), resolved.members[-1].plugin_id if resolved.members else None)
        if isinstance(error, ProviderHubSettingsError) and current is not None:
            # The member was staged and could not be enabled. It is discarded with the rest, and the
            # failure names it, because the plugin being installed was never staged.
            raise ProviderHubInstallError(
                f"{current.plugin.name}, which this install needs, could not be enabled, so nothing was "
                f"installed: {ENABLED_PROVIDERS_NOT_SAVED}") from error
        raise
    return written()


def _discard_staged_members(plugin_ids: list[str], root_id: str | None = None) -> None:
    """Remove the plugins a call staged for a plugin that then failed to stage, last staged first.

    A plugin leaves only when nothing outside that failed closure depends on it, active or staged: the
    members themselves and the failed root do not count (the root's active release, when an update is the
    one that failed, needed the capability before the member existed), but a plugin another install adopted
    them for does, and keeps them. A removal that is refused or fails is logged.
    """
    own = set(plugin_ids) | ({root_id} if root_id else set())
    was_enabled = getattr(plugin_ids, "was_enabled", {})
    for plugin_id in reversed(plugin_ids):
        try:
            # A rollback: whatever an earlier removal kept of this plugin stays exactly as it was, whether or not this
            # install got as far as taking it over.
            removed = remove_installation(plugin_id, ignoring_dependents=frozenset(own), rollback=True)
            if removed and plugin_id in was_enabled:
                # The person had it on before this install, and removing it switched it off.
                get_kind(was_enabled[plugin_id]).set_enabled({"provider_id": plugin_id}, True)
        except Exception:
            # It stays, so it is no longer part of the failed closure: whatever it depends on is still
            # needed, and the next member is judged with it counted as a dependent.
            own.discard(plugin_id)
            logging.getLogger(__name__).exception(
                "Could not discard %s, which was staged for a plugin that failed to install", plugin_id)


def _install_closure(state: dict[str, Any], entry: dict[str, Any]):
    """The closure an install of ``entry`` acts on, or ProviderHubInstallError when it has no plugin to install."""
    resolved = _closure_of(state, entry)
    if not resolved.members:
        raise ProviderHubInstallError(resolved.refusal or "The plugins this one needs could not be resolved.")
    return resolved


def _closure_needs_consent(resolved) -> bool:
    """Whether installing this closure asks for the person's consent: any plugin in it that is not a subtitle provider."""
    return any(member.plugin.kind != SUBTITLE_PROVIDER for member in resolved.members)


def _provider_replacement_delta(state: dict[str, Any], entry: dict[str, Any], resolved: closure.Closure):
    """An explicit catalog install can also replace a provider release that is already installed."""
    if catalog_kind_of(entry) != SUBTITLE_PROVIDER:
        return None
    provider = (state.get("installations") or {}).get(entry["provider_id"])
    if (not isinstance(provider, dict) or kind_of(provider) != SUBTITLE_PROVIDER
            or provider.get("state") == "removed"
            or not (provider.get("active_version") or provider.get("staged_version"))):
        return None
    delta = _update_delta(state, provider, entry, resolved)
    return delta if delta and delta["needs_consent"] else None


def _catalog_install_summary(state: dict[str, Any], entry: dict[str, Any], resolved: closure.Closure,
                             *, marks: bool = False) -> dict[str, Any]:
    summary = consent.install_summary(
        resolved, _source_description(state), consent.sandbox_status() if marks else None)
    delta = _provider_replacement_delta(state, entry, resolved)
    if delta:
        summary["needs_consent"] = True
        summary["digest"] = consent.digest_of({"install": summary["digest"], "update": delta["digest"]})
    return summary


def install_consent(
    source_id: Any, provider_id: Any, version: Any, entry_key: Any = None, catalog_list: Any = None,
) -> dict[str, Any]:
    """The consent summary of installing the entry a request names, or ProviderHubInstallError with the
    reason it cannot be installed. It describes the plugin and its whole dependency closure and carries
    the digest an install request sends back."""
    state = load_state()
    _key, entry = _requested_entry(
        state, source_id, provider_id, version, entry_key=entry_key, catalog_list=catalog_list)
    return _catalog_install_summary(state, entry, _install_closure(state, entry), marks=True)


def install_needs_consent(
    source_id: Any, provider_id: Any, version: Any, entry_key: Any = None, catalog_list: Any = None,
) -> bool:
    """Whether installing the entry a request names, with its closure, asks for consent."""
    state = load_state()
    _key, entry = _requested_entry(
        state, source_id, provider_id, version, entry_key=entry_key, catalog_list=catalog_list)
    resolved = (closure.Closure() if catalog_kind_of(entry) == SUBTITLE_PROVIDER
                and not closure.has_needs(entry.get("manifest")) else _install_closure(state, entry))
    return _closure_needs_consent(resolved) or _provider_replacement_delta(state, entry, resolved) is not None


def check_install_consent(
    digest: Any, source_id: Any, provider_id: Any, version: Any, entry_key: Any = None, catalog_list: Any = None,
) -> dict[str, Any]:
    """Check the summary digest before installing a different kind or expanding a provider's access.

    A digest sent for any other install must match too. Returns the summary it checked.
    """
    summary = install_consent(source_id, provider_id, version, entry_key=entry_key, catalog_list=catalog_list)
    if digest is None or digest == "":
        if summary.get("needs_consent") or any(member["kind"] != SUBTITLE_PROVIDER for member in summary["members"]):
            raise ProviderHubConsentError(CONSENT_MISSING_REASON)
        return summary
    if digest != summary["digest"]:
        raise ProviderHubConsentError(CONSENT_CHANGED_REASON, changed=True)
    return summary


def install_catalog_entry(
    source_id: str, provider_id: str, version: str, checkpoint=None, entry_key: str | None = None,
    consent_digest: str | None = None,
) -> dict[str, Any]:
    """Install the entry ``source_id`` lists for ``provider_id`` at ``version``.

    ``entry_key`` pins the stored entry a request resolved to, which matters when
    the source lists the same id and version in both catalog lists. An install of another kind or a provider
    replacement that adds access requires ``consent_digest`` to match the current summary, however it was queued.
    """
    state = load_state()
    _key, entry = _requested_entry(state, source_id, provider_id, version, entry_key=entry_key)
    resolved = None
    if closure.has_needs(entry.get("manifest")) or catalog_kind_of(entry) != SUBTITLE_PROVIDER:
        resolved = _install_closure(state, entry)
    if consent_digest is not None:
        # The set may have changed since the request was accepted, so the job checks it again.
        shown = _catalog_install_summary(state, entry, resolved or _install_closure(state, entry))
        if shown["digest"] != consent_digest:
            raise ProviderHubConsentError(CONSENT_CHANGED_REASON, changed=True)
    else:
        candidate = resolved or closure.Closure()
        if _closure_needs_consent(candidate) or _provider_replacement_delta(state, entry, candidate):
            raise ProviderHubConsentError(CONSENT_MISSING_REASON)
    # The plugins it extends or needs are installed first, from the same source, by the same rules.
    members = _stage_closure_members(resolved, source_id, checkpoint) if resolved is not None else []
    try:
        # An entry stored before the plugins list existed carries no list and came from providers.
        return stage_install(
            entry["manifest"], checkpoint=checkpoint, source_id=source_id,
            catalog_list=entry.get("catalog_list", CATALOG_LIST_PROVIDERS),
        )
    except ProviderHubSettingsError:
        # The plugin is staged and only could not be enabled: it keeps what it needs.
        raise
    except Exception:
        _discard_staged_members(members, provider_id)
        raise


def stage_install_local(package: bytes | str | Path, checkpoint=None) -> dict[str, Any]:
    """Install a Provider Hub provider from an uploaded .zip package.

    ``package`` is the path of the spooled upload, or the package's bytes.

    A local package has no catalog vouching for it, so it is always recorded as
    origin="local" and forced untrusted: it can never shadow a built-in provider.
    Its files are still hash-verified against the manifest and smoke-tested before
    activation, exactly like a catalog install.
    """
    if isinstance(package, (bytes, bytearray)):
        if not package:
            raise ProviderHubInstallError("uploaded package is empty")
        package = bytes(package)
    elif not isinstance(package, (str, Path)):
        raise ProviderHubInstallError("uploaded package is empty")

    # A local upload can be the very first Provider Hub write, before any catalog
    # refresh/job has created the directory, so make sure it exists.
    hub_dir = provider_hub_dir()
    hub_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="phub-local-", dir=str(hub_dir)))
    try:
        _safe_extract_zip(package, work_dir)
        manifest, bundle_root = _find_local_manifest(work_dir)
        # A local package has no catalog behind it, and only the official catalog installs the other kinds.
        if isinstance(manifest, dict) and manifest.get("schema_version") == 2 and (
                manifest.get("kind") or SUBTITLE_PROVIDER) != SUBTITLE_PROVIDER:
            raise ProviderHubInstallError(LOCAL_PACKAGE_KIND_REASON)
        # Untrusted (full built-in set, no replacements): a local package can
        # never shadow a built-in provider.
        validated = validate_manifest(
            manifest,
            built_in_provider_ids=_built_in_provider_ids(),
        )
        refusal = _host_refusal({"manifest": manifest}, current_host())
        if refusal:
            raise ProviderHubInstallError(refusal)
        return _stage_validated(
            validated,
            source_trusted=False,
            origin="local",
            source_id=None,
            bundle_stager=lambda candidate, deadline=None: _stage_local_bundle(candidate, bundle_root),
            checkpoint=checkpoint,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


BLOCKED_BY_REVOCATION = "revoked"
BLOCKED_BY_NEEDS = "needs"


def _held_for_revocation(row: dict[str, Any]) -> bool:
    """Whether the boot has held this row back because its release is revoked. Judged on the hold's own cause,
    never on the text it shows."""
    return bool(row.get("blocked")) and row.get("blocked_cause") == BLOCKED_BY_REVOCATION


def _detaches_live(row: Any) -> bool:
    """Whether the kind of this installation can stop a running release while Bazarr+ runs."""
    try:
        return get_kind(kind_of(row)).detach_policy == DETACH_LIVE
    except UnknownKindError:
        return False


def _revoked_reasons(
    installations: dict[str, Any], revocations: tuple[revocation.Revocation, ...], boot: bool,
    stopped: bool = False,
) -> dict[str, str]:
    """For each installation whose active release the revocation list names, what it shows.

    At boot the release is held back and does not start. After a refresh a release of a kind that stops while
    Bazarr+ runs is stopped at once, and says so; one of any other kind is still running, so the person has to
    restart, unless the boot has already held it back. A release with an update staged over it says that
    instead. A release that was stopped live keeps saying so (``revoked_stopped``) until a boot holds it back
    instead, and one whose stop failed (``detach_failed``) says it still runs. ``stopped`` is the live hold itself
    judging the release it is about to stop. A plugin being removed is left out: its removal is pending and nothing
    else is judged about it.
    """
    reasons = {}
    for plugin_id, row in installations.items():
        if not isinstance(row, dict) or not row.get("active_version") or row.get("state") == "removed":
            continue
        found = revocation.lookup(revocations, plugin_id, row.get("active_version"))
        if found is None:
            continue
        if row.get("staged_version"):
            state = revocation.STAGED
        elif boot:
            state = revocation.HELD
        elif row.get("detach_failed"):
            # It could not be stopped live: it still runs, whatever else the row says.
            state = revocation.RUNNING
        elif _detaches_live(row) and (stopped or row.get("revoked_stopped") or not row.get("blocked")):
            state = revocation.STOPPED
        elif row.get("blocked"):
            # A row the boot held back for anything is not running, so it gets the held text.
            state = revocation.HELD
        else:
            state = revocation.RUNNING
        reasons[str(plugin_id)] = _revocation_text(
            revocation.held_back, found, row.get("name") or plugin_id, row.get("active_version"), state)
    return reasons


def _mark_revoked_installations(installations: dict[str, Any], revocations: tuple[revocation.Revocation, ...]) -> bool:
    """The refresh's part of the revocation: mark the revoked, release the lifted, hold nothing back.

    A revoked release is still running when this runs, so it is neither held back nor taken from the registry and
    the settings overlay here: that is the boot's to do, or, for a kind that stops while Bazarr+ runs,
    ``enforce_revocations_live``'s right after the refresh commits. It is marked ``revoked`` with its reason as
    its error, and for a kind that cannot be stopped live the restart cue the Hub already reads is raised so the
    person restarts to stop it. A row the boot already held back is left as the boot wrote it and gets no cue.
    A release the list no longer names loses the marks; if the boot had held it back it is released and the
    restart cue is raised, because it starts again only after a restart, and a cue this raised is dropped when
    nothing is left to restart for.
    A plugin being removed is not touched. Returns whether any row changed.
    """
    changed = False
    reasons = _revoked_reasons(installations, revocations, boot=False)
    for plugin_id, row in installations.items():
        if not isinstance(row, dict) or row.get("state") == "removed":
            continue
        if str(plugin_id) in reasons:
            reason = reasons[str(plugin_id)]
            if not row.get("revoked"):
                row["revoked"] = True
                changed = True
                logging.getLogger(__name__).warning("Provider Hub: %s", reason)
            if row.get("revoked_reason") != reason or not row.get("last_error"):
                row["revoked_reason"] = reason
                row["last_error"] = reason
                changed = True
            if (row.get("state") == "active" and not row.get("pending_restart") and not row.get("staged_version")
                    and not row.get("blocked") and not _detaches_live(row)):
                # A kind that stops a release while Bazarr+ runs needs no restart for this: the release is
                # stopped right after the refresh commits (``enforce_revocations_live``), and the cue is raised
                # only if that fails.
                row.update(pending_restart=True, revoked_restart=True)
                changed = True
        elif row.get("revoked"):
            previous = row.pop("revoked_reason", None)
            row.pop("revoked", None)
            if previous and row.get("last_error") == previous:
                row["last_error"] = None
            if _held_for_revocation(row):
                # It was held back at boot for this and does not run: it starts again after a restart.
                for field in ("blocked", "blocked_reason", "blocked_cause"):
                    row.pop(field, None)
                if row.get("state") == "active":
                    row.update(pending_restart=True, revoked_restart=True)
            elif row.pop("revoked_restart", None):
                # Limitation: the marker only says a refresh raised the cue. A lift and a new revocation within
                # one run, with no restart between, drop and raise it again, and a restart another change
                # asked for on this row in the meantime is dropped with it.
                row["pending_restart"] = False
            changed = True
    return changed


def _apply_unmet_needs(installations: dict[str, Any], revocations: tuple[revocation.Revocation, ...] = (),
                       *, live: bool = False, again: frozenset[str] = frozenset()) -> bool:
    """Hold back each plugin that must not run, and release the others. This is the boot check.

    With ``live`` it is the same check made while Bazarr+ runs, for the kinds that stop a release live (see
    ``enforce_revocations_live``): it only adds holds, and only to rows of those kinds. It never releases one,
    since a held release comes back only after a restart, and it leaves every other kind as the refresh marked it.
    A row that is already held back (by the boot, or by an earlier live stop) is left as it is, with the words it
    has, unless it is one whose stop failed and is tried again (``again``).

    A plugin is held back when the official catalog revoked its active release, or when that release relies on
    a plugin that is not running. A held-back plugin carries the reason. A revoked one always shows it as its
    error, switched on or off, because it cannot be switched on again; one held back for what it relies on shows
    it only while it is switched on, and loses it once it is off. An error that is not the reason is never
    touched. A revoked plugin supplies nothing, so what relies on it is held back too. The restart cue a refresh
    raised for a revocation is dropped, because this boot is that restart. Returns whether any row changed.
    """
    changed = False
    if live:
        # The text of a release that is being stopped, not of one that will not start.
        revoked = {plugin_id: reason for plugin_id, reason in _revoked_reasons(
            installations, revocations, boot=False, stopped=True).items()
            if _detaches_live(installations.get(plugin_id))
            and (plugin_id in again or not installations[plugin_id].get("blocked"))}
    else:
        revoked = _revoked_reasons(installations, revocations, boot=True)
    for plugin_id, row in installations.items():
        if not isinstance(row, dict):
            continue
        if live and str(plugin_id) not in revoked and not (str(plugin_id) in again and row.get("blocked")):
            # (A row whose stop is tried again is judged only while it is still held back: one whose revocation was
            # lifted meanwhile is unblocked and keeps the restart cue the lift raised.)
            continue
        if not live and row.pop("detach_pending", None):
            # Nothing runs at boot, so no stop is waiting.
            changed = True
        if str(plugin_id) in revoked:
            if not row.get("revoked"):
                row["revoked"] = True
                changed = True
                logging.getLogger(__name__).warning("Provider Hub: %s", revoked[str(plugin_id)])
            if row.get("revoked_reason") != revoked[str(plugin_id)]:
                row["revoked_reason"] = revoked[str(plugin_id)]
                changed = True
            if live:
                row["revoked_stopped"] = True
            elif row.pop("revoked_stopped", None):
                changed = True
            if row.pop("detach_failed", None):
                changed = True
        elif row.get("revoked"):
            row.pop("revoked", None)
            row.pop("revoked_reason", None)
            changed = True
        if row.pop("revoked_restart", None):
            # The restart a refresh asked for is this one. (Limitation: see _mark_revoked_installations; a
            # restart another change asked for on this row is cleared with it.)
            if row.get("state") == "active":
                row["pending_restart"] = False
            changed = True
    unmet = closure.unmet_needs(installations)
    if live:
        # Only what relies on a revoked plugin is held here: judged against the same rows with the revocation
        # taken off, so a plugin that already lacked what it needs (a supplier flagged for repair) is the boot's
        # to hold, and is not stopped by a refresh.
        lifted = {plugin_id: ({key: value for key, value in row.items() if key != "revoked"}
                              if plugin_id in revoked and isinstance(row, dict) else row)
                  for plugin_id, row in installations.items()}
        already = closure.unmet_needs(lifted)
        unmet = {plugin_id: needs for plugin_id, needs in unmet.items() if plugin_id not in already}
    for plugin_id, row in installations.items():
        if not isinstance(row, dict):
            continue
        if plugin_id in revoked:
            reason, always, cause = revoked[plugin_id], True, BLOCKED_BY_REVOCATION
        elif live and (not _detaches_live(row) or row.get("blocked") or not row.get("active_version")
                       or row.get("state") == "removed"):
            continue
        elif plugin_id in unmet:
            reason = (f"It needs {', '.join(unmet[plugin_id])}, which is not running, so it was not started. "
                      "Once that is running again, restart Bazarr+.")
            always, cause = False, BLOCKED_BY_NEEDS
        else:
            reason, always, cause = None, False, None
        if reason is not None:
            on = always or closure.is_on(row)
            previous = row.get("blocked_reason")
            if row.get("blocked_cause") != cause:
                row["blocked_cause"] = cause
                changed = True
            if not row.get("blocked") or previous != reason:
                row.update(blocked=True, blocked_reason=reason)
                if on:
                    row["last_error"] = reason
                elif row.get("last_error") == previous:
                    row["last_error"] = None
                changed = True
            elif on and not row.get("last_error"):
                # Something cleared the reason (an activation, a repair): a held-back plugin always says why.
                row["last_error"] = reason
                changed = True
            elif not on and row.get("last_error") == reason:
                row["last_error"] = None
                changed = True
        elif row.get("blocked") and not live:
            if row.get("last_error") == row.get("blocked_reason"):
                row["last_error"] = None
            row.pop("blocked", None)
            row.pop("blocked_reason", None)
            row.pop("blocked_cause", None)
            changed = True
    return changed


def enforce_revocations_live() -> list[str]:
    """After a refresh has committed: stop each running release the list revokes, without a restart.

    For the kinds that stop a release while Bazarr+ runs this is the boot's hold made now: the row is held back
    and says so, so everything that reads what runs (the registry rebuild, the settings overlay, the translator
    binding, the host's trust record) stops seeing it, and the kind detaches its consumers, which cuts the calls
    the release still has after ``detach.REVOKE_GRACE_SECONDS``. What relies on a revoked plugin is held back
    and stopped with it. The hold marks the row (``detach_pending``) in the same write, and the mark is cleared
    once the stop has been started, so a stop that did not get that far (a failure, a process that ended) is tried
    again by the next refresh. A release whose stop failed is tried again the same way. The restart cue is raised
    only for a release that could not be stopped. Returns the ids that were stopped.
    """
    def apply(state: dict[str, Any]) -> list[tuple[str, str, Any]]:
        installations = state.setdefault("installations", {})
        before = {str(pid) for pid, row in installations.items() if isinstance(row, dict) and row.get("blocked")}
        # A stop that failed, or never got started, is tried again: the row is judged as one still to be stopped.
        retry = {str(pid) for pid, row in installations.items() if isinstance(row, dict) and (
            row.pop("detach_failed", None) or row.get("detach_pending"))}
        _apply_unmet_needs(installations, _revocations(state), live=True, again=frozenset(retry))
        found = [(str(pid), kind_of(row), (row.get("manifest") or {}).get("bundle_sha256"))
                 for pid, row in installations.items()
                 if isinstance(row, dict) and row.get("blocked") and (str(pid) not in before or str(pid) in retry)
                 and row.get("active_version") and row.get("state") != "removed"]
        for pid, _kind, _key in found:
            installations[pid]["detach_pending"] = True
        return found

    newly = mutate_state(apply) or []
    stopped: list[str] = []
    failed: list[str] = []
    for plugin_id, kind_name, release_key in newly:
        try:
            workers = detach.snapshot_workers(plugin_id, release_key)
            drains = detach.detach(plugin_id, kind_name, withdraw=True, grace=detach.REVOKE_GRACE_SECONDS)
            detach.when_drained(drains, lambda workers=workers: detach.stop_stragglers(workers, cut=True),
                                name=plugin_id)
            stopped.append(plugin_id)
        except Exception:
            logging.getLogger(__name__).exception("Provider Hub: could not stop %s live; it stops at the next restart",
                                                  plugin_id)
            failed.append(plugin_id)
    if stopped:
        _clear_detach_pending(stopped)
    if failed:
        _ask_restart_to_stop(failed)
    return stopped


def _clear_detach_pending(plugin_ids: list[str]) -> None:
    """The stop of these releases has been started: they no longer wait for one."""
    def apply(state: dict[str, Any]) -> None:
        for plugin_id in plugin_ids:
            row = (state.get("installations") or {}).get(plugin_id)
            if isinstance(row, dict):
                row.pop("detach_pending", None)

    try:
        mutate_state(apply)
    except Exception:
        # Left marked, the next refresh stops it again, which an instance that is already gone makes harmless.
        logging.getLogger(__name__).warning("Provider Hub: could not clear the stop mark of %s", ", ".join(plugin_ids),
                                            exc_info=True)


def ask_restart_for_unstopped() -> list[str]:
    """The live stop could not run at all (the write that holds the releases back failed): each running release the
    list revokes that is of a kind that stops live was worded as stopped and given no restart cue, so it is given the
    cue and the words of a release that still runs. Returns their ids."""
    state = load_state()
    revocations = _revocations(state)
    ids = [str(plugin_id) for plugin_id, row in (state.get("installations") or {}).items()
           if isinstance(row, dict) and row.get("state") == "active" and row.get("active_version")
           and (row.get("detach_pending") or not row.get("blocked")) and _detaches_live(row)
           and revocation.lookup(revocations, plugin_id, row.get("active_version")) is not None]
    if ids:
        _ask_restart_to_stop(ids)
    return ids


def _ask_restart_to_stop(plugin_ids: list[str]) -> None:
    """A release could not be stopped while Bazarr+ runs: it says so, and asks for the restart that holds it back.
    The row is marked (``detach_failed``) so the next refresh tries to stop it again and does not clear the cue."""
    def apply(state: dict[str, Any]) -> None:
        revocations = _revocations(state)
        for plugin_id in plugin_ids:
            row = (state.get("installations") or {}).get(plugin_id)
            if not isinstance(row, dict) or row.get("state") != "active":
                continue
            row["detach_failed"] = True
            row.pop("detach_pending", None)
            row.pop("revoked_stopped", None)
            found = revocation.lookup(revocations, plugin_id, row.get("active_version"))
            if found is not None and not row.get("staged_version"):
                running = _revocation_text(revocation.held_back, found, row.get("name") or plugin_id,
                                           row.get("active_version"), revocation.RUNNING)
                stopped_text = row.get("revoked_reason")
                row["revoked_reason"] = running
                for field in ("blocked_reason", "last_error"):
                    if row.get(field) == stopped_text:
                        row[field] = running
            if not row.get("pending_restart") and not row.get("staged_version"):
                row.update(pending_restart=True, revoked_restart=True)

    mutate_state(apply)


def enforce_activation_needs() -> bool:
    """Run the boot dependency check again, over what the environment reconciliation has since found.

    A supplier whose environment is missing or damaged does not run, and that is only known once the
    environments have been reconciled, which is after activation. Returns whether any row changed."""
    def apply(state: dict[str, Any]) -> bool:
        official = (state.get("catalog_sources") or {}).get(OFFICIAL_CATALOG_SOURCE_ID)
        read = catalog_feed.feed_channel(official.get("dev_ref")) if isinstance(official, dict) else None
        stale = _revocations_stale(state, (read,) if read else ())
        if stale and isinstance(official, dict):
            logging.getLogger(__name__).warning(
                "Provider Hub: the revocation list this host holds for %s is older than the catalog it last "
                "accepted, so it is kept and the catalog is read again at the next refresh", ", ".join(stale))
            official["signed_feed_retry"] = True
        return _apply_unmet_needs(state.setdefault("installations", {}), _revocations(state))

    return bool(mutate_state(apply))


def _enforce_kept_data_ownership() -> None:
    """Before anything activates: a staged plugin whose id has data kept for another owner does not start until
    that data is cleared. Staging refuses such an install, so this only catches what got past it. A plugin that
    cannot be cleared is marked failed and stays inactive."""
    state = load_state()
    kept = state.get("kept_data") or {}
    for provider_id, row in list((state.get("installations") or {}).items()):
        entry = kept.get(provider_id)
        # Only a first install: an update of a plugin that is running has no kept data to take over.
        if (not isinstance(row, dict) or not isinstance(entry, dict) or row.get("state") != "staged"
                or not row.get("pending_restart") or row.get("active_version")):
            continue
        origin = row.get("origin") or "catalog"
        if (entry.get("source_id") == row.get("source_id") and entry.get("kind") == kind_of(row)
                and (entry.get("origin") or origin) == origin):
            continue
        try:
            claim_kept_data(provider_id, row.get("source_id"), kind_of(row), origin)
        except Exception as error:
            logging.getLogger(__name__).warning("Not activating %s: %s", provider_id, error)
            reason = refusal_text(error)

            def refuse(state: dict[str, Any], provider_id=provider_id, reason=reason) -> None:
                current = (state.get("installations") or {}).get(provider_id)
                if isinstance(current, dict):
                    current.update(state="failed", pending_restart=False, last_error=reason)

            mutate_state(refuse)


ACTIVATED = "activated"
ACTIVATION_FAILED = "failed"
# A kind that attaches on restart is not activated while Bazarr+ runs: the release stays staged for the boot.
ACTIVATION_WAITING = "waiting"
# There is nothing staged, or what was staged changed while the release was being checked.
ACTIVATION_SKIPPED = "skipped"


@dataclass(frozen=True)
class Activation:
    """What ``activate_release`` did for one plugin."""

    plugin_id: str
    outcome: str
    version: str | None = None
    previous_version: str | None = None
    error: str | None = None
    # The release this one replaced may still be serving calls. It is draining, and whoever asked for the
    # activation retires it once its calls are done (``retire_release``).
    draining: bool = False
    # The thread that retires the replaced release once its calls are done, when the kind can stop a release live
    # (the caller needs nothing more). None otherwise: the replaced release stays draining, and the caller of a
    # kind that cannot stop one live retires it (``retire_release``), or the next start does.
    drained: "threading.Thread | None" = dataclass_field(default=None, compare=False)

    @property
    def activated(self) -> bool:
        return self.outcome == ACTIVATED


def _verified_staged_release(provider_id: str, installation: dict[str, Any], revocations):
    """The manifest of the staged release once it has been checked, or the reason it cannot activate (raised).

    The checks, in order: the release is not revoked, its kind accepts its manifest, the bundle and interpreter
    are the hub's own, the bundle matches the manifest, and the kind's smoke test starts it.
    """
    target_version = installation.get("staged_version")
    target_name = installation.get("name") or provider_id
    found = revocation.lookup(revocations, provider_id, target_version)
    if found is not None:
        # Revoked since it was staged: it does not become the active release.
        raise ProviderHubInstallError(_revocation_text(
            revocation.refusal, found, target_name, target_version, "installed"))
    trusted = bool(installation.get("trusted", False))
    # The kind validates its own manifests: a translator is not a subtitle provider.
    kind = get_kind(kind_of(installation))
    manifest = kind.validate_manifest(installation.get("manifest") or {}, trusted)
    if isinstance(installation.get("staged_manifest"), dict):
        manifest = kind.validate_manifest(installation.get("staged_manifest"), trusted)
    staged_path = installation.get("staged_path")
    staged_python_path = installation.get("staged_python_path")
    if not staged_path or not staged_python_path:
        raise ProviderHubInstallError("staged bundle or python path is missing")
    staged_interpreter = validated_interpreter(provider_hub_dir(), staged_python_path)
    if staged_interpreter is None:
        raise ProviderHubInstallError("staged python path is not one of the hub's environments")
    staged_bundle_path = validated_bundle_path(provider_hub_dir(), staged_path)
    if staged_bundle_path is None:
        raise ProviderHubInstallError("staged bundle path is not one of the hub's bundles")
    _remove_bundle_runtime_artifacts(staged_bundle_path)
    verify_bundle_tree(manifest, staged_bundle_path)
    kind.smoke_test(manifest, staged_bundle_path, staged_interpreter)
    return manifest


def _abandon_if_still_staged(provider_id: str, version: Any, message: str, *, expected=None) -> None:
    """Drop the staged release a failed activation looked at, unless another one has replaced it meanwhile."""
    def drop(state: dict[str, Any]) -> None:
        row = (state.get("installations") or {}).get(provider_id)
        if (isinstance(row, dict) and row.get("staged_version") == version and row.get("state") == "staged"
                and (expected is None or all(row.get(key) == value for key, value in expected.items()))):
            _abandon_staged_release(row, message)

    mutate_state(drop)


def activate_release(provider_id: str, *, at_boot: bool = False, _expected_staged=None) -> Activation:
    """The one way a staged release becomes the active one, at boot and while Bazarr+ runs.

    The staged release is checked (not revoked, accepted by its kind, bundle matches its manifest, starts), the
    kind's ``on_activated`` brings what it stores in line with the new manifest, and one write makes the
    release active: the release it replaces is retired, or draining when a running process may still be
    serving its calls, and the plugin's restart flag clears. A release that fails any step is dropped and the
    active one, if any, stays as it was, so a failed update never takes a working plugin away.

    The boot passes ``at_boot=True``: nothing is running, so every kind activates. Otherwise the kind's attach
    policy decides: a kind that attaches on restart answers ``ACTIVATION_WAITING`` and the release stays staged
    for the next boot; a kind that attaches live activates now and the replaced release drains. A rollback pins
    ``_expected_staged`` so an intervening install cannot make it activate or abandon a different release.
    """
    state = load_state()
    installation = (state.get("installations") or {}).get(provider_id)
    if (not isinstance(installation, dict) or installation.get("state") != "staged"
            or not installation.get("pending_restart")
            or (_expected_staged is not None
                and any(installation.get(key) != value for key, value in _expected_staged.items()))):
        return Activation(provider_id, ACTIVATION_SKIPPED)
    previous_version = installation.get("active_version")
    previous_manifest = installation.get("manifest") if previous_version else None
    target_version = installation.get("staged_version")
    target_name = installation.get("name") or provider_id
    rollback = installation.get(releases.STAGED_ROLLBACK) is not None
    expected = ({key: installation.get(key) for key in _ROLLBACK_STAGED_FIELDS}
                if rollback else {releases.STAGED_ROLLBACK: None, **(_expected_staged or {})})

    def check_rollback(document: dict[str, Any], row: dict[str, Any]) -> str | None:
        refusal = _staged_rollback_refusal(document, provider_id, row)
        if refusal and at_boot:
            refusal += "; the staged rollback was discarded"
            _abandon_staged_release(row, refusal)
        return refusal

    def still_staged(row: Any) -> bool:
        return (isinstance(row, dict) and row.get("state") == "staged"
                and all(row.get(key) == value for key, value in expected.items()))

    def discard_stale(document: dict[str, Any]) -> str | None:
        row = (document.get("installations") or {}).get(provider_id)
        return check_rollback(document, row) if still_staged(row) else None

    try:
        live = get_kind(kind_of(installation)).attach_policy == ATTACH_LIVE
    except UnknownKindError:
        live = False
    refusal = _staged_rollback_refusal(state, provider_id, installation)
    if not at_boot and not live and not refusal:
        return Activation(provider_id, ACTIVATION_WAITING, target_version, previous_version)

    with record_job(
        "activate",
        target_kind="provider",
        target_id=provider_id,
        target_name=target_name,
        from_version=previous_version,
        to_version=target_version,
    ) as job:
        def failed_rollback(refusal: str) -> Activation:
            job.data["state"] = "failed"
            job.update(error=refusal, message=refusal)
            outcome = ACTIVATION_FAILED if at_boot else ACTIVATION_SKIPPED
            return Activation(provider_id, outcome, target_version, previous_version, refusal)

        if refusal:
            if at_boot:
                refusal = mutate_state(discard_stale)
            if refusal:
                return failed_rollback(refusal)
            return Activation(provider_id, ACTIVATION_SKIPPED, target_version, previous_version)
        try:
            manifest = _verified_staged_release(provider_id, installation, _revocations(state))
        except Exception as error:
            message = refusal_text(error)
            _abandon_if_still_staged(provider_id, target_version, message, expected=expected)
            job.data["state"] = "failed"
            job.update(
                error=str(error) or error.__class__.__name__,
                message=f"Activation failed: {error}",
            )
            return Activation(provider_id, ACTIVATION_FAILED, target_version, previous_version, message)

        def activation_attention() -> str | None:
            try:
                return get_kind(kind_of(installation)).on_activated(provider_id, previous_manifest, manifest.raw)
            except Exception:
                logging.getLogger(__name__).exception("The kind's activation step failed for %s", provider_id)
                return "Its saved settings could not be brought up to date with this version."

        attention = None if rollback else activation_attention()

        def promote(state: dict[str, Any]) -> tuple[bool, str | None]:
            row = (state.get("installations") or {}).get(provider_id)
            if (not still_staged(row)
                    or row.get("staged_version") != target_version
                    or row.get("staged_path") != installation.get("staged_path")
                    or (row.get("staged_manifest") or {}).get("bundle_sha256")
                    != (installation.get("staged_manifest") or {}).get("bundle_sha256")
                    or revocation.lookup(_revocations(state), provider_id, target_version) is not None):
                # Replaced, removed, revoked or finished by someone else while it was being checked: theirs
                # stands, and a revoked release is dropped by the next refresh.
                return False, None
            if rollback and (refusal := check_rollback(state, row)):
                return False, refusal
            row["active_version"] = row.get("staged_version")
            if row.get("staged_path"):
                row["active_path"] = row.get("staged_path")
            if row.get("staged_python_path"):
                row["python_path"] = row.get("staged_python_path")
            row["staged_version"] = None
            row["staged_path"] = None
            row["staged_python_path"] = None
            row["staged_manifest"] = None
            if rollback:
                row[releases.PROMOTE_ROLLBACK] = row.pop(releases.STAGED_ROLLBACK)["release_id"]
            row.pop("previous_catalog_list", None)
            row["manifest"] = manifest.raw
            row["supplied_by"] = row.pop("staged_supplied_by", None) or {}
            row["state"] = "active"
            row["pending_restart"] = False
            row["last_error"] = None
            row.pop("needs_repair", None)
            row["activated_at"] = utcnow_iso()
            if attention:
                row["last_error"] = attention
            if previous_version and not at_boot:
                # Calls the replaced release took are still running: it drains instead of retiring at once.
                row[releases.DRAIN_PREVIOUS] = True
            # Activation swaps the installation's manifest for the staged one; the staged schema's secret
            # paths (a new version can add them) join the persisted set with it.
            _persist_plugin_secret_paths(state, row)
            return True, None

        promoted, refusal = mutate_state(promote)
        if refusal:
            return failed_rollback(refusal)
        if not promoted:
            job.update(message=f"Activation of '{target_name}' v{target_version} skipped: it changed meanwhile")
            return Activation(provider_id, ACTIVATION_SKIPPED, target_version, previous_version)
        if rollback:
            # A kind can change saved data here. Do it only once the rollback's final identity check has committed.
            if attention := activation_attention():
                _record_provider_last_error(provider_id, attention, installation.get("staged_python_path"))
            job.update(message=f"Activated rollback {previous_version} -> {target_version}")
        elif previous_version:
            job.update(message=f"Activated update {previous_version} -> {target_version}")
        else:
            job.update(message=f"Activated plugin '{target_name}' v{target_version}")
    draining = bool(previous_version and not at_boot)
    drained = (_drain_replaced_release(provider_id, kind_of(installation), previous_manifest, previous_version)
               if draining else None)
    return Activation(provider_id, ACTIVATED, target_version, previous_version, draining=draining, drained=drained)


def _drain_replaced_release(provider_id: str, kind_name: str, previous_manifest: Any, previous_version: Any):
    """A live activation replaced a release that may still be serving calls: for a kind that stops a release while
    Bazarr+ runs, its pools let go of it, its workers end once their calls are done, and it retires. Returns the
    thread that does the last of that, or None when the kind cannot (the release stays draining) or the detach
    failed (the next start retires it)."""
    if not detach.detaches_live(kind_name):
        return None
    release_key = previous_manifest.get("bundle_sha256") if isinstance(previous_manifest, dict) else None
    try:
        workers = detach.snapshot_workers(provider_id, release_key)
        drains = detach.detach(provider_id, kind_name, withdraw=False, release_key=release_key)
    except Exception:
        logging.getLogger(__name__).exception(
            "Provider Hub: could not release the pools of the replaced %s; it retires at the next start", provider_id)
        return None

    def retire() -> None:
        detach.stop_stragglers(workers)
        # Only the release that was replaced: a second live activation may have another one still draining.
        retire_release(provider_id, previous_version)

    return detach.when_drained(drains, retire, name=provider_id)


def retire_release(plugin_id: str, version: str | None = None) -> int:
    """A draining release has finished its calls: it becomes the previous release. Returns how many moved.

    Without ``version`` every draining release of the plugin retires. Does nothing without the release
    tables (the file backend a few tools use).
    """
    from .state import _uses_file
    if _uses_file(None):
        return 0
    from . import state_store

    moved = 0
    for release in state_store.list_releases(plugin_id, states=(releases.DRAINING,)):
        if version is None or release.version == version:
            moved += bool(state_store.transition_release(release.id, releases.RETIRED))
    return moved


def _rollback_needs_refusal(state: dict[str, Any], plugin: Any, provider_id: str, name: str, target) -> str | None:
    """Why the previous release cannot come back for want of what it relies on, or None.

    The capabilities it once had a supplier for must be supplied by a plugin that runs after the next restart and
    must be recorded as its bindings, which the boot guard holds it to.
    """
    if plugin is None:
        return None
    row = (state.get("installations") or {}).get(provider_id) or {}
    missing = closure.unsupplied_needs(state, plugin, provider_id, target.supplied_by, row.get("source_id"))
    if not missing:
        bindings = closure.supplier_map(state, plugin, provider_id)
        missing = sorted(capability for capability in (target.supplied_by or {})
                         if capability in plugin.requires and capability not in bindings)
    if missing:
        return f"{name} v{target.version} needs {', '.join(missing)}, which is not running, so it is not restored"
    return None


def _previous_release(provider_id: str):
    from .state import _uses_file
    if _uses_file(None):
        return None
    from . import state_store
    previous = state_store.list_releases(provider_id, states=(releases.RETIRED,))
    return max(previous, key=lambda release: (release.ended_seq or 0, release.id)) if previous else None


_ROLLBACK_ACTIVE_FIELDS = ("active_version", "active_path", "python_path", "manifest", "kind")
_ROLLBACK_STAGED_FIELDS = (releases.STAGED_ROLLBACK, "staged_version", "staged_path",
                           "staged_python_path", "staged_manifest")
_ROLLBACK_CHANGED = "The previous release changed; review the rollback again"


def _rollback_hash(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _rollback_release_identity(release: releases.Release) -> str:
    return _rollback_hash((release.plugin_id, release.id, release.created_at, release.ended_seq,
                           release.version, release.bundle_path, release.bundle_digest,
                           release.python_path, release.env_key, release.source_id, release.origin,
                           release.source_registration))


def _rollback_source_binding(state: dict[str, Any], row: dict[str, Any]) -> str:
    source_id = row.get("source_id")
    source = (state.get("catalog_sources") or {}).get(source_id) if isinstance(source_id, str) else None
    registration = ([source.get(key) for key in ("registration_id", "type", "url", "dev_ref", "binding_revision",
                                                "enabled", "trusted")]
                    if isinstance(source, dict) else None)
    return _rollback_hash((source_id, row.get("origin"), row.get("source_bound"), row.get("trusted"),
                           _installed_catalog_list(row), registration))


def _rollback_confirmation(state: dict[str, Any], row: dict[str, Any], target: releases.Release) -> str:
    return _rollback_hash((_rollback_release_identity(target), target.manifest, _rollback_source_binding(state, row)))


def _staged_rollback_refusal(state: dict[str, Any], provider_id: str, row: dict[str, Any]) -> str | None:
    binding = row.get(releases.STAGED_ROLLBACK)
    if binding is None:
        return None
    if not isinstance(binding, dict) or binding.get("source_binding") != _rollback_source_binding(state, row):
        return "The catalog source binding changed"
    staged = getattr(state, "staged_releases", {}).get(provider_id)
    if (staged is None or staged.get("id") != binding.get("release_id")
            or _rollback_confirmation(state, row, releases.release_of(staged)) != binding.get("confirmation_identity")
            or _rollback_hash([row.get(key) for key in _ROLLBACK_ACTIVE_FIELDS]) != binding.get("active_identity")):
        return "The previous release changed"
    return None


def rollback_consent(provider_id: str, *, _state=None, _row=None, _target=None) -> dict[str, Any] | None:
    """What going back to the previous release adds to what the active release is allowed, or None.

    Worked out from the two manifests alone, so it does not depend on the catalog still listing the old release.
    The Subtitle Hub has no consent step for a provider without dependencies, as for an update. The digest in the
    result is what ``rollback_release`` takes as ``consent_digest``.
    """
    state = _state if _state is not None else load_state()
    row = _row if _row is not None else (state.get("installations") or {}).get(provider_id)
    target = _target if _target is not None else _previous_release(provider_id)
    if not isinstance(row, dict) or target is None or target.manifest is None:
        return None
    if kind_of(row) == SUBTITLE_PROVIDER and not closure.has_needs(target.manifest):
        return None
    return consent.update_delta(state, row.get("manifest"), {"manifest": target.manifest}, closure.Closure())


def _checked_rollback_target(state, provider_id, target, *, verify_files=True):
    """The existing rollback guards, shared by its read preview and its writer."""
    row = (state.get("installations") or {}).get(provider_id)
    if not isinstance(row, dict) or not row.get("active_version") or row.get("state") == "removed":
        raise ProviderHubInstallError(f"{provider_id} is not installed")
    if row.get("staged_version"):
        if row.get(releases.STAGED_ROLLBACK):
            status = "waiting for restart" if needs_restart(row) else "in progress"
            raise ProviderHubInstallError(f"{provider_id} has a rollback {status}")
        raise ProviderHubInstallError(f"{provider_id} has an update waiting; discard it before going back")
    if target is None:
        raise ProviderHubInstallError(f"{provider_id} has no previous release to go back to")
    name = row.get("name") or provider_id
    source_id = row.get("source_id")
    source = (state.get("catalog_sources") or {}).get(source_id) if isinstance(source_id, str) else None
    registration = source.get("registration_id") if isinstance(source, dict) else None
    if ((target.source_id, target.origin) != (source_id, row.get("origin"))
            or target.source_registration != registration):
        raise ProviderHubInstallError(
            f"{name} v{target.version} was installed from another source than {name} is now, so it is not restored")
    found = revocation.lookup(_revocations(state), provider_id, target.version)
    if found is not None:
        raise ProviderHubInstallError(_revocation_text(revocation.refusal, found, name, target.version, "installed"))
    if target.manifest is None or not target.bundle_path or not target.python_path:
        raise ProviderHubInstallError(f"{name} v{target.version} is no longer on disk, so it cannot be restored")
    # What activation checks again later, checked now, so a release that cannot come back is refused before
    # anything changes.
    try:
        kind = get_kind(kind_of(row))
        manifest = kind.validate_manifest(target.manifest, bool(row.get("trusted", False)))
        if target.bundle_digest and manifest.bundle_sha256 != target.bundle_digest:
            raise ProviderHubInstallError("bundle digest changed")
        if verify_files:
            bundle = validated_bundle_path(provider_hub_dir(), target.bundle_path)
            interpreter = validated_interpreter(provider_hub_dir(), target.python_path)
            if bundle is None or interpreter is None or not Path(bundle).is_dir() or not Path(interpreter).exists():
                raise ProviderHubInstallError("not on disk")
            verify_bundle_tree(manifest, bundle)
    except Exception as error:
        raise ProviderHubInstallError(
            f"{name} v{target.version} is no longer on disk, or no longer matches what it was, "
            f"so it cannot be restored ({refusal_text(error)})") from error
    plugin = parse_plugin_manifest(target.manifest) if target.manifest.get("schema_version") == 2 else None
    # An early answer. The check that counts runs again inside the write that stages the release.
    refusal = _rollback_needs_refusal(state, plugin, provider_id, name, target)
    if refusal:
        raise ProviderHubInstallError(refusal)
    return row, plugin


def rollback_preview(provider_id: str, *, state=None, verify_files=True, include_consent=True) -> dict[str, Any]:
    """The server-selected previous version and why it cannot return, without changing anything.

    Listings skip file hashing and consent calculation. Opening the preview and submitting the rollback both
    perform the full target checks; the listing is only a current hint.
    """
    state = state if state is not None else load_state()
    target = _previous_release(provider_id)
    result = {"previous_version": target.version if target is not None else None, "blocked_reason": None}
    if include_consent:
        row = (state.get("installations") or {}).get(provider_id)
        result["consent"] = None
        result["confirmation_identity"] = (_rollback_confirmation(state, row, target)
                                           if isinstance(row, dict) and target is not None else None)
    try:
        row, _plugin = _checked_rollback_target(state, provider_id, target, verify_files=verify_files)
        _refuse_a_release_that_breaks_dependents(state, provider_id, target.version, target.manifest)
    except ProviderHubInstallError as error:
        result["blocked_reason"] = refusal_text(error)
    else:
        if include_consent:
            result["consent"] = rollback_consent(provider_id, _state=state, _row=row, _target=target)
    return result


def rollback_release(provider_id: str, consent_digest: str | None = None, *, expected_version: str | None = None,
                     confirmation_identity: str | None = None) -> Activation:
    """Go back to the release this plugin ran before its active one.

    The previous release is checked against what it was (its files are on disk and match its manifest, it comes
    from the source the plugin comes from now, it is not revoked), then staged again under its own identity by the
    same rules a new release is staged by (a plugin that relies on what the active release supplies refuses
    it, its bindings are worked out again, what it adds over the active release needs consent), and activated by
    ``activate_release``: a kind that attaches on restart waits for the boot, a kind that attaches live switches
    now. Raises ``ProviderHubInstallError`` when there is nothing to go back to or the rules refuse it, and then
    changes nothing: the previous release stays recorded. A confirmation pins the server-selected release
    and its source binding independently of permission consent.
    """
    state = load_state()
    target = _previous_release(provider_id)
    row = (state.get("installations") or {}).get(provider_id)
    if expected_version is not None or confirmation_identity is not None:
        if (not isinstance(row, dict) or target is None or target.version != expected_version
                or confirmation_identity != _rollback_confirmation(state, row, target)):
            raise ProviderHubConsentError(_ROLLBACK_CHANGED, changed=True)
    row, plugin = _checked_rollback_target(state, provider_id, target)
    name = row.get("name") or provider_id

    def matches_retained(document):
        latest = max((release for release in getattr(document, "retained_releases", ())
                      if release.get("plugin_id") == provider_id and release.get("state") == releases.RETIRED),
                     key=lambda release: (release.get("ended_seq") or 0, release["id"]), default=None)
        return latest is not None and _rollback_release_identity(releases.release_of(latest)) == release_identity

    release_identity = _rollback_release_identity(target)
    if not matches_retained(state):
        raise ProviderHubConsentError(_ROLLBACK_CHANGED, changed=True)
    delta = rollback_consent(provider_id, _state=state, _row=row, _target=target)
    if (expected_version is not None and consent_digest is not None
            and (delta is None or consent_digest != delta["digest"])):
        raise ProviderHubConsentError("The rollback consent changed; review the rollback again", changed=True)
    if delta and delta["needs_consent"] and consent_digest != delta["digest"]:
        raise ProviderHubConsentError(consent.delta_text(delta))

    identity = {key: row.get(key) for key in _ROLLBACK_ACTIVE_FIELDS}
    binding = {
        "release_id": target.id,
        "confirmation_identity": _rollback_confirmation(state, row, target),
        "source_binding": _rollback_source_binding(state, row),
        "active_identity": _rollback_hash(list(identity.values())),
    }
    staged_identity = {releases.STAGED_ROLLBACK: binding, "staged_version": target.version,
                       "staged_path": target.bundle_path, "staged_python_path": target.python_path,
                       "staged_manifest": target.manifest}

    def stage(state: dict[str, Any]) -> bool:
        current = (state.get("installations") or {}).get(provider_id)
        if (not isinstance(current, dict) or any(current.get(key) != value for key, value in identity.items())
                or _rollback_source_binding(state, current) != binding["source_binding"]
                or not matches_retained(state)
                or current.get("staged_version") or current.get("state") == "removed"):
            return False
        # The rule every staging is held to: a plugin that relies on what the active release supplies refuses it.
        _refuse_a_release_that_breaks_dependents(state, provider_id, target.version, target.manifest)
        # What the release relies on has to be supplied by what runs after the restart, judged on the state this
        # write commits to, so a supplier changed since the early check still refuses it.
        refusal = _rollback_needs_refusal(state, plugin, provider_id, name, target)
        if refusal:
            raise ProviderHubInstallError(refusal)
        current.update(staged_identity, state="staged", pending_restart=True, last_error=None)
        if plugin is not None:
            current["staged_supplied_by"] = closure.supplier_map(state, plugin, provider_id)
        return True

    if not mutate_state(stage):
        raise ProviderHubConsentError(f"{provider_id} changed while it was being rolled back", changed=True)
    return activate_release(provider_id, _expected_staged=staged_identity)


def _finish_removal(provider_id: str) -> bool:
    """The restart that follows a staged removal takes the plugin's row, and with it its releases, away."""
    state = load_state()
    installation = (state.get("installations") or {}).get(provider_id)
    # A removal that was being finished live when the last run ended is marked removed without the restart flag.
    if not isinstance(installation, dict) or installation.get("state") != "removed":
        return False
    target_name = installation.get("name") or provider_id
    with record_job(
        "uninstall",
        target_kind="provider",
        target_id=provider_id,
        target_name=target_name,
        from_version=installation.get("active_version"),
    ) as job:
        removed = _drop_removed_row(provider_id)
        if removed:
            job.update(message=f"Removed plugin '{target_name}' on restart")
    return removed


def activate_staged_installations() -> list[str]:
    """The boot step: finish the removals staged in the last run and activate what was staged, through
    ``activate_release``. Returns the ids that activated."""
    try:
        close_stale_jobs()
    except Exception:
        logging.getLogger(__name__).warning("Could not close the job rows an earlier run left open", exc_info=True)
    try:
        _enforce_kept_data_ownership()
    except Exception:
        logging.getLogger(__name__).warning("Could not check who owns the data kept for staged plugins",
                                            exc_info=True)
    try:
        _note_orphan_releases()
    except Exception:
        logging.getLogger(__name__).warning("Could not look for release records without an installation",
                                            exc_info=True)
    try:
        _retire_stale_draining()
    except Exception:
        logging.getLogger(__name__).warning("Could not retire the releases an earlier run left draining",
                                            exc_info=True)
    state = load_state()
    activated = []
    try:
        for provider_id, installation in list((state.get("installations") or {}).items()):
            if not isinstance(installation, dict):
                continue
            removal = installation.get("state") == "removed"
            if not removal and (not installation.get("pending_restart") or installation.get("state") != "staged"):
                continue
            # One plugin that cannot be handled never keeps the others from starting.
            try:
                if removal:
                    _finish_removal(provider_id)
                    state["installations"].pop(provider_id, None)
                elif activate_release(provider_id, at_boot=True).activated:
                    activated.append(provider_id)
            except Exception:
                logging.getLogger(__name__).exception("Could not activate or remove %s at startup", provider_id)
    finally:
        # What actually activated is known now. A plugin whose active release relies on one that is not
        # running does not start, and says why.
        probe = load_state()
        if _apply_unmet_needs(probe.get("installations") or {}, _revocations(probe)):
            def hold(state: dict[str, Any]) -> None:
                _apply_unmet_needs(state.setdefault("installations", {}), _revocations(state))

            mutate_state(hold)
    return activated


def _note_orphan_releases() -> None:
    """Say so when the tables hold releases of plugins the document does not list. They are never deleted
    silently: a plugin listed again is matched to them, and ``state_store.discard_orphan_releases`` ends them."""
    from .state import _uses_file
    if _uses_file(None):
        return
    from . import state_store

    orphans = state_store.orphan_plugin_ids()
    if orphans:
        logging.getLogger(__name__).warning(
            "Provider Hub: the release records of %s have no installation in the state document. They and their "
            "files are kept; an administrator ends them with state_store.discard_orphan_releases", ", ".join(orphans))


def _retire_stale_draining() -> None:
    """A release left draining by the last run has no worker now: it is the previous release."""
    from .state import _uses_file
    if not _uses_file(None):
        from . import state_store
        state_store.retire_draining()


def reconcile_plugin_environments() -> dict[str, list[str]]:
    """Boot step: adopt the environments installations already use.

    Runs no pip and needs no network. An active installation whose environment
    is gone is marked ``needs_repair`` instead of being rebuilt here.
    """

    def validate(row: dict[str, Any], staged: bool):
        raw = row.get("staged_manifest") if staged and isinstance(row.get("staged_manifest"), dict) else row.get("manifest")
        # Each kind validates its own manifests; the subtitle provider kind delegates to the provider validator.
        return get_kind(kind_of(row)).validate_manifest(raw, bool(row.get("trusted", False)))

    def probe(validated, bundle_path: Path, python_path: Path) -> None:
        get_kind(kind_of(validated)).smoke_test(validated, bundle_path, python_path)

    return env_registry.reconcile_environments(
        validate=validate, probe=probe, verify_bundle=_verify_bundle_for_run
    )


def repair_installation_environment(provider_id: str, checkpoint=None) -> dict[str, Any]:
    """Rebuild an active installation's environment into a new directory; see ``_repair_environment``.

    The environment the installation runs from now is held for the whole repair,
    so a cleanup cannot remove it between the repoint and the moment the old
    workers that still run from it have been drained.
    """
    row = (load_state().get("installations") or {}).get(provider_id)
    old_python = row.get("python_path") if isinstance(row, dict) else None
    release = hold_environment(env_registry.env_dir_of(old_python)) if old_python else (lambda: None)
    try:
        return _repair_environment(provider_id, checkpoint)
    finally:
        release()


def _repair_environment(provider_id: str, checkpoint=None) -> dict[str, Any]:
    """Rebuild an active installation's environment into a new directory.

    The old directory is left alone, because other plugins may share it. The new
    one is recorded and the installation pointed at it only after the worker
    check passes there. A bundle that carries its own wheels rebuilds from them;
    any other needs the network. It is an explicit action and never runs at boot.
    """
    step = checkpoint or (lambda message: None)
    row = (load_state().get("installations") or {}).get(provider_id)
    if not isinstance(row, dict) or not row.get("active_version"):
        raise ProviderHubInstallError(f"{provider_id} has no active version to repair")
    if row.get("revoked"):
        # The check after a rebuild starts the plugin, and a revoked release is not run for that or anything else.
        raise ProviderHubInstallError(
            row.get("revoked_reason") or f"{provider_id} was revoked by the official catalog, so it is not repaired")
    active_path = row.get("active_path")
    manifest_data = row.get("manifest")
    if not active_path or not isinstance(manifest_data, dict):
        raise ProviderHubInstallError(f"{provider_id} has no bundle to repair")
    bundle_path = validated_bundle_path(provider_hub_dir(), active_path)
    if bundle_path is None:
        raise ProviderHubInstallError(f"{provider_id} has a bundle path outside the hub's bundles; nothing was changed")
    name = row.get("name") or provider_id
    with record_job(
        "repair",
        target_kind="provider",
        target_id=provider_id,
        target_name=name,
        from_version=row.get("active_version"),
    ) as job, pin_scope():
        env_path = None
        try:
            kind = get_kind(kind_of(row))
            validated = kind.validate_manifest(manifest_data, bool(row.get("trusted", False)))
            try:
                _verify_bundle_for_run(validated, bundle_path)
            except Exception:
                # Modified or foreign code is not run, and nothing is built for it.
                env_registry.mark_needs_repair(provider_id)
                raise
            step("Rebuilding the plugin's dependencies")
            env_path = PluginEnvironment(provider_hub_dir()).install(
                validated, rebuild=True, **_bundled_wheels_argument(validated, bundle_path))
            python_path = python_executable(env_path)
            step("Checking that the plugin starts")
            kind.smoke_test(validated, bundle_path, python_path)
            # A stop pressed during the check ends the repair here, before the
            # installation is pointed at the new environment.
            step("Recording the repair")
        except Exception:
            if env_path is not None:
                release_pins()
                env_registry.discard_if_unused(provider_hub_dir(), env_path)
            raise

        def record_repair(state: dict[str, Any]) -> dict[str, Any] | None:
            current = (state.get("installations") or {}).get(provider_id)
            if not isinstance(current, dict):
                return None
            if current.get("python_path") != row.get("python_path"):
                # Another repair or an activation moved the installation while
                # this one was building; theirs stands.
                return {}
            current["python_path"] = str(python_path)
            if current.pop("needs_repair", None):
                # A plugin held back at boot keeps saying why.
                current["last_error"] = closure.held_back_error(current)
            env_registry.register_environment(state, environment_fingerprint(validated), env_path)
            return dict(current)

        try:
            with env_registry.recording_environment(env_path):
                repaired = mutate_state(record_repair)
        except env_registry.EnvironmentGone as error:
            release_pins()
            raise ProviderHubInstallError(str(error)) from error
        if repaired is None:
            raise ProviderHubInstallError(f"{provider_id} was removed while it was being repaired")
        if repaired == {}:
            release_pins()
            env_registry.discard_if_unused(provider_hub_dir(), env_path)
            raise ProviderHubInstallError(f"{provider_id} changed while it was being repaired")
        if kind_of(repaired) == SUBTITLE_PROVIDER:
            # Only a subtitle provider has a registered class and pooled workers to refresh.
            try:
                from .registry import register_active_provider_classes

                register_active_provider_classes(
                    [item for item in running_installations(kind=None) if item.provider_id == provider_id]
                )
            except Exception:
                logging.getLogger(__name__).exception("Provider Hub: could not re-register %s after its repair", provider_id)
            try:
                # Pools keep the instance, and the worker, they built before the
                # repair. They drop it now; it drains, and the old environment
                # stays held until its worker has exited.
                from .registry import retire_provider_instances

                retire_provider_instances(provider_id)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Provider Hub: could not retire the workers of %s after its repair; "
                    "its previous environment is kept in place", provider_id,
                )
                old_python = row.get("python_path")
                if old_python:
                    # Never released: a worker may still run from it, and a cleanup
                    # must leave it alone until the process restarts.
                    hold_environment(env_registry.env_dir_of(old_python))
        job.update(message=f"Rebuilt the environment of '{name}'")
        return _redact_installation(repaired)


def cleanup_plugin_environments() -> list[str]:
    """Remove environments no installation references. Not for boot."""
    return env_registry.cleanup_environments(provider_hub_dir())


def dependents_refusal_text(provider_id: str, names: list[str], action: str) -> str:
    """The refusal for removing or switching off a plugin that others depend on."""
    return closure.dependents_text(provider_id, names, action)


def installed_dependents(provider_id: str, *, enabled_only: bool = False,
                         state: dict[str, Any] | None = None) -> list[str]:
    """The ids of the installed plugins that depend on ``provider_id``: the one answer the refusal to remove or
    switch it off and the listing both give. With ``enabled_only``, plugins that are switched off do not count."""
    return closure.dependents(state if state is not None else load_state(), provider_id, enabled_only=enabled_only)


def dependent_refusal(provider_id: str, action: str, *, enabled_only: bool = False) -> str | None:
    """Why ``provider_id`` cannot be removed or switched off while others depend on it, or None."""
    names = installed_dependents(provider_id, enabled_only=enabled_only)
    return closure.dependents_text(provider_id, names, action) if names else None


def remove_installation(provider_id: str, *, ignoring_dependents: frozenset[str] = frozenset(),
                        keep_data: bool = False, rollback: bool = False) -> bool:
    """Stage the removal of an installation, or remove it at once when it was never active.

    A kind that stops a release while Bazarr+ runs finishes the removal without a restart: the row is marked
    removed, the release is detached (it leaves the registry and the pools, its worker ends once its calls are
    done) and the row is deleted when that is over. Only when the release cannot be stopped does the removal wait
    for the restart, as it does for every other kind.

    A plugin another one depends on is refused. ``ignoring_dependents`` names plugins that do not count as
    dependents, which is how a failed install discards the plugins it staged for itself while any other
    plugin that has come to depend on them still protects them. ``keep_data`` is the person's choice to keep the
    plugin's saved settings and data folder, which the kind honours; the default deletes them.

    ``rollback`` is for discarding what a failed or stopped install staged. It deletes only what that install made:
    data an earlier removal kept (it has a record), and the settings of a subtitle provider, which may be older than
    the install, are left exactly as they were, and no record is written or changed.
    """
    state = load_state()
    installations = state.setdefault("installations", {})
    if provider_id not in installations:
        return False
    names = [name for name in closure.dependents(state, provider_id) if name not in ignoring_dependents]
    if names:
        raise ProviderHubInstallError(closure.dependents_text(provider_id, names, "removed"))
    item = installations[provider_id]
    target_name = (
        item.get("name") if isinstance(item, dict) else None
    ) or provider_id
    active_version = item.get("active_version") if isinstance(item, dict) else None
    if rollback:
        keep_data = (keep_data or bool((state.get("kept_data") or {}).get(provider_id))
                     or (isinstance(item, dict) and kind_of(item) == SUBTITLE_PROVIDER))

    with record_job(
        "uninstall",
        target_kind="provider",
        target_id=provider_id,
        target_name=target_name,
        from_version=active_version,
    ) as job:
        # Disabled first: once the removal is staged, a list that still names
        # the provider after the restart would bring it back, and a failure
        # here leaves nothing changed.
        kind_hooks = get_kind(kind_of(item))
        live_removal = kind_hooks.detach_policy == DETACH_LIVE
        # The hooks read the row keyed by the id the caller passed, so a row
        # that is not an object still answers for its own id.
        row = dict(item) if isinstance(item, dict) else {}
        row["provider_id"] = provider_id
        was_enabled = kind_hooks.is_enabled(row)
        if not kind_hooks.on_removed(provider_id):
            raise ProviderHubSettingsError(ENABLED_PROVIDERS_NOT_SAVED)

        def remove_or_stage(state: dict[str, Any]) -> str:
            installations = state.setdefault("installations", {})
            item = installations.get(provider_id)
            if not isinstance(item, dict):
                return "missing"
            # Checked against the document this write commits to, so a dependent installed since the
            # early check above still blocks the removal.
            names = [name for name in closure.dependents(state, provider_id) if name not in ignoring_dependents]
            if names:
                raise ProviderHubInstallError(closure.dependents_text(provider_id, names, "removed"))
            kept = (state.get("kept_data") or {}).get(provider_id)
            if rollback and isinstance(kept, dict) and not kept.get("config") and item.get("config"):
                # An install that took the kept settings over put them on this row and cleared the record's copy.
                # The row is going, so they go back to the record, as they were.
                kept["config"] = copy.deepcopy(item["config"])
            if not item.get("active_version"):
                del installations[provider_id]
                return "removed_pending"
            item["state"] = "removed"
            # A kind that stops a release while Bazarr+ runs needs no restart to finish this: the release is
            # detached right after, and the row goes once its calls are done. The row stays marked removed until
            # then, so a process that ends first finishes the removal at its next start.
            item["pending_restart"] = not live_removal
            item["staged_version"] = None
            item["staged_path"] = None
            item["staged_python_path"] = None
            item["staged_manifest"] = None
            item.pop(releases.STAGED_ROLLBACK, None)
            item["last_error"] = None
            if not keep_data:
                # Its settings go with the choice to delete, now and not at the restart, so installing it again
                # before then does not find them.
                item["config"] = {}
            return "staged"

        try:
            result = mutate_state(remove_or_stage)
        except Exception:
            # Nothing was staged, so the provider is still installed and has to
            # stay enabled as it was rather than silently switched off.
            if was_enabled:
                kind_hooks.set_enabled(row, True)
            raise
        if result == "missing":
            job.update(message=f"Plugin '{target_name}' not found")
            return False
        from . import runtime_status
        runtime_status.clear(provider_id)
        if not rollback:
            _record_kept_data(provider_id, item, kind_hooks, keep_data)
        try:
            kind_hooks.after_removed(provider_id, keep_data=keep_data)
        except Exception:
            logging.getLogger(__name__).exception("Cleanup after the removal of %s failed", provider_id)
        if result == "removed_pending":
            job.update(message=f"Removed pending install of '{target_name}'")
            return True
        if live_removal and _detach_removed(provider_id, kind_of(item)):
            job.update(message=f"Removed plugin '{target_name}'")
            return True
        job.update(message=f"Staged removal of '{target_name}' (restart Bazarr+ to finalize)")
        return True


def _detach_removed(provider_id: str, kind_name: str) -> bool:
    """A removal is saved and the release is still running: take its consumers away now, and delete the row once
    the calls it holds are done. False when that could not be done, and then the row asks for the restart that
    finishes the removal, as it did before releases could be stopped live."""
    try:
        workers = detach.snapshot_workers(provider_id)
        drains = detach.detach(provider_id, kind_name, withdraw=True, grace=detach.REMOVE_GRACE_SECONDS)
    except Exception:
        logging.getLogger(__name__).exception(
            "Provider Hub: could not stop %s live; its removal finishes at the next restart", provider_id)

        def ask(state: dict[str, Any]) -> None:
            row = (state.get("installations") or {}).get(provider_id)
            if isinstance(row, dict) and row.get("state") == "removed":
                row["pending_restart"] = True

        mutate_state(ask)
        return False

    def finish() -> None:
        # The workers it had when it was detached go, whatever release they were started for.
        detach.stop_stragglers(workers, cut=True)
        if _drop_removed_row(provider_id):
            try:
                get_kind(kind_name).removal_finished(provider_id)
            except Exception:
                logging.getLogger(__name__).warning("Provider Hub: the step after removing %s failed", provider_id,
                                                    exc_info=True)

    detach.when_drained(drains, finish, name=provider_id)
    return True


def _drop_removed_row(provider_id: str) -> bool:
    """Delete the row of a removed plugin, and with it its releases. A row that is not marked removed any more (it
    was installed again meanwhile) stays."""
    def drop(state: dict[str, Any]) -> bool:
        row = (state.get("installations") or {}).get(provider_id)
        if isinstance(row, dict) and row.get("state") == "removed":
            del state["installations"][provider_id]
            return True
        return False

    return bool(mutate_state(drop))


def _record_kept_data(provider_id: str, item: Any, kind_hooks: Any, keep_data: bool) -> None:
    """Remember whose data was kept, so only the same plugin from the same source finds it again.

    The kept data belongs to a plugin id, a source and a kind. A later install of the same id from another source,
    or as another kind, is a different package, and ``claim_kept_data`` discards what was kept instead of handing
    it over. The plugin's saved settings travel in the record, because the installation row that holds them goes at
    the next restart. A removal that deletes the data takes the record with it.
    """
    if not keep_data and not (load_state().get("kept_data") or {}).get(provider_id):
        return
    row = item if isinstance(item, dict) else {}
    config = row.get("config")
    entry = {"source_id": row.get("source_id"), "origin": row.get("origin"), "kind": kind_of(row) if row else None,
             "config": copy.deepcopy(config) if keep_data and isinstance(config, dict) and config else None}

    def record(state: dict[str, Any]) -> None:
        kept = state.setdefault("kept_data", {})
        if keep_data and getattr(kind_hooks, "keeps_data", False):
            kept[provider_id] = entry
        else:
            kept.pop(provider_id, None)

    try:
        mutate_state(record)
    except Exception:
        logging.getLogger(__name__).warning("Could not record the kept data of %s", provider_id, exc_info=True)


def claim_kept_data(provider_id: str, source_id: str | None, kind: str, origin: str | None = None) -> bool:
    """A first install, or a reinstall of a plugin removed in this run, has staged its row. Data kept by an earlier
    removal is handed over only when it is the same plugin from the same source and of the same kind, with its
    saved settings put back; otherwise it is discarded, so a different package under the same id never inherits
    settings and keys it was not given. Returns True when data was handed over.

    The record stays until a removal rewrites it, and only the copy of the settings is dropped once they are put
    back. An install that fails after this therefore leaves the data owned by the source it was kept for.
    """
    entry = (load_state().get("kept_data") or {}).get(provider_id)
    if not isinstance(entry, dict):
        return False
    # An upload takes back what an upload kept, and nothing a catalog source kept. A row that says nothing of its
    # origin is judged by its source alone.
    if (entry.get("source_id") == source_id and entry.get("kind") == kind
            and (entry.get("origin") or origin) == origin):
        config = entry.get("config")

        def hand_over(state: dict[str, Any]) -> None:
            row = (state.get("installations") or {}).get(provider_id)
            if isinstance(config, dict) and config and isinstance(row, dict) and not row.get("config"):
                row["config"] = copy.deepcopy(config)
            kept = (state.get("kept_data") or {}).get(provider_id)
            if isinstance(kept, dict):
                kept["config"] = None

        mutate_state(hand_over)
        get_kind(kind).kept_data_reclaimed(provider_id)
        return True
    try:
        get_kind(str(entry.get("kind"))).discard_kept_data(provider_id)
    except Exception as error:
        # The data stays, with its record, and the install is refused: going on would hand it to a package it
        # was not kept for.
        logging.getLogger(__name__).warning("Could not discard the kept data of %s", provider_id, exc_info=True)
        raise ProviderHubInstallError(
            f"{provider_id} was removed earlier with its data kept, and that data belongs to another source. "
            "It could not be cleared, so nothing was installed."
        ) from error

    def forget(state: dict[str, Any]) -> None:
        (state.get("kept_data") or {}).pop(provider_id, None)

    mutate_state(forget)
    return False


def check_updates(*, kind: str | None = None) -> dict[str, Any]:
    job_id: str | None = None
    with record_job("check_updates", target_kind="system") as job:
        job_id = job.data["id"]
        state = load_state()
        available = 0
        provider_summaries: list[str] = []
        needs_consent: list[str] = []
        refusals: list[str] = []
        for provider_id, installation in (state.get("installations") or {}).items():
            if not isinstance(installation, dict):
                continue
            if kind is not None and kind_of(installation) != kind:
                continue
            if installation.get("origin") == "local":
                # Local packages are owned by their uploaded file, not the catalog.
                continue
            active_version = installation.get("active_version")
            if not active_version:
                continue
            catalog_list = _installed_catalog_list(installation)
            update = _latest_catalog_update(
                state, provider_id, active_version, _bound_source_id(installation), catalog_list,
                installed_kind=kind_of(installation),
            )
            latest_version = update[0]["manifest"].get("version") if update is not None else None
            refusal = _refused_update_reason(
                state, provider_id, latest_version or active_version, _bound_source_id(installation), catalog_list,
                installed_kind=kind_of(installation))
            if refusal:
                refusals.append(f"{installation.get('name') or provider_id}: {refusal}")
            if update is None or not latest_version:
                continue
            available += 1
            delta = _update_delta(state, installation, *update)
            if delta and delta["needs_consent"]:
                needs_consent.append(provider_id)
            display_name = installation.get("name") or provider_id
            provider_summaries.append(
                f"{display_name}: {active_version} -> {latest_version}"
            )
        if available:
            message = (
                f"{available} update(s) available: {', '.join(provider_summaries)}"
            )
        else:
            message = "Catalog metadata checked. No new updates available."
        if refusals:
            message = f"{message} (newer version not offered for {'; '.join(refusals)})"
        job.update(
            message=message,
            details={"updates_available": available, "providers": provider_summaries,
                     "needs_consent": needs_consent},
        )
    persisted = get_job(job_id) if job_id else None
    return persisted or {}


def _bound_source_id(installation: dict[str, Any]) -> str | None:
    """The source an install updates from: the one it is bound to, if any."""
    return installation.get("source_id") if installation.get("source_bound") else None


def _offerable_as_update(entry: dict[str, Any], installed_kind: str) -> bool:
    """Whether a catalog entry may be offered as an update to an installation of ``installed_kind``.

    A candidate of another kind is never an update: a source replacing a subtitle provider with a
    translator under the same id is not one. ``stage_install`` stages every kind with that kind's own
    rules, so an installation is offered updates of its own kind.
    """
    return catalog_kind_of(entry) == installed_kind


def _latest_catalog_update(
    state: dict[str, Any], provider_id: str, active_version: Any, source_id: str | None,
    catalog_list: str | None = None, installed_kind: str = SUBTITLE_PROVIDER,
) -> tuple[dict[str, Any], closure.Closure] | None:
    """The newest admissible update entry and the dependency closure that qualified it.

    An install updates only from its bound source and prefers the catalog list it came from.
    Keep the selected closure for consent and staging, since service bindings can change at any time.
    """
    if source_id is None or _install_refusal(state, source_id, provider_id, installed_kind):
        return None
    host = current_host()
    resolved_entries: dict[int, closure.Closure] = {}

    def usable(entry: dict[str, Any]) -> bool:
        if not _offerable_as_update(entry, installed_kind):
            return False
        # A revoked release is never offered; the newest one that is not revoked still is.
        if _revocation_refusal(state, provider_id, entry.get("version")):
            return False
        manifest = entry["manifest"]
        try:
            get_kind(catalog_kind_of(entry)).validate_manifest(manifest, bool(entry.get("trusted", False)))
        except ValueError:
            return False
        if entry.get("origin") == ORIGIN_BUNDLED or _host_refusal(entry, host) is not None:
            return False
        resolved = _closure_of(state, entry, host) if closure.has_needs(manifest) else closure.Closure()
        if not resolved.resolved or closure.broken_by(state, provider_id, manifest):
            return False
        resolved_entries[id(entry)] = resolved
        return True

    entry = select_entry(
        _stored_manifest_entries(state), provider_id,
        usable=usable, source_id=source_id, above_version=active_version, catalog_list=catalog_list)
    return (entry, resolved_entries[id(entry)]) if entry is not None else None


def _latest_catalog_manifest(
    state: dict[str, Any], provider_id: str, active_version: Any, source_id: str | None,
    catalog_list: str | None = None, installed_kind: str = SUBTITLE_PROVIDER,
) -> dict[str, Any] | None:
    update = _latest_catalog_update(state, provider_id, active_version, source_id, catalog_list, installed_kind)
    return update[0]["manifest"] if update is not None else None


def offered_update_version(provider_id: str) -> str | None:
    """The newer version the hub offers for an installed plugin, or None. Reads the stored catalog, changes nothing.

    It is the same answer an update check gives, so a page that shows an "update available" chip never
    names a version the Updates tab would not offer.
    """
    state = load_state()
    installation = (state.get("installations") or {}).get(provider_id)
    if not isinstance(installation, dict) or installation.get("origin") == "local":
        return None
    active_version = installation.get("active_version")
    if not active_version or installation.get("state") == "removed":
        return None
    latest = _latest_catalog_manifest(
        state, provider_id, active_version, _bound_source_id(installation), _installed_catalog_list(installation),
        installed_kind=kind_of(installation))
    version = latest.get("version") if isinstance(latest, dict) else None
    return str(version) if version else None


def installable_catalog_plugins(kind: str) -> list[dict[str, Any]]:
    """The newest installable entry of each catalog plugin of ``kind`` that is not installed or staged.

    It asks what the install path asks, with the same functions: the entry's source may supply its kind (a kind
    other than the subtitle provider only from the trusted official source, whose signed feed must be current),
    this host can run it, its kind accepts its manifest and its dependencies resolve. An entry any of those
    refuses is left out, so the page never starts an install the Hub
    would refuse. A trusted source is preferred when two list the same version. Consent is not a refusal: the
    install asks for it. Each item is the stored entry: ``source``, ``provider_id``, ``name``, ``version``,
    ``trusted`` and ``manifest``.
    """
    state = load_state()
    # A row that only records a failed install is not an installation, so the engine can be offered again.
    installed = {str(plugin_id) for plugin_id, row in (state.get("installations") or {}).items()
                 if isinstance(row, dict) and (row.get("active_version") or row.get("staged_version"))}
    host = current_host()
    entries = sorted((entry for entry in _stored_manifest_entries(state) if catalog_kind_of(entry) == kind),
                     key=lambda entry: not bool(entry.get("trusted", False)))
    entry_usable = _entry_usable(state, host)

    def usable(entry: dict[str, Any]) -> bool:
        return entry_usable(entry) and _closure_refusal(state, entry, host) is None

    found = []
    for provider_id in sorted({str(entry.get("provider_id") or "") for entry in entries} - installed - {""}):
        entry = select_entry(entries, provider_id, usable=usable)
        if entry is not None:
            found.append(entry)
    return found


def _refused_update_reason(
    state: dict[str, Any], provider_id: str, active_version: Any, source_id: str | None,
    catalog_list: str | None = None, installed_kind: str = SUBTITLE_PROVIDER,
) -> str | None:
    """The host refusal of the newest refused version ``source_id`` lists for ``provider_id``
    above ``active_version``, or None when no refused version is above it. Callers pass the
    offered version, or the installed version (the staged version when nothing is active) when none is offered."""
    if source_id is None or _install_refusal(state, source_id, provider_id, installed_kind):
        return None
    host = current_host()

    def refused(entry: dict[str, Any]) -> bool:
        return (
            entry.get("origin") != ORIGIN_BUNDLED
            and _offerable_as_update(entry, installed_kind)
            and (_revocation_refusal(state, provider_id, entry.get("version")) is not None
                 or _host_refusal(entry, host) is not None or _closure_refusal(state, entry, host) is not None
                 or bool(closure.broken_by(state, provider_id, entry["manifest"])))
        )

    entry = select_entry(
        _stored_manifest_entries(state), provider_id,
        usable=refused, source_id=source_id, above_version=active_version, catalog_list=catalog_list)
    if not entry:
        return None
    broken = closure.broken_by(state, provider_id, entry["manifest"])
    return (_revocation_refusal(state, provider_id, entry.get("version"), entry.get("name"), "offered as an update")
            or _host_refusal(entry, host) or _closure_refusal(state, entry, host)
            or (closure.breaks_text(provider_id, entry["manifest"].get("version"), broken) if broken else None))


def _update_delta(state: dict[str, Any], provider: dict[str, Any], entry: dict[str, Any],
                  resolved: closure.Closure, *, marks: bool = False) -> dict[str, Any] | None:
    """What updating ``provider`` to the selected entry with this closure adds.

    ``marks`` asks the sandbox probe which permissions this host enforces, which only a summary shown to
    a person needs; the digest and the decision do not.
    """
    # Legacy manifests cannot declare consent facts. A schema 2 update can add them even when it needs no plugins.
    if entry["manifest"].get("schema_version") == 1:
        return None
    return consent.update_delta(
        state, provider.get("manifest"), entry, resolved, consent.sandbox_status() if marks else None)


def update_consent(provider_id: str) -> dict[str, Any] | None:
    """The consent delta of the update ``apply_update`` would stage, or None for a legacy or unavailable update."""
    provider = get_provider(provider_id, redact=False)
    if not provider or provider.get("origin") == "local":
        return None
    state = load_state()
    source_id = _bound_source_id(provider)
    update = _latest_catalog_update(
        state, provider_id, provider.get("active_version") or provider.get("staged_version"), source_id,
        _installed_catalog_list(provider), installed_kind=kind_of(provider))
    return _update_delta(state, provider, *update, marks=True) if update is not None else None


def apply_update(provider_id: str, checkpoint=None, consent_digest: str | None = None) -> dict[str, Any] | None:
    provider = get_provider(provider_id, redact=False)
    if not provider:
        return None
    if provider.get("origin") == "local":
        # A local package is never replaced by a catalog update; ignore the request.
        return _redact_installation(provider)
    state = load_state()
    source_id = _bound_source_id(provider)
    catalog_list = _installed_catalog_list(provider)
    update = _latest_catalog_update(
        state,
        provider_id,
        provider.get("active_version") or provider.get("staged_version"),
        source_id,
        catalog_list,
        installed_kind=kind_of(provider),
    )
    if update is None:
        message = _refused_update_reason(
            state,
            provider_id,
            provider.get("active_version") or provider.get("staged_version"),
            source_id,
            catalog_list,
            installed_kind=kind_of(provider),
        ) or (_supply_refusal(state) if source_id == OFFICIAL_CATALOG_SOURCE_ID else None) \
            or "No update manifest is available"
        return _record_failed_update(provider_id, provider, message)
    entry, resolved = update
    # An update that adds a destination, a permission, a binary or a plugin asks again first.
    delta = _update_delta(state, provider, entry, resolved)
    if delta and delta["needs_consent"] and consent_digest != delta["digest"]:
        return _record_failed_update(provider_id, provider, consent.delta_text(delta))
    members: list[str] = []
    if len(resolved.members) > 1:
        # A plugin the new release needs is staged first, as an install does.
        try:
            members = _stage_closure_members(resolved, source_id, checkpoint)
        except ProviderHubInstallError as error:
            return _record_failed_update(provider_id, provider, refusal_text(error))
    try:
        return stage_install(entry["manifest"], checkpoint=checkpoint, source_id=source_id)
    except ProviderHubSettingsError:
        raise
    except ProviderHubInstallError:
        _discard_staged_members(members, provider_id)
        return get_provider(provider_id)
    except ManifestValidationError as error:
        # The install path refused the manifest before it started a job, so the refusal is
        # recorded here as a failed update, the way a missing manifest is.
        _discard_staged_members(members, provider_id)
        return _record_failed_update(provider_id, provider, refusal_text(error))
    except Exception:
        _discard_staged_members(members, provider_id)
        raise


def _record_failed_update(provider_id: str, provider: dict[str, Any], message: str) -> dict[str, Any]:
    """Record an update that could not be staged: the reason on the installation and a failed job."""
    with record_job(
        "stage_update",
        target_kind="provider",
        target_id=provider_id,
        target_name=provider.get("name") or provider_id,
        from_version=provider.get("active_version") or provider.get("staged_version"),
    ) as job:
        provider["last_error"] = message

        def record_failure(state: dict[str, Any]) -> None:
            state.setdefault("installations", {})[provider_id] = provider

        mutate_state(record_failure)
        job.data["state"] = "failed"
        job.update(
            error=message,
            message=message,
        )
    return _redact_installation(provider)


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def close_stale_jobs(now: datetime | None = None) -> int:
    """Mark as failed every job row that is still open although nothing can finish it.

    A row is left ``pending`` or ``running`` when the process that wrote it stopped, and nothing closes it: the
    page would show the plugin as busy for good. A row written by another run of Bazarr+ (or by a build that
    did not say which) is dead, and a row of this run that has not reported within ``JOB_TIME_LIMIT_SECONDS``
    is given up on. Returns how many were closed.
    """
    now = now or datetime.now(timezone.utc)
    limit = JOB_TIME_LIMIT_SECONDS

    def stale(job: Any) -> str | None:
        if not isinstance(job, dict) or job.get("state") not in ("pending", "running"):
            return None
        if job.get("session") != _PROCESS_SESSION:
            return JOB_STOPPED_MESSAGE
        reported = _parse_time(job.get("updated_at") or job.get("created_at"))
        if reported is not None:
            if reported.tzinfo is None:
                reported = reported.replace(tzinfo=timezone.utc)
            if (now - reported).total_seconds() > limit:
                return JOB_TIMED_OUT_MESSAGE
        return None

    if not any(stale(job) for job in load_state().get("jobs") or []):
        return 0
    closed: list[int] = []

    def close(state: dict[str, Any]) -> None:
        closed.clear()
        for job in state.get("jobs") or []:
            reason = stale(job)
            if reason:
                stamp = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                job.update(state="failed", error=reason, completed_at=stamp, updated_at=stamp)
                closed.append(1)

    mutate_state(close)
    return len(closed)


def list_jobs() -> list[dict[str, Any]]:
    try:
        close_stale_jobs()
    except Exception:
        logging.getLogger(__name__).warning("Could not close the job rows that were left open", exc_info=True)
    return list(load_state().get("jobs") or [])


def get_job(job_id: str) -> dict[str, Any] | None:
    for job in list_jobs():
        if job.get("id") == job_id:
            return job
    return None
