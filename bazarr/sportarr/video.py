"""Movie-compatible provider input for an explicitly owned sports event file."""

import os

from subliminal import Movie
from subzero.video import parse_video

from app.config import settings
from app.database import TableSportsEvents
from sportarr.connection import check_cancelled
from sportarr.subtitles import candidate_signature, validate_context
from subtitles.indexer.sports import parse_sports_video_metadata
from subtitles.refiners.ffprobe import refine_from_metadata


def get_sports_video(path, title, scene_name, providers, context, cancel=None):
    from app.database import database

    check_cancelled(cancel)
    validate_context(context)
    if path != context.mapped_path:
        raise ValueError("Sports provider path does not match its local event")
    signature = candidate_signature(context)
    hints = {"title": title, "type": "movie"}
    video = parse_video(
        path,
        hints=hints,
        skip_hashing=settings.general.skip_hashing,
        dry_run=False,
        providers=providers,
    )
    if not isinstance(video, Movie):
        raise ValueError("Sports provider input must be Movie-compatible")
    # Filename parsing supplies release metadata and selective real-file hashes.
    # The event's own title/date supply descriptive sports metadata, never movie IDs.
    row = database.get(TableSportsEvents, context.event_id, populate_existing=True)
    video.title = row.title
    date = row.eventDate or row.broadcastDate
    if date and str(date)[:4].isdigit():
        video.year = int(str(date)[:4])
    video.imdb_id = None
    video.size = os.path.getsize(path)
    video.original_path = path
    video.arr_instance_id = context.arr_instance_id
    video.sports_context = context
    data = parse_sports_video_metadata(
        context.event_id, context.arr_instance_id, file=path, cancel=cancel
    )
    refine_from_metadata(video, data)
    parser = data.get("ffprobe") or data.get("mediainfo") or {}
    duration = parser.get("duration")
    if duration is not None:
        video.duration = (
            duration.total_seconds() if hasattr(duration, "total_seconds") else duration
        )
    if candidate_signature(context) != signature:
        raise ValueError("Sports file changed during provider preparation")
    check_cancelled(cancel)
    return video
