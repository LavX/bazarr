# coding=utf-8

import re

from flask import request
from flask_restx import Namespace, Resource

from media_servers import links
from ..utils import authenticate

api_ns_system_media_server_links = Namespace('media_server_links',
                                             description='Where a library row opens on each media server')
_ROOT = 'system/media-server-links'


def _row():
    """The media type and local id a request names, or None when it is not one."""
    media_type, raw_id = request.args.get('media_type'), request.args.get('id', '')
    if media_type not in links.MEDIA_TYPES or not re.fullmatch(r'[0-9]{1,10}', raw_id) or int(raw_id) <= 0:
        return None
    return media_type, int(raw_id)


@api_ns_system_media_server_links.route(_ROOT + '/destinations')
class MediaServerLinkDestinations(Resource):
    @authenticate
    def get(self):
        """Every destination a link can open, with the label its row carries."""
        return {'data': links.destination_rows()}, 200


@api_ns_system_media_server_links.route(_ROOT + '/candidates')
class MediaServerLinkCandidates(Resource):
    @authenticate
    def get(self):
        """The destinations that can hold one library row, decided without the network."""
        row = _row()
        if row is None:
            return {'error_code': 'invalid_media'}, 400
        data = links.candidate_rows(*row)
        if data is None:
            return {'error_code': 'not_found'}, 404
        return {'data': data}, 200


@api_ns_system_media_server_links.route(_ROOT + '/resolve')
class MediaServerLinkResolve(Resource):
    @authenticate
    def get(self):
        """One destination's answer for one library row."""
        row, instance_id = _row(), request.args.get('instance_id', '')
        if row is None or not instance_id:
            return {'error_code': 'invalid_media'}, 400
        answer = links.resolve(instance_id, *row)
        if answer is None:
            return {'error_code': 'not_found'}, 404
        return answer, 200
