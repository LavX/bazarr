from flask import request
from flask_restx import Namespace, Resource

from discover.library import copy_options
from ..utils import authenticate

api_ns_discover_library = Namespace("Discover library", description="Exact local copies of a confirmed target")


def _number(value):
    return int(value) if isinstance(value, str) and value.isdecimal() and len(value) <= 4 else value


_EPISODE_EVIDENCE = {"source_season": "season", "source_episode": "episode", "episode_tmdb_id": "id",
                     "episode_imdb_id": "imdb_id", "episode_tvdb_id": "tvdb_id"}


@api_ns_discover_library.route("discover/copies")
class DiscoverCopies(Resource):
    """Offer the exact copies of one confirmed target.

    Browsing this list adopts nothing. It reads bounded scalar columns, never
    opens a media file and never writes. A catalog episode can include its
    original numbering and episode IDs alongside the Arr target numbers.
    """

    @authenticate
    def get(self):
        allowed = {"media_type", "imdb_id", "tmdb_id", "season", "episode"}
        media_type = request.args.get("media_type")
        if media_type == "episode":
            allowed.update(_EPISODE_EVIDENCE)
        if set(request.args) - allowed or any(len(request.args.getlist(key)) > 1 for key in allowed):
            return {"message": "Choose a movie or an exact episode."}, 400
        target = {"media_type": media_type, "imdb_id": request.args.get("imdb_id"), "tmdb_id": request.args.get("tmdb_id")}
        if media_type == "episode":
            target["season"] = _number(request.args.get("season"))
            target["episode"] = _number(request.args.get("episode"))
            if _EPISODE_EVIDENCE.keys() & request.args.keys():
                target["episode_identity"] = {
                    field: _number(value) if field in ("season", "episode") else value
                    for key, field in _EPISODE_EVIDENCE.items() if (value := request.args.get(key)) is not None
                }
        try:
            return copy_options(target)
        except ValueError as error:
            return {"message": str(error)}, 400
