# coding=utf-8

from flask_restx import Resource, reqparse

from . import api_ns_jellyfin
from ..utils import authenticate
from jellyfin.operations import (
    jellyfin_test_connection,
    jellyfin_get_libraries,
    jellyfin_refresh_all_libraries,
)


def _parse_verify_ssl(raw):
    """Form / query strings arrive as 'true'/'false' (Mantine + Settings UI).
    Treat None as "use saved setting" so callers can opt out."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() not in ('false', '0', 'no', 'off')


@api_ns_jellyfin.route('jellyfin/test-connection')
class JellyfinTestConnection(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('url', type=str, required=True, help='Jellyfin server URL')
    post_request_parser.add_argument('apikey', type=str, required=True, help='Jellyfin API key')
    post_request_parser.add_argument('verify_ssl', type=str, required=False,
                                     help='Override saved verify_ssl flag for this test (true/false)')

    @authenticate
    @api_ns_jellyfin.doc(parser=post_request_parser)
    @api_ns_jellyfin.response(200, 'Success')
    @api_ns_jellyfin.response(401, 'Not Authenticated')
    def post(self):
        """Test connection to a Jellyfin server with provided credentials.

        verify_ssl, if provided, overrides the saved setting so the user can
        Test before Saving when toggling the verify_ssl checkbox in the UI.
        Without this, the test would always use the saved value, which makes
        the toggle feel broken on first use."""
        args = self.post_request_parser.parse_args()
        result = jellyfin_test_connection(
            url=args['url'],
            apikey=args['apikey'],
            verify_ssl=_parse_verify_ssl(args.get('verify_ssl')),
        )

        return result, 200


@api_ns_jellyfin.route('jellyfin/libraries')
class JellyfinLibraries(Resource):
    # POST (not GET) so apikey rides in the request body instead of the
    # query string. apikey-in-URL leaks to browser history, reverse-proxy
    # access logs, and any URL telemetry; body keeps it inside the TLS
    # tunnel and out of long-lived logs.
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('url', type=str, required=False, help='Jellyfin server URL')
    post_request_parser.add_argument('apikey', type=str, required=False, help='Jellyfin API key')
    post_request_parser.add_argument('verify_ssl', type=str, required=False,
                                     help='Override saved verify_ssl flag for this query (true/false)')

    @authenticate
    @api_ns_jellyfin.doc(parser=post_request_parser)
    @api_ns_jellyfin.response(200, 'Success')
    @api_ns_jellyfin.response(401, 'Not Authenticated')
    def post(self):
        """List available movie and series libraries from the Jellyfin server.
        Accepts optional url/apikey params (in the request body) to query
        before saving config."""
        args = self.post_request_parser.parse_args()
        libraries = jellyfin_get_libraries(
            url=args.get('url'),
            apikey=args.get('apikey'),
            verify_ssl=_parse_verify_ssl(args.get('verify_ssl')),
        )
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
