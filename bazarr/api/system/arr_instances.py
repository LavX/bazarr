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
        body, status = service.create_instance(database, args)
        if status < 400:
            database.commit()
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
        body, status = service.update_instance(database, instance_id, args)
        if status < 400:
            database.commit()
        return body, status

    @authenticate
    def delete(self, instance_id):
        body, status = service.delete_instance(database, instance_id)
        if status < 400:
            database.commit()
        return body, status
