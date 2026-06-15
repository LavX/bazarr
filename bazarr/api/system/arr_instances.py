# coding=utf-8

from flask import request
from flask_restx import Namespace, Resource, reqparse

from app.database import database
from arr_instances import service

from ..utils import authenticate

api_ns_system_arr_instances = Namespace(
    "arr_instances", description="Manage multiple Sonarr/Radarr instances")

# Plaintext API keys are accepted only in the JSON body, never in the URL/query,
# and are never echoed back (responses carry api_key_set, not the key).
_create_parser = reqparse.RequestParser()
_create_parser.add_argument("kind", type=str, required=True, location="json")
_create_parser.add_argument("name", type=str, required=True, location="json")
_create_parser.add_argument("api_key", type=str, location="json")
_create_parser.add_argument("ip", type=str, location="json")
_create_parser.add_argument("port", type=int, location="json")
_create_parser.add_argument("base_url", type=str, location="json")
_create_parser.add_argument("ssl", type=bool, location="json")
_create_parser.add_argument("verify_ssl", type=bool, location="json")
_create_parser.add_argument("http_timeout", type=int, location="json")
_create_parser.add_argument("enabled", type=bool, location="json")
_create_parser.add_argument("is_default", type=bool, location="json")

_update_parser = reqparse.RequestParser()
_update_parser.add_argument("name", type=str, location="json")
_update_parser.add_argument("api_key", type=str, location="json")
_update_parser.add_argument("clear_api_key", type=bool, location="json")
_update_parser.add_argument("ip", type=str, location="json")
_update_parser.add_argument("port", type=int, location="json")
_update_parser.add_argument("base_url", type=str, location="json")
_update_parser.add_argument("ssl", type=bool, location="json")
_update_parser.add_argument("verify_ssl", type=bool, location="json")
_update_parser.add_argument("http_timeout", type=int, location="json")
_update_parser.add_argument("enabled", type=bool, location="json")
_update_parser.add_argument("is_default", type=bool, location="json")

_test_parser = reqparse.RequestParser()
_test_parser.add_argument("kind", type=str, required=True, location="json")
_test_parser.add_argument("api_key", type=str, location="json")
_test_parser.add_argument("ip", type=str, location="json")
_test_parser.add_argument("port", type=int, location="json")
_test_parser.add_argument("base_url", type=str, location="json")
_test_parser.add_argument("ssl", type=bool, location="json")
_test_parser.add_argument("verify_ssl", type=bool, location="json")
_test_parser.add_argument("http_timeout", type=int, location="json")

# By-id test: the kind and API key come from the stored row (the key never
# leaves the server), so only optional connection overrides are accepted here.
_test_by_id_parser = reqparse.RequestParser()
_test_by_id_parser.add_argument("ip", type=str, location="json")
_test_by_id_parser.add_argument("port", type=int, location="json")
_test_by_id_parser.add_argument("base_url", type=str, location="json")
_test_by_id_parser.add_argument("ssl", type=bool, location="json")
_test_by_id_parser.add_argument("verify_ssl", type=bool, location="json")
_test_by_id_parser.add_argument("http_timeout", type=int, location="json")


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

    @authenticate
    def delete(self, instance_id):
        # Capture the kind before the row is gone so the post-delete refresh can
        # scope to the right scheduler/SignalR feed and remove the orphaned job.
        existing, _ = service.get_instance(database, instance_id)
        kind = existing.get("kind") if isinstance(existing, dict) else None
        body, status = service.delete_instance(database, instance_id)
        if status < 400:
            database.commit()
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
