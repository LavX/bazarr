# coding=utf-8
"""Hide and unhide embedded subtitle tracks, per media row.

Delete on an embedded row means Hide: Bazarr+ never rewrites a media file, so
the track stays in the container and Bazarr+ stops listing it for that title.
Rows are addressed by their LOCAL id, which is unique across instances; the
owner, when the caller sends one, must match the row.
"""

import ast
import logging
import os

from flask_restx import Namespace, Resource, reqparse

from app.database import TableEpisodes, TableMovies, database, select
from app.event_handler import event_stream
from languages.get_languages import language_from_alpha2
from subtitles.embedded_hidden import (MEDIA_TYPES, applicable_owners, episode_file_siblings, hide_track,
                                       language_key, list_hidden, media_owners, unhide_track)
from utilities.path_mappings import path_mappings

from ..utils import authenticate

api_ns_embedded_hidden = Namespace('Embedded Hidden Tracks',
                                   description='Hide or unhide embedded subtitle tracks per media item')

_TRUE = ('true', 'True', True)


class _Refused(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


def _media(media_type, media_id, arr_instance_id):
    """(original_path, mapped_path, owner, stored_subtitles, event_ids) for the row.

    ``event_ids`` is what the page listens for: (radarrId,) for a movie,
    (sonarrSeriesId, sonarrEpisodeId) for an episode, (local id,) for sports.
    """
    if media_type == 'sports':
        from sportarr.errors import SportsNotFound
        from sportarr.identity import resolve_event_in_session
        from sportarr.workflows import require_sports_enabled
        from app.database import TableSportsEvents
        try:
            require_sports_enabled()
            context = resolve_event_in_session(database, media_id, arr_instance_id)
        except SportsNotFound as exc:
            raise _Refused(str(exc), 404)
        except ValueError as exc:
            raise _Refused(str(exc), 400)
        stored = database.execute(select(TableSportsEvents.subtitles)
                                  .where(TableSportsEvents.id == context.event_id)).scalar()
        return context.original_path, context.mapped_path, context.arr_instance_id, stored, (context.event_id,)

    table = TableMovies if media_type == 'movie' else TableEpisodes
    columns = [table.path, table.arr_instance_id, table.subtitles]
    columns += [TableMovies.radarrId] if media_type == 'movie' else [TableEpisodes.sonarrSeriesId,
                                                                     TableEpisodes.sonarrEpisodeId]
    row = database.execute(select(*columns).where(table.id == media_id)).first()
    if row is None or (arr_instance_id is not None and row.arr_instance_id != arr_instance_id):
        raise _Refused(f'{media_type.capitalize()} not found for this owner', 404)
    mapped = path_mappings.path_replace_instance(row.path, row.arr_instance_id,
                                                'movie' if media_type == 'movie' else 'series')
    ids = (row.radarrId,) if media_type == 'movie' else (row.sonarrSeriesId, row.sonarrEpisodeId)
    return row.path, mapped, row.arr_instance_id, row.subtitles, ids


def _listing(stored):
    """A stored ``subtitles`` or ``missing_subtitles`` column as a list."""
    try:
        value = ast.literal_eval(stored or '[]')
    except (SyntaxError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _embedded_entries(stored, key):
    """Every stored embedded entry with this key. One hide covers all of them:
    an SRT and a PGS English track on one remux share the key ``en``."""
    return [entry for entry in _listing(stored)
            if isinstance(entry, (list, tuple)) and entry and entry[0] == key
            and not (len(entry) > 1 and entry[1])]


def _variant(key):
    """(base language, hi, forced) of a stored key such as ``en:hi``."""
    base, *variants = key.split(':')
    return base, 'hi' in variants, 'forced' in variants


def _table(media_type):
    if media_type == 'sports':
        from app.database import TableSportsEvents
        return TableSportsEvents
    return TableMovies if media_type == 'movie' else TableEpisodes


def _still_listed(media_type, media_id, key):
    """True when the row's stored listing still carries an embedded ``key``,
    or when the listing cannot be read: a hide must not stay behind unseen."""
    table = _table(media_type)
    try:
        stored = database.execute(select(table.subtitles).where(table.id == media_id)).scalar()
    except Exception:
        logging.exception('BAZARR could not read back the subtitles of %s %s', media_type, media_id)
        return True
    return bool(_embedded_entries(stored, key))


def _outcome(media_type, media_id, key):
    """What the hide changed, read back from the row the re-index wrote.

    ``wanted`` is True when the language is now missing. It stays False when
    another subtitle of that language still satisfies it (``covered_by``): the
    wanted computation counts an HI track as good as a normal one, and a
    subtitle file counts too. It also stays False when the profile does not
    ask for the language at all. The page tells the user either way, since the
    confirmation promised the language would be wanted again.
    """
    table = _table(media_type)
    row = database.execute(select(table.subtitles, table.missing_subtitles)
                           .where(table.id == media_id)).first()
    base, hi, forced = _variant(key)
    missing = [item for item in _listing(row.missing_subtitles if row else None) if isinstance(item, str)]
    wanted = any(_variant(item)[0] == base and _variant(item)[2] == forced for item in missing)
    covered_by = []
    for entry in _listing(row.subtitles if row else None):
        if not (isinstance(entry, (list, tuple)) and entry and isinstance(entry[0], str)):
            continue
        entry_base, entry_hi, entry_forced = _variant(entry[0])
        if (entry_base != base or entry_forced != forced
                or any(part.startswith('combined-') for part in entry[0].split(':')[1:])):
            continue
        covered_by.append({'code2': entry_base, 'name': _language_name(entry_base), 'hi': entry_hi,
                           'forced': entry_forced, 'embedded': not (len(entry) > 1 and entry[1])})
    return {'wanted': wanted,
            'hidden': {'code2': base, 'name': _language_name(base), 'hi': hi, 'forced': forced},
            'covered_by': covered_by}


def _reindex(media_type, media_id, original_path, mapped_path, owner, ids):
    """Re-read the file's tracks and push the events a Delete pushes."""
    if media_type == 'movie':
        from subtitles.indexer.movies import store_subtitles_movie
        store_subtitles_movie(original_path, mapped_path, arr_instance_id=owner)
        event_stream(type='movie-wanted', action='update', payload=ids[0])
    elif media_type == 'episode':
        from subtitles.indexer.series import store_subtitles
        store_subtitles(original_path, mapped_path, arr_instance_id=owner)
        event_stream(type='series', action='update', payload=ids[0])
        event_stream(type='episode-wanted', action='update', payload=ids[1])
    else:
        from subtitles.indexer.sports import store_subtitles_sports
        try:
            store_subtitles_sports(media_id, owner)
        except (OSError, ValueError) as exc:
            raise _Refused(f'Could not re-read this sports file: {exc}', 409)
        event_stream(type='sports', action='update', payload=media_id)


def _flag(value):
    return value in _TRUE


class _Body:
    parser = reqparse.RequestParser()
    parser.add_argument('media_type', type=str, required=True, help='movie, episode or sports')
    parser.add_argument('media_id', type=int, required=True, help='Local id of the movie, episode or event')
    parser.add_argument('arr_instance_id', type=int, required=False, help='Owning instance id')
    parser.add_argument('language', type=str, required=True, help='Language code2')
    parser.add_argument('hi', type=str, required=False, help='HI track, "True" or "False"')
    parser.add_argument('forced', type=str, required=False, help='Forced track, "True" or "False"')

    @classmethod
    def parse(cls):
        args = cls.parser.parse_args()
        media_type = args.get('media_type')
        if media_type not in MEDIA_TYPES:
            raise _Refused('media_type must be movie, episode or sports', 400)
        media_id = args.get('media_id')
        if media_id is None or media_id <= 0:
            raise _Refused('media_id must be a positive local id', 400)
        key = language_key(args.get('language'), hi=_flag(args.get('hi')), forced=_flag(args.get('forced')))
        if key is None:
            raise _Refused('language must be a language code', 400)
        return media_type, media_id, args.get('arr_instance_id'), key


@api_ns_embedded_hidden.route('subtitles/embedded-hidden')
class EmbeddedHiddenTracks(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument('media_type', type=str, required=True, location='args')
    # media_id[] is how the frontend's axios client spells an array parameter.
    get_parser.add_argument('media_id[]', type=int, required=True, action='append', location='args')

    @authenticate
    @api_ns_embedded_hidden.doc(parser=get_parser)
    @api_ns_embedded_hidden.response(200, 'Success')
    @api_ns_embedded_hidden.response(400, 'Bad request')
    def get(self):
        """List the hidden embedded tracks of one or more media rows."""
        args = self.get_parser.parse_args()
        media_type = args.get('media_type')
        if media_type not in MEDIA_TYPES:
            return 'media_type must be movie, episode or sports', 400
        requested = [media_id for media_id in args.get('media_id[]') or [] if media_id and media_id > 0]
        groups = (episode_file_siblings(database, requested) if media_type == 'episode'
                  else {media_id: {media_id} for media_id in requested})
        rows = list_hidden(database, media_type, sorted(set().union(*groups.values())) if groups else [])
        owners = media_owners(database, media_type, requested)
        data = []
        for media_id in requested:
            if media_id not in owners:
                continue
            # Only what the indexer applies to this row: a record under
            # another owner belongs to a deleted row whose id was reused.
            applicable = applicable_owners(owners[media_id])
            seen = set()
            for row in rows:
                if (row.media_id not in groups[media_id] or row.arr_instance_id not in applicable
                        or row.language in seen):
                    continue
                seen.add(row.language)
                base, hi, forced = _variant(row.language)
                data.append({
                    'media_type': media_type,
                    'media_id': media_id,
                    'arr_instance_id': row.arr_instance_id or None,
                    'language': row.language,
                    'code2': base,
                    'name': _language_name(base),
                    'hi': hi,
                    'forced': forced,
                    'codec': row.codec,
                    'hidden_at': row.hidden_at.isoformat() if row.hidden_at else None,
                })
        return {'data': data}, 200

    @authenticate
    @api_ns_embedded_hidden.doc(parser=_Body.parser)
    @api_ns_embedded_hidden.response(200, 'Hidden; says whether the language is now wanted')
    @api_ns_embedded_hidden.response(400, 'Bad request')
    @api_ns_embedded_hidden.response(404, 'No such media row, or no embedded track with that language')
    @api_ns_embedded_hidden.response(409, 'The video file cannot be read right now')
    def post(self):
        """Hide an embedded track. The media file is not changed."""
        try:
            media_type, media_id, arr_instance_id, key = _Body.parse()
            original, mapped, owner, stored, ids = _media(media_type, media_id, arr_instance_id)
            entries = _embedded_entries(stored, key)
            if not entries:
                return 'No embedded track with that language on this item.', 404
            # The re-index below is what removes the track. It cannot read a
            # file it cannot reach, and a movie or episode indexer leaves the
            # old listing in place when the file is missing, so refuse rather
            # than record a hide the page would not show.
            if not os.path.exists(mapped):
                return 'The video file cannot be read right now, so the track was not hidden.', 409
            # Every codec the hide covers, for the disclosure. A stored entry
            # carries its codec as a fourth element once the indexer records
            # one; older three-element entries have none.
            codecs = sorted({entry[3] for entry in entries
                             if len(entry) > 3 and isinstance(entry[3], str) and entry[3]})
            recorded = hide_track(database, media_type, media_id, owner, key, ', '.join(codecs) or None)
            failure = None
            try:
                _reindex(media_type, media_id, original, mapped, owner, ids)
            except Exception as error:
                failure = error
            # The engine autocommits, so reconcile the record by hand with the
            # listing the re-index actually left. A refusal means the file
            # could not be read, and the sports indexer then keeps the listing
            # it had, so the listing proves nothing there.
            if isinstance(failure, _Refused) or _still_listed(media_type, media_id, key):
                # Not applied: the re-index could not read the file, failed
                # before it wrote the listing, or returned without applying the
                # hide (the file went away after the check above, or the hidden
                # lookup failed). Take the hide back rather than surprise the
                # user at the next index.
                if recorded:
                    try:
                        unhide_track(database, media_type, [media_id], owner, key)
                    except Exception:
                        logging.exception('BAZARR could not take back a hide it could not apply')
                if failure is not None:
                    raise failure
                return 'The track could not be hidden right now, so nothing changed.', 409
            if failure is not None:
                # Applied, but a later step failed, so what is wanted now is
                # unknown. The record stays; the page refreshes after an error
                # too, so it shows the hide and can undo it.
                raise failure
        except _Refused as refused:
            return refused.message, refused.status
        return {'data': _outcome(media_type, media_id, key)}, 200

    @authenticate
    @api_ns_embedded_hidden.doc(parser=_Body.parser)
    @api_ns_embedded_hidden.response(204, 'Unhidden')
    @api_ns_embedded_hidden.response(400, 'Bad request')
    @api_ns_embedded_hidden.response(404, 'No such media row')
    def delete(self):
        """Unhide an embedded track. The next index lists it again."""
        try:
            media_type, media_id, arr_instance_id, key = _Body.parse()
            original, mapped, owner, _stored, ids = _media(media_type, media_id, arr_instance_id)
            targets = (episode_file_siblings(database, [media_id])[media_id] if media_type == 'episode'
                       else {media_id})
            unhide_track(database, media_type, sorted(targets), owner, key)
            # A file that cannot be read now, or a re-index that fails, lists
            # the track at the next successful index. The record is gone
            # either way, so this is not an error for the request.
            if os.path.exists(mapped):
                try:
                    _reindex(media_type, media_id, original, mapped, owner, ids)
                except _Refused as refused:
                    logging.warning('BAZARR unhid an embedded track but could not re-index: %s',
                                    refused.message)
                except Exception:
                    logging.exception('BAZARR unhid an embedded track but could not re-index it')
        except _Refused as refused:
            return refused.message, refused.status
        return '', 204


def _language_name(code2):
    try:
        return language_from_alpha2(code2) or code2
    except Exception:
        return code2
