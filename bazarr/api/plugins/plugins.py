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
from provider_hub.kinds import UnknownKindError, catalog_kind_of, get_kind, kind_of, known_kinds
from provider_hub.service import CatalogSourceError, ProviderHubConsentError, ProviderHubSettingsError


api_ns_plugins = Namespace('Plugins', description='Plugin catalog and installation lifecycle for every kind')


def _package_too_large():
    return upload_too_large_message('Package', service.MAX_LOCAL_PACKAGE_SIZE), 413


def _kind_filter():
    """The ``kind`` query argument, checked against the kinds this host defines."""
    kind = request.args.get("kind") or None
    if kind is not None:
        get_kind(kind)
    return kind


def _with_runtime(installation, state=None):
    """The installation with how its worker is doing now, when it has one, and which installed plugins depend on
    it. The dependents come from the same check that refuses a removal or a switch-off, so the page shows exactly
    what the server would refuse over: ``dependents`` for a removal and ``enabled_dependents`` for a switch-off."""
    from subtitles.tools.translate.runtime import workers

    if not isinstance(installation, dict) or not installation.get("provider_id"):
        return installation
    plugin_id = installation["provider_id"]
    state = state if state is not None else service.load_state()
    decorated = {**installation,
                 "dependents": service.installed_dependents(plugin_id, state=state),
                 "enabled_dependents": service.installed_dependents(plugin_id, enabled_only=True, state=state)}
    # Subtitle providers run in the provider workers, not the translator pool, so there is nothing to ask for them.
    runtime = None if kind_of(installation) == "subtitle_provider" else workers.runtime_of(plugin_id)
    return decorated if runtime is None else {**decorated, "runtime": runtime}


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
            catalog["entries"] = [entry for entry in catalog["entries"] if catalog_kind_of(entry) == kind]
        return catalog


@api_ns_plugins.route('plugins/catalog/consent')
class PluginsCatalogConsent(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'The consent summary and its digest')
    @api_ns_plugins.response(400, 'The entry cannot be installed')
    @api_ns_plugins.response(401, 'Not Authenticated')
    def get(self):
        """What installing a catalog entry and its dependencies would add, for the person to agree to."""
        try:
            return service.install_consent(
                request.args.get("source"), request.args.get("id"), request.args.get("version"),
                catalog_list=service.CATALOG_LIST_PLUGINS)
        except service.ProviderHubInstallError as error:
            return str(error), 400


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
        state = service.load_state()
        return {"data": [_with_runtime(item, state) for item in service.list_installations(kind=kind)]}

    @authenticate
    @require_password_confirmation
    @api_ns_plugins.response(202, 'Install queued as a job')
    @api_ns_plugins.response(400, 'Invalid request')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(403, 'Password confirmation required')
    @api_ns_plugins.response(409, 'What the install adds changed since the consent summary was shown')
    def post(self):
        # An install names a catalog entry by source, id and version, and
        # nothing else: a raw manifest is not an install request here.
        payload = request.json or {}
        if not isinstance(payload, dict):
            return 'source, id and version are required', 400
        try:
            entry = service.resolve_catalog_install(
                payload.get("source"), payload.get("id"), payload.get("version"),
                catalog_list=service.CATALOG_LIST_PLUGINS)
            # A plugin of a new kind installs only with the digest of the summary the person agreed to,
            # and any digest that is sent must still match the set the install would act on.
            digest = payload.get("consent")
            if digest is not None and not isinstance(digest, str):
                return 'consent must be the digest of the consent summary', 400
            service.check_install_consent(
                digest, entry["source"], entry["provider_id"], entry["version"], entry_key=entry["entry_key"],
                catalog_list=service.CATALOG_LIST_PLUGINS)
        except ProviderHubConsentError as error:
            return str(error), 409 if error.changed else 400
        except service.ProviderHubInstallError as error:
            return str(error), 400
        return {"job_id": hub_jobs.queue_install(
            entry["source"], entry["provider_id"], entry["version"], entry["name"],
            entry_key=entry["entry_key"], consent=digest or None)}, 202


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
        return _with_runtime(installation)

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
    @require_password_confirmation
    @api_ns_plugins.param('keep_data', 'true (the default) keeps its saved settings and data folder, false deletes them')
    @api_ns_plugins.response(202, 'Uninstall queued as a job')
    @api_ns_plugins.response(400, 'Another plugin depends on it, or keep_data is not true or false')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(403, 'Password confirmation required')
    @api_ns_plugins.response(404, 'Plugin not found')
    def delete(self, plugin_id):
        installation = _installation(plugin_id)
        if not installation:
            return 'Plugin not found', 404
        keep = request.args.get("keep_data", "true").strip().lower()
        if keep not in ("true", "false"):
            return 'keep_data must be true or false', 400
        refusal = service.dependent_refusal(plugin_id, "removed")
        if refusal:
            return refusal, 400
        return {"job_id": hub_jobs.queue_uninstall(
            plugin_id, installation.get("name"), keep_data=keep == "true")}, 202


@api_ns_plugins.route('plugins/installations/<string:plugin_id>/repair')
class PluginsInstallationRepair(Resource):
    @authenticate
    @require_password_confirmation
    @api_ns_plugins.response(202, 'Repair queued as a job')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(403, 'Password confirmation required')
    @api_ns_plugins.response(404, 'Plugin not found')
    def post(self, plugin_id):
        installation = _installation(plugin_id)
        if not installation:
            return 'Plugin not found', 404
        return {"job_id": hub_jobs.queue_repair(plugin_id, installation.get("name"))}, 202


@api_ns_plugins.route('plugins/installations/<string:plugin_id>/log')
class PluginsInstallationLog(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(404, 'Plugin not found')
    def get(self, plugin_id):
        """The plugin's recent output: the stderr lines of its worker, and the last error Bazarr+ recorded."""
        from provider_hub.compatibility import refusal_text
        from secret_store.scrub import scrub_text
        from subtitles.tools.translate.runtime import workers

        installation = _installation(plugin_id)
        if not installation:
            return 'Plugin not found', 404
        # The worker's session scrubs what it keeps. The text is scrubbed again here, since this is the one
        # place the lines leave the host.
        lines = [scrub_text(line) for line in workers.stderr_tail_of(plugin_id)]
        last_error = installation.get("last_error")
        return {"lines": lines, "last_error": refusal_text(last_error) if last_error else None}


@api_ns_plugins.route('plugins/installations/<string:plugin_id>/review/dismiss')
class PluginsInstallationReviewDismiss(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(404, 'Plugin not found')
    def post(self, plugin_id):
        if not _installation(plugin_id):
            return 'Plugin not found', 404
        service.dismiss_review_notice(plugin_id)
        return {"review_pending": False}


@api_ns_plugins.route('plugins/updates/check')
class PluginsUpdatesCheck(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'Success')
    @api_ns_plugins.response(401, 'Not Authenticated')
    def post(self):
        service.refresh_catalog()
        return service.check_updates()


@api_ns_plugins.route('plugins/updates/consent')
class PluginsUpdatesConsent(Resource):
    @authenticate
    @api_ns_plugins.response(200, 'What the update adds, whether it needs consent, and the digest to send back')
    @api_ns_plugins.response(401, 'Not Authenticated')
    @api_ns_plugins.response(404, 'Plugin not found or no update is available')
    def get(self):
        plugin_id = request.args.get("id")
        if not plugin_id or not _installation(plugin_id):
            return 'Plugin not found', 404
        delta = service.update_consent(plugin_id)
        if delta is None:
            return 'No update is available', 404
        return delta


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
        digest = payload.get("consent")
        if digest is not None and not isinstance(digest, str):
            return 'consent must be the digest of the consent summary', 400
        return {"job_id": hub_jobs.queue_update(
            plugin_id, installation.get("name"), consent=digest or None)}, 202


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
