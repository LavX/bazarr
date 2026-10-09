# coding=utf-8

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from sonarr.filesystem import browse_sonarr_filesystem
from app.database import database
from arr_instances.resolution import client_for_instance

from ..utils import authenticate
from .directories import directory_rows

api_ns_files_sonarr = Namespace('Files Browser for Sonarr', description='Browse content of file system as seen by '
                                                                        'Sonarr')


@api_ns_files_sonarr.route('files/sonarr')
class BrowseSonarrFS(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('path', type=str, default='', help='Path to browse')
    get_request_parser.add_argument('instance_id', type=int, required=False,
                                    help='Owning Sonarr instance id to browse (#156)')

    get_response_model = api_ns_files_sonarr.model('SonarrFileBrowserGetResponse', {
        'name': fields.String(),
        'children': fields.Boolean(),
        'path': fields.String(),
    })

    @authenticate
    @api_ns_files_sonarr.response(401, 'Not Authenticated')
    @api_ns_files_sonarr.doc(parser=get_request_parser)
    def get(self):
        """List Sonarr file system content"""
        args = self.get_request_parser.parse_args()
        path = args.get('path')
        # When an instance_id is given, browse THAT instance's Sonarr (#156);
        # otherwise the default-server behaviour is unchanged.
        instance_id = args.get('instance_id')
        arr_client = client_for_instance(database, instance_id, enabled_only=False) if instance_id is not None else None
        try:
            result = browse_sonarr_filesystem(path, arr_client=arr_client)
            data = directory_rows(result, 'Sonarr')
            if data is None:
                raise ValueError
        except Exception:
            return []
        return marshal(data, self.get_response_model)
