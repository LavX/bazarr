# coding=utf-8

from flask import request
from flask_restx import Resource

from . import api_ns_silo
from ..utils import authenticate
from media_servers.http import MediaServerError, parse_verify_ssl
from silo.operations import silo_get_libraries, silo_test_connection


def _connection_settings():
    body = request.get_json(silent=True) if request.is_json else request.form
    if not body or not hasattr(body, 'get'):
        raise MediaServerError('missing_credentials')
    if set(body) - {'url', 'apikey', 'verify_ssl'}:
        raise MediaServerError('invalid_settings')
    url, apikey = body.get('url'), body.get('apikey')
    if not all(isinstance(value, str) and value.strip() for value in (url, apikey)):
        raise MediaServerError('missing_credentials')
    return url, apikey, parse_verify_ssl(body.get('verify_ssl', True))


@api_ns_silo.route('silo/test-connection')
class SiloTestConnection(Resource):
    @authenticate
    @api_ns_silo.response(200, 'Connection result')
    @api_ns_silo.response(400, 'Invalid connection settings')
    @api_ns_silo.response(401, 'Not Authenticated')
    def post(self):
        """Test the explicitly supplied unsaved native connection settings."""
        try:
            values = _connection_settings()
        except MediaServerError as error:
            return {'success': False, 'error_code': error.code}, 400
        return silo_test_connection(*values), 200


@api_ns_silo.route('silo/libraries')
class SiloLibraries(Resource):
    @authenticate
    @api_ns_silo.response(200, 'Library result')
    @api_ns_silo.response(400, 'Invalid connection settings')
    @api_ns_silo.response(401, 'Not Authenticated')
    def post(self):
        """List supported libraries using explicitly supplied unsaved settings."""
        try:
            values = _connection_settings()
        except MediaServerError as error:
            return {'data': [], 'error_code': error.code}, 400
        return silo_get_libraries(*values), 200
