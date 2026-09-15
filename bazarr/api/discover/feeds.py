from flask import request
from flask_restx import Namespace, Resource

from discover.feeds import digital_releases, recent_episodes, trending
from ..utils import authenticate

api_ns_discover_feeds = Namespace("Discover feeds", description="Global metadata feeds")

# These answers are never stored by the browser, and the reason is not caution.
#
# A feed is only usable by the client while it still matches the metadata
# revision and, for the dated feeds, the UTC day it was built for. The revision
# is a fresh uuid whenever the TMDB token or locale changes AND on every restart,
# because it lives in a process global. A stored copy therefore goes stale in a
# way the client can detect but not repair: it drops the payload as mismatched,
# renders "temporarily unavailable", and Refresh re-issues the same URL and is
# answered from the same stored copy. The endpoints reject unknown query
# arguments, so no cache-buster can be added from the browser either.
#
# The expensive part is already solved server side: the feed itself is held for
# FRESH_SECONDS, so a request that arrives here is answered from memory without
# touching TMDB, and the client's own query cache covers repeat views within a
# session. Storing it a third time bought one local round trip and cost a state
# the reader could not get out of.
NO_STORE = {"Cache-Control": "no-store"}


@api_ns_discover_feeds.route("discover/feeds/trending")
class TrendingFeed(Resource):
    @authenticate
    def get(self):
        if set(request.args) - {"media_type"} or len(request.args.getlist("media_type")) > 1:
            return {"message": "Invalid trending feed parameters."}, 400
        try:
            return trending(request.args.get("media_type", "all")), 200, NO_STORE
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_feeds.route("discover/feeds/digital")
class DigitalReleaseFeed(Resource):
    @authenticate
    def get(self):
        if set(request.args) - {"region"} or len(request.args.getlist("region")) > 1:
            return {"message": "Invalid digital feed parameters."}, 400
        try:
            return digital_releases(request.args.get("region", "US")), 200, NO_STORE
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_feeds.route("discover/feeds/recent-episodes")
class RecentEpisodeFeed(Resource):
    @authenticate
    def get(self):
        if request.args:
            return {"message": "Recent episodes do not accept feed parameters."}, 400
        return recent_episodes(), 200, NO_STORE
