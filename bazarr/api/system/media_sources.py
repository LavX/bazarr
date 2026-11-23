# coding=utf-8

from flask import request
from flask_restx import Namespace, Resource

from media_sources import service
from media_sources.service import SourceError

from ..queued_job import queued_job_response
from ..utils import authenticate

api_ns_system_media_sources = Namespace(
    "media_sources", description="Manage media sources: libraries of series, movies and sports that come "
                                 "from folders or a media server, with no arr in front of them")


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise SourceError('A JSON object is required')
    return body


def _refused(error):
    return {"error": error.code, "message": str(error)}, error.status


@api_ns_system_media_sources.route("system/media-sources")
class MediaSourcesList(Resource):
    @authenticate
    @api_ns_system_media_sources.doc(params={
        'availability': 'Set to 1 to include per-kind navigation availability alongside the source list',
    })
    def get(self):
        """List the media sources with their libraries and roots"""
        sources = service.list_sources()
        if request.args.get('availability') == '1':
            return {"data": sources, "availability": service.source_availability(sources)}, 200
        return sources, 200

    @authenticate
    @api_ns_system_media_sources.response(201, 'The source was created, with its library')
    @api_ns_system_media_sources.response(400, 'The request was refused')
    @api_ns_system_media_sources.response(409, 'A source with that name exists')
    def post(self):
        """Create a folder source or register a saved Jellyfin or Emby connection as a source."""
        try:
            source_id = service.create_source(_body())
        except SourceError as error:
            return {"error": error.code, "message": str(error)}, error.status
        return service.describe_source(source_id), 201


@api_ns_system_media_sources.route("system/media-sources/server-libraries")
class MediaSourceServerLibraries(Resource):
    @authenticate
    @api_ns_system_media_sources.doc(params={'media_server_instance_id': 'Already saved Jellyfin or Emby connection'})
    def get(self):
        """List server libraries and their proposed roots using the saved connection."""
        try:
            return {'data': service.server_libraries(request.args.get('media_server_instance_id'))}
        except SourceError as error:
            return _refused(error)


@api_ns_system_media_sources.route("system/media-sources/server-roots/validate")
class MediaSourceServerRootsValidate(Resource):
    @authenticate
    def post(self):
        """Check mappings for a new server library without changing it."""
        try:
            service.validate_server_roots(_body())
            return {}, 200
        except SourceError as error:
            return _refused(error)


@api_ns_system_media_sources.route("system/media-sources/validate-path")
class MediaSourceValidatePath(Resource):
    @authenticate
    @api_ns_system_media_sources.response(200, 'The refusals and warnings for the folder')
    def post(self):
        """Check a folder before it is added: {path, content_kind?}. Refusals come as errors, the rest as warnings"""
        try:
            return service.validate_path(_body()), 200
        except SourceError as error:
            return _refused(error)


@api_ns_system_media_sources.route("system/media-sources/<int:source_id>")
class MediaSourceItem(Resource):
    @authenticate
    @api_ns_system_media_sources.response(404, 'No such source')
    def get(self, source_id):
        """One media source with its libraries"""
        body = service.describe_source(source_id)
        return (body, 200) if body is not None else ({"error": "not_found"}, 404)

    @authenticate
    @api_ns_system_media_sources.response(409, 'A source with that name exists')
    def patch(self, source_id):
        """Edit a source: {name?, enabled?, scan_frequency_hours?}"""
        try:
            return service.update_source(source_id, _body()), 200
        except SourceError as error:
            return _refused(error)

    @authenticate
    @api_ns_system_media_sources.response(204, 'The source was removed; its history and blacklist rows stay')
    def delete(self, source_id):
        """Remove a source with its libraries, groups and items. History and blacklist rows are kept"""
        try:
            service.delete_source(source_id)
        except SourceError as error:
            return _refused(error)
        return "", 204


@api_ns_system_media_sources.route("system/media-sources/<int:source_id>/libraries")
class MediaSourceLibraries(Resource):
    @authenticate
    @api_ns_system_media_sources.response(201, 'The library was added')
    def post(self, source_id):
        """Add a local folder or a server library with its local root mappings."""
        try:
            return service.add_library(source_id, _body()), 201
        except SourceError as error:
            return _refused(error)


@api_ns_system_media_sources.route("system/media-sources/<int:source_id>/libraries/<int:library_id>")
class MediaSourceLibrary(Resource):
    @authenticate
    @api_ns_system_media_sources.response(400, 'A folder library path cannot be edited')
    def patch(self, source_id, library_id):
        """Edit library settings or replace server roots after confirming the preview count."""
        try:
            return service.update_library(source_id, library_id, _body()), 200
        except SourceError as error:
            return _refused(error)

    @authenticate
    @api_ns_system_media_sources.response(204, 'The library was removed; its history and blacklist rows stay')
    def delete(self, source_id, library_id):
        """Remove a library with its groups and items. History and blacklist rows are kept"""
        try:
            service.delete_library(source_id, library_id)
        except SourceError as error:
            return _refused(error)
        return "", 204


@api_ns_system_media_sources.route("system/media-sources/<int:source_id>/libraries/<int:library_id>/roots/preview")
class MediaSourceRootsPreview(Resource):
    @authenticate
    def post(self, source_id, library_id):
        """Count existing media rows that would be removed by a server root edit."""
        try:
            return {'removed_items': service.preview_library_roots(source_id, library_id, _body().get('roots'))}
        except SourceError as error:
            return _refused(error)


@api_ns_system_media_sources.route("system/media-sources/<int:source_id>/libraries/<int:library_id>/scan")
class MediaSourceLibraryScan(Resource):
    @authenticate
    @api_ns_system_media_sources.response(202, 'The scan was queued as a job')
    def post(self, source_id, library_id):
        """Queue a scan of one library"""
        library = service.describe_library(library_id)
        if library is None or library["source_id"] != source_id:
            return {"error": "not_found"}, 404
        try:
            return queued_job_response(service.queue_scan(source_id, library_id) or None), 202
        except SourceError as error:
            return _refused(error)


@api_ns_system_media_sources.route("system/media-sources/<int:source_id>/scan")
class MediaSourceScan(Resource):
    @authenticate
    @api_ns_system_media_sources.response(202, 'The scan was queued as a job')
    @api_ns_system_media_sources.response(404, 'No such source')
    @api_ns_system_media_sources.response(409, 'The source or its library is disabled')
    def post(self, source_id):
        """Queue a scan of every enabled library of the source"""
        if service.describe_source(source_id) is None:
            return {"error": "not_found"}, 404
        try:
            return queued_job_response(service.queue_scan(source_id) or None), 202
        except SourceError as error:
            return {"error": error.code, "message": str(error)}, error.status
