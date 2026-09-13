"""Authenticated sports library API using local IDs and explicit owners."""
from flask import request
from flask_restx import Namespace, Resource

from app.database import database
from app.jobs_queue import jobs_queue
from sportarr import library
from sportarr.sync.leagues import require_sportarr
from ..utils import authenticate
from sportarr.errors import SportsNotFound

api_ns_sports_leagues = Namespace('Sports Leagues', description='Owned sports leagues')


def _owner(value):
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ValueError('arr_instance_id is required and must be a positive integer')
    return value


def _optional_owner(value):
    """The owner when the caller sent one, None when they did not.

    A local event or league id is a primary key and already unique across
    instances, so these endpoints do not need an owner to find the row; they
    need one only to enforce ownership when the caller asserts it. Routing
    everything through _owner made arr_instance_id mandatory in practice while
    the parser and the Swagger doc both advertised it as optional, so an
    upload without it answered 400 instead of working the way the episode and
    movie equivalents do.
    """
    if value is None or value == '':
        return None
    return _owner(value)


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError('A JSON object is required')
    return body


_JOBS = {
    'update_sports_for_instance': ('Sync sports library', 'sportarr.sync.leagues'),
    'sync_one_league': ('Sync league events', 'sportarr.sync.events'),
}


def _queue(func, kwargs):
    # A table rather than a pair of ternaries: a third job made the ternaries
    # wrong rather than merely long, silently labelling it as a profile refresh
    # and dispatching it to the wrong module.
    job_name, module = _JOBS.get(func, ('Refresh sports profiles', 'sportarr.library'))
    # Return the id. feed_jobs_pending_queue answers False when an identical
    # job is already pending or running, and discarding that meant both sync
    # routes reported 202 "queued" for work that was dropped, with no job_id
    # for the client to poll.
    return jobs_queue.feed_jobs_pending_queue(
        job_name=job_name, module=module, func=func, kwargs=kwargs, is_progress=False)


def _queued(job_id, queued_message):
    """The SportsJob shape the client declares, matching api/sports/workflows.

    202 only when something was actually queued; a duplicate is a 200 saying
    so, which is what every other sports job endpoint already returns.
    """
    return (
        {
            'queued': bool(job_id),
            'job_id': job_id or None,
            'message': queued_message if job_id
            else 'No work queued; a matching sync is already active',
        },
        202 if job_id else 200,
    )


@api_ns_sports_leagues.route('/sports/leagues')
class SportsLeagues(Resource):
    @authenticate
    def get(self):
        try:
            owner = request.args.get('arr_instance_id')
            owner = _owner(owner) if owner is not None else None
            start = int(request.args.get('start', 0))
            length = int(request.args.get('length', 100))
            # No bespoke bounds check here: list_leagues validates through the
            # shared definition, which also accepts the fetch-all length of -1.
            # The copy that used to live here rejected -1, so the library page
            # could not fetch every row and its filters only ever searched the
            # page currently on screen.
            return library.list_leagues(database, owner, start, length), 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400


@api_ns_sports_leagues.route('/sports/leagues/profiles')
class SportsLeagueProfiles(Resource):
    """Assign a language profile to many leagues in one request.

    Series and movies have taken parallel id/profileid arrays since forever;
    sports had only the single-league PATCH, so mass-editing 200 leagues fired
    200 requests, each with its own transaction and its own re-index, and a
    partial failure left the selection half-applied with no aggregate status.
    """

    @authenticate
    def post(self):
        try:
            body = _body()
            ids = body.get('id')
            owners = body.get('arr_instance_id')
            profiles = body.get('profileId')
            if not isinstance(ids, list) or not ids:
                raise ValueError('id must be a non-empty list of league ids')
            if not isinstance(owners, list) or len(owners) != len(ids):
                raise ValueError('arr_instance_id must be a list matching id')
            if not isinstance(profiles, list) or len(profiles) != len(ids):
                raise ValueError('profileId must be a list matching id')
            assignments = [
                (_owner(league_id), _owner(owner),
                 None if profile is None else _owner(profile))
                for league_id, owner, profile in zip(ids, owners, profiles)
            ]
            updated = library.assign_profiles(database, assignments)
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400
        # Re-index per owner, not with a None owner: the sports indexer resolves
        # paths against the owning instance's mappings.
        by_owner = {}
        for league_id, owner in updated:
            by_owner.setdefault(owner, []).append(league_id)
        for owner, league_ids in by_owner.items():
            library.refresh_league_profiles(league_ids, owner)
        return {'updated': len(updated), 'requested': len(assignments)}, 200


@api_ns_sports_leagues.route('/sports/leagues/<int:league_id>')
class SportsLeague(Resource):
    @authenticate
    def get(self, league_id):
        try:
            owner = request.args.get('arr_instance_id')
            result = library.get_league(database, league_id, _owner(owner) if owner is not None else None)
            return (result, 200) if result else ({'message': 'League not found'}, 404)
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
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
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400
        library.refresh_league_profiles([league_id], owner)
        # 204 carries no body, and every episode/movie/series equivalent
        # returns the empty string here.
        return '', 204


@api_ns_sports_leagues.route('/sports/leagues/<int:league_id>/sync')
class SportsLeagueEventSync(Resource):
    """Re-sync one league's events from its Sportarr.

    The league detail page has had a Sync button since it was built, posting
    here. Nothing served this path, so the button 405'd and the user got no
    sync and no error worth reading. The whole-instance route below is the
    wrong target for it: this button sits on one league, and the episodes page
    it mirrors syncs its own series.
    """

    @authenticate
    def post(self, league_id):
        try:
            owner = _owner(_body().get('arr_instance_id'))
            if library.get_league(database, league_id, owner) is None:
                raise SportsNotFound('Sports league not found for this owner')
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400
        return _queued(
            _queue('sync_one_league', {'league_id': league_id, 'arr_instance_id': owner}),
            'League sync queued')


@api_ns_sports_leagues.route('/sports/leagues/sync')
class SportsLeagueSync(Resource):
    @authenticate
    def post(self):
        try:
            owner = _owner(_body().get('arr_instance_id'))
            require_sportarr(database, owner)
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {'message': str(exc)}, 400
        return _queued(
            _queue('update_sports_for_instance', {'arr_instance_id': owner}),
            'Library sync queued')
