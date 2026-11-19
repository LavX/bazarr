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


@api_ns_system_media_sources.route("system/media-sources")
class MediaSourcesList(Resource):
    @authenticate
    @api_ns_system_media_sources.response(201, 'The source was created, with its library')
    @api_ns_system_media_sources.response(400, 'The request was refused')
    @api_ns_system_media_sources.response(409, 'A source with that name exists')
    def post(self):
        """Create a folder source with one library: {name, library: {path, content_kind, profile_id?}}"""
        try:
            source_id = service.create_source(_body())
        except SourceError as error:
            return {"error": "invalid", "message": str(error)}, error.status
        return service.describe_source(source_id), 201


@api_ns_system_media_sources.route("system/media-sources/<int:source_id>/scan")
class MediaSourceScan(Resource):
    @authenticate
    @api_ns_system_media_sources.response(202, 'The scan was queued as a job')
    @api_ns_system_media_sources.response(404, 'No such source')
    def post(self, source_id):
        """Queue a scan of every enabled library of the source"""
        if service.describe_source(source_id) is None:
            return {"error": "not_found"}, 404
        return queued_job_response(service.queue_scan(source_id) or None), 202
