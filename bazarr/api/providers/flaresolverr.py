# coding=utf-8

from flask import request
from flask_restx import Resource, Namespace

from app.config import settings
from provider_hub.endpoints import check_flaresolverr_address, flaresolverr_status

from ..utils import authenticate

api_ns_providers_flaresolverr = Namespace('Providers FlareSolverr',
                                          description='The FlareSolverr address shared by every provider')


@api_ns_providers_flaresolverr.route('providers/flaresolverr')
class ProvidersFlareSolverr(Resource):
    @authenticate
    @api_ns_providers_flaresolverr.doc(parser=None)
    @api_ns_providers_flaresolverr.response(200, 'Success')
    @api_ns_providers_flaresolverr.response(401, 'Not Authenticated')
    def get(self):
        """Get the effective FlareSolverr address and a detected sibling, if any

        The detected address is a hint only. Nothing is saved until the user accepts it in the settings."""
        return {'data': flaresolverr_status(settings)}

    @authenticate
    @api_ns_providers_flaresolverr.doc(parser=None)
    @api_ns_providers_flaresolverr.response(200, 'Success')
    @api_ns_providers_flaresolverr.response(401, 'Not Authenticated')
    def post(self):
        """Look for a FlareSolverr container next to Bazarr again

        Reports what answered. It never writes the setting."""
        return {'data': flaresolverr_status(settings, refresh=True)}


@api_ns_providers_flaresolverr.route('providers/flaresolverr/test')
class ProvidersFlareSolverrTest(Resource):
    @authenticate
    @api_ns_providers_flaresolverr.doc(parser=None)
    @api_ns_providers_flaresolverr.response(200, 'Success')
    @api_ns_providers_flaresolverr.response(401, 'Not Authenticated')
    def post(self):
        """Test a FlareSolverr address, typed or saved

        Body: {"url": "<address>"}. Answers with ok, a code (ok, not_flaresolverr, unreachable,
        timeout, tls, invalid_address), a short message and the version when it answered. The address
        is checked as typed and is never saved."""
        body = request.get_json(silent=True)
        return {'data': check_flaresolverr_address(body.get('url') if isinstance(body, dict) else None)}
