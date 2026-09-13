"""Manual provider search and download for an exact owned sports file."""

import os
from io import BytesIO

from flask import request
from flask_restx import fields, marshal, Namespace, Resource, reqparse
from werkzeug.datastructures import FileStorage

from app.database import (database, select, TableArrInstances, TableSportsEvents,
                          TableSportsLeagues)
from subliminal_patch.core import SUBTITLE_EXTENSIONS
from sportarr import library
from sportarr.subtitles import manual_search_sports, manual_download_sports
from .leagues import _body, _optional_owner, _owner
from ..utils import authenticate
from sportarr.errors import SportsNotFound

api_ns_sports_subtitles = Namespace(
    "Sports Subtitles", description="Sports subtitle search and download"
)


def _league_owner(league_id, owner):
    """Resolve a league's owning instance, enforcing it when the caller sent one."""
    query = (
        select(TableSportsLeagues.arr_instance_id)
        .join(TableArrInstances,
              TableSportsLeagues.arr_instance_id == TableArrInstances.id)
        .where(TableSportsLeagues.id == league_id,
               TableArrInstances.kind == 'sportarr',
               TableArrInstances.enabled == 1)
    )
    if owner is not None:
        query = query.where(TableSportsLeagues.arr_instance_id == owner)
    found = database.execute(query).scalar_one_or_none()
    if found is None:
        raise SportsNotFound('Sports league not found for this owner')
    return found


@api_ns_sports_subtitles.route("/sports/events/<int:event_id>/search")
class SportsSearch(Resource):
    # The same model the providers episodes/movies endpoints marshal through.
    # Unmarshalled, this endpoint returned manual_search's raw dicts, so
    # original_format came back as a JSON boolean while the series and movies
    # searches return the string "False". Both feed the one shared
    # SearchResultType in the frontend, so a consumer comparing against
    # "True" read every sports row as false.
    get_response_model = api_ns_sports_subtitles.model('SportsSearchResult', {
        'dont_matches': fields.List(fields.String),
        'forced': fields.String(),
        'hearing_impaired': fields.String(),
        'language': fields.String(),
        'matches': fields.List(fields.String),
        'original_format': fields.String(),
        'orig_score': fields.Integer(),
        'provider': fields.String(),
        'release_info': fields.List(fields.String),
        'score': fields.Integer(),
        'score_without_hash': fields.Integer(),
        'subtitle': fields.String(),
        'uploader': fields.String(),
        'url': fields.String(),
    })

    @authenticate
    def post(self, event_id):
        try:
            body = _body()
            results = manual_search_sports(
                event_id,
                body.get("language"),
                body.get("hi", False),
                body.get("forced", False),
                _owner(body.get("arr_instance_id")),
            )
            return {"data": marshal(results, self.get_response_model)}, 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400
        except OSError:
            return {
                "message": "Could not search this sports file. Check its accessibility and providers."
            }, 409


@api_ns_sports_subtitles.route("/sports/events/<int:event_id>/download")
class SportsDownload(Resource):
    @authenticate
    def post(self, event_id):
        try:
            body = _body()
            owner = _owner(body.get("arr_instance_id"))
            result = manual_download_sports(event_id, body.get("candidate"), owner)
            try:
                event = library.get_event(database, event_id, owner)
            except Exception:
                # Publication already happened. A later owner/read failure must
                # not turn the response into an ordinary failed download.
                event = None
            return {"event": event, "publication": result.publication}, 200
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400
        except OSError:
            return {
                "message": "Subtitle was not published. Check the file and provider before trying again."
            }, 409


@api_ns_sports_subtitles.route("/sports/events/<int:event_id>/subtitles/combine")
class SportsEventSubtitlesCombine(Resource):
    """Build (or rebuild) the combined subtitle file for a sports event.

    The combine engine has understood sports since it was written, but only
    ever ran as a side effect of a download or a translation. Episodes and
    movies both have a manual trigger; sports had none, so a combined file
    could not be rebuilt after its sources changed on disk.

    No ad-hoc languages/format override here, unlike the episode and movie
    routes: a sports composition publishes through the owned-file guard, which
    is captured from the event's assigned profile, and try_combine_for_video
    rejects an override alongside it rather than publishing something the
    profile did not ask for.
    """

    @authenticate
    @api_ns_sports_subtitles.response(200, "Result of combine attempt")
    @api_ns_sports_subtitles.response(401, "Not Authenticated")
    @api_ns_sports_subtitles.response(404, "Sports event not found")
    @api_ns_sports_subtitles.response(500, "Combine failed")
    def post(self, event_id):
        from sportarr.identity import resolve_event_in_session
        from sportarr.profile_hooks import capture_profile_operation
        from sportarr.subtitles import candidate_signature
        from subtitles.tools.combine.main import try_combine_for_video

        try:
            body = request.get_json(silent=True) or {}
            owner = _optional_owner(body.get("arr_instance_id") or request.args.get("arr_instance_id"))
            context = resolve_event_in_session(database, event_id, owner)
            operation = capture_profile_operation(context, candidate_signature(context))
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400

        if not operation.profile:
            return {"status": "skipped", "path": "", "alignment": "",
                    "reason": "no language profile is assigned", "error": ""}, 200

        result = try_combine_for_video(
            video_path=context.mapped_path,
            media_type="sports",
            sports_operation=operation,
        )
        body = {
            "status": result.status,
            "path": result.path,
            "alignment": result.alignment,
            "reason": result.reason,
            "error": result.error,
        }
        return body, 500 if result.status == "failed" else 200


@api_ns_sports_subtitles.route("/sports/leagues/<int:league_id>/subtitles/combine")
class SportsLeagueSubtitlesCombine(Resource):
    """Combine every event in a league that has all its source languages.

    The series route's counterpart, so a league selected in the library gets
    the same batch treatment a show does.
    """

    @authenticate
    @api_ns_sports_subtitles.response(200, "Batch combine summary")
    @api_ns_sports_subtitles.response(401, "Not Authenticated")
    @api_ns_sports_subtitles.response(404, "Sports league not found")
    def post(self, league_id):
        from sportarr.identity import resolve_event_in_session
        from sportarr.profile_hooks import capture_profile_operation
        from sportarr.subtitles import candidate_signature
        from subtitles.tools.combine.main import try_combine_for_video

        try:
            body = request.get_json(silent=True) or {}
            owner = _optional_owner(body.get("arr_instance_id") or request.args.get("arr_instance_id"))
            # A league id is a primary key, so resolve the owner from the row
            # when the caller omitted it. Without this, owner=None compiled to
            # `arr_instance_id IS NULL` below and matched no events at all.
            owner = _league_owner(league_id, owner)
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400

        event_ids = database.execute(
            select(TableSportsEvents.id).where(
                TableSportsEvents.league_id == league_id,
                TableSportsEvents.arr_instance_id == owner,
            )
        ).scalars().all()
        if not event_ids:
            return {"status": "not_found"}, 404

        built = skipped = failed = 0
        details = []
        for event_id in event_ids:
            try:
                context = resolve_event_in_session(database, event_id, owner)
                operation = capture_profile_operation(context, candidate_signature(context))
                if not operation.profile:
                    result_status, result = "skipped", None
                else:
                    result = try_combine_for_video(
                        video_path=context.mapped_path,
                        media_type="sports",
                        sports_operation=operation,
                    )
                    result_status = result.status
            except (ValueError, OSError) as exc:
                # One unreadable or moved recording must not end the batch.
                result_status, result = "failed", None
                details.append({"eventId": event_id, "status": "failed",
                                "path": "", "reason": "", "error": str(exc)})
            else:
                details.append({
                    "eventId": event_id,
                    "status": result_status,
                    "path": result.path if result else "",
                    "reason": result.reason if result else "no language profile is assigned",
                    "error": result.error if result else "",
                })
            if result_status == "built":
                built += 1
            elif result_status == "skipped":
                skipped += 1
            else:
                failed += 1

        return {"status": "batch_complete", "built": built, "skipped": skipped,
                "failed": failed, "details": details}, 200


@api_ns_sports_subtitles.route("/sports/events/<int:event_id>/subtitles/upload")
class SportsEventSubtitleUpload(Resource):
    """Uploading a subtitle for a sports event.

    Episodes and movies have had this since forever; sports had no route, so a
    subtitle you already owned could not be given to Bazarr.
    """

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                     help='Owning Sportarr instance id')
    post_request_parser.add_argument('language', type=str, required=True, help='Language code2')
    post_request_parser.add_argument('forced', type=str, required=False, help='Forced true/false as string')
    post_request_parser.add_argument('hi', type=str, required=False, help='HI true/false as string')
    post_request_parser.add_argument('file', type=FileStorage, location='files', required=True,
                                     help='Subtitles file as file upload object')

    @authenticate
    @api_ns_sports_subtitles.doc(parser=post_request_parser)
    @api_ns_sports_subtitles.response(204, 'Success')
    @api_ns_sports_subtitles.response(401, 'Not Authenticated')
    @api_ns_sports_subtitles.response(404, 'Sports event not found')
    def post(self, event_id):
        """Upload a subtitle for a sports event."""
        from sportarr.identity import resolve_event_in_session
        from subtitles.upload import manual_upload_subtitle

        args = self.post_request_parser.parse_args()
        uploaded_file = args.get('file')
        _, ext = os.path.splitext(uploaded_file.filename)
        if not isinstance(ext, str) or ext.lower() not in SUBTITLE_EXTENSIONS:
            return {"message": "A subtitle of an invalid format was uploaded."}, 400

        try:
            owner = _optional_owner(args.get('arr_instance_id'))
            context = resolve_event_in_session(database, event_id, owner)
        except SportsNotFound as exc:
            return {'message': str(exc)}, 404
        except ValueError as exc:
            return {"message": str(exc)}, 400

        if not os.path.exists(context.mapped_path):
            return {"message": "Sports file not found. Path mapping issue?"}, 500

        row = database.execute(
            select(TableSportsEvents.audio_language)
            .where(TableSportsEvents.id == context.event_id,
                   TableSportsEvents.arr_instance_id == context.arr_instance_id)
        ).first()

        manual_upload_subtitle(path=context.mapped_path,
                               language=args.get('language'),
                               forced=args.get('forced') == 'true',
                               hi=args.get('hi') == 'true',
                               media_type='sports',
                               subtitle=BytesIO(uploaded_file.read()),
                               filename=uploaded_file.filename,
                               audio_language=row.audio_language if row else '[]',
                               arr_instance_id=context.arr_instance_id,
                               sportsEventId=context.event_id)

        return '', 204
