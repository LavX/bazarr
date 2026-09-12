from flask import request
from flask_restx import Namespace, Resource

from discover.feeds import digital_releases, recent_episodes, trending
from ..utils import authenticate

api_ns_discover_feeds = Namespace("Discover feeds", description="Global metadata feeds")


@api_ns_discover_feeds.route("discover/feeds/trending")
class TrendingFeed(Resource):
    @authenticate
    def get(self):
        if set(request.args) - {"media_type"} or len(request.args.getlist("media_type")) > 1:
            return {"message": "Invalid trending feed parameters."}, 400
        try:
            return trending(request.args.get("media_type", "all"))
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_feeds.route("discover/feeds/digital")
class DigitalReleaseFeed(Resource):
    @authenticate
    def get(self):
        if set(request.args) - {"region"} or len(request.args.getlist("region")) > 1:
            return {"message": "Invalid digital feed parameters."}, 400
        try:
            return digital_releases(request.args.get("region", "US"))
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_feeds.route("discover/feeds/recent-episodes")
class RecentEpisodeFeed(Resource):
    @authenticate
    def get(self):
        if request.args:
            return {"message": "Recent episodes do not accept feed parameters."}, 400
        return recent_episodes()
