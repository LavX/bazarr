# coding=utf-8
"""Detect subtitles that exist only for another release type.

For smaller subtitle communities an episode or movie is often only ever
synchronised to one release type. The downloader picks a release with no
knowledge of subtitle availability, so it can grab a web release when the only
subtitles that exist are cut for the Blu-ray. The search then comes back empty
even though a perfectly good subtitle is sitting there for the other release.

This module looks at the candidates the search ALREADY listed and rejected. It
issues no provider request of its own: the caller hands it the scored-candidate
record that ``SZProviderPool.download_best_subtitles`` fills in while it is
picking a winner.

Notification volume is the design constraint. A detector that fires on every
slightly-off release becomes noise the user turns off, at which point it
protects nothing. Every rule below is therefore biased towards silence:

* Anything that cleared the minimum score suppresses the detection outright.
  If an acceptable subtitle existed and still was not downloaded, the reason is
  not the release type.
* A candidate whose release description does not yield a release type is
  unknown, never evidence that another release type exists.
* Generated-subtitle providers are excluded: a transcription says nothing about
  what the communities have released.
* Release types are bucketed exactly the way the scorer buckets them, through
  subliminal_patch's MERGED_FORMATS. A difference the score never punished is
  not a mismatch.
* The alternative must clear the threshold on the strength of the release-type
  points alone. Resolution, release group and codec differences are never
  credited, so "would have cleared" means exactly that and nothing more.
* A detection is persisted, so a repeated scheduled pass stays quiet.
* The caller skips upgrade searches entirely: they raise the minimum score
  above the score of the subtitle the user already has.
"""

import logging

from datetime import datetime
from typing import NamedTuple, Optional

from guessit import guessit
from sqlalchemy.exc import IntegrityError

from subliminal_patch.score import DEFAULT_SCORES
from subliminal_patch.subtitle import MERGED_FORMATS_REV

from app.config import settings
from app.database import (TableEpisodes, TableMovies, TableReleaseTypeMismatch, database, delete,
                          insert, select)
from app.event_handler import event_stream
from app.notifier import send_notifications, send_notifications_movie
from arr_instances.resolution import scoped
from utilities.path_mappings import path_mappings
from utilities.sql_limits import MAX_IN_CLAUSE, in_chunks

logger = logging.getLogger(__name__)

# Stored where the owning instance is unknown, instead of NULL. The unique index
# is what keeps two concurrent searches from both notifying, and NULL never
# equals NULL, so a nullable owner column silently opts those rows out of it.
# 0 is not a real instance id: they are assigned from 1.
UNOWNED = 0


# Providers that synthesise a subtitle rather than distributing a released one.
# Their release description is generated text, so it can neither be evidence of
# a release type nor a victim of one.
GENERATED_SUBTITLE_PROVIDERS = frozenset({'whisperai'})



class ReleaseTypeMismatch(NamedTuple):
    """One rejected candidate that would have been acceptable if only it had
    been cut for the release the user actually holds."""

    video_release_type: str
    subtitle_release_type: str
    provider_name: str
    release_info: str
    score: int
    projected_score: int


def source_score(media_type):
    """Points the scorer awards for a release-type ('source') match."""
    kind = 'episode' if media_type == 'series' else 'movie'
    return DEFAULT_SCORES[kind]['source']


def normalize_release_type(value):
    """Display form of one guessit ``source`` value: what the user is told."""
    if not value:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def release_type_group(value):
    """Comparison key for a release type, using the scorer's own equivalence.

    ``guess_matches`` awards the 'source' match through ``MERGED_FORMATS``, so
    Blu-ray, Ultra HD Blu-ray and HD-DVD are one release type to it, as are HDTV
    and SDTV. Bucketing any finer here would report items the scorer never
    penalised and would credit release-type points the score already contains.
    A source the map does not know keeps its own name, which pairs it only with
    itself.
    """
    normalized = normalize_release_type(value)
    if normalized is None:
        return None
    return MERGED_FORMATS_REV.get(normalized, normalized)


def parse_release_type(release_info, media_type):
    """Release type named by a subtitle's release description, or None.

    None means "unknown", which the detector treats as no evidence at all. A
    description that names more than one release type (providers routinely join
    several release names with newlines) is ambiguous and therefore unknown too.
    """
    if not release_info:
        return None

    hints = {'type': 'episode' if media_type == 'series' else 'movie'}
    found = {}

    for line in str(release_info).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            guess = guessit(line, hints)
        except Exception:  # guessit throws on hostile input
            logger.debug('BAZARR could not parse the release description %r', line)
            continue
        source = guess.get('source')
        for candidate in (source if isinstance(source, list) else [source]):
            normalized = normalize_release_type(candidate)
            if normalized:
                found.setdefault(release_type_group(normalized), normalized)

    if len(found) != 1:
        return None
    return next(iter(found.values()))


def detect_release_type_mismatch(video_release_type, candidates, min_score,
                                 media_type) -> Optional[ReleaseTypeMismatch]:
    """Report the best candidate that only the release type kept out.

    ``candidates`` are the scored-candidate records of one already-completed
    search: mappings with ``provider_name``, ``release_info``, ``score`` and
    ``downloaded``.
    """
    video_type = normalize_release_type(video_release_type)
    if not video_type:
        # Nothing to compare against. Never guess.
        return None
    video_group = release_type_group(video_type)

    release_type_points = source_score(media_type)
    if release_type_points <= 0:
        return None

    candidates = list(candidates or ())

    for candidate in candidates:
        if candidate.get('downloaded'):
            # The user got a subtitle. Whatever else was listed does not matter.
            return None
        if int(candidate.get('score') or 0) >= min_score:
            # Something was acceptable and still was not downloaded (hearing
            # impaired preference, a series check, a failed download). That is
            # not a release-type problem, so stay quiet.
            return None

    best = None
    for candidate in candidates:
        provider_name = candidate.get('provider_name') or ''
        if provider_name in GENERATED_SUBTITLE_PROVIDERS:
            continue

        release_info = candidate.get('release_info')
        subtitle_type = parse_release_type(release_info, media_type)
        if subtitle_type is None or release_type_group(subtitle_type) == video_group:
            continue

        # The projection promises the points guess_matches would award once the
        # item is re-grabbed as this release type. It awards 'source' only when
        # both sides map through MERGED_FORMATS: a source the map does not know
        # leaves the video side None and the candidate side a literal string,
        # which never compare equal. So for a type outside the map the points
        # can never be earned, and adding them would advertise a regrab that
        # changes nothing.
        if normalize_release_type(subtitle_type) not in MERGED_FORMATS_REV:
            continue

        score = int(candidate.get('score') or 0)
        projected_score = score + release_type_points
        if projected_score < min_score:
            continue

        if not _would_be_downloadable(candidate, media_type):
            continue

        if best is None or projected_score > best.projected_score:
            best = ReleaseTypeMismatch(
                video_release_type=video_type,
                subtitle_release_type=subtitle_type,
                provider_name=provider_name,
                release_info=release_info,
                score=score,
                projected_score=projected_score,
            )

    return best



def _would_be_downloadable(candidate, media_type):
    """False when the download loop would refuse this candidate anyway.

    Score is not the only gate. For an episode the loop separately requires the
    season and episode to match, plus the series or the imdb id, so a
    wrong-episode subtitle sitting just under the threshold would still be
    refused after a release-type regrab. Reporting it promises the user a fix
    that would not work.

    A candidate carrying no recorded matches is judged on score alone: that is
    what older records look like, and treating them as ineligible would turn
    the detector off wherever the record has not caught up.
    """
    if media_type != 'series':
        return True

    matches = candidate.get('matches')
    if not matches:
        return True

    matches = set(matches)
    return ({'season', 'episode'}.issubset(matches)
            and ('series' in matches or 'imdb_id' in matches))


# --------------------------------------------------------------------------
# Recording and notifying. Everything below runs only when the user turned the
# detection on, and only after a search that came back empty.
# --------------------------------------------------------------------------

MAX_STORED_RELEASE_INFO = 512


def _language_key(language):
    """Stable string form of the searched language, used as record identity.

    str(Language) carries the forced suffix but not the hi flag, so it would
    collapse a plain search and a hearing-impaired one for the same language:
    a recorded English mismatch would then stand in for an English HI search
    and suppress its notification. The flag is appended explicitly.
    """
    key = str(language)
    if getattr(language, 'hi', False):
        key += ':hi'
    return key


def _language_label(language):
    """Human name of the searched language for the notification body."""
    name = getattr(language, 'name', None)
    return name if isinstance(name, str) and name else str(language)


def _resolve_media_id_by_path(media_type, video, arr_instance_id):
    """Local id from the video's path, for a video with no upstream id set.

    The path is what the indexer stores on the media row, so it identifies the
    item even when the refiner could not. Reversed through the owning instance's
    mapping, which is the whole reason the refiner missed it.
    """
    path = getattr(video, 'original_path', None) or getattr(video, 'name', None)
    if not path:
        return None

    table = TableEpisodes if media_type == 'series' else TableMovies
    kind = 'series' if media_type == 'series' else 'movie'
    try:
        stored = path_mappings.path_replace_reverse_instance(path, arr_instance_id, kind)
    except Exception:
        logger.debug('BAZARR could not reverse %s for instance %s', path, arr_instance_id,
                     exc_info=True)
        stored = path

    rows = database.execute(
        scoped(select(table.id).where(table.path == stored),
               table.arr_instance_id, arr_instance_id)).all()
    if len(rows) != 1:
        logger.debug('BAZARR release-type mismatch: %s path %s resolved to %s rows, skipping',
                     media_type, stored, len(rows))
        return None
    return rows[0].id


def _resolve_media_id(media_type, video, arr_instance_id):
    """Local id (#156) of the media row the video belongs to, or None.

    Upstream ids are not unique across arr instances, so an unscoped lookup can
    land on the wrong instance's row. When the owner cannot be pinned down the
    detection is dropped rather than recorded against a guess.
    """
    if media_type == 'series':
        upstream_id = getattr(video, 'sonarrEpisodeId', None)
        if upstream_id is None:
            # The database refiner is what sets these, and it reverses the video
            # path through the GLOBAL mapping, so on an instance with a mapping
            # of its own it can find no row and leave them unset. The path still
            # identifies the item, and it is what the indexer stores.
            return _resolve_media_id_by_path(media_type, video, arr_instance_id)
        stmt = scoped(
            select(TableEpisodes.id)
            .where(TableEpisodes.sonarrEpisodeId == upstream_id),
            TableEpisodes.arr_instance_id, arr_instance_id)
    else:
        upstream_id = getattr(video, 'radarrId', None)
        if upstream_id is None:
            return _resolve_media_id_by_path(media_type, video, arr_instance_id)
        stmt = scoped(
            select(TableMovies.id)
            .where(TableMovies.radarrId == upstream_id),
            TableMovies.arr_instance_id, arr_instance_id)

    rows = database.execute(stmt).all()
    if len(rows) != 1:
        logger.debug('BAZARR release-type mismatch: %s upstream id %s resolved to %s rows, '
                     'skipping', media_type, upstream_id, len(rows))
        return None
    return rows[0].id


def record_mismatch(session, media_type, media_id, arr_instance_id, language, mismatch):
    """Store one detection. Returns True only the first time it is seen.

    The identity is the item, its owning instance, the searched language and the
    release type the item itself is. A later pass over the same wanted item
    therefore records nothing and notifies nothing, while a re-grab as another
    release type is a new situation and may be reported once more.
    """
    owner = UNOWNED if arr_instance_id is None else arr_instance_id
    existing = (
        select(TableReleaseTypeMismatch.video_release_type)
        .where(TableReleaseTypeMismatch.media_type == media_type)
        .where(TableReleaseTypeMismatch.media_id == media_id)
        .where(TableReleaseTypeMismatch.language == language)
    )
    if owner == UNOWNED:
        # Rows written before the sentinel existed still carry NULL, so match
        # both spellings of "no known owner".
        existing = existing.where(
            (TableReleaseTypeMismatch.arr_instance_id == UNOWNED)
            | (TableReleaseTypeMismatch.arr_instance_id.is_(None)))
    else:
        existing = existing.where(TableReleaseTypeMismatch.arr_instance_id == owner)

    # Compared by group, not by name: a re-grab that lands on another spelling
    # of the same release type (Blu-ray to Ultra HD Blu-ray) changed nothing for
    # the user and must not produce a second notification. The unique index is
    # the backstop for the exact-name case only.
    recorded_group = release_type_group(mismatch.video_release_type)
    for row in session.execute(existing).all():
        if release_type_group(row.video_release_type) == recorded_group:
            return False

    release_info = (mismatch.release_info or '')[:MAX_STORED_RELEASE_INFO] or None
    try:
        session.execute(insert(TableReleaseTypeMismatch).values(
            media_type=media_type,
            media_id=media_id,
            arr_instance_id=owner,
            language=language,
            video_release_type=mismatch.video_release_type,
            subtitle_release_type=mismatch.subtitle_release_type,
            provider=mismatch.provider_name or None,
            release_info=release_info,
            score=mismatch.score,
            detected_at=datetime.now(),
        ))
    except IntegrityError:
        # A concurrent pass recorded it first. The unique index is the backstop
        # for the check above, and losing the race means someone else notified.
        logger.debug('BAZARR release-type mismatch already recorded concurrently')
        return False
    return True


def clear_mismatch(session, media_type, media_id, language=None):
    """Drop recorded mismatches for an item, optionally just one language.

    Called when a subtitle actually lands, because at that point the recorded
    detection describes a problem the user no longer has. Without this the badge
    outlives the mismatch: the item stays flagged for a language that has since
    been satisfied, which is worse than not flagging it at all, since the user
    cannot tell the stale flags from the live ones.
    """
    stmt = (delete(TableReleaseTypeMismatch)
            .where(TableReleaseTypeMismatch.media_type == media_type)
            .where(TableReleaseTypeMismatch.media_id == media_id))
    if language is not None:
        stmt = stmt.where(TableReleaseTypeMismatch.language == _language_key(language))

    session.execute(stmt)


def clear_mismatch_for_video(video, media_type, language, arr_instance_id=None):
    """Clear the recorded mismatch for a video that just got its subtitle.

    Resolves the same local id the reporter records against, so a language that
    has been satisfied stops being flagged. Never raises: this runs on the
    success path of a download and must not turn a working search into an error.
    """
    try:
        if arr_instance_id is None:
            arr_instance_id = getattr(video, 'arr_instance_id', None)
        media_id = _resolve_media_id(media_type, video, arr_instance_id)
        if media_id is None:
            return

        clear_mismatch(database, media_type, media_id, language)
    except Exception:
        logger.exception('BAZARR could not clear the release-type mismatch record')


def _notification_body(language_label, mismatch):
    return (f'No {language_label} subtitle reached the minimum score for this '
            f'{mismatch.video_release_type} release, but {mismatch.provider_name} has one for a '
            f'{mismatch.subtitle_release_type} release that would. Grabbing the '
            f'{mismatch.subtitle_release_type} release would likely give you a subtitle.')


def report_release_type_mismatch(video, media_type, language, candidates, min_score,
                                 arr_instance_id=None):
    """Detect, record and notify once. Returns the mismatch when it notified.

    Called after a search that downloaded nothing for ``language``, with the
    candidates that search already scored. It issues no provider request.
    """
    if not settings.general.detect_release_type_mismatch:
        return None

    mismatch = detect_release_type_mismatch(
        video_release_type=getattr(video, 'source', None),
        candidates=candidates,
        min_score=min_score,
        media_type=media_type,
    )
    if mismatch is None:
        return None

    # The caller's value wins. video.arr_instance_id is set by the database
    # refiner, which reverses the path through the GLOBAL mapping and so can
    # fail to find the row for an instance with a mapping of its own, while
    # generate_subtitles already knows which instance it is searching for.
    if arr_instance_id is None:
        arr_instance_id = getattr(video, 'arr_instance_id', None)
    media_id = _resolve_media_id(media_type, video, arr_instance_id)
    if media_id is None:
        return None

    if not record_mismatch(database, media_type, media_id, arr_instance_id,
                           _language_key(language), mismatch):
        return None

    body = _notification_body(_language_label(language), mismatch)
    logger.info('BAZARR %s', body)
    try:
        if media_type == 'series':
            send_notifications(getattr(video, 'sonarrSeriesId', None),
                               getattr(video, 'sonarrEpisodeId', None), body,
                               arr_instance_id=arr_instance_id)
        else:
            send_notifications_movie(getattr(video, 'radarrId', None), body,
                                     arr_instance_id=arr_instance_id)
    except Exception:  # a notifier must never break a search
        logger.exception('BAZARR could not send the release-type mismatch notification')

    # The Wanted pagination query refreshes only from this event: it has no
    # polling and no refetch on focus, so without it a page that is already open
    # shows the new badge on the next manual reload and not before.
    try:
        event_stream(type='episode-wanted' if media_type == 'series' else 'movie-wanted',
                     action='update', payload=media_id)
    except Exception:
        logger.exception('BAZARR could not announce the release-type mismatch')

    return mismatch


def forget_media(session, media_type, media_ids):
    """Drop every recorded mismatch for media that no longer exists.

    Called when a sync removes the media row. The link is a plain integer, not
    a foreign key, because the column is polymorphic across two tables, so
    nothing removes these rows on its own. They do not merely accumulate:
    SQLite reuses a deleted row id for a later insert, so an orphan can badge
    an unrelated new item.
    """
    media_ids = [media_id for media_id in media_ids if media_id is not None]
    if not media_ids:
        return

    for chunk in _in_chunks(media_ids):
        session.execute(
            delete(TableReleaseTypeMismatch)
            .where(TableReleaseTypeMismatch.media_type == media_type)
            .where(TableReleaseTypeMismatch.media_id.in_(chunk)))


def forget_media_by_upstream(media_type, upstream_ids, arr_instance_id=None,
                             series_upstream_id=None):
    """Forget the mismatches of media about to be deleted, by upstream id.

    Called from the sync paths, which know an item by its upstream id, just
    before they remove the row. Never raises: losing a cleanup must not turn a
    successful sync delete into an error, and the rows it leaves behind are the
    status quo.

    ``series_upstream_id`` forgets every episode of one series instead, which is
    what a series deletion needs: the records are per episode.
    """
    try:
        upstream_ids = [i for i in (upstream_ids or []) if i is not None]
        if not upstream_ids and series_upstream_id is None:
            return

        if media_type == 'series':
            stmt = select(TableEpisodes.id)
            stmt = (stmt.where(TableEpisodes.sonarrSeriesId == int(series_upstream_id))
                    if series_upstream_id is not None
                    else stmt.where(TableEpisodes.sonarrEpisodeId.in_(upstream_ids)))
            stmt = scoped(stmt, TableEpisodes.arr_instance_id, arr_instance_id)
        else:
            stmt = scoped(select(TableMovies.id).where(TableMovies.radarrId.in_(upstream_ids)),
                          TableMovies.arr_instance_id, arr_instance_id)

        forget_media(database, media_type, [row.id for row in database.execute(stmt).all()])
    except Exception:
        logger.exception('BAZARR could not forget the release-type mismatches of deleted media')


# One page of Wanted may legitimately be 1000 rows, and SQLite builds carrying
# the legacy limit reject a statement binding more than 999 variables with "too
# many SQL variables". The ceiling and the splitting live in utilities.sql_limits
# so every caller that binds one variable per row respects the same number.
_MAX_IN_CLAUSE = MAX_IN_CLAUSE
_in_chunks = in_chunks


def flagged_media_ids(session, media_type, media_ids):
    """Local ids, out of ``media_ids``, that carry a recorded mismatch.

    One query per wanted page rather than one per row. Instance-agnostic on
    purpose: ``media_id`` is the local id, which is already unique across
    instances, and the caller has a page of them in hand.
    """
    media_ids = [media_id for media_id in media_ids if media_id is not None]
    if not media_ids:
        return set()

    flagged = set()
    for chunk in _in_chunks(media_ids):
        rows = session.execute(
            select(TableReleaseTypeMismatch.media_id)
            .where(TableReleaseTypeMismatch.media_type == media_type)
            .where(TableReleaseTypeMismatch.media_id.in_(chunk))).all()
        flagged.update(row.media_id for row in rows)

    return flagged
