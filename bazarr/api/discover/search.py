from flask import request
from flask_restx import Namespace, Resource

from discover.library import CopyUnavailable
from discover.search import queue_search, validate_context
from ..queued_job import queued_job_model, queued_job_response
from ..utils import authenticate

api_ns_discover_search = Namespace("Discover", description="Explicit title subtitle searches")


@api_ns_discover_search.route("discover/search")
class DiscoverSearch(Resource):
    post_job_model = api_ns_discover_search.model("DiscoverSearchQueued", queued_job_model)

    @authenticate
    @api_ns_discover_search.response(202, "Search queued as a job; its progress and rows are the job result",
                                     post_job_model)
    @api_ns_discover_search.response(400, "Invalid search context")
    @api_ns_discover_search.response(401, "Not Authenticated")
    @api_ns_discover_search.response(409, "The chosen copy no longer resolves")
    def post(self):
        """Search subtitles for a title or a release name as a job

        The context is validated here, so a bad request answers at once. The search itself runs as a job
        that publishes the providers heard from and the rows built so far while it works, and the whole
        snapshot at the end: read it from system/jobs/result."""
        try:
            prepared = validate_context(request.get_json(silent=True))
        except CopyUnavailable as error:
            # Recoverable by an explicit new choice, never by substitution.
            return {"message": str(error), "reason": "copy_unavailable", "recoverable": True}, 409
        except ValueError as error:
            return {"message": str(error)}, 400
        return queued_job_response(queue_search(prepared)), 202
