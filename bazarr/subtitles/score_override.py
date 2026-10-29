# coding=utf-8
"""Manual score for one listed subtitle file of a movie or episode.

The score is stored as a history row of action MANUAL_SCORE_ACTION that names
the subtitle file. The score filter reads that row as the newest record for the
file, so it replaces the provider score. The upgrade job leaves the subtitle
alone while such a row is at least as new as the candidate row for it, so the
pinned file is neither searched for nor downloaded again. A later download of
the file writes a newer row and releases the pin.

The score is kept on the full scale of the media type, the same scale the
history writers use, so every reader that divides by score_out_of or by
MAX_SCORES agrees on it.
"""

import ast
from datetime import datetime

from sqlalchemy import insert, select

from app.database import TableEpisodes, TableHistory, TableHistoryMovie, TableMovies
from app.event_handler import event_stream
from arr_instances.resolution import scoped
from subliminal_patch.score import MAX_SCORES

# Not a provider action and not a translation: only the score filter and the
# upgrade pin read rows of this action.
MANUAL_SCORE_ACTION = 9


def _listed_language(listing, subtitles_path):
    """The language code the subtitles column lists for ``subtitles_path``."""
    try:
        entries = ast.literal_eval(listing) if listing else []
    except (ValueError, SyntaxError):
        entries = []
    for entry in entries:
        # An embedded track is listed with no path, so only a real path counts.
        if entry and len(entry) >= 2 and entry[1] and entry[1] == subtitles_path:
            return entry[0]
    raise ValueError(f"{subtitles_path} is not a subtitle listed for this media")


def set_score_override(
    database, media, item_id, subtitles_path, score_percent, arr_instance_id=None
):
    """Pin ``score_percent`` (0 to 100) on one listed subtitle.

    ``media`` is 'movie' (``item_id`` is the Radarr movie id) or 'episode'
    (``item_id`` is the Sonarr episode id). ``arr_instance_id`` names the
    owning instance, since upstream ids repeat across instances; None keeps
    the unscoped lookup. ``subtitles_path`` must be a non-empty path matching
    a path in the item's stored subtitles listing exactly.
    """
    if media not in ("movie", "episode"):
        raise ValueError(f"unknown media type: {media}")
    if not 0 <= score_percent <= 100:
        raise ValueError("score must be a percentage from 0 to 100")
    if not subtitles_path:
        raise ValueError("a subtitles path is required")

    if media == "movie":
        item = database.execute(
            scoped(
                select(
                    TableMovies.id,
                    TableMovies.path,
                    TableMovies.arr_instance_id,
                    TableMovies.subtitles,
                ).where(TableMovies.radarrId == item_id),
                TableMovies.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if item is None:
            raise LookupError(f"no movie with radarr id {item_id}")
        language = _listed_language(item.subtitles, subtitles_path)
        full = MAX_SCORES["movie"]
        database.execute(
            insert(TableHistoryMovie).values(
                movie_id=item.id,
                radarrId=item_id,
                arr_instance_id=item.arr_instance_id,
                action=MANUAL_SCORE_ACTION,
                description="Score set manually.",
                language=language,
                provider=None,
                subtitles_path=subtitles_path,
                video_path=item.path,
                score=round(full * score_percent / 100),
                score_out_of=full,
                timestamp=datetime.now(),
            )
        )
    else:
        item = database.execute(
            scoped(
                select(
                    TableEpisodes.id,
                    TableEpisodes.series_id,
                    TableEpisodes.sonarrSeriesId,
                    TableEpisodes.path,
                    TableEpisodes.arr_instance_id,
                    TableEpisodes.subtitles,
                ).where(TableEpisodes.sonarrEpisodeId == item_id),
                TableEpisodes.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if item is None:
            raise LookupError(f"no episode with sonarr episode id {item_id}")
        language = _listed_language(item.subtitles, subtitles_path)
        full = MAX_SCORES["episode"]
        database.execute(
            insert(TableHistory).values(
                series_id=item.series_id,
                episode_id=item.id,
                sonarrSeriesId=item.sonarrSeriesId,
                sonarrEpisodeId=item_id,
                arr_instance_id=item.arr_instance_id,
                action=MANUAL_SCORE_ACTION,
                description="Score set manually.",
                language=language,
                provider=None,
                subtitles_path=subtitles_path,
                video_path=item.path,
                score=round(full * score_percent / 100),
                score_out_of=full,
                timestamp=datetime.now(),
            )
        )
    database.commit()
    event_stream(type='movie-history' if media == 'movie' else 'episode-history')
