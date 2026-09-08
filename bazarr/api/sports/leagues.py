"""Authenticated sports library API using local IDs and explicit owners."""
from flask import request
from flask_restx import Namespace, Resource

from app.database import database
from app.jobs_queue import jobs_queue
from sportarr import library
from sportarr.sync.leagues import require_sportarr
from ..utils import authenticate

api_ns_sports_leagues = Namespace('Sports Leagues', description='Owned sports leagues')


def _owner(value):
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ValueError('arr_instance_id is required and must be a positive integer')
    return value


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError('A JSON object is required')
    return body


def _queue(func, kwargs):
    jobs_queue.feed_jobs_pending_queue(
        job_name='Sync sports library' if func == 'update_sports_for_instance' else 'Refresh sports profiles',
        module='sportarr.sync.leagues' if func == 'update_sports_for_instance' else 'sportarr.library',
        func=func, kwargs=kwargs, is_progress=False)


@api_ns_sports_leagues.route('/sports/leagues')
class SportsLeagues(Resource):
    @authenticate
    def get(self):
        try:
            owner = request.args.get('arr_instance_id')
            owner = _owner(owner) if owner is not None else None
            start = int(request.args.get('start', 0))
            length = int(request.args.get('length', 100))
            if start < 0 or not 1 <= length <= 1000:
                raise ValueError('Invalid pagination')
            return library.list_leagues(database, owner, start, length), 200
        except ValueError as exc:
            return {'message': str(exc)}, 400


@api_ns_sports_leagues.route('/sports/leagues/<int:league_id>')
class SportsLeague(Resource):
    @authenticate
    def get(self, league_id):
        try:
            owner = request.args.get('arr_instance_id')
            result = library.get_league(database, league_id, _owner(owner) if owner is not None else None)
            return (result, 200) if result else ({'message': 'League not found'}, 404)
        except ValueError as exc:
            return {'message': str(exc)}, 400

    @authenticate
    def patch(self, league_id):
        try:
            body = _body()
            owner = _owner(body.get('arr_instance_id'))
            if 'profileId' not in body:
                raise ValueError('profileId is required')
            if not library.assign_profile(database, league_id, owner, body['profileId']):
                return {'message': 'League not found'}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400
        library.refresh_league_profiles([league_id], owner)
        return {}, 204


@api_ns_sports_leagues.route('/sports/leagues/sync')
class SportsLeagueSync(Resource):
    @authenticate
    def post(self):
        try:
            owner = _owner(_body().get('arr_instance_id'))
            require_sportarr(database, owner)
        except ValueError as exc:
            return {'message': str(exc)}, 400
        _queue('update_sports_for_instance', {'arr_instance_id': owner})
        return {'queued': True}, 202
