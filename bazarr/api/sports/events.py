"""Authenticated file-row and root-folder views, addressed with local IDs."""
from flask import request
from flask_restx import Namespace, Resource

from app.database import database
from sportarr import library, rootfolder
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


@api_ns_sports_events.route('/sports/rootfolders')
class SportsRootfolders(Resource):
    @authenticate
    def get(self):
        try:
            return rootfolder.list_rootfolders(database, _owner(request.args.get('arr_instance_id'))), 200
        except ValueError as exc:
            return {'message': str(exc)}, 400
