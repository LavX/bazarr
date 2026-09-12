import re

from flask import request
from flask_restx import Namespace, Resource

from discover import progress
from discover.library import CopyUnavailable
from discover.search import SearchAdmissionExpired, search, validate_context
from ..utils import authenticate

api_ns_discover_search = Namespace("Discover", description="Explicit title subtitle searches")


@api_ns_discover_search.route("discover/search")
class DiscoverSearch(Resource):
    @authenticate
    def post(self):
        try:
            prepared = validate_context(request.get_json(silent=True))
        except CopyUnavailable as error:
            # Recoverable by an explicit new choice, never by substitution.
            return {"message": str(error), "reason": "copy_unavailable", "recoverable": True}, 409
        except ValueError as error:
            return {"message": str(error)}, 400
        identity = request.headers.get("X-Discover-Progress")
        if identity and not re.fullmatch(r"[a-f0-9-]{36}", identity):
            return {"message": "Invalid progress identity."}, 400
        if identity and not progress.begin(identity):
            identity = None  # Observation capacity must not block a search.
        try:
            snapshot = search(prepared, on_progress=(
                lambda observation: progress.publish(identity, observation)) if identity else None)
            if identity:
                progress.publish(identity, {"phase": "finished", "providers": snapshot["coverage"]["providers"]})
            return snapshot
        except SearchAdmissionExpired as error:
            return {"message": str(error)}, 400
        finally:
            if identity:
                progress.finish(identity)

    @authenticate
    def get(self):
        identity = request.args.get("progress_id", "")
        observation = progress.read(identity)
        if observation is None:
            return {"phase": "preparing", "providers": []}, 202
        return observation
