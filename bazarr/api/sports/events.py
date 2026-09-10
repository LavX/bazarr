"""Authenticated file-row and root-folder views, addressed with local IDs."""
from flask import request
from flask_restx import Namespace, Resource

from app.config import settings
from app.database import database
from sportarr import library, rootfolder
from utilities.path_mappings import path_mappings
from utilities.security_guards import subtitle_path_within_area
from .leagues import _body, _owner
from ..utils import authenticate

api_ns_sports_events = Namespace('Sports Events', description='Owned playable sports files')


@api_ns_sports_events.route('/sports/leagues/<int:league_id>/events')
class SportsEvents(Resource):
    @authenticate
    def get(self, league_id):
        try:
            owner = request.args.get('arr_instance_id')
            owner = _owner(owner) if owner is not None else None
            start, length = int(request.args.get('start', 0)), int(request.args.get('length', 100))
            return library.list_events(database, league_id, owner, start, length), 200
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
        from sportarr.identity import resolve_event_in_session
        from subtitles.tools.delete import delete_subtitles

        try:
            body = _body()
            owner = _owner(body.get('arr_instance_id'))
            language, path = body.get('language'), body.get('path')
            if not language or not path:
                return {'message': 'language and path are required'}, 400

            context = resolve_event_in_session(database, event_id, owner)

            # Containment, the same guard the shared toolbox endpoint applies
            # (#GHSA). Without it the only check on a caller-supplied path was
            # its file extension, so an authenticated request could name any
            # subtitle Bazarr can reach, including one belonging to a different
            # media item, and have it removed. The mapped path is what gets
            # deleted, so the mapped path is what has to be contained.
            mapped_subtitle = path_mappings.path_replace_instance(
                path, context.arr_instance_id, 'sports')
            if not subtitle_path_within_area(
                mapped_subtitle,
                context.mapped_path,
                subfolder_mode=settings.general.subfolder,
                custom_subfolder=settings.general.subfolder_custom,
            ):
                return {'message': 'Subtitle path is outside the media library.'}, 403

            removed = delete_subtitles(
                media_type='sports',
                language=language,
                forced=body.get('forced', False),
                hi=body.get('hi', False),
                media_path=context.mapped_path,
                subtitles_path=path,
                arr_instance_id=context.arr_instance_id,
                sports_event_id=context.event_id,
            )
            if not removed:
                return {'message': 'Could not delete this subtitle.'}, 409
            return '', 204
        except ValueError as exc:
            return {'message': str(exc)}, 400


@api_ns_sports_events.route('/sports/rootfolders')
class SportsRootfolders(Resource):
    @authenticate
    def get(self):
        try:
            return rootfolder.list_rootfolders(database, _owner(request.args.get('arr_instance_id'))), 200
        except ValueError as exc:
            return {'message': str(exc)}, 400
