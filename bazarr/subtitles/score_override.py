# coding=utf-8
"""Manual score for one listed subtitle file.

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

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.orm import aliased

from app.database import (TableEpisodes, TableHistory, TableHistoryMovie, TableHistorySports, TableMovies,
                          TableSportsEvents)
from app.event_handler import event_stream
from arr_instances.resolution import scoped
from subtitles.wanted.utils import FILE_REPLACING_ACTIONS
from subliminal_patch.score import MAX_SCORES

# History action for a user-assigned score on an existing subtitle file.
MANUAL_SCORE_ACTION = 9


def manual_score_percent(score, score_out_of):
    return round(score * 100 / score_out_of)


def sports_history_path(path):
    """History and the Sportarr index can spell the same Windows path differently."""
    return func.replace(path, '\\', '/')


def newer_sports_file_history(actions):
    """A newer history row for the same sports subtitle file."""
    history = TableHistorySports
    newer = aliased(history)
    return select(newer.id).where(
        newer.action.in_(actions),
        newer.arr_instance_id == history.arr_instance_id,
        newer.event_id == history.event_id,
        newer.language == history.language,
        sports_history_path(newer.video_path) == sports_history_path(history.video_path),
        sports_history_path(newer.subtitles_path) == sports_history_path(history.subtitles_path),
        or_(newer.timestamp > history.timestamp,
            (newer.timestamp == history.timestamp) & (newer.id > history.id)),
    ).exists()


def add_sports_score_overrides(session, events):
    """Attach the current manual scores to indexed files in this event page."""
    by_id = {(event['arr_instance_id'], event['id']): event for event in events if event['subtitles']}
    if not by_id:
        return
    history = TableHistorySports
    rows = session.execute(
        select(history.arr_instance_id, history.event_id, sports_history_path(history.video_path).label('video_path'),
               history.language, sports_history_path(history.subtitles_path).label('subtitles_path'),
               history.score, history.score_out_of)
        .where(history.arr_instance_id.in_({owner for owner, _ in by_id}),
               history.event_id.in_({event_id for _, event_id in by_id}),
               history.action == MANUAL_SCORE_ACTION,
               history.score.is_not(None), history.score_out_of > 0,
               ~newer_sports_file_history(FILE_REPLACING_ACTIONS + (MANUAL_SCORE_ACTION,)))
    )
    for row in rows:
        event = by_id.get((row.arr_instance_id, row.event_id))
        if event is None or event['path'].replace('\\', '/') != row.video_path:
            continue
        for subtitle in event['subtitles']:
            if (subtitle and len(subtitle) >= 2 and subtitle[1]
                    and (subtitle[0], subtitle[1].replace('\\', '/')) == (row.language, row.subtitles_path)):
                event.setdefault('manual_subtitle_scores', {})[subtitle[1]] = manual_score_percent(
                    row.score, row.score_out_of)


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


def _set_sports_score_override(database, event_id, subtitles_path, score_percent, arr_instance_id):
    from sportarr.db import sports_transaction
    from sportarr.errors import SportsNotFound
    from sportarr.identity import resolve_event_in_session
    from sportarr.sync.leagues import require_sportarr
    from utilities.path_mappings import apply_sports_mapping, read_sports_mappings

    with sports_transaction(database) as session:
        context = resolve_event_in_session(session, event_id, arr_instance_id)
        # Event sync and publication both lock the owner before the event.
        instance = require_sportarr(session, context.arr_instance_id)
        row = session.execute(
            select(TableSportsEvents.subtitles)
            .where(TableSportsEvents.id == context.event_id,
                   TableSportsEvents.arr_instance_id == context.arr_instance_id,
                   TableSportsEvents.league_id == context.league_id,
                   TableSportsEvents.file_id == context.file_id,
                   TableSportsEvents.path == context.original_path)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise SportsNotFound("Sports event not found for this owner")

        try:
            language = _listed_language(row.subtitles, subtitles_path)
        except ValueError:
            subtitles_path = apply_sports_mapping(
                subtitles_path, read_sports_mappings(instance.path_mappings), reverse=True)
            try:
                language = _listed_language(row.subtitles, subtitles_path)
            except ValueError as exc:
                raise SportsNotFound("Subtitle is not listed for this sports event") from exc

        full = MAX_SCORES["movie"]
        if session.get_bind().dialect.name == 'postgresql':
            # Publish a new row version with the pin. An upgrade whose serializable
            # snapshot predates the pin must fail its event lock before writing bytes.
            session.execute(update(TableSportsEvents).where(
                TableSportsEvents.id == context.event_id,
                TableSportsEvents.arr_instance_id == context.arr_instance_id,
            ).values(subtitles=TableSportsEvents.subtitles))
        session.execute(
            insert(TableHistorySports).values(
                event_id=context.event_id,
                league_id=context.league_id,
                arr_instance_id=context.arr_instance_id,
                action=MANUAL_SCORE_ACTION,
                description="Score set manually.",
                language=language,
                subtitles_path=subtitles_path,
                video_path=context.original_path,
                score=round(full * score_percent / 100),
                score_out_of=full,
                timestamp=datetime.now(),
            )
        )
    database.commit()
    event_stream(type="sports", payload=context.event_id)


def set_score_override(
    database, media, item_id, subtitles_path, score_percent, arr_instance_id=None
):
    """Pin ``score_percent`` (0 to 100) on one listed subtitle.

    Movies and episodes use upstream ids. Sports uses the local event id,
    which also identifies the owner when ``arr_instance_id`` is omitted.
    ``subtitles_path`` must match a path in the item's stored subtitles listing.
    """
    if media not in ("movie", "episode", "sports"):
        raise ValueError(f"unknown media type: {media}")
    if not 0 <= score_percent <= 100:
        raise ValueError("score must be a percentage from 0 to 100")
    if not subtitles_path:
        raise ValueError("a subtitles path is required")

    if media == "sports":
        _set_sports_score_override(database, item_id, subtitles_path, score_percent, arr_instance_id)
        return

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
    event_stream(type="movie-history" if media == "movie" else "episode-history")
