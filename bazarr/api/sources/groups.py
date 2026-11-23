# coding=utf-8

from flask import request
from flask_restx import Namespace, Resource

from media_sources import identify, overrides, repository
from media_sources.service import SourceError

from ..utils import authenticate

api_ns_sources_groups = Namespace("sources_groups", description="Groups of media source items")


def group_to_dict(row):
    return {
        'id': row.id, 'source_id': row.source_id, 'library_id': row.library_id, 'kind': row.kind,
        'title': row.title, 'parsed_title': row.parsed_title, 'year': row.year, 'poster': row.poster,
        'imdb_id': row.imdb_id, 'tmdb_id': row.tmdb_id, 'tvdb_id': row.tvdb_id,
        'identity_source': row.identity_source, 'identity_state': row.identity_state,
        'identity_candidates': identify.candidates_from_json(row.identity_candidates),
        'identify_pending': bool(row.identify_pending),
        'profile_id': row.profile_id, 'monitored': bool(row.monitored), 'resolution_note': row.resolution_note,
    }


@api_ns_sources_groups.route("sources/groups/<int:group_id>")
class SourceGroup(Resource):
    @authenticate
    @api_ns_sources_groups.response(404, 'No such group')
    def get(self, group_id):
        """Get a source group and its identity"""
        row = repository.get_group(group_id)
        if row is None:
            return {"error": "not_found"}, 404
        return group_to_dict(row)

    @authenticate
    @api_ns_sources_groups.response(400, 'Invalid identity correction')
    @api_ns_sources_groups.response(404, 'No such group')
    @api_ns_sources_groups.response(409, 'The match candidates changed while editing')
    def patch(self, group_id):
        """Correct a source group's identity"""
        try:
            row = overrides.override_identity(group_id, request.get_json(silent=True), group=True)
        except SourceError as error:
            return {"error": error.code, "message": str(error)}, error.status
        return group_to_dict(row)
