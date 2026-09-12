"""Sports automatic jobs, wanted, history and release exclusion management."""

from flask import request
from flask_restx import Namespace, Resource

from app.database import database, TableHistorySports
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


def _filters():
    include_embedded = request.args.get("include_embedded")
    return dict(
        arr_instance_id=_owner(request.args["arr_instance_id"])
        if "arr_instance_id" in request.args
        else None,
        event_id=_owner(request.args["event_id"])
        if "event_id" in request.args
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


def _queued(job_id):
    return (
        {
            "queued": bool(job_id),
            "job_id": job_id or None,
            "message": "Search queued"
            if job_id
            else "No work queued; no eligible items or a matching job is active",
        },
        202 if job_id else 200,
    )


@api_ns_sports_workflows.route("/sports/wanted")
class SportsWanted(Resource):
    @authenticate
    def get(self):
        try:
            filters = _filters()
            return workflows.list_wanted(
                database,
                filters["arr_instance_id"],
                filters["start"],
                filters["length"],
            ), 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400

    @authenticate
    def post(self):
        try:
            owner = _owner(_body().get("arr_instance_id"))
            require_sportarr(database, owner)
            return _queued(
                workflows.wanted_search_missing_subtitles_sports(arr_instance_id=owner)
            )
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
            return history.list_records(database, "history", **_filters()), 200
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
            return history.list_records(database, "blacklist", **_filters()), 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400

    @authenticate
    def delete(self):
        try:
            return {
                "removed": history.clear_blacklist(
                    database, _owner(request.args.get("arr_instance_id"))
                )
            }, 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400


@api_ns_sports_workflows.route("/sports/blacklist/<int:entry_id>")
class SportsBlacklistEntry(Resource):
    @authenticate
    def delete(self, entry_id):
        try:
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
