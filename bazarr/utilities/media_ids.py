# coding=utf-8
"""Resolve LOCAL media ids from upstream Sonarr/Radarr ids (#156).

The frontend caches episode/movie detail by the LOCAL id (the canonical PK),
while many post-action ``event_stream`` emits only have the upstream
``sonarrEpisodeId`` / ``radarrId`` in scope. In multi-instance the upstream id
is no longer globally unique, so emitting it would invalidate the wrong (or no)
cached row. These helpers resolve the LOCAL id for the OWNING instance with a
single scoped SELECT, and fall back to the upstream id only when no local row
matches (legacy-safe: on a single-default install local == upstream).

These are per-item, post-action lookups, never tight loops, so one SELECT is
fine. ``arr_instance_id`` None is a no-op scope (the default/single-instance
path), so the query stays byte-identical to the legacy behaviour.
"""
import logging


def local_episode_id(sonarr_episode_id, arr_instance_id=None):
    """Return the LOCAL ``table_episodes.id`` for an upstream sonarrEpisodeId,
    scoped to the owning instance. Falls back to ``sonarr_episode_id`` when no
    local row matches (so a payload is always emitted)."""
    if sonarr_episode_id is None:
        return sonarr_episode_id
    try:
        from app.database import TableEpisodes, database, select
        from arr_instances.resolution import scoped

        row = database.execute(
            scoped(select(TableEpisodes.id)
                   .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id),
                   TableEpisodes.arr_instance_id, arr_instance_id)).first()
    except Exception:
        logging.debug('BAZARR could not resolve local episode id for upstream %s',
                      sonarr_episode_id)
        return sonarr_episode_id
    return row.id if row is not None else sonarr_episode_id


def local_movie_id(radarr_id, arr_instance_id=None):
    """Return the LOCAL ``table_movies.id`` for an upstream radarrId, scoped to
    the owning instance. Falls back to ``radarr_id`` when no local row matches."""
    if radarr_id is None:
        return radarr_id
    try:
        from app.database import TableMovies, database, select
        from arr_instances.resolution import scoped

        row = database.execute(
            scoped(select(TableMovies.id)
                   .where(TableMovies.radarrId == radarr_id),
                   TableMovies.arr_instance_id, arr_instance_id)).first()
    except Exception:
        logging.debug('BAZARR could not resolve local movie id for upstream %s', radarr_id)
        return radarr_id
    return row.id if row is not None else radarr_id
