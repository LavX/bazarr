# coding=utf-8

import ast

from flask import request
from flask_restx import Namespace, Resource, reqparse

from media_sources import identify, overrides, repository, service
from media_sources.service import SourceError

from ..queued_job import queued_job_response
from ..utils import authenticate

api_ns_sources_items = Namespace("sources_items", description="Items a media source supplies")


def _listing(text):
    try:
        value = ast.literal_eval(text) if text else []
    except (ValueError, SyntaxError):
        return []
    return value if isinstance(value, list) else []


def item_to_dict(item):
    """One item as the API shows it. Paths are the Bazarr-visible ones, as stored."""
    return {
        'id': item.id, 'source_id': item.source_id, 'library_id': item.library_id, 'group_id': item.group_id,
        'kind': item.kind, 'path': item.path, 'title': item.title, 'parsed_title': item.parsed_title,
        'year': item.year, 'season': item.season, 'episode': item.episode,
        'identity_source': item.identity_source, 'identity_state': item.identity_state,
        'identity_candidates': identify.candidates_from_json(item.identity_candidates),
        'identify_pending': bool(item.identify_pending), 'poster': item.poster,
        'imdb_id': item.imdb_id, 'tmdb_id': item.tmdb_id, 'tvdb_id': item.tvdb_id,
        'profile_id': item.profile_id, 'monitored': bool(item.monitored), 'arr_shadow': bool(item.arr_shadow),
        'file_size': item.file_size, 'scene_name': item.scene_name,
        'subtitles': _listing(item.subtitles), 'missing_subtitles': _listing(item.missing_subtitles),
        'indexed_at': item.indexed_at.isoformat() if item.indexed_at else None,
    }


@api_ns_sources_items.route("sources/items")
class SourceItemsList(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('source_id', type=int, required=False, help='Only this source')
    get_request_parser.add_argument('library_id', type=int, required=False, help='Only this library')
    get_request_parser.add_argument('kind', type=str, required=False, choices=repository.ITEM_KINDS,
                                    help='Only this kind of item')
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start')
    get_request_parser.add_argument('length', type=int, required=False, default=25,
                                    help='Paging length, -1 for all')

    @authenticate
    @api_ns_sources_items.doc(parser=get_request_parser)
    def get(self):
        """List source items"""
        args = self.get_request_parser.parse_args()
        filters = dict(source_id=args.get('source_id'), library_id=args.get('library_id'), kind=args.get('kind'))
        length = args.get('length')
        rows = repository.list_items(limit=length if length is not None and length >= 0 else 1_000_000,
                                     offset=max(args.get('start') or 0, 0), **filters)
        return {'data': [item_to_dict(row) for row in rows], 'total': repository.count_items(**filters)}


@api_ns_sources_items.route("sources/items/<int:item_id>")
class SourceItem(Resource):
    @authenticate
    @api_ns_sources_items.response(404, 'No such item')
    def get(self, item_id):
        """Get a source item and its identity"""
        row = repository.get_item(item_id)
        if row is None:
            return {"error": "not_found"}, 404
        return item_to_dict(row)

    @authenticate
    @api_ns_sources_items.response(400, 'Invalid identity correction')
    @api_ns_sources_items.response(404, 'No such item')
    @api_ns_sources_items.response(409, 'The match candidates changed while editing')
    def patch(self, item_id):
        """Correct a source item's identity"""
        try:
            row = overrides.override_identity(item_id, request.get_json(silent=True))
        except SourceError as error:
            return {"error": error.code, "message": str(error)}, error.status
        return item_to_dict(row)


@api_ns_sources_items.route("sources/items/<int:item_id>/search")
class SourceItemSearch(Resource):
    @authenticate
    @api_ns_sources_items.response(202, 'The search was queued as a job')
    @api_ns_sources_items.response(404, 'No such item')
    @api_ns_sources_items.response(409, 'The source or library of the item is disabled')
    def post(self, item_id):
        """Search the item's missing subtitles"""
        if repository.get_item(item_id) is None:
            return {"error": "not_found"}, 404
        try:
            return queued_job_response(service.queue_search(item_id) or None), 202
        except SourceError as error:
            return {"error": error.code, "message": str(error)}, error.status
