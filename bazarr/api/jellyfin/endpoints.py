# coding=utf-8

from flask_restx import Resource, reqparse

from . import api_ns_jellyfin
from ..utils import authenticate
from jellyfin.operations import (
    jellyfin_test_connection,
    jellyfin_get_libraries,
    jellyfin_refresh_all_libraries,
)


@api_ns_jellyfin.route('jellyfin/test-connection')
class JellyfinTestConnection(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('url', type=str, required=True, help='Jellyfin server URL')
    post_request_parser.add_argument('apikey', type=str, required=True, help='Jellyfin API key')

    @authenticate
    @api_ns_jellyfin.doc(parser=post_request_parser)
    @api_ns_jellyfin.response(200, 'Success')
    @api_ns_jellyfin.response(401, 'Not Authenticated')
    def post(self):
        """Test connection to a Jellyfin server with provided credentials."""
        args = self.post_request_parser.parse_args()
        result = jellyfin_test_connection(url=args['url'], apikey=args['apikey'])

        return result, 200


@api_ns_jellyfin.route('jellyfin/libraries')
class JellyfinLibraries(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('url', type=str, required=False, location='args', help='Jellyfin server URL')
    get_request_parser.add_argument('apikey', type=str, required=False, location='args', help='Jellyfin API key')

    @authenticate
    @api_ns_jellyfin.doc(parser=get_request_parser)
    @api_ns_jellyfin.response(200, 'Success')
    @api_ns_jellyfin.response(401, 'Not Authenticated')
    def get(self):
        """List available movie and series libraries from the Jellyfin server.
        Accepts optional url/apikey params to query before saving config."""
        args = self.get_request_parser.parse_args()
        libraries = jellyfin_get_libraries(url=args.get('url'), apikey=args.get('apikey'))
        return {'data': libraries}, 200


@api_ns_jellyfin.route('jellyfin/refresh-libraries')
class JellyfinRefreshLibraries(Resource):
    @authenticate
    @api_ns_jellyfin.response(200, 'Success')
    @api_ns_jellyfin.response(401, 'Not Authenticated')
    def post(self):
        """Trigger an on-demand refresh for every configured Jellyfin library
        (movie + series). Used by the Maintenance card on the Settings page
        so users can verify their setup without waiting for the next subtitle
        download to fire the auto-refresh.
        """
        return jellyfin_refresh_all_libraries(), 200
