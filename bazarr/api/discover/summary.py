from flask_restx import Namespace, Resource

from discover.summary import get_summary
from ..utils import authenticate

api_ns_discover_summary = Namespace("Discover summary", description="Read-only local work summary")


@api_ns_discover_summary.route("discover/summary")
class DiscoverSummary(Resource):
    @authenticate
    def get(self):
        return get_summary()
