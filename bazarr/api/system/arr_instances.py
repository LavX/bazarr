# coding=utf-8

import logging

from flask import request
from flask_restx import Namespace, Resource, inputs, reqparse

from app.database import database
from app.jobs_queue import jobs_queue
from arr_instances import service

from ..utils import authenticate

api_ns_system_arr_instances = Namespace(
    "arr_instances", description="Manage Sonarr, Radarr and Sportarr instances")

# Plaintext API keys are accepted only in the JSON body, never in the URL/query,
# and are never echoed back (responses carry api_key_set, not the key).
_create_parser = reqparse.RequestParser()
_create_parser.add_argument("kind", type=str, required=True, location="json")
_create_parser.add_argument("name", type=str, required=True, location="json")
_create_parser.add_argument("api_key", type=str, location="json")
_create_parser.add_argument("ip", type=str, location="json")
_create_parser.add_argument("port", type=service.whole_number, location="json")
_create_parser.add_argument("base_url", type=str, location="json")
_create_parser.add_argument("ssl", type=bool, location="json")
_create_parser.add_argument("verify_ssl", type=bool, location="json")
_create_parser.add_argument("http_timeout", type=service.whole_number, location="json")
_create_parser.add_argument("enabled", type=bool, location="json")
_create_parser.add_argument("is_default", type=bool, location="json")
_create_parser.add_argument("subtitle_settings", type=dict, location="json")
_create_parser.add_argument("media_defaults", type=dict, location="json")
_create_parser.add_argument("sports_settings", type=dict, location="json")
_create_parser.add_argument("path_mappings", type=lambda value: value, location="json", store_missing=False)

_update_parser = reqparse.RequestParser()
_update_parser.add_argument("name", type=str, location="json")
_update_parser.add_argument("api_key", type=str, location="json")
_update_parser.add_argument("clear_api_key", type=bool, location="json")
_update_parser.add_argument("ip", type=str, location="json")
_update_parser.add_argument("port", type=service.whole_number, location="json")
_update_parser.add_argument("base_url", type=str, location="json")
_update_parser.add_argument("ssl", type=bool, location="json")
_update_parser.add_argument("verify_ssl", type=bool, location="json")
_update_parser.add_argument("http_timeout", type=service.whole_number, location="json")
_update_parser.add_argument("enabled", type=bool, location="json")
_update_parser.add_argument("is_default", type=bool, location="json")
_update_parser.add_argument("subtitle_settings", type=dict, location="json")
_update_parser.add_argument("media_defaults", type=dict, location="json")
_update_parser.add_argument("sports_settings", type=dict, location="json")
_update_parser.add_argument("path_mappings", type=lambda value: value, location="json", store_missing=False)

_test_parser = reqparse.RequestParser()
_test_parser.add_argument("kind", type=str, required=True, location="json")
_test_parser.add_argument("api_key", type=str, location="json")
_test_parser.add_argument("ip", type=str, location="json")
_test_parser.add_argument("port", type=service.whole_number, location="json")
_test_parser.add_argument("base_url", type=str, location="json")
_test_parser.add_argument("ssl", type=bool, location="json")
_test_parser.add_argument("verify_ssl", type=bool, location="json")
_test_parser.add_argument("http_timeout", type=service.whole_number, location="json")

# By-id test: the kind and API key come from the stored row (the key never
# leaves the server), so only optional connection overrides are accepted here.
_test_by_id_parser = reqparse.RequestParser()
_test_by_id_parser.add_argument("ip", type=str, location="json")
_test_by_id_parser.add_argument("port", type=service.whole_number, location="json")
_test_by_id_parser.add_argument("base_url", type=str, location="json")
_test_by_id_parser.add_argument("ssl", type=bool, location="json")
_test_by_id_parser.add_argument("verify_ssl", type=bool, location="json")
_test_by_id_parser.add_argument("http_timeout", type=service.whole_number, location="json")


@api_ns_system_arr_instances.route("/system/arr-instances")
class ArrInstancesList(Resource):
    @authenticate
    def get(self):
        kind = request.args.get("kind") or None
        body, status = service.list_instances(database, kind)
        return body, status

    @authenticate
    def post(self):
        args = _create_parser.parse_args()
        # Persist the master key to config.yaml BEFORE the encrypted api_key is
        # written: the repository flush commits immediately under AUTOCOMMIT, so
        # a key generated only in memory here would be lost on restart and make
        # the stored api_key undecryptable.
        from secret_store import persist_master_key
        persist_master_key()
        body, status = service.create_instance(database, args)
        if status < 400:
            database.commit()
            # Rebuild scheduler sync jobs + re-fan-out this kind's SignalR feed so
            # the new instance is live without a restart (#156). Best-effort: the
            # row is already committed.
            service.refresh_runtime(body.get("kind"), instance_id=body.get("id"))
        return body, status


@api_ns_system_arr_instances.route("/system/arr-instances/<int:instance_id>")
class ArrInstanceItem(Resource):
    @authenticate
    def get(self, instance_id):
        body, status = service.get_instance(database, instance_id)
        return body, status

    @authenticate
    def patch(self, instance_id):
        args = _update_parser.parse_args()
        # See post(): persist the master key before a (possibly new) encrypted
        # api_key is committed.
        if args.get("api_key"):
            from secret_store import persist_master_key
            persist_master_key()
        body, status = service.update_instance(database, instance_id, args)
        if status < 400:
            database.commit()
            # Rebuild jobs + re-fan-out this kind's SignalR (#156). A disabled
            # instance's per-instance sync job is now orphaned, so remove it (the
            # rebuild only adds/replaces jobs, never removes them).
            service.refresh_runtime(
                body.get("kind"), instance_id=body.get("id"),
                removed=not body.get("enabled", True))
        return body, status

    delete_request_parser = reqparse.RequestParser()
    # reqparse also puts help in front of the 400 for a value it cannot read,
    # so it stays short. The docstring below has the details.
    delete_request_parser.add_argument(
        "remove_library", type=inputs.boolean, location="args", default=False,
        help="Also delete the library synced from this Sonarr or Radarr instance.")

    @authenticate
    @api_ns_system_arr_instances.expect(delete_request_parser)
    @api_ns_system_arr_instances.response(204, "Deleted")
    @api_ns_system_arr_instances.response(
        409, "Still owns synced rows (can_remove_library and library say what), "
             "or its library sync or a subtitle job for it is running")
    def delete(self, instance_id):
        """Delete an instance.

        A Sonarr or Radarr instance that still owns synced rows is refused
        with 409 unless remove_library is true. With it, the series, episodes,
        movies, history, exclusions and root folders Bazarr+ synced from the
        instance go with it, and for the last instance of its kind that
        includes the rows of that kind no instance owns. Database rows only:
        no file on disk is touched. A Sonarr or Radarr delete, with or without
        it, is refused with 409 while a library sync of the instance, or of
        its whole kind, is running or queued (sync_in_progress), or while a
        subtitle job that may write for it is running (job_in_progress), such
        as a search its sync queued. Whether or not it is set,
        deleting the last Sonarr or Radarr instance turns Use Sonarr or Use
        Radarr off. Sportarr ignores it.
        """
        remove_library = self.delete_request_parser.parse_args()["remove_library"]
        # Capture the kind before the row is gone so the post-delete refresh can
        # scope to the right scheduler/SignalR feed and remove the orphaned job.
        existing, _ = service.get_instance(database, instance_id)
        kind = existing.get("kind") if isinstance(existing, dict) else None
        body, status = service.delete_instance(database, instance_id,
                                               remove_library=remove_library)
        if status < 400:
            database.commit()
            # Switch a kind with no instance left off first: the refresh
            # restarts the live feed, and with the kind still on and no
            # instance it would start the fallback feed on the stored
            # connection settings, which still describe the deleted server.
            service.after_instance_deleted(database, kind, removed_library=remove_library)
            service.refresh_runtime(kind, instance_id=instance_id, removed=True)
        return body, status


@api_ns_system_arr_instances.route("/system/arr-instances/test")
class ArrInstanceTest(Resource):
    @authenticate
    def post(self):
        # Connection details, including the plaintext API key, come from the
        # JSON body only - never the URL/query - so the key never lands in logs
        # or request lines.
        args = _test_parser.parse_args()
        body, status = service.test_connection(args)
        return body, status


@api_ns_system_arr_instances.route("/system/arr-instances/<int:instance_id>/test")
class ArrInstanceTestById(Resource):
    @authenticate
    def post(self, instance_id):
        # Tests a SAVED instance with its stored (decrypted) key, so the card
        # "Test" and the edit-modal "Keep current key" mode work without the
        # plaintext key ever reaching the browser. The body carries only
        # optional connection overrides for unsaved edits.
        args = _test_by_id_parser.parse_args()
        body, status = service.test_connection_for_instance(database, instance_id, args)
        return body, status


@api_ns_system_arr_instances.route(
    "/system/arr-instances/<int:instance_id>/apply-default-profile")
class ArrInstanceApplyDefaultProfile(Resource):
    @authenticate
    def post(self, instance_id):
        """Assign this instance's default language profile to its media that has
        none yet.

        Opt-in and one-way: only rows with no profile are filled in, so
        hand-picked profiles survive. Reassigning media that already has a
        profile stays with the mass-edit profile selector in the Series and
        Movies views, where it is explicit and reviewable.
        """
        body, status = service.apply_default_profile(database, instance_id)
        if status >= 400:
            return body, status
        database.commit()

        # Recompute what is missing for exactly the items that changed, in the
        # background. Each pass scans every episode of an item and emits events,
        # so doing a whole library inside this request would hold a web worker
        # for minutes and can outlive a proxy timeout.
        upstream_ids = body.get("upstream_ids") or []
        if upstream_ids:
            try:
                jobs_queue.feed_jobs_pending_queue(
                    job_name=f"Refreshing missing subtitles for {len(upstream_ids)} items",
                    module="arr_instances.service",
                    func="reindex_after_default_profile",
                    kwargs={"kind": body.get("kind"), "upstream_ids": upstream_ids,
                            "arr_instance_id": instance_id},
                    is_progress=False)
            except Exception:
                # The profiles are already committed. Failing the request here
                # would report that Apply did nothing while the library was in
                # fact updated, and invite a retry that finds nothing to do. The
                # scheduled indexer picks the refresh up regardless.
                logging.exception(
                    "BAZARR could not queue the missing-subtitles refresh after applying "
                    "the default language profile of instance %s", instance_id)

        # upstream_ids is an implementation detail of the refresh above.
        return {"updated": body["updated"], "profileId": body["profileId"]}, 200
