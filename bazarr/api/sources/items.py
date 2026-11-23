# coding=utf-8

import ast
import json
import os
from io import BytesIO

from flask import request
from flask_restx import Namespace, Resource, inputs, reqparse
from subliminal_patch.core import SUBTITLE_EXTENSIONS
from werkzeug.datastructures import FileStorage

from media_sources import identify, identity, overrides, repository, service
from media_sources.api_rows import postprocess_source
from media_sources.service import SourceError
from media_sources.context import SourceItemGone, resolve_item, source_item_title
from media_sources.indexer import effective_profile_id
from media_sources.manual import current_manual_source, queue_source_download, source_search_title
from app.jobs_queue import JobFailed
from languages.get_languages import alpha3_from_alpha2
from subtitles.manual import manual_search, queue_manual_search
from subtitles.search_result import marshal_search_results
from subtitles.tools.delete import delete_subtitles
from subtitles.tools.delete_ownership import SubtitleDeletionError
from subtitles.upload import manual_upload_subtitle

from ..queued_job import queued_job_response
from ..utils import (MAX_SUBTITLE_UPLOAD_SIZE, UploadTooLarge, authenticate, read_bounded_upload,
                     upload_declared_too_large, upload_too_large_message)

api_ns_sources_items = Namespace("sources_items", description="Items a media source supplies")


def _listing(text):
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return []
    return value if isinstance(value, list) else []


def item_to_dict(item):
    """One item as the API shows it, with its last mapped path if it no longer claims a file."""
    subtitles = [entry[:3] if isinstance(entry, (list, tuple)) and len(entry) == 4 and entry[1]
                 and isinstance(entry[3], (list, tuple)) else entry for entry in _listing(item.subtitles)]
    return {
        'id': item.id, 'source_id': item.source_id, 'library_id': item.library_id, 'group_id': item.group_id,
        'kind': item.kind, 'path': identity.last_mapped_path(item.path), 'title': item.title,
        'parsed_title': item.parsed_title,
        'year': item.year, 'season': item.season, 'episode': item.episode,
        'episode_title': item.episode_title, 'part_number': item.part_number, 'part_name': item.part_name,
        'identity_source': item.identity_source, 'identity_state': item.identity_state,
        'identity_candidates': identify.candidates_from_json(item.identity_candidates),
        'identify_pending': bool(item.identify_pending), 'poster': item.poster,
        'imdb_id': item.imdb_id, 'tmdb_id': item.tmdb_id, 'tvdb_id': item.tvdb_id,
        'profile_id': item.profile_id, 'monitored': bool(item.monitored), 'arr_shadow': bool(item.arr_shadow),
        'shadowed_by': item.shadow_owner, 'server_missing': bool(item.server_missing),
        'audio_language': _listing(item.audio_language),
        'file_size': item.file_size, 'scene_name': item.scene_name,
        **({'external_id': item.external_id} if item.external_id is not None else {}),
        'subtitles': subtitles, 'missing_subtitles': _listing(item.missing_subtitles),
        'indexed_at': item.indexed_at.isoformat() if item.indexed_at else None,
        **({'event_date': item.event_date, 'round': item.episode, 'part_number': item.part_number or None,
            'part_name': item.part_name} if item.kind == 'sports_event' else {}),
    }


def _item_detail(item, group, library):
    if group is not None and (group.source_id, group.library_id) != (item.source_id, item.library_id):
        group = None
    if library is not None and library.source_id != item.source_id:
        library = None
    profile = effective_profile_id(item, group, library)
    data = item_to_dict(item)
    details = postprocess_source({
        'subtitles': repr(data['subtitles']), 'missing_subtitles': repr(data['missing_subtitles']),
        'profileId': profile,
    })
    data.update(effective_profile_id=profile, subtitle_details=details['subtitles'],
                missing_subtitle_details=details['missing_subtitles'])
    return data


def item_detail(item):
    group = repository.get_group(item.group_id) if item.group_id is not None else None
    return _item_detail(item, group, repository.get_library(item.library_id))


@api_ns_sources_items.route("sources/items")
class SourceItemsList(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('source_id', type=int, required=False, help='Only this source')
    get_request_parser.add_argument('library_id', type=int, required=False, help='Only this library')
    get_request_parser.add_argument('group_id', type=int, required=False, help='Only items in this group')
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
        filters = {key: args.get(key) for key in ('source_id', 'library_id', 'group_id', 'kind')}
        length = args.get('length')
        rows = repository.list_items(limit=length if length is not None and length >= 0 else 1_000_000,
                                     offset=max(args.get('start') or 0, 0), **filters)
        if rows and args.get('group_id') is not None:
            group = repository.get_group(args['group_id'])
            library = repository.get_library(rows[0].library_id)
            data = [_item_detail(row, group, library) for row in rows]
        else:
            data = [item_to_dict(row) for row in rows]
        return {'data': data, 'total': repository.count_items(**filters)}


@api_ns_sources_items.route("sources/items/<int:item_id>")
class SourceItem(Resource):
    @authenticate
    @api_ns_sources_items.response(404, 'No such item')
    def get(self, item_id):
        """Get a source item and its identity"""
        row = repository.get_item(item_id)
        kind = request.args.get('kind')
        if row is None or (kind is not None and row.kind != kind):
            return {"error": "not_found"}, 404
        return item_detail(row)

    @authenticate
    @api_ns_sources_items.response(400, 'Invalid identity correction or language profile')
    @api_ns_sources_items.response(404, 'No such item')
    @api_ns_sources_items.response(409, 'The match candidates changed while editing')
    def patch(self, item_id):
        """Correct a source item's identity or change its language profile"""
        body = request.get_json(silent=True)
        kind = request.args.get('kind')
        try:
            if isinstance(body, dict) and 'profile_id' in body:
                row = service.update_item_profile(item_id, body, expected_kind=kind)
            else:
                row = overrides.override_identity(item_id, body, expected_kind=kind)
        except SourceError as error:
            return {"error": error.code, "message": str(error)}, error.status
        return item_detail(row)


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


def _manual_target(item_id):
    try:
        return current_manual_source(resolve_item(item_id)), None
    except SourceItemGone:
        return None, ({'error': 'not_found'}, 404)
    except JobFailed as error:
        return None, ({'error': error.reason, 'message': str(error)}, 409)


def _subtitle_options():
    parser = reqparse.RequestParser()
    parser.add_argument('forced', type=inputs.boolean, default=False)
    parser.add_argument('hi', type=inputs.boolean, default=False)
    return parser


def _subtitle_language(code):
    if len(code) != 2 or not alpha3_from_alpha2(code):
        raise ValueError('Unknown subtitle language')
    return code


def _delete_args():
    parser = _subtitle_options()
    parser.add_argument('subtitles_path', type=str, required=True, nullable=False)
    parser.add_argument('language', type=_subtitle_language, required=True, nullable=False, help='Subtitle language code')
    return parser


def _delete_item_subtitle(item_id, args, blacklist=None):
    source, refusal = _manual_target(item_id)
    if refusal:
        return refusal
    if args.get('source_id') is not None and args['source_id'] != source.source_id:
        return {'message': 'The source item no longer belongs to this source.'}, 409
    try:
        removed = delete_subtitles(source.media_type, args['language'], args['forced'], args['hi'],
                                   source.path, args['subtitles_path'], source=source, blacklist=blacklist)
    except SubtitleDeletionError as error:
        return {'message': str(error)}, error.status
    return ('', 204) if removed else ({'message': 'The subtitle could not be deleted.'}, 409)


@api_ns_sources_items.route("sources/items/<int:item_id>/manual")
class SourceItemManual(Resource):
    post_request_parser = _subtitle_options()
    post_request_parser.add_argument('original_format', type=inputs.boolean, default=False)
    post_request_parser.add_argument('provider', type=str, required=True, nullable=False)
    post_request_parser.add_argument('subtitle', type=str, required=True, nullable=False, help='Subtitle token from a manual search')

    @authenticate
    def get(self, item_id):
        """List all matching provider subtitles for a source item"""
        from app.get_providers import get_providers_sorted

        source, refusal = _manual_target(item_id)
        if refusal:
            return refusal
        outcomes = []
        found = manual_search(source.path, source.profile_id, get_providers_sorted(), str(source.scene_name),
                              source_search_title(source), source.media_type, source=source,
                              provider_outcomes=outcomes)
        if isinstance(found, str):
            return {'message': found}, 503
        return {'data': marshal_search_results(found), 'providers': outcomes}

    @authenticate
    @api_ns_sources_items.doc(parser=post_request_parser)
    def post(self, item_id):
        """Queue downloading a chosen provider subtitle for a source item"""
        source, refusal = _manual_target(item_id)
        if refusal:
            return refusal
        args = self.post_request_parser.parse_args()
        job_id = queue_source_download(source, args['provider'], args['subtitle'], str(args['hi']),
                                       str(args['forced']), args['original_format'])
        return queued_job_response(job_id or None), 202


@api_ns_sources_items.route("sources/items/<int:item_id>/manual/search")
class SourceItemManualSearch(Resource):
    @authenticate
    def post(self, item_id):
        """Queue a manual search and return its job ID"""
        source, refusal = _manual_target(item_id)
        if refusal:
            return refusal
        job_id = queue_manual_search(source.path, source.profile_id, str(source.scene_name), source_search_title(source),
                                     source.media_type, source_item_title(source), source=source)
        return queued_job_response(job_id or None), 202


@api_ns_sources_items.route("sources/items/<int:item_id>/subtitles")
class SourceItemSubtitles(Resource):
    post_request_parser = _subtitle_options()
    post_request_parser.add_argument('language', type=_subtitle_language, required=True, nullable=False, help='Subtitle language code')
    post_request_parser.add_argument('file', type=FileStorage, location='files', required=True, nullable=False)
    delete_request_parser = _delete_args()

    @authenticate
    @api_ns_sources_items.doc(parser=post_request_parser)
    def post(self, item_id):
        """Queue uploading a subtitle for a source item"""
        if upload_declared_too_large(MAX_SUBTITLE_UPLOAD_SIZE):
            return upload_too_large_message('Subtitle file', MAX_SUBTITLE_UPLOAD_SIZE), 413
        source, refusal = _manual_target(item_id)
        if refusal:
            return refusal
        args = self.post_request_parser.parse_args()
        uploaded = args['file']
        filename = uploaded.filename or ''
        if os.path.splitext(filename)[1].lower() not in SUBTITLE_EXTENSIONS:
            return {'message': 'A subtitle of an invalid format was uploaded.'}, 400
        try:
            content = BytesIO(read_bounded_upload(uploaded, MAX_SUBTITLE_UPLOAD_SIZE))
        except UploadTooLarge:
            return upload_too_large_message('Subtitle file', MAX_SUBTITLE_UPLOAD_SIZE), 413
        job_id = manual_upload_subtitle(source.path, args['language'], args['forced'], args['hi'], source.media_type,
                                        content, filename, source.audio_language, source=source)
        if not job_id:
            content.close()
        return queued_job_response(job_id or None), 202

    @authenticate
    @api_ns_sources_items.doc(parser=delete_request_parser)
    def delete(self, item_id):
        """Delete one indexed external subtitle from a source item"""
        return _delete_item_subtitle(item_id, self.delete_request_parser.parse_args())


@api_ns_sources_items.route("sources/items/<int:item_id>/blacklist")
class SourceItemBlacklist(Resource):
    post_request_parser = _delete_args()
    post_request_parser.add_argument('provider', type=str, required=True, nullable=False)
    post_request_parser.add_argument('subs_id', type=str, required=True, nullable=False)
    post_request_parser.add_argument('source_id', type=int, nullable=False)

    @authenticate
    @api_ns_sources_items.doc(parser=post_request_parser)
    def post(self, item_id):
        """Delete a source item's subtitle and exclude its provider release"""
        args = self.post_request_parser.parse_args()
        if not args['provider'].strip() or not args['subs_id'].strip():
            return {'message': 'A provider and subtitle ID are required.'}, 400
        return _delete_item_subtitle(item_id, args, blacklist=(args['provider'], args['subs_id']))
