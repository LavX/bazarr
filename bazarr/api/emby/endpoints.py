# coding=utf-8

from flask import request
from flask_restx import Resource

from . import api_ns_emby
from ..utils import authenticate
from emby.operations import emby_test_connection
from media_servers.http import MediaServerError, parse_verify_ssl


@api_ns_emby.route('emby/test-connection')
class EmbyTestConnection(Resource):
    @authenticate
    @api_ns_emby.response(200, 'Connection result')
    @api_ns_emby.response(400, 'Invalid connection settings')
    @api_ns_emby.response(401, 'Not Authenticated')
    def post(self):
        """Test the explicitly supplied unsaved connection settings."""
        body = request.get_json(silent=True) if request.is_json else request.form
        if not body or not hasattr(body, 'get'):
            return {'success': False, 'error_code': 'missing_credentials'}, 400
        if set(body) - {'url', 'apikey', 'verify_ssl'}:
            return {'success': False, 'error_code': 'invalid_settings'}, 400
        url, apikey = body.get('url'), body.get('apikey')
        if not all(isinstance(value, str) and value.strip() for value in (url, apikey)):
            return {'success': False, 'error_code': 'missing_credentials'}, 400
        try:
            verify_ssl = parse_verify_ssl(body.get('verify_ssl', True))
        except MediaServerError as error:
            return {'success': False, 'error_code': error.code}, 400
        return emby_test_connection(url, apikey, verify_ssl), 200
