"""Manual provider search and download for an exact owned sports file."""

import os
from io import BytesIO

from flask_restx import Namespace, Resource, reqparse
from werkzeug.datastructures import FileStorage

from app.database import database, select, TableSportsEvents
from subliminal_patch.core import SUBTITLE_EXTENSIONS
from sportarr import library
from sportarr.subtitles import manual_search_sports, manual_download_sports
from .leagues import _body, _owner
from ..utils import authenticate

api_ns_sports_subtitles = Namespace(
    "Sports Subtitles", description="Sports subtitle search and download"
)


@api_ns_sports_subtitles.route("/sports/events/<int:event_id>/search")
class SportsSearch(Resource):
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
            return {"data": results}, 200
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
        except ValueError as exc:
            return {"message": str(exc)}, 400
        except OSError:
            return {
                "message": "Subtitle was not published. Check the file and provider before trying again."
            }, 409


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
            owner = _owner(args.get('arr_instance_id'))
            context = resolve_event_in_session(database, event_id, owner)
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
