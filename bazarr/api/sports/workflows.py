"""Sports automatic jobs, wanted, history and release exclusion management."""

from flask import request
from flask_restx import Namespace, Resource

from app.database import database, TableHistorySports
from app.process_session import PROCESS_SESSION
from media_sources import sports_activity
from media_sources.union import SourceKeyFilter
from media_sources.wanted import queue_wanted_search
from sportarr import history, workflows
from sportarr.identity import resolve_event_in_session
from sportarr.sync.leagues import require_sportarr
from .leagues import _body, _owner
from ..utils import authenticate
from sportarr.errors import SportsNotFound

api_ns_sports_workflows = Namespace(
    "Sports Workflows", description="Owned automatic subtitle workflows"
)


_TRUTHY = {"true", "t", "yes", "y", "on", "1"}


def _source_scope():
    return SourceKeyFilter.from_values(request.args.getlist('source_key[]'))


def _filters():
    include_embedded = request.args.get("include_embedded")
    return dict(
        arr_instance_id=_owner(request.args["arr_instance_id"])
        if "arr_instance_id" in request.args
        else None,
        event_id=_owner(request.args["event_id"])
        if "event_id" in request.args
        else None,
        league_id=_league_id(request.args["league_id"])
        if "league_id" in request.args
        else None,
        start=int(request.args.get("start", 0)),
        length=int(request.args.get("length", 100)),
        language=request.args.get("language"),
        provider=request.args.get("provider"),
        action=int(request.args["action"]) if "action" in request.args else None,
        # Same default as the episodes and movies endpoints: Embedded Source
        # records (media state, not events) stay out unless the caller asks.
        include_embedded=(
            include_embedded.lower() in _TRUTHY
            if include_embedded is not None
            else False
        ),
    )


def _league_id(value):
    """A local sports league id from a query string, or None when omitted.

    A local league id is a primary key and already unique across instances, so
    the history and blacklist listings accept it on its own, exactly as they do
    an event_id.
    """
    if value is None or value == '':
        return None
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ValueError('league_id must be a positive integer')
    return value


def _queued(job_id):
    body = {
        "queued": bool(job_id),
        "job_id": job_id or None,
        "message": "Search queued"
        if job_id
        else "No work queued; no eligible items or a matching job is active",
    }
    if job_id:
        # Ids restart with the backend, so the client names the job by its id together with the run.
        body["session"] = PROCESS_SESSION
    return body, 202 if job_id else 200


@api_ns_sports_workflows.route("/sports/wanted")
class SportsWanted(Resource):
    @authenticate
    def get(self):
        try:
            filters = _filters()
            return sports_activity.list_wanted(
                database, request.args, _source_scope(), arr_instance_id=filters["arr_instance_id"],
                start=filters["start"], length=filters["length"],
            ), 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400

    @authenticate
    def post(self):
        try:
            body = _body()
            owner = body.get("arr_instance_id")
            scope = _source_scope()
            if request.args.get('sources') == '1' and owner is None:
                selected = {key for origin, key in scope.keys if origin == 'source' and key is not None}
                source_ids = tuple(sorted(selected)) if scope.keys else None
                return _queued(queue_wanted_search('sports_event', source_ids=source_ids))
            owner = _owner(owner)
            if scope.keys and ('arr', owner) not in scope.keys:
                return _queued(None)
            require_sportarr(database, owner)
            return _queued(workflows.wanted_search_missing_subtitles_sports(arr_instance_id=owner))
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400


@api_ns_sports_workflows.route("/sports/events/<int:event_id>/automatic")
class SportsAutomatic(Resource):
    @authenticate
    def post(self, event_id):
        try:
            body = _body()
            # An optional single language, so one missing-language badge can be
            # actioned on its own instead of searching every missing language
            # on the event. Omitted, the behaviour is unchanged.
            language = body.get("language")
            if language is not None and (not isinstance(language, str) or not language):
                raise ValueError("language must be a non-empty language code")
            return _queued(
                workflows.automatic_search_sports(
                    event_id, _owner(body.get("arr_instance_id")), language=language
                )
            )
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400


@api_ns_sports_workflows.route("/sports/leagues/<int:league_id>/download")
class SportsMassDownload(Resource):
    @authenticate
    def post(self, league_id):
        try:
            return _queued(
                workflows.sports_download_subtitles(
                    league_id, _owner(_body().get("arr_instance_id"))
                )
            )
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400


@api_ns_sports_workflows.route("/sports/upgrade")
class SportsUpgrade(Resource):
    @authenticate
    def post(self):
        try:
            owner = _owner(_body().get("arr_instance_id"))
            require_sportarr(database, owner)
            return _queued(workflows.upgrade_sports_subtitles(arr_instance_id=owner))
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400


@api_ns_sports_workflows.route("/sports/history")
class SportsHistory(Resource):
    @authenticate
    def get(self):
        try:
            return sports_activity.list_activity(database, "history", request.args, _source_scope(), **_filters()), 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400


@api_ns_sports_workflows.route("/sports/history/<int:history_id>/blacklist")
class SportsBlacklistHistory(Resource):
    @authenticate
    def post(self, history_id):
        try:
            owner = _owner(_body().get("arr_instance_id"))
            row = database.get(TableHistorySports, history_id, populate_existing=True)
            if (
                row is None
                or row.arr_instance_id != owner
                or row.action not in (1, 2, 3)
                or not row.provider
                or not row.subs_id
            ):
                raise ValueError(
                    "A provider download history entry for this owner is required"
                )
            resolve_event_in_session(database, row.event_id, owner)
            return _queued(workflows.blacklist_sports_subtitle(history_id, owner))
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400


@api_ns_sports_workflows.route("/sports/blacklist")
class SportsBlacklist(Resource):
    @authenticate
    def get(self):
        try:
            return sports_activity.list_activity(database, "blacklist", request.args, _source_scope(), **_filters()), 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400

    @authenticate
    def delete(self):
        try:
            owner = request.args.get("arr_instance_id")
            scope = _source_scope()
            if request.args.get('sources') == '1' and owner is None:
                removed = sports_activity.clear_blacklist(database, scope)
            else:
                owner = _owner(owner)
                removed = history.clear_blacklist(database, owner) if not scope.keys or ('arr', owner) in scope.keys else 0
            return {"removed": removed}, 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400


@api_ns_sports_workflows.route("/sports/blacklist/<int:entry_id>")
class SportsBlacklistEntry(Resource):
    @authenticate
    def delete(self, entry_id):
        try:
            if request.args.get('origin') == 'source':
                if request.args.get('sources') != '1' or 'arr_instance_id' in request.args:
                    raise ValueError('Source exclusion removal requires sources=1 and a source row.')
                removed = sports_activity.remove_blacklist_entry(database, entry_id)
            else:
                removed = history.remove_blacklist(
                    database, entry_id, _owner(request.args.get("arr_instance_id"))
                )
            return (
                ({"removed": True}, 200)
                if removed
                else ({"message": "Exclusion not found"}, 404)
            )
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400


@api_ns_sports_workflows.route("/sports/jobs/<int:job_id>")
class SportsJob(Resource):
    @authenticate
    def get(self, job_id):
        try:
            result = workflows.sports_job_status(
                database, job_id, _owner(request.args.get("arr_instance_id"))
            )
            return (
                (result, 200)
                if result is not None
                else ({"message": "Sports job not found for this owner"}, 404)
            )
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400
