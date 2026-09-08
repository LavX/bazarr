# coding=utf-8

from flask import request
from flask_restx import Namespace, Resource

from app.database import database
from media_servers import service
from media_servers.dispatcher import get_refresh_status, retry_pending_refreshes
from media_servers.http import MediaServerError
from ..utils import authenticate

api_ns_system_media_server_instances = Namespace('media_server_instances', description='Native media server destinations')
_ROOT = 'system/media-server-instances'


def _body(*, optional=False):
    if optional and not request.data and not request.form:
        return {}
    return request.get_json(silent=True)


@api_ns_system_media_server_instances.route(_ROOT)
class MediaServerInstances(Resource):
    @authenticate
    def get(self):
        return service.list_instances(database, request.args.get('kind'))

    @authenticate
    def post(self):
        return service.create_instance(database, _body())


@api_ns_system_media_server_instances.route(_ROOT + '/<string:instance_id>')
class MediaServerInstance(Resource):
    @authenticate
    def get(self, instance_id):
        return service.get_instance(database, instance_id)

    @authenticate
    def patch(self, instance_id):
        return service.update_instance(database, instance_id, _body())

    @authenticate
    def delete(self, instance_id):
        return service.delete_instance(database, instance_id)


@api_ns_system_media_server_instances.route(_ROOT + '/<string:instance_id>/test-connection')
class MediaServerInstanceTest(Resource):
    @authenticate
    def post(self, instance_id):
        return service.probe_instance(database, instance_id, _body(optional=True))


@api_ns_system_media_server_instances.route(_ROOT + '/<string:instance_id>/libraries')
class MediaServerInstanceLibraries(Resource):
    @authenticate
    def post(self, instance_id):
        return service.probe_instance(database, instance_id, _body(optional=True), libraries=True)


@api_ns_system_media_server_instances.route(_ROOT + '/<string:instance_id>/status')
class MediaServerInstanceStatus(Resource):
    @authenticate
    def get(self, instance_id):
        _body, status = service.get_instance(database, instance_id)
        if status != 200:
            return _body, status
        try:
            return get_refresh_status(instance_id), 200
        except MediaServerError:
            return {'pending': 0, 'state': 'unconfirmed', 'error_code': 'migration_failed'}, 200


@api_ns_system_media_server_instances.route(_ROOT + '/<string:instance_id>/retry-pending')
class MediaServerInstanceRetry(Resource):
    @authenticate
    def post(self, instance_id):
        _body, status = service.get_instance(database, instance_id)
        if status != 200:
            return _body, status
        body = request.get_json(silent=True) if request.data else {}
        if not isinstance(body, dict) or body:
            return {'error_code': 'invalid_settings'}, 400
        try:
            return {'queued': retry_pending_refreshes(instance_id)}, 200
        except MediaServerError:
            return {'queued': 0}, 200
