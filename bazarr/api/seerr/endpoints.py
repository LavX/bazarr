# coding=utf-8

from flask import request
from flask_restx import Resource, reqparse

from app.config import settings
from media_servers.http import MediaServerError
from seerr import operations
from . import api_ns_seerr
from ..utils import authenticate

_MEDIA_TYPES = ('movie', 'tv')


def _parse_verify_ssl(raw):
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() not in ('false', '0', 'no', 'off')


def _configured():
    return bool(settings.general.use_seerr and settings.seerr.url and settings.seerr.apikey)


def _status_payload(media_type, tmdb_id):
    if not _configured():
        return {"configured": False, "error_code": "not_configured"}
    try:
        with operations.get_seerr_client() as client:
            cap = operations.capability(client.public_settings())
            status, body = client.media(media_type, tmdb_id)
            if status == 403:
                return {"configured": True, "error_code": "rejected_key"}
            return {"configured": True,
                    **operations.normalize_media(media_type, tmdb_id, status, body, cap, operations.link_base(cap))}
    except MediaServerError as error:
        code = "upstream_error" if error.code in ("upstream_error", "invalid_response", "server_error") else "unreachable"
        return {"configured": True, "error_code": code}
    except ValueError:
        return {"configured": False, "error_code": "not_configured"}


@api_ns_seerr.route('seerr/test-connection')
class SeerrTestConnection(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('url', type=str, required=True, help='Seerr URL')
    # Body only: this is Seerr's admin key, forwarded to a caller-supplied
    # host, so it must never travel in a URL.
    post_request_parser.add_argument('apikey', type=str, required=True, help='Seerr API key',
                                     location=('json', 'form'))
    post_request_parser.add_argument('verify_ssl', type=str, required=False)

    @authenticate
    @api_ns_seerr.doc(parser=post_request_parser)
    @api_ns_seerr.response(200, 'Success')
    @api_ns_seerr.response(401, 'Not Authenticated')
    def post(self):
        args = self.post_request_parser.parse_args()
        if args['apikey'] == '***':
            return {"success": False, "error_code": "configuration"}, 200
        return operations.test_connection(args['url'], args['apikey'], _parse_verify_ssl(args.get('verify_ssl'))), 200


@api_ns_seerr.route('seerr/media/<string:media_type>/<int:tmdb_id>')
class SeerrMedia(Resource):
    @authenticate
    @api_ns_seerr.response(200, 'Success')
    @api_ns_seerr.response(401, 'Not Authenticated')
    def get(self, media_type, tmdb_id):
        if media_type not in _MEDIA_TYPES or tmdb_id <= 0:
            return {"error": "invalid media"}, 400
        return _status_payload(media_type, tmdb_id), 200


@api_ns_seerr.route('seerr/media/tv/by-tvdb/<int:tvdb_id>')
class SeerrMediaByTvdb(Resource):
    @authenticate
    @api_ns_seerr.response(200, 'Success')
    @api_ns_seerr.response(401, 'Not Authenticated')
    def get(self, tvdb_id):
        if not _configured():
            return {"configured": False, "error_code": "not_configured"}, 200
        tmdb_id = operations.tmdb_id_for_tvdb(tvdb_id)
        if tmdb_id is None:
            return {"configured": True, "error_code": "unresolved"}, 200
        return {"tmdb_id": tmdb_id, **_status_payload('tv', tmdb_id)}, 200


def _validated_request_body(body):
    if not isinstance(body, dict):
        return None
    media_type, tmdb_id = body.get('media_type'), body.get('tmdb_id')
    if media_type not in _MEDIA_TYPES or type(tmdb_id) is not int or tmdb_id <= 0:
        return None
    payload = {"mediaType": media_type, "mediaId": tmdb_id, "is4k": body.get('is4k') is True}
    tvdb_id = body.get('tvdb_id')
    if tvdb_id is not None:
        if type(tvdb_id) is not int or tvdb_id <= 0:
            return None
        payload["tvdbId"] = tvdb_id
    seasons = body.get('seasons')
    if seasons is not None:
        if seasons == "all":
            payload["seasons"] = "all"
        elif isinstance(seasons, list) and seasons and all(type(s) is int and s >= 0 for s in seasons):
            payload["seasons"] = sorted(set(seasons))
        else:
            return None
    if media_type == 'tv' and "seasons" not in payload:
        payload["seasons"] = "all"
    return payload


@api_ns_seerr.route('seerr/request')
class SeerrRequest(Resource):
    @authenticate
    @api_ns_seerr.response(200, 'Success')
    @api_ns_seerr.response(400, 'Invalid request')
    @api_ns_seerr.response(401, 'Not Authenticated')
    def post(self):
        payload = _validated_request_body(request.get_json(silent=True))
        if payload is None:
            return {"error": "invalid request"}, 400
        if not _configured():
            return {"error_code": "not_configured"}, 200
        try:
            with operations.get_seerr_client() as client:
                cap = operations.capability(client.public_settings())
                status, body = client.create_request(payload)
                result = operations.request_outcome(status, body, cap)
                # Only a successful outcome gets a link: an error body must
                # stay exactly the error_code shape, never gain extra keys
                # that could carry upstream detail toward the client.
                if "outcome" in result:
                    base = operations.link_base(cap)
                    if base:
                        result["link"] = f"{base}/{payload['mediaType']}/{payload['mediaId']}"
                return result, 200
        except MediaServerError as error:
            code = "upstream_error" if error.code in ("invalid_response", "server_error") else "unreachable"
            return {"error_code": code}, 200
        except ValueError:
            return {"error_code": "not_configured"}, 200
