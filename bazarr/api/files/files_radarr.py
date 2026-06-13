# coding=utf-8

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from radarr.filesystem import browse_radarr_filesystem
from app.database import database
from arr_instances.resolution import client_for_instance

from ..utils import authenticate

api_ns_files_radarr = Namespace('Files Browser for Radarr', description='Browse content of file system as seen by '
                                                                        'Radarr')


@api_ns_files_radarr.route('files/radarr')
class BrowseRadarrFS(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('path', type=str, default='', help='Path to browse')
    get_request_parser.add_argument('instance_id', type=int, required=False,
                                    help='Owning Radarr instance id to browse (#156)')

    get_response_model = api_ns_files_radarr.model('RadarrFileBrowserGetResponse', {
        'name': fields.String(),
        'children': fields.Boolean(),
        'path': fields.String(),
    })

    @authenticate
    @api_ns_files_radarr.response(401, 'Not Authenticated')
    @api_ns_files_radarr.doc(parser=get_request_parser)
    def get(self):
        """List Radarr file system content"""
        args = self.get_request_parser.parse_args()
        path = args.get('path')
        # When an instance_id is given, browse THAT instance's Radarr (#156);
        # otherwise the default-server behaviour is unchanged.
        instance_id = args.get('instance_id')
        arr_client = client_for_instance(database, instance_id) if instance_id is not None else None
        data = []
        try:
            result = browse_radarr_filesystem(path, arr_client=arr_client)
            if result is None:
                raise ValueError
        except Exception:
            return []
        for item in result['directories']:
            data.append({'name': item['name'], 'children': True, 'path': item['path']})  # noqa: PERF401
        return marshal(data, self.get_response_model)
