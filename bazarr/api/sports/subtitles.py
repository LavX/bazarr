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


@api_ns_sports_subtitles.route("/sports/events/<int:event_id>/subtitles")
class SportsEventSubtitleFile(Resource):
    """Removing a subtitle Bazarr placed on a sports event.

    Episodes and movies have had this since forever; sports had no route at
    all, so a bad sports subtitle could not be removed from Bazarr and the
    only way out was deleting the file by hand and waiting for a re-index.
    """

    @authenticate
    def delete(self, event_id):
        from sportarr.identity import resolve_event_in_session
        from subtitles.tools.delete import delete_subtitles

        try:
            body = _body()
            owner = _owner(body.get("arr_instance_id"))
            language = body.get("language")
            path = body.get("path")
            if not language or not path:
                return {"message": "language and path are required"}, 400

            context = resolve_event_in_session(database, event_id, owner)
            removed = delete_subtitles(
                media_type="sports",
                language=language,
                forced=body.get("forced", False),
                hi=body.get("hi", False),
                media_path=context.mapped_path,
                subtitles_path=path,
                arr_instance_id=context.arr_instance_id,
                sports_event_id=context.event_id,
            )
            if not removed:
                return {"message": "Could not delete this subtitle."}, 409
            return "", 204
        except ValueError as exc:
            return {"message": str(exc)}, 400
