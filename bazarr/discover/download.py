"""Bounded shared SRT bytes and literal cue previews for exact Discover results."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import logging
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
# A download job is not holding a request open, so it can give a slow provider
# longer than a preview can.
JOB_WAIT_SECONDS = 60
# How long a finished download job's file stays available to save.
TICKET_TTL_SECONDS = 30 * 60
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


FAILURE_MESSAGES = {
    "expired_handle": "This result has expired. Search again for this selection.",
    "provider_error": "The provider did not return a subtitle. Retry or choose another result.",
    "archive_no_match": "The provider's archive has no subtitle for this language or episode. Choose another result.",
    "invalid_subtitle": "The provider returned a file that is not a usable subtitle. Choose another result.",
    "timeout": "The provider took too long to answer. Retry in a moment.",
}


class DownloadFailure(ValueError):
    """A classified reason why no subtitle could be delivered.

    ``detail`` is the underlying error text. It is for the log only and never
    leaves the process in a response.
    """

    def __init__(self, reason, detail=""):
        super().__init__(FAILURE_MESSAGES[reason])
        self.reason = reason
        self.detail = str(detail)[:500]

    @property
    def message(self):
        return FAILURE_MESSAGES[self.reason]


def classify_failure(error):
    """Map any error from the download path onto one fixed reason."""
    if isinstance(error, DownloadFailure):
        return error
    if isinstance(error, (ExpiredResultError, UnauthorizedResultError)):
        return DownloadFailure("expired_handle", error)
    if isinstance(error, TimeoutError):
        return DownloadFailure("timeout", error)
    return DownloadFailure("provider_error", error)


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
    srt = _drop_empty_cues(text if parsed.format == "srt" else parsed.to_string("srt")).encode("utf-8")
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


def _blocks(text):
    return re.split(r"\n[ \t]*\n+", text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip("\n"))


def _is_empty_cue(block):
    """A numbered timing line with no text under it, which real files carry."""
    lines = block.split("\n")
    if lines and lines[0].strip().isdigit():
        lines.pop(0)
    return (len(lines) >= 1 and _TIMING.fullmatch(lines[0]) is not None
            and not "\n".join(lines[1:]).strip())


def _drop_empty_cues(text):
    """Remove cues that have timing but no text; leave everything else as it was.

    One empty cue used to reject a whole file. Any other malformed block still
    does, in _cues. The text is only rebuilt when something was dropped, so a
    clean file keeps its exact bytes.
    """
    blocks = _blocks(text)
    kept = [block for block in blocks if not _is_empty_cue(block)]
    if len(kept) == len(blocks):
        return text
    return "\n\n".join(kept) + "\n\n" if kept else ""


def _cues(content):
    cues, total, characters = [], 0, 0
    truncated = False
    for block in _blocks(content.decode("utf-8")):
        if _is_empty_cue(block):
            continue
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


def _provider_bytes(subtitle):
    try:
        content = service._fetch_subtitle_bytes(subtitle)
    except Exception as error:
        raise DownloadFailure("timeout" if isinstance(error, TimeoutError) else "provider_error", error) from error
    if not content:
        # The pool reports a candidate it rejected, which for Provider Hub is an
        # archive without a member for this language or episode, only as an
        # empty result. It leaves the reason on the subtitle.
        rejected = getattr(subtitle, "rejected_reason", None)
        if rejected:
            raise DownloadFailure("archive_no_match", rejected)
        # Any other provider exception is swallowed the same way, and kept.
        error = getattr(subtitle, "download_error", None)
        if error is not None:
            import requests
            timed_out = isinstance(error, (TimeoutError, requests.Timeout))
            raise DownloadFailure("timeout" if timed_out else "provider_error", f"{type(error).__name__}: {error}")
        raise DownloadFailure("provider_error", "provider returned no subtitle content")
    try:
        return _normalize(content)
    except Exception as error:
        # pysubs2 raises its own exception types for text that is no subtitle.
        raise DownloadFailure("invalid_subtitle", error) from error


def _fetch(key, flight, record, result_id, search_id, authority, deadline):
    global _cache_bytes
    try:
        _record(result_id, search_id, authority)
        # The provider mutates content and redirect fields. Separate credential scopes
        # and result handles must never race through the same mutable instance.
        subtitle = deepcopy(record["subtitle"])
        artifact = _Artifact(_provider_bytes(subtitle), _filename(record))
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
        # A fixed classification for every caller. The underlying text rides
        # along as detail for the log and is never put in a response.
        flight.error = (ExpiredResultError("Discover result expired") if isinstance(error, ExpiredResultError)
                        else UnauthorizedResultError("Authentication changed") if isinstance(error, UnauthorizedResultError)
                        else classify_failure(error))
    finally:
        with _lock:
            _inflight.pop(key, None)
            flight.ready.set()


def _artifact(result_id, search_id, authority, wait_seconds=None):
    deadline = time.monotonic() + (FETCH_WAIT_SECONDS if wait_seconds is None else wait_seconds)
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


def _job_label(record):
    context = record["context"]
    subtitle = record["subtitle"]
    title = context.get("query") if context.get("mode") == "release" else (context.get("title") or context.get("imdb_id"))
    if context.get("media_type") == "episode" and context.get("season") is not None:
        title = f"{title} S{context['season']:02d}E{context['episode']:02d}"
    parts = (title, context.get("language"), getattr(subtitle, "provider_name", None), _filename(record))
    return " · ".join(str(part) for part in parts if part)


def _ticket_key(scope, job_id):
    return (scope, "job", int(job_id))


def _existing_job(result_id, search_id, scope):
    from app.jobs_queue import jobs_queue
    for status in ("pending", "running"):
        for job in jobs_queue.list_jobs_from_queue(status=status):
            kwargs = job.get("kwargs") or {}
            if (job.get("module") == __name__ and job.get("func") == "run_download_job"
                    and kwargs.get("result_id") == result_id and kwargs.get("search_id") == search_id
                    and getattr(kwargs.get("authority"), "scope", None) == scope):
                return job["job_id"]
    return None


def enqueue_download(result_id: str, search_id: str, *, authority: ResultAuthority) -> int:
    """Validate the exact result now and queue its retrieval as a standard job.

    Returns the job id, which is also the ticket the finished file is fetched
    by. Clicking the same result while its job is still queued or running
    answers with that job rather than queueing a second one.
    """
    from app.jobs_queue import jobs_queue
    record = _record(result_id, search_id, authority)
    _require_current_copy(record["context"])
    job_id = jobs_queue.feed_jobs_pending_queue(
        job_name=_job_label(record), module=__name__, func="run_download_job",
        kwargs={"result_id": result_id, "search_id": search_id, "authority": authority,
                "provider": getattr(record["subtitle"], "provider_name", None), "filename": _filename(record)},
        retryable=True)
    if not job_id:
        job_id = _existing_job(result_id, search_id, authority.scope)
    if not job_id:
        raise TimeoutError("Subtitle retrieval busy")
    return job_id


def _store_ticket(job_id, scope, artifact):
    global _cache_bytes
    key = _ticket_key(scope, job_id)
    with _locked(time.monotonic() + FETCH_WAIT_SECONDS):
        _prune(scope)
        if key in _cache:
            _evict(key)
        while _cache and (len(_cache) >= CACHE_MAX_ENTRIES or _cache_bytes + len(artifact.content) > CACHE_MAX_BYTES):
            _evict(next(iter(_cache)))
        _cache[key] = (time.monotonic() + TICKET_TTL_SECONDS, artifact)
        _cache_bytes += len(artifact.content)


def fetch_ticket(job_id, *, authority: ResultAuthority) -> tuple[bytes, str]:
    """The file a finished download job produced, under the key that asked for it."""
    if not authority.is_current():
        raise UnauthorizedResultError("Authentication changed")
    with _locked(time.monotonic() + FETCH_WAIT_SECONDS):
        _prune(authority.scope)
        cached = _cache.get(_ticket_key(authority.scope, job_id))
    if cached is None:
        raise ExpiredResultError("Discover download expired")
    return cached[1].content, cached[1].filename


def run_download_job(result_id, search_id, authority, provider=None, filename=None, job_id=None):
    """The job body: fetch the exact result and keep its file for the ticket.

    It re-runs every handle, scope and copy check itself, because the reader's
    key or the result may have changed while the job waited in the queue.
    ``provider`` and ``filename`` are what the result was when it was queued,
    so the log can name it even when the handle has expired since.
    """
    from app.jobs_queue import JobFailed, jobs_queue
    try:
        artifact = _artifact(result_id, search_id, authority, wait_seconds=JOB_WAIT_SECONDS)
        if not authority.is_current():
            raise UnauthorizedResultError("Authentication changed")
        _store_ticket(job_id, authority.scope, artifact)
    except Exception as error:
        failure = classify_failure(error)
        logging.warning("Discover download failed: reason=%s provider=%s filename=%s: %s",
                        failure.reason, provider, filename, failure.detail or type(error).__name__,
                        exc_info=logging.getLogger().isEnabledFor(logging.DEBUG))
        # A retry repeats the same checks, so it cannot bring an expired result back.
        raise JobFailed(failure.message, reason=failure.reason, retryable=failure.reason != "expired_handle",
                        returned_value={"ticket": None, "filename": filename, "reason": failure.reason,
                                        "message": failure.message}) from None
    jobs_queue.update_job_progress(job_id=job_id, progress_message="Ready to save", allow_cancelled=True)
    jobs_queue.set_job_action(job_id, {"kind": "discover.save", "label": "Save", "ticket": job_id,
                                       "filename": artifact.filename})
    return {"ticket": job_id, "filename": artifact.filename, "size": len(artifact.content)}


def preview_result(result_id: str, search_id: str, *, authority: ResultAuthority) -> dict:
    """Project bounded literal cues from exactly the bytes offered by download."""
    artifact = _artifact(result_id, search_id, authority)
    cues, total, truncated = _cues(artifact.content)
    _record(result_id, search_id, authority)
    return {"result_id": result_id, "search_id": search_id, "filename": artifact.filename,
            "cues": cues, "total_cues": total, "truncated": truncated}
