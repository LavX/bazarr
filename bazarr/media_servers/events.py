# coding=utf-8
"""Actual subtitle publications, expressed in Bazarr-visible paths."""

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import os
import stat


@dataclass(frozen=True)
class SubtitleMutation:
    media_type: str
    video_path: str
    subtitle_path: str
    operation: str
    arr_instance_id: int | None = None


def notify_subtitle_mutation(event: SubtitleMutation) -> None:
    # Publication must remain successful even if a refresh cannot be queued.
    from .dispatcher import notify_subtitle_mutation as notify
    notify(event)


def publication_callback(media_type, video_path, operation, arr_instance_id=None):
    media_type = {'series': 'episode', 'movies': 'movie'}.get(media_type, media_type)

    def published(subtitle_path):
        notify_subtitle_mutation(SubtitleMutation(media_type, video_path, str(subtitle_path),
                                                  operation, arr_instance_id))
    return published


def _subtitle_fingerprint(path):
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            # Bound each read and the total work to the file's initial size.
            # Include content because scripts can preserve timestamps and size.
            remaining = info.st_size + 1
            while remaining:
                chunk = handle.read(min(remaining, 256 * 1024))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        return info.st_ino, info.st_mtime_ns, info.st_size, digest.digest()
    except OSError:
        return None


@contextmanager
def observe_subtitle_change(media_type, video_path, subtitle_path, operation, arr_instance_id=None):
    """Account for a configured postprocessor that may rewrite or remove this file."""
    before = _subtitle_fingerprint(subtitle_path)
    try:
        yield
    finally:
        if before != _subtitle_fingerprint(subtitle_path):
            publication_callback(media_type, video_path, operation, arr_instance_id)(subtitle_path)
