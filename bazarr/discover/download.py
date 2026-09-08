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


class ExpiredResultError(FileNotFoundError):
    """The exact result capability no longer resolves."""


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
    return f"{title}.{language}{scope}.srt"


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
