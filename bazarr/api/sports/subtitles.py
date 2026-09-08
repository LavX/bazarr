"""Manual provider search and download for an exact owned sports file."""

from flask_restx import Namespace, Resource

from app.database import database
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
