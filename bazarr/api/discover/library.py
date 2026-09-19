from flask import request
from flask_restx import Namespace, Resource

from discover.library import copy_options
from ..utils import authenticate

api_ns_discover_library = Namespace("Discover library", description="Exact local copies of a confirmed target")


def _number(value):
    return int(value) if isinstance(value, str) and value.isdigit() and len(value) <= 4 else value


@api_ns_discover_library.route("discover/copies")
class DiscoverCopies(Resource):
    """Offer the exact copies of one confirmed target.

    Browsing this list adopts nothing. It reads bounded scalar columns, never
    opens a media file and never writes.
    """

    @authenticate
    def get(self):
        allowed = {"media_type", "imdb_id", "season", "episode"}
        if set(request.args) - allowed or any(len(request.args.getlist(key)) > 1 for key in allowed):
            return {"message": "Choose a movie or an exact episode."}, 400
        target = {"media_type": request.args.get("media_type"),
                  "imdb_id": request.args.get("imdb_id")}
        if request.args.get("media_type") == "episode":
            target["season"] = _number(request.args.get("season"))
            target["episode"] = _number(request.args.get("episode"))
        try:
            return copy_options(target)
        except ValueError as error:
            return {"message": str(error)}, 400
