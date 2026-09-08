from flask import request
from flask_restx import Namespace, Resource

from discover.search import SearchAdmissionExpired, search, validate_context
from ..utils import authenticate

api_ns_discover_search = Namespace("Discover", description="Explicit title subtitle searches")


@api_ns_discover_search.route("discover/search")
class DiscoverSearch(Resource):
    @authenticate
    def post(self):
        try:
            prepared = validate_context(request.get_json(silent=True))
        except ValueError as error:
            return {"message": str(error)}, 400
        try:
            return search(prepared)
        except SearchAdmissionExpired as error:
            return {"message": str(error)}, 400
