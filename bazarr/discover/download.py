"""Bounded shared SRT bytes and literal cue previews for exact Discover results."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import re
from threading import Event, Lock, Thread
import time
from typing import Callable

import pysubs2
from werkzeug.utils import secure_filename

from compat import service
from compat.local_subs import _decode_subtitle_bytes
from .handles import resolve_result

CACHE_TTL_SECONDS = 120
CACHE_MAX_ENTRIES = 64
CACHE_MAX_BYTES = 16 * 1024 * 1024
SUBTITLE_MAX_BYTES = 2 * 1024 * 1024
FETCH_MAX_CONCURRENT = 4
FETCH_WAIT_SECONDS = 12
PREVIEW_MAX_CUES = 40
PREVIEW_MAX_CHARACTERS = 24000
# One filesystem name, in bytes. ext4, APFS and SMB all stop here.
FILENAME_MAX_BYTES = 255
# How stale the entry validation of a chosen copy may be when bytes are handed
# over without a second look. Acquiring the cache lock is bounded by
# FETCH_WAIT_SECONDS, not by nothing, so this is the number that makes the
# guarantee concrete rather than an assumption about how fast a lock is.
COPY_RECHECK_AFTER_SECONDS = 1


class ExpiredResultError(FileNotFoundError):
    """The exact result can no longer be delivered.

    Two distinct causes reach the client as the same recoverable 410. The
    capability itself no longer resolves: an unusable handle, or a record that
    expired or was evicted. Or the capability resolves perfectly well and the
    chosen library copy underneath it does not, which is what
    _require_current_copy raises for. Recovery is the same either way, a fresh
    search for the same selection, which is why they share an error rather than
    each having one.
    """


class UnauthorizedResultError(PermissionError):
    """The request's captured application authority is no longer current."""


@dataclass(frozen=True)
class ResultAuthority:
    scope: bytes = field(repr=False)
    is_current: Callable[[], bool] = field(repr=False, compare=False)


@dataclass(frozen=True)
class _Artifact:
    content: bytes
    filename: str


@dataclass
class _Flight:
    ready: Event = field(default_factory=Event)
    artifact: _Artifact | None = None
    error: Exception | None = None


_cache: OrderedDict = OrderedDict()
_inflight: dict = {}
_lock = Lock()
_cache_bytes = 0


def _record(result_id, search_id, authority):
    if not authority.is_current():
        raise UnauthorizedResultError("Authentication changed")
    if not isinstance(search_id, str) or not search_id or len(search_id) > 128:
        raise ExpiredResultError("Discover result expired")
    record = resolve_result(result_id, search_id)
    if record is None or record["expires_at"] <= time.time():
        raise ExpiredResultError("Discover result expired")
    return record


@contextmanager
def _locked(deadline):
    if not _lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise TimeoutError("Subtitle retrieval timed out")
    try:
        yield
    finally:
        _lock.release()


def _evict(key):
    global _cache_bytes
    entry = _cache.pop(key)
    _cache_bytes -= len(entry[1].content)


def _prune(scope):
    now = time.monotonic()
    for key, (expiry, _) in list(_cache.items()):
        if key[0] != scope or expiry <= now:
            _evict(key)


def _normalize(content):
    if not content or len(content) > SUBTITLE_MAX_BYTES:
        raise ValueError("Empty or oversized subtitle")
    text = _decode_subtitle_bytes(content)
    parsed = pysubs2.SSAFile.from_string(text)
    if not parsed.events or not any(event.plaintext.strip() for event in parsed):
        raise ValueError("Subtitle has no dialogue")
    if any(event.start < 0 or event.end <= event.start for event in parsed):
        raise ValueError("Invalid subtitle timing")
    srt = text.encode("utf-8") if parsed.format == "srt" else parsed.to_string("srt").encode("utf-8")
    if len(srt) > SUBTITLE_MAX_BYTES:
        raise ValueError("Oversized normalized subtitle")
    # Validate the promised SRT representation as well as the source format.
    _cues(srt)
    return srt


def _filename(record):
    """Name the artifact after the result being saved, not after its search.

    Two results for one target differ by their own release, provider and
    forced/full identity, so the file the reader ends up with has to carry all
    three. A chosen library copy is search context and deliberately contributes
    nothing to this name.
    """
    subtitle = record["subtitle"]
    context = record["context"]
    if context.get("mode") == "release":
        title = secure_filename(context["query"])[:120].strip("._") or "release-query"
    else:
        title = secure_filename(context.get("title") or context["imdb_id"])[:120].strip("._")
        title = title or context["imdb_id"]
        if context["media_type"] == "episode":
            title += f".S{context['season']:02d}E{context['episode']:02d}"
        elif context.get("year"):
            title += f".{context['year']}"
    language = secure_filename(context["language"])
    forced = getattr(subtitle, "_reported_forced", None)
    scope = ".forced" if forced is True else ".full" if forced is False else ""
    release = secure_filename(getattr(subtitle, "release_info", None) or "")[:120].strip("._")
    provider = secure_filename(getattr(subtitle, "provider_name", None) or "")[:60].strip("._")
    # A release name usually already carries the title and the episode or year.
    # Repeating them reads as a bug in the saved file, so keep the identity
    # prefix only when the release does not already state it.
    if release and _tokens(title) <= _tokens(release):
        title = ""
    tail = f"{language}{scope}.srt"
    fixed = len(".".join(part for part in (title, provider, tail) if part).encode())
    # Every part above is already capped, but their sum is not: 120 plus 120
    # plus 60 plus the tail overflows the 255 bytes every common filesystem
    # allows for one name. The release is the part that yields, because the
    # identity prefix and the provider are what tell two saved files apart at a
    # glance, while a shortened release still reads as the same release.
    release = _clip(release, FILENAME_MAX_BYTES - fixed - 1)
    parts = [part for part in (title, release, provider) if part] + [tail]
    return ".".join(parts)


def _clip(text, budget):
    """Trim to a byte budget, leaving neither a split character nor a trailing separator."""
    if budget <= 0:
        return ""
    encoded = text.encode()
    if len(encoded) <= budget:
        return text
    return encoded[:budget].decode(errors="ignore").rstrip("._")


def _tokens(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _require_current_copy(context):
    """Refuse delivery when the chosen copy is no longer the one that was searched.

    A minted handle freezes its context, so a copy that changed after the search
    would otherwise keep serving bytes retrieved for a different physical
    revision. Refusal happens through exactly two mechanisms, and no others.

    The copy no longer resolves at all, which resolve_copy refuses with a named
    reason: a malformed identity, an unknown owner, no matching row for that
    local id and owner within this target, a missing instance, no recorded file,
    or a mapped path that cannot be stat-ed or is not a regular file.

    Or it resolves and its revision digest differs. What that digest can see,
    and every case in which it sees nothing, is stated in full by
    discover.library._physical_revision.

    All three of the obvious cases refuse, but by three different routes, so do
    not collapse them. A deleted file never reaches the digest: os.stat raises
    and resolve_copy refuses with copy_unavailable, which is the first mechanism
    above, not this one. A moved file whose row was updated is caught because
    the stored path and the mapped path are both digest inputs, so the digest
    changes even if the bytes did not. Only a replacement in place turns on size
    and modification time, and only there does the content hash matter: a
    same-size timestamp-preserving replacement is seen just while a hash is
    present and the change touches the hashed first or last 64 KiB.
    """
    if not context.get("copy_id"):
        return
    from .library import resolve_copy
    try:
        facts = resolve_copy(context["copy_id"], context)
    except ValueError:
        raise ExpiredResultError("Discover result expired") from None
    if facts["file_revision"] != context.get("file_revision"):
        raise ExpiredResultError("Discover result expired")


_TIMING = re.compile(
    r"^[ \t]*(\d+):(\d{2}):(\d{2})[,.](\d{3})[ \t]*-->[ \t]*"
    r"(\d+):(\d{2}):(\d{2})[,.](\d{3})"
    r"(?:[ \t]+X1:\d+[ \t]+X2:\d+[ \t]+Y1:\d+[ \t]+Y2:\d+)?[ \t]*$"
)


def _cues(content):
    text = content.decode("utf-8").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues, total, characters = [], 0, 0
    truncated = False
    for block in re.split(r"\n[ \t]*\n+", text.strip("\n")):
        lines = block.split("\n")
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        timing = _TIMING.fullmatch(lines.pop(0)) if lines else None
        if timing is None or not lines:
            raise ValueError("Invalid SRT cue")
        values = [int(value) for value in timing.groups()]
        if any(values[index] >= 60 for index in (1, 2, 5, 6)):
            raise ValueError("Invalid SRT timestamp")
        start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
        end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
        if end <= start:
            raise ValueError("Invalid SRT timing")
        literal = "\n".join(lines)
        if not literal.strip():
            raise ValueError("Empty SRT cue")
        total += 1
        if len(cues) < PREVIEW_MAX_CUES and characters < PREVIEW_MAX_CHARACTERS:
            visible = literal[:PREVIEW_MAX_CHARACTERS - characters]
            cues.append({"start_ms": start, "end_ms": end, "text": visible})
            characters += len(visible)
            truncated = truncated or len(visible) < len(literal)
        else:
            truncated = True
    if not cues:
        raise ValueError("No bounded preview dialogue")
    return cues, total, truncated


def _fetch(key, flight, record, result_id, search_id, authority, deadline):
    global _cache_bytes
    try:
        _record(result_id, search_id, authority)
        # The provider mutates content and redirect fields. Separate credential scopes
        # and result handles must never race through the same mutable instance.
        subtitle = deepcopy(record["subtitle"])
        artifact = _Artifact(_normalize(service._fetch_subtitle_bytes(subtitle)), _filename(record))
        with _locked(deadline):
            current = _record(result_id, search_id, authority)
            if time.monotonic() >= deadline:
                raise TimeoutError("Subtitle retrieval timed out")
            _prune(authority.scope)
            while _cache and (len(_cache) >= CACHE_MAX_ENTRIES or _cache_bytes + len(artifact.content) > CACHE_MAX_BYTES):
                _evict(next(iter(_cache)))
            if len(artifact.content) <= CACHE_MAX_BYTES:
                _cache[key] = (min(time.monotonic() + CACHE_TTL_SECONDS,
                                   time.monotonic() + current["expires_at"] - time.time()), artifact)
                _cache_bytes += len(artifact.content)
            flight.artifact = artifact
    except Exception as error:
        # Keep only a fixed error classification, never provider payloads/tracebacks.
        flight.error = (ExpiredResultError("Discover result expired") if isinstance(error, ExpiredResultError)
                        else UnauthorizedResultError("Authentication changed") if isinstance(error, UnauthorizedResultError)
                        else ValueError("No usable subtitle returned"))
    finally:
        with _lock:
            _inflight.pop(key, None)
            flight.ready.set()


def _artifact(result_id, search_id, authority):
    deadline = time.monotonic() + FETCH_WAIT_SECONDS
    record = _record(result_id, search_id, authority)
    _require_current_copy(record["context"])
    validated = time.monotonic()
    context = hashlib.sha256(json.dumps(record["context"], sort_keys=True, separators=(",", ":")).encode()).digest()
    key = (authority.scope, result_id, search_id, context, record["subtitle"].provider_name)
    with _locked(deadline):
        _record(result_id, search_id, authority)
        if time.monotonic() >= deadline:
            raise TimeoutError("Subtitle retrieval timed out")
        _prune(authority.scope)
        cached = _cache.get(key)
        if cached:
            _cache.move_to_end(key)
            artifact = cached[1]
            flight = None
        else:
            flight = _inflight.get(key)
            if flight is None:
                if len(_inflight) >= FETCH_MAX_CONCURRENT:
                    raise TimeoutError("Subtitle retrieval busy")
                flight = _Flight()
                _inflight[key] = flight
                Thread(target=_fetch, args=(key, flight, record, result_id, search_id, authority, deadline),
                       daemon=True, name="discover-subtitle").start()
    if flight is not None:
        if not flight.ready.wait(max(0, deadline - time.monotonic())):
            _record(result_id, search_id, authority)
            raise TimeoutError("Subtitle retrieval timed out")
        _record(result_id, search_id, authority)
        if flight.error:
            raise flight.error
        artifact = flight.artifact
    _record(result_id, search_id, authority)
    # Three cases, and the third is the one worth naming rather than leaving
    # between the other two.
    #
    #   1. A fetch was involved, whether this request started it or joined one
    #      already in flight. Re-observe: that wait is unbounded by anything
    #      smaller than the deadline.
    #   2. A cache hit more than COPY_RECHECK_AFTER_SECONDS after the entry
    #      validation, which is what a contended cache lock produces.
    #      Re-observe.
    #   3. A cache hit within COPY_RECHECK_AFTER_SECONDS of the entry
    #      validation. No second look. The guarantee this leaves is exact:
    #      bytes are delivered against a validation at most that old.
    #
    # This is a per-request decision taken from this request's own clock, not a
    # memo: nothing here survives the call.
    if flight is not None or time.monotonic() - validated > COPY_RECHECK_AFTER_SECONDS:
        _require_current_copy(record["context"])
    return artifact


def download_result(result_id: str, search_id: str, *, authority: ResultAuthority) -> tuple[bytes, str]:
    """Return the exact guarded normalized SRT and device filename. No library writes."""
    artifact = _artifact(result_id, search_id, authority)
    return artifact.content, artifact.filename


def preview_result(result_id: str, search_id: str, *, authority: ResultAuthority) -> dict:
    """Project bounded literal cues from exactly the bytes offered by download."""
    artifact = _artifact(result_id, search_id, authority)
    cues, total, truncated = _cues(artifact.content)
    _record(result_id, search_id, authority)
    return {"result_id": result_id, "search_id": search_id, "filename": artifact.filename,
            "cues": cues, "total_cues": total, "truncated": truncated}
