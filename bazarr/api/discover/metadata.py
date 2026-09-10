from flask import request
from flask_restx import Namespace, Resource

from discover import metadata
from ..utils import authenticate

api_ns_discover_metadata = Namespace("Discover metadata", description="Global movie and show metadata")


@api_ns_discover_metadata.route("discover/metadata/status")
class MetadataStatus(Resource):
    @authenticate
    def get(self):
        result = metadata.connection_status()
        result["data"]["fallback_revision"] = metadata.omdb_configuration().revision
        return result


@api_ns_discover_metadata.route("discover/metadata/test")
class MetadataTest(Resource):
    @authenticate
    def post(self):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {"token"}:
            return {"message": "Invalid connection check."}, 400
        try:
            if body.get("token") == "***":
                raise ValueError("Enter a replacement token or test the saved token.")
            return metadata.connection_status(body.get("token"), use_saved="token" not in body)
        except ValueError:
            return {"message": "Invalid TMDB access token."}, 400


@api_ns_discover_metadata.route("discover/metadata/search")
class MetadataSearch(Resource):
    @authenticate
    def get(self):
        if (request.args.get("type", "movie") not in ("movie", "show") or len(request.args.getlist("type")) > 1 or len(request.args.getlist("q")) != 1
                or request.args.get("source", "tmdb") not in ("tmdb", "all") or len(request.args.getlist("source")) > 1
                or len(request.args.getlist("local_q")) > 1 or ("local_q" in request.args and request.args.get("source") != "all")
                or set(request.args) - {"q", "type", "source", "local_q"}):
            return {"message": "Enter a movie title."}, 400
        try:
            if request.args.get("source") == "all":
                return metadata.candidates(request.args.get("q"), request.args.get("type", "movie"), request.args.get("local_q"))
            loader = metadata.search_shows if request.args.get("type") == "show" else metadata.search_movies
            return loader(request.args.get("q"))
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_metadata.route("discover/metadata/movies/<string:movie_id>")
class MetadataDetails(Resource):
    @authenticate
    def get(self, movie_id):
        try:
            if request.args:
                if set(request.args) != {"source"} or len(request.args.getlist("source")) != 1:
                    raise ValueError("Invalid metadata source.")
                return metadata.title_details(movie_id, "movie", request.args.get("source"))
            return metadata.movie_details(movie_id)
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_metadata.route("discover/metadata/shows/<string:show_id>")
class ShowDetails(Resource):
    @authenticate
    def get(self, show_id):
        try:
            if request.args:
                if set(request.args) != {"source"} or len(request.args.getlist("source")) != 1:
                    raise ValueError("Invalid metadata source.")
                return metadata.title_details(show_id, "show", request.args.get("source"))
            return metadata.show_details(show_id)
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_metadata.route("discover/metadata/shows/<string:show_id>/seasons/<string:season>")
class SeasonDetails(Resource):
    @authenticate
    def get(self, show_id, season):
        try:
            return metadata.season_details(show_id, season)
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_metadata.route("discover/metadata/shows/<string:show_id>/seasons/<string:season>/episodes/<string:episode>")
class EpisodeDetails(Resource):
    @authenticate
    def get(self, show_id, season, episode):
        try:
            return metadata.episode_details(show_id, season, episode)
        except ValueError as error:
            return {"message": str(error)}, 400
