# coding=utf-8
from __future__ import annotations

import base64
import hashlib

from typing import Any

from subzero.language import Language
from subliminal.video import Episode, Movie
from subliminal_patch.subtitle import Subtitle


class WorkerProtocolError(ValueError):
    """Raised when a provider worker response violates the V1 ABI."""


class HubWorkerSubtitle(Subtitle):
    provider_name = "providerhub"
    hash_verifiable = False
    hearing_impaired_verifiable = False

    def __init__(
        self,
        provider_name: str,
        source_provider: str,
        worker_id: str,
        language,
        provider_payload: dict[str, Any],
        **kwargs,
    ):
        super().__init__(language, worker_id, **kwargs)
        self.provider_name = provider_name
        self.source_provider = source_provider
        self.worker_id = str(worker_id)
        self.provider_payload = provider_payload
        self.score = None
        self.score_without_hash = None
        self.score_out_of = None

    @property
    def id(self):
        return f"{self.source_provider}:{self.worker_id}"

    @property
    def numeric_id(self):
        return self.worker_id

    def get_matches(self, video):
        return set(self.matches or set())


def language_to_payload(language) -> dict[str, Any]:
    return {
        "alpha3": getattr(language, "alpha3", None),
        "alpha2": getattr(language, "alpha2", None),
        "country_alpha2": getattr(getattr(language, "country", None), "alpha2", None),
        "script": str(getattr(language, "script", "") or "") or None,
        "basename": getattr(language, "basename", None),
        "ietf": getattr(language, "ietf", None),
        "hi": bool(getattr(language, "hi", False) or getattr(language, "hearing_impaired", False)),
        "forced": bool(getattr(language, "forced", False)),
    }


def language_from_payload(payload: dict[str, Any]):
    if not isinstance(payload, dict):
        raise WorkerProtocolError("language payload must be an object")

    alpha3 = payload.get("alpha3")
    if not alpha3:
        raise WorkerProtocolError("language.alpha3 is required")

    country = payload.get("country_alpha2")
    kwargs = {
        "hi": bool(payload.get("hi", False)),
        "forced": bool(payload.get("forced", False)),
    }
    return Language(str(alpha3), country, **kwargs)


def video_to_payload(video) -> dict[str, Any]:
    is_episode = isinstance(video, Episode)
    is_movie = isinstance(video, Movie)
    return {
        "kind": "episode" if is_episode else "movie" if is_movie else video.__class__.__name__.lower(),
        "name": getattr(video, "name", None),
        "original_path": getattr(video, "original_path", None),
        "original_name": getattr(video, "original_name", None),
        "title": getattr(video, "title", None),
        "series": getattr(video, "series", None),
        "episode_title": getattr(video, "episode_title", None),
        "year": getattr(video, "year", None),
        "season": getattr(video, "season", None),
        "episode": getattr(video, "episode", None),
        "absolute_episode": getattr(video, "absolute_episode", None),
        "source": getattr(video, "source", None),
        "release_group": getattr(video, "release_group", None),
        "resolution": getattr(video, "resolution", None),
        "streaming_service": getattr(video, "streaming_service", None),
        "video_codec": getattr(video, "video_codec", None),
        "audio_codec": getattr(video, "audio_codec", None),
        "fps": getattr(video, "fps", None),
        "duration": getattr(video, "duration", None),
        "size": getattr(video, "size", None),
        "hashes": dict(getattr(video, "hashes", {}) or {}),
        "imdb_id": getattr(video, "imdb_id", None),
        "series_imdb_id": getattr(video, "series_imdb_id", None),
        "tmdb_id": getattr(video, "tmdb_id", None),
        "tvdb_id": getattr(video, "tvdb_id", None),
        "series_tvdb_id": getattr(video, "series_tvdb_id", None),
        "series_anidb_id": getattr(video, "series_anidb_id", None),
        "series_anidb_series_id": getattr(video, "series_anidb_series_id", None),
        "series_anidb_episode_id": getattr(video, "series_anidb_episode_id", None),
        "series_anidb_episode_no": getattr(video, "series_anidb_episode_no", None),
        "series_anidb_season_episode_offset": getattr(video, "series_anidb_season_episode_offset", None),
        "info_url": getattr(video, "info_url", None),
        "edition": getattr(video, "edition", None),
        "other": list(getattr(video, "other", []) or []),
        "subtitle_languages": [
            language_to_payload(item)
            for item in getattr(video, "subtitle_languages", []) or []
        ],
        "audio_languages": [
            language_to_payload(item)
            for item in getattr(video, "audio_languages", []) or []
        ],
        "media_ids": {
            "radarrId": getattr(video, "radarrId", None),
            "sonarrSeriesId": getattr(video, "sonarrSeriesId", None),
            "sonarrEpisodeId": getattr(video, "sonarrEpisodeId", None),
        },
    }


def candidate_from_worker(provider_name: str, payload: dict[str, Any]) -> HubWorkerSubtitle:
    if not isinstance(payload, dict):
        raise WorkerProtocolError("candidate payload must be an object")

    language = language_from_payload(payload.get("language", {}))
    provider_payload = payload.get("provider_payload")
    if not isinstance(provider_payload, dict):
        raise WorkerProtocolError("candidate.provider_payload is required")

    source_provider = str(payload.get("provider") or provider_payload.get("provider") or provider_name)
    worker_id = str(payload.get("id") or "")
    if not worker_id:
        raise WorkerProtocolError("candidate.id is required")

    subtitle = HubWorkerSubtitle(
        provider_name=provider_name,
        source_provider=source_provider,
        worker_id=worker_id,
        language=language,
        provider_payload=provider_payload,
        hearing_impaired=bool(payload.get("hearing_impaired", getattr(language, "hi", False))),
        page_link=payload.get("page_link"),
    )
    subtitle.release_info = payload.get("release_info")
    subtitle.filename = payload.get("filename")
    subtitle.uploader = payload.get("uploader")
    subtitle.matches = set(payload.get("matches") or [])
    subtitle.score = payload.get("score")
    subtitle.score_without_hash = payload.get("score_without_hash")
    subtitle.score_out_of = payload.get("score_out_of")
    subtitle.hash_verifiable = bool(payload.get("hash_verifiable", False))
    subtitle.hearing_impaired_verifiable = bool(payload.get("hearing_impaired_verifiable", False))

    display = payload.get("display") or {}
    if isinstance(display, dict):
        for key, value in display.items():
            setattr(subtitle, key, value)

    return subtitle


_SUBTITLE_FORMATS = {"srt": "srt", "ass": "ass", "ssa": "ass", "vtt": "vtt", "sub": "sub"}


def _format_from_member(name: str | None) -> str | None:
    if not name or "." not in name:
        return None
    return _SUBTITLE_FORMATS.get(name.rsplit(".", 1)[-1].lower())


def worker_download_to_content(subtitle: HubWorkerSubtitle, payload: dict[str, Any]) -> bool:
    if payload.get("empty"):
        subtitle.content = b""
        return True

    archive_b64 = payload.get("archive_b64")
    if isinstance(archive_b64, str):
        return _worker_archive_to_content(subtitle, payload, archive_b64)

    content_b64 = payload.get("content_b64")
    if not isinstance(content_b64, str):
        raise WorkerProtocolError("download.content_b64 or download.archive_b64 is required")

    content = base64.b64decode(content_b64.encode("ascii"), validate=True)
    expected_hash = payload.get("content_sha256")
    if expected_hash:
        actual = hashlib.sha256(content).hexdigest()
        if actual != str(expected_hash).lower():
            raise WorkerProtocolError("download.content_sha256 mismatch")

    subtitle.content = content
    subtitle.format = payload.get("format") or getattr(subtitle, "format", "srt")
    subtitle.encoding = payload.get("encoding") or getattr(subtitle, "encoding", None)
    return True


# Hard caps so a worker, or the untrusted site it fetched the archive from, cannot OOM
# the host with a decompression bomb or an oversized response. Real subtitle archives
# are a few KB; these limits are deliberately generous.
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 64 * 1024 * 1024
# Real subtitle archives hold a handful of files; cap the member count so a 32 MB zip
# packed with hundreds of thousands of zero-byte entries cannot DoS the host (the byte
# caps above never trip on empty members). Mirrors service.py's _LOCAL_PACKAGE_MAX_MEMBERS.
_MAX_ARCHIVE_MEMBERS = 5000


def _guard_archive_members(archive) -> None:
    """Reject decompression bombs by checking declared uncompressed sizes up front."""
    try:
        infos = archive.infolist()
    except Exception:  # pragma: no cover - exotic archive objects
        return
    # Cap the entry count before the O(N) scan below (and the later namelist()/extract):
    # an archive can stay under the byte caps while carrying a pathological member count.
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise WorkerProtocolError("download.archive_b64 contains too many members")
    total = 0
    for info in infos:
        # zip/rar expose file_size; py7zr exposes uncompressed.
        size = int(getattr(info, "file_size", 0) or getattr(info, "uncompressed", 0) or 0)
        if size > _MAX_MEMBER_BYTES:
            raise WorkerProtocolError("download.archive_b64 member exceeds the size limit")
        total += size
        if total > _MAX_ARCHIVE_TOTAL_BYTES:
            raise WorkerProtocolError("download.archive_b64 decompresses past the size limit")


_SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"


class _SevenZipArchive:
    """A minimal zip/rar-like view over py7zr so get_subtitle_from_archive and the bomb
    guard work on 7z archives unchanged. py7zr (a bazarr host dependency) extracts to
    disk, so read() extracts one member into a temp dir and returns its bytes."""

    def __init__(self, data: bytes):
        self._data = data

    def _open(self):
        import io as _io

        import py7zr

        return py7zr.SevenZipFile(_io.BytesIO(self._data))

    def namelist(self):
        with self._open() as archive:
            return archive.getnames()

    def infolist(self):
        with self._open() as archive:
            return archive.list()

    def read(self, name: str) -> bytes:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self._open() as archive:
                archive.extract(path=tmp, targets=[name])
            # py7zr preserves the archived file's mode, which can drop the owner read
            # bit; make directories traversable and files readable before reading.
            for root, dirs, files in os.walk(tmp):
                for directory in dirs:
                    try:
                        os.chmod(os.path.join(root, directory), 0o700)
                    except OSError:  # pragma: no cover
                        pass
                for filename in files:
                    path = os.path.join(root, filename)
                    try:
                        os.chmod(path, 0o600)
                    except OSError:  # pragma: no cover
                        pass
                    with open(path, "rb") as handle:
                        return handle.read()
        raise WorkerProtocolError(f"download.archive_b64 7z member could not be read: {name}")


def _seven_zip_archive(raw: bytes):
    if not raw.startswith(_SEVEN_ZIP_MAGIC):
        return None
    try:
        import py7zr  # noqa: F401
    except Exception:  # pragma: no cover - py7zr is a declared host dependency
        return None
    return _SevenZipArchive(raw)


def _worker_archive_to_content(
    subtitle: HubWorkerSubtitle, payload: dict[str, Any], archive_b64: str
) -> bool:
    """Extract a subtitle from an archive the worker handed back.

    The worker returns the raw zip/rar bytes and, when it chose one during search,
    the member name. Bazarr extracts with its own rarfile/zipfile stack, so provider
    bundles never carry an archive binary (no py7zz), and the encoding is left to
    Subtitle.normalize() rather than being guessed inside the worker.
    """
    from subliminal.subtitle import fix_line_ending
    from subliminal_patch.providers.utils import (
        get_archive_from_bytes,
        get_subtitle_from_archive,
    )

    if len(archive_b64) > _MAX_ARCHIVE_BYTES * 4 // 3 + 16:
        raise WorkerProtocolError("download.archive_b64 exceeds the maximum archive size")
    raw = base64.b64decode(archive_b64.encode("ascii"), validate=True)
    if len(raw) > _MAX_ARCHIVE_BYTES:
        raise WorkerProtocolError("download.archive_b64 exceeds the maximum archive size")
    expected_hash = payload.get("archive_sha256")
    if expected_hash:
        if hashlib.sha256(raw).hexdigest() != str(expected_hash).lower():
            raise WorkerProtocolError("download.archive_sha256 mismatch")

    archive = get_archive_from_bytes(raw)
    if archive is None:
        archive = _seven_zip_archive(raw)
    if archive is None:
        raise WorkerProtocolError("download.archive_b64 is not a zip, rar, or 7z archive")
    _guard_archive_members(archive)

    member = payload.get("member")
    if member:
        if member not in set(archive.namelist()):
            raise WorkerProtocolError(f"download.member is not in the archive: {member}")
        content = fix_line_ending(archive.read(member))
        chosen = member
    else:
        forced = bool(getattr(getattr(subtitle, "language", None), "forced", False))
        episode = payload.get("episode")
        if isinstance(episode, (list, tuple)):
            episode = episode[0] if episode else None
        content = get_subtitle_from_archive(
            archive,
            forced=forced,
            episode=episode,
            episode_title=payload.get("episode_title"),
            get_first_subtitle=bool(payload.get("first_subtitle")),
            extensions=(".srt", ".sub", ".ssa", ".ass", ".vtt"),
        )
        if content is None:
            raise WorkerProtocolError("download.archive_b64 has no usable subtitle member")
        chosen = payload.get("filename") or ""

    subtitle.content = content
    subtitle.format = payload.get("format") or _format_from_member(chosen) or getattr(subtitle, "format", "srt")
    # Clear any encoding carried over from search (e.g. via the display attribute splat)
    # so Subtitle.normalize()/chardet detects it from the extracted bytes, the same as
    # native subliminal providers; do not let a stale or worker-supplied guess pin it.
    subtitle.encoding = None
    subtitle._guessed_encoding = None
    return True
