# coding=utf-8

from flask import request
from flask_restx import Namespace, Resource
import sqlalchemy as sa

from media_sources import identify, overrides, repository, service
from media_sources.db import handle
from media_sources.indexer import effective_profile_id
from media_sources.service import SourceError

from ..queued_job import queued_job_response
from ..utils import authenticate

api_ns_sources_groups = Namespace("sources_groups", description="Groups of media source items")


def group_to_dict(row):
    library = repository.get_library(row.library_id)
    if library is not None and library.source_id != row.source_id:
        library = None
    source = repository.get_source(row.source_id)
    return {
        'id': row.id, 'source_id': row.source_id, 'library_id': row.library_id, 'kind': row.kind,
        'title': row.title, 'parsed_title': row.parsed_title, 'year': row.year, 'poster': row.poster,
        'imdb_id': row.imdb_id, 'tmdb_id': row.tmdb_id, 'tvdb_id': row.tvdb_id,
        'identity_source': row.identity_source, 'identity_state': row.identity_state,
        'identity_candidates': identify.candidates_from_json(row.identity_candidates),
        'identify_pending': bool(row.identify_pending),
        'profile_id': row.profile_id, 'monitored': bool(row.monitored), 'resolution_note': row.resolution_note,
        'effective_profile_id': effective_profile_id(row, library=library),
        'path': row.group_key if source is not None and source.kind == 'folder' else None,
    }


@api_ns_sources_groups.route("sources/groups/<int:group_id>")
class SourceGroup(Resource):
    @authenticate
    @api_ns_sources_groups.response(404, 'No such group')
    def get(self, group_id):
        """Get a source group and its identity"""
        row = repository.get_group(group_id)
        kind = request.args.get('kind')
        if row is None or (kind is not None and row.kind != kind):
            return {"error": "not_found"}, 404
        return group_to_dict(row)

    @authenticate
    @api_ns_sources_groups.response(400, 'Invalid identity correction or language profile')
    @api_ns_sources_groups.response(404, 'No such group')
    @api_ns_sources_groups.response(409, 'The match candidates changed while editing')
    def patch(self, group_id):
        """Correct a source group's identity or change its language profile"""
        body = request.get_json(silent=True)
        kind = request.args.get('kind')
        try:
            if isinstance(body, dict) and 'profile_id' in body:
                row = service.update_group_profile(group_id, body, expected_kind=kind)
            else:
                row = overrides.override_identity(group_id, body, group=True, expected_kind=kind)
        except SourceError as error:
            return {"error": error.code, "message": str(error)}, error.status
        return group_to_dict(row)


@api_ns_sources_groups.route("sources/groups/<int:group_id>/search")
class SourceGroupSearch(Resource):
    @authenticate
    @api_ns_sources_groups.response(202, 'The search was queued as a job')
    @api_ns_sources_groups.response(404, 'No such show')
    @api_ns_sources_groups.response(409, 'The source or library is disabled')
    def post(self, group_id):
        """Search eligible missing subtitles within this source show."""
        try:
            return queued_job_response(service.queue_group_search(group_id) or None), 202
        except SourceError as error:
            return {"error": error.code, "message": str(error)}, error.status


@api_ns_sources_groups.route("sources/groups/<int:group_id>/items")
class SourceLeagueItems(Resource):
    @authenticate
    def get(self, group_id):
        """Return the source league's events, with dated events first."""
        from .items import _item_detail

        group = repository.get_group(group_id)
        if group is None or group.kind != 'league':
            return {'error': 'not_found'}, 404
        library = repository.get_library(group.library_id)
        items = repository.Items
        rows = handle().execute(sa.select(items).where(
            items.c.group_id == group_id, items.c.source_id == group.source_id,
            items.c.library_id == group.library_id, items.c.kind == 'sports_event').order_by(
                items.c.event_date.asc().nulls_last(), items.c.season.asc().nulls_last(),
                items.c.episode.asc().nulls_last(), items.c.part_number, items.c.id))
        data = [_item_detail(item, group, library) for item in rows]
        return {'data': data, 'total': len(data)}
