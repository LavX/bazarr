from datetime import datetime, timezone

from flask import request
from flask_restx import Namespace, Resource

from discover.feeds import digital_releases, recent_episodes, trending
from ..utils import authenticate

api_ns_discover_feeds = Namespace("Discover feeds", description="Global metadata feeds")


def _feed(payload):
    """Let the browser reuse a feed for exactly as long as the server would.

    Without this every page load spends a round trip re-asking for titles the
    server is holding unchanged for the next hour. The window is not restated
    here: it is read back off the payload's own ``expires_at``, so the browser's
    copy lapses at the same instant the server's freshness does and the two can
    never drift apart.

    Only a feed that actually has titles and no failure behind it is cacheable.
    An empty, busy, degraded or unconfigured answer is ``no-store``, which is
    what keeps Refresh honest: those are precisely the states whose copy tells
    the reader to press it, and a pinned error would make it do nothing.
    """
    headers = {"Cache-Control": "no-store"}
    seconds = _reusable_for(payload)
    if seconds:
        headers["Cache-Control"] = f"private, max-age={seconds}"
    return payload, 200, headers


def _reusable_for(payload):
    if not isinstance(payload, dict) or payload.get("retry_after_ms") or not payload.get("items"):
        return None
    if payload.get("status") not in ("live", "cached") or payload.get("service_status"):
        return None
    try:
        expires = datetime.fromisoformat(payload["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if expires.tzinfo is None:
        return None
    remaining = int((expires - datetime.now(timezone.utc)).total_seconds())
    return remaining if remaining > 0 else None


@api_ns_discover_feeds.route("discover/feeds/trending")
class TrendingFeed(Resource):
    @authenticate
    def get(self):
        if set(request.args) - {"media_type"} or len(request.args.getlist("media_type")) > 1:
            return {"message": "Invalid trending feed parameters."}, 400
        try:
            return _feed(trending(request.args.get("media_type", "all")))
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_feeds.route("discover/feeds/digital")
class DigitalReleaseFeed(Resource):
    @authenticate
    def get(self):
        if set(request.args) - {"region"} or len(request.args.getlist("region")) > 1:
            return {"message": "Invalid digital feed parameters."}, 400
        try:
            return _feed(digital_releases(request.args.get("region", "US")))
        except ValueError as error:
            return {"message": str(error)}, 400


@api_ns_discover_feeds.route("discover/feeds/recent-episodes")
class RecentEpisodeFeed(Resource):
    @authenticate
    def get(self):
        if request.args:
            return {"message": "Recent episodes do not accept feed parameters."}, 400
        return _feed(recent_episodes())
