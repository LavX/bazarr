# coding=utf-8
from flask import request
from flask_restx import Namespace, Resource, reqparse

from api.utils import authenticate
from provider_hub import service
from provider_hub.manifest import ManifestValidationError
from provider_hub.service import CatalogSourceError, ProviderHubInstallError


api_ns_provider_hub = Namespace('Provider Hub', description='Provider Hub catalog and installation lifecycle')


@api_ns_provider_hub.route('provider-hub/catalog')
class ProviderHubCatalog(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    def get(self):
        return service.list_catalog(auto_refresh=True)


@api_ns_provider_hub.route('provider-hub/catalog/refresh')
class ProviderHubCatalogRefresh(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    def post(self):
        return service.refresh_catalog()


@api_ns_provider_hub.route('provider-hub/catalog/sources')
class ProviderHubCatalogSources(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(400, 'Invalid catalog source')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    def post(self):
        payload = request.json or {}
        try:
            return service.add_catalog_source(
                name=payload.get("name"),
                url=payload.get("url"),
            )
        except CatalogSourceError as error:
            return str(error), 400


@api_ns_provider_hub.route('provider-hub/catalog/sources/<string:name>')
class ProviderHubCatalogSource(Resource):
    @authenticate
    @api_ns_provider_hub.response(204, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    @api_ns_provider_hub.response(404, 'Catalog source not found')
    def delete(self, name):
        if not service.remove_catalog_source(name):
            return 'Catalog source not found', 404
        return '', 204


@api_ns_provider_hub.route('provider-hub/providers')
class ProviderHubProviders(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    def get(self):
        return {"data": service.list_providers()}


@api_ns_provider_hub.route('provider-hub/providers/<string:provider_id>')
class ProviderHubProvider(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    @api_ns_provider_hub.response(404, 'Provider not found')
    def get(self, provider_id):
        provider = service.get_provider(provider_id)
        if not provider:
            return 'Provider not found', 404
        return provider

    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    @api_ns_provider_hub.response(404, 'Provider not found')
    def patch(self, provider_id):
        payload = request.json or {}
        try:
            provider = service.update_provider(
                provider_id,
                enabled=payload.get("enabled") if "enabled" in payload else None,
                config=payload.get("config") if "config" in payload else None,
            )
        except ValueError as error:
            return str(error), 400
        if not provider:
            return 'Provider not found', 404
        return provider


@api_ns_provider_hub.route('provider-hub/providers/<string:provider_id>/test')
class ProviderHubProviderTest(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    @api_ns_provider_hub.response(404, 'Provider not found')
    def post(self, provider_id):
        provider = service.get_provider(provider_id)
        if not provider:
            return 'Provider not found', 404
        return {"provider_id": provider_id, "status": "pending_restart" if provider.get("pending_restart") else "ready"}


@api_ns_provider_hub.route('provider-hub/installations')
class ProviderHubInstallations(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(400, 'Invalid manifest')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    def post(self):
        payload = request.json or {}
        manifest = payload.get("manifest") if isinstance(payload, dict) else None
        if not isinstance(manifest, dict):
            return 'manifest is required', 400
        try:
            return service.stage_install(manifest)
        except (ManifestValidationError, ProviderHubInstallError) as error:
            return str(error), 400


@api_ns_provider_hub.route('provider-hub/installations/<string:provider_id>')
class ProviderHubInstallation(Resource):
    @authenticate
    @api_ns_provider_hub.response(204, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    @api_ns_provider_hub.response(404, 'Provider not found')
    def delete(self, provider_id):
        if not service.remove_installation(provider_id):
            return 'Provider not found', 404
        return '', 204


@api_ns_provider_hub.route('provider-hub/updates/check')
class ProviderHubUpdatesCheck(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    def post(self):
        service.refresh_catalog()
        return service.check_updates()


@api_ns_provider_hub.route('provider-hub/updates/apply')
class ProviderHubUpdatesApply(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('provider_id', type=str, required=True)

    @authenticate
    @api_ns_provider_hub.doc(parser=post_request_parser)
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    @api_ns_provider_hub.response(404, 'Provider not found')
    def post(self):
        args = self.post_request_parser.parse_args()
        provider = service.apply_update(args["provider_id"])
        if not provider:
            return 'Provider not found', 404
        return provider


@api_ns_provider_hub.route('provider-hub/jobs')
class ProviderHubJobs(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    def get(self):
        return {"data": service.list_jobs()}


@api_ns_provider_hub.route('provider-hub/jobs/<string:job_id>')
class ProviderHubJob(Resource):
    @authenticate
    @api_ns_provider_hub.response(200, 'Success')
    @api_ns_provider_hub.response(401, 'Not Authenticated')
    @api_ns_provider_hub.response(404, 'Job not found')
    def get(self, job_id):
        job = service.get_job(job_id)
        if not job:
            return 'Job not found', 404
        return job
