# coding=utf-8
"""Kind-neutral plugin routes.

The same lifecycle as the provider-hub routes, for every kind: listings take
an optional ``kind`` filter, and one installation is addressed by its plugin
id whatever its kind. The provider-hub routes stay as the Subtitle Hub's view
of the subtitle provider kind.

A route that starts code carries the same decorator stack as the provider-hub
route it mirrors, the password confirmation included.
"""
import os
import tempfile

from flask import request
from flask_restx import Namespace, Resource

from api.password_confirm import require_password_confirmation
from api.utils import (UploadTooLarge, authenticate, copy_bounded_upload, upload_declared_too_large,
                       upload_too_large_message)
from provider_hub import jobs as hub_jobs
from provider_hub import service
from provider_hub.kinds import UnknownKindError, get_kind, kind_of, known_kinds
from provider_hub.service import CatalogSourceError, ProviderHubSettingsError


api_ns_plugins = Namespace('Plugins', description='Plugin catalog and installation lifecycle for every kind')


def _package_too_large():
    return upload_too_large_message('Package', service.MAX_LOCAL_PACKAGE_SIZE), 413


def _kind_filter():
    """The ``kind`` query argument, checked against the kinds this host defines."""
    kind = request.args.get("kind") or None
    if kind is not None:
        get_kind(kind)
    return kind


def _installation(plugin_id):
    """The installation to serve; None when it is absent or of a kind this host does not define."""
    row = service.get_provider(plugin_id, redact=False)
    if row is None or kind_of(row) not in known_kinds():
        return None
    return service.get_provider(plugin_id)


@api_ns_plugins.route('plugins/catalog')
class PluginsCatalog(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(400, 'Unknown kind')
    @api_ns_plugins.response(401, 'Not Authenticated')
    def get(self):
        try:
            kind = _kind_filter()
        except UnknownKindError as error:
            return str(error), 400
        catalog = service.list_catalog(auto_refresh=True)
        if kind is not None:
            catalog["entries"] = [entry for entry in catalog["entries"] if kind_of(entry) == kind]
        return catalog


@api_ns_plugins.route('plugins/catalog/refresh')
class PluginsCatalogRefresh(Resource):
    @authenticate
    @api_ns_plugins.response(202, 'Refresh queued as a job')
    @api_ns_plugins.response(401, 'Not Authenticated')
    def post(self):
        return {"job_id": hub_jobs.queue_catalog_refresh()}, 202


@api_ns_plugins.route('plugins/installations')
class PluginsInstallations(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(400, 'Unknown kind')
    @api_ns_plugins.response(401, 'Not Authenticated')
    def get(self):
        try:
            kind = _kind_filter()
        except UnknownKindError as error:
            return str(error), 400
        return {"data": service.list_installations(kind=kind)}

    @authenticate
    @require_password_confirmation
    @api_ns_plugins.response(202, 'Install queued as a job')
    @api_ns_plugins.response(400, 'Invalid request')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(403, 'Password confirmation required')
    def post(self):
        # An install names a catalog entry by source, id and version, and
        # nothing else: a raw manifest is not an install request here.
        payload = request.json or {}
        if not isinstance(payload, dict):
            return 'source, id and version are required', 400
        try:
            entry = service.resolve_catalog_install(payload.get("source"), payload.get("id"), payload.get("version"))
        except service.ProviderHubInstallError as error:
            return str(error), 400
        return {"job_id": hub_jobs.queue_install(
            entry["source"], entry["provider_id"], entry["version"], entry["name"])}, 202


@api_ns_plugins.route('plugins/installations/local')
class PluginsLocalInstallations(Resource):
    @authenticate
    @require_password_confirmation
    @api_ns_plugins.response(202, 'Install queued as a job')
    @api_ns_plugins.response(400, 'Invalid package')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(403, 'Password confirmation required')
    @api_ns_plugins.response(413, 'Package is too large')
    def post(self):
        # Same as the provider-hub route: refuse an oversized package from its
        # declared length before the form is parsed, spool the upload to a file
        # with a bounded copy, and queue the path, never the bytes.
        if upload_declared_too_large(service.MAX_LOCAL_PACKAGE_SIZE):
            return _package_too_large()
        upload = request.files.get("file") or request.files.get("package")
        if upload is None:
            return 'a .zip package file is required', 400
        uploads = service.local_package_uploads_dir()
        uploads.mkdir(parents=True, exist_ok=True)
        handle, package_path = tempfile.mkstemp(prefix='package-', suffix='.zip', dir=str(uploads))
        try:
            with os.fdopen(handle, 'wb') as spool:
                size = copy_bounded_upload(upload, spool, service.MAX_LOCAL_PACKAGE_SIZE)
            if not size:
                service.discard_local_package(package_path)
                return 'uploaded package is empty', 400
            job_id = hub_jobs.queue_install_local(package_path, upload.filename or None)
        except UploadTooLarge:
            service.discard_local_package(package_path)
            return _package_too_large()
        except BaseException:
            service.discard_local_package(package_path)
            raise
        return {"job_id": job_id}, 202


@api_ns_plugins.route('plugins/installations/<string:plugin_id>')
class PluginsInstallation(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(404, 'Plugin not found')
    def get(self, plugin_id):
        installation = _installation(plugin_id)
        if not installation:
            return 'Plugin not found', 404
        return installation

    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(400, 'Invalid request')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(404, 'Plugin not found')
    @api_ns_plugins.response(500, 'The change could not be saved')
    def patch(self, plugin_id):
        payload = request.json
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return 'a JSON object is required', 400
        try:
            row = service.get_provider(plugin_id, redact=False)
            if row is not None and kind_of(row) not in known_kinds():
                return 'Plugin not found', 404
            installation = service.update_provider(
                plugin_id,
                enabled=payload.get("enabled") if "enabled" in payload else None,
                config=payload.get("config") if "config" in payload else None,
            )
        except ValueError as error:
            return str(error), 400
        except ProviderHubSettingsError as error:
            return str(error), 500
        if not installation:
            return 'Plugin not found', 404
        return installation

    @authenticate
    @api_ns_plugins.response(202, 'Uninstall queued as a job')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(404, 'Plugin not found')
    def delete(self, plugin_id):
        installation = _installation(plugin_id)
        if not installation:
            return 'Plugin not found', 404
        return {"job_id": hub_jobs.queue_uninstall(plugin_id, installation.get("name"))}, 202


@api_ns_plugins.route('plugins/updates/check')
class PluginsUpdatesCheck(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(401, 'Not Authenticated')
    def post(self):
        service.refresh_catalog()
        return service.check_updates()


@api_ns_plugins.route('plugins/updates/apply')
class PluginsUpdatesApply(Resource):
    @authenticate
    @require_password_confirmation
    @api_ns_plugins.response(202, 'Update queued as a job')
    @api_ns_plugins.response(400, 'Invalid request')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(403, 'Password confirmation required')
    @api_ns_plugins.response(404, 'Plugin not found')
    def post(self):
        payload = request.json
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return 'a JSON object is required', 400
        plugin_id = payload.get("id")
        if not isinstance(plugin_id, str) or not plugin_id:
            return 'id is required', 400
        installation = _installation(plugin_id)
        if not installation:
            return 'Plugin not found', 404
        return {"job_id": hub_jobs.queue_update(plugin_id, installation.get("name"))}, 202


@api_ns_plugins.route('plugins/catalog/sources')
class PluginsCatalogSources(Resource):
    @authenticate
    @require_password_confirmation
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(400, 'Invalid catalog source')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(403, 'Password confirmation required')
    def post(self):
        payload = request.json
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return 'a JSON object is required', 400
        try:
            return service.add_catalog_source(name=payload.get("name"), url=payload.get("url"))
        except CatalogSourceError as error:
            return str(error), 400


@api_ns_plugins.route('plugins/jobs')
class PluginsJobs(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(401, 'Not Authenticated')
    def get(self):
        return {"data": service.list_jobs()}


@api_ns_plugins.route('plugins/catalog/sources/<path:name>')
class PluginsCatalogSource(Resource):
    @authenticate
    @api_ns_plugins.response(204, 'Success')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(404, 'Catalog source not found')
    def delete(self, name):
        if not service.remove_catalog_source(name):
            return 'Catalog source not found', 404
        return '', 204

    @authenticate
    @require_password_confirmation
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(400, 'Invalid catalog source')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(403, 'Password confirmation required')
    @api_ns_plugins.response(404, 'Catalog source not found')
    def patch(self, name):
        payload = request.json
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return 'a JSON object is required', 400
        kwargs = {"dev_ref": payload["dev_ref"]} if "dev_ref" in payload else {}
        try:
            source = service.update_catalog_source(name, **kwargs)
        except CatalogSourceError as error:
            return str(error), 400
        if source is None:
            return 'Catalog source not found', 404
        service.refresh_catalog()
        return service.get_catalog_source(name) or source


@api_ns_plugins.route('plugins/jobs/<string:job_id>')
class PluginsJob(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(404, 'Job not found')
    def get(self, job_id):
        job = service.get_job(job_id)
        if not job:
            return 'Job not found', 404
        return job
