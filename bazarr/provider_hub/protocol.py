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


def worker_download_to_content(subtitle: HubWorkerSubtitle, payload: dict[str, Any]) -> bool:
    if payload.get("empty"):
        subtitle.content = b""
        return True

    content_b64 = payload.get("content_b64")
    if not isinstance(content_b64, str):
        raise WorkerProtocolError("download.content_b64 is required")

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
