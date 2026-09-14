"""Authenticated file-row and root-folder views, addressed with local IDs."""
import ast
import os

from flask import request
from flask_restx import Namespace, Resource

from app.config import settings
from app.database import TableSportsEvents, database, select
from sportarr import library, rootfolder
from utilities.path_mappings import path_mappings
from utilities.security_guards import subtitle_path_within_area
from .leagues import _body, _owner
from ..utils import authenticate
from sportarr.errors import SportsNotFound

api_ns_sports_events = Namespace('Sports Events', description='Owned playable sports files')



def _indexed_subtitle(context, path, mapped_subtitle):
    """True when the event's own subtitle index records this file.

    Both forms are compared because the column stores the Sportarr-side path
    the UI sends back, while path mapping is what makes it local: a caller that
    names either one of the pair is naming the same indexed file.
    """
    recorded = database.execute(
        select(TableSportsEvents.subtitles)
        .where(TableSportsEvents.id == context.event_id,
               TableSportsEvents.arr_instance_id == context.arr_instance_id)).scalar()
    try:
        entries = ast.literal_eval(recorded or '[]')
    except (SyntaxError, TypeError, ValueError):
        return False
    if not isinstance(entries, (list, tuple)):
        return False
    wanted = {os.path.normpath(path), os.path.normpath(mapped_subtitle)}
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2 or not isinstance(entry[1], str):
            continue
        known = os.path.normpath(entry[1])
        if known in wanted or os.path.normpath(path_mappings.path_replace_instance(
                entry[1], context.arr_instance_id, 'sports')) in wanted:
            return True
    return False


@api_ns_sports_events.route('/sports/leagues/<int:league_id>/events')
class SportsEvents(Resource):
    @authenticate
    def get(self, league_id):
        try:
            owner = request.args.get('arr_instance_id')
            owner = _owner(owner) if owner is not None else None
            start, length = int(request.args.get('start', 0)), int(request.args.get('length', 100))
            return library.list_events(database, league_id, owner, start, length), 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400


@api_ns_sports_events.route('/sports/events/<int:event_id>')
class SportsEvent(Resource):
    @authenticate
    def get(self, event_id):
        try:
            owner = request.args.get('arr_instance_id')
            owner = _owner(owner) if owner is not None else None
            result = library.get_event(database, event_id, owner)
            return (result, 200) if result else ({'message': 'Event not found'}, 404)
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400


@api_ns_sports_events.route('/sports/events/<int:event_id>/subtitles')
class SportsEventSubtitles(Resource):
    @authenticate
    def post(self, event_id):
        from subtitles.indexer.sports import store_subtitles_sports
        try:
            owner = _owner(_body().get('arr_instance_id'))
            store_subtitles_sports(event_id, owner, use_cache=False)
            return library.get_event(database, event_id, owner), 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400
        except OSError:
            return {'message': 'Could not index this sports file. Check its accessibility and try again.'}, 409

    @authenticate
    def delete(self, event_id):
        """Remove a subtitle Bazarr placed on this event.

        Lives on the resource that already owns this path rather than a second
        Resource registered over it, which would shadow the index POST above.
        """
        from sportarr.subtitles import sports_manual_operation
        from subtitles.tools.delete import delete_subtitles

        try:
            body = _body()
            owner = _owner(body.get('arr_instance_id'))
            language, path = body.get('language'), body.get('path')
            if not language or not path:
                return {'message': 'language and path are required'}, 400

            # The same owned boundary the upload, edit and promote routes use.
            # It pins the recording's signature here and hands down a guard that
            # locks the event row and revalidates around the unlink, so a
            # reconciliation that reassigns the recording between these checks
            # and the removal cannot make this request delete the subtitle of
            # whichever event has adopted it.
            with sports_manual_operation(event_id, owner) as operation:
                context, _validate, guard, _video = operation

                # Containment, the same guard the shared toolbox endpoint
                # applies (#GHSA). Without it the only check on a
                # caller-supplied path was its file extension, so an
                # authenticated request could name any subtitle Bazarr can
                # reach, including one belonging to a different media item, and
                # have it removed. The mapped path is what gets deleted, so the
                # mapped path is what has to be contained.
                mapped_subtitle = path_mappings.path_replace_instance(
                    path, context.arr_instance_id, 'sports')
                if not subtitle_path_within_area(
                    mapped_subtitle,
                    context.mapped_path,
                    subfolder_mode=settings.general.subfolder,
                    custom_subfolder=settings.general.subfolder_custom,
                ):
                    return {'message': 'Subtitle path is outside the media library.'}, 403

                # Containment alone answers "is this inside the library", not
                # "is this THIS event's subtitle". Sports recordings share a
                # directory routinely, and a custom subtitle folder is one
                # absolute directory for everything, so a sibling event's
                # subtitle passed the check above and was deleted with the
                # history written against the event in the URL. The event's own
                # index is the list the UI offers, so a path outside it is
                # either stale or not this event's to remove.
                if not _indexed_subtitle(context, path, mapped_subtitle):
                    return {'message': 'Subtitle does not belong to this sports event.'}, 403

                removed = delete_subtitles(
                    media_type='sports',
                    language=language,
                    forced=body.get('forced', False),
                    hi=body.get('hi', False),
                    media_path=context.mapped_path,
                    subtitles_path=path,
                    arr_instance_id=context.arr_instance_id,
                    sports_event_id=context.event_id,
                    publication_guard=guard,
                )
            if not removed:
                return {'message': 'Could not delete this subtitle.'}, 409
            return '', 204
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            # Anything that refuses mid-deletion refuses because the event or
            # its recording moved underneath the request, which is a conflict
            # rather than a malformed request.
            return {'message': str(exc)}, 409


@api_ns_sports_events.route('/sports/rootfolders')
class SportsRootfolders(Resource):
    @authenticate
    def get(self):
        try:
            return rootfolder.list_rootfolders(database, _owner(request.args.get('arr_instance_id'))), 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400
