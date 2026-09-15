# coding=utf-8

import os

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from sportarr.filesystem import browse_sportarr_filesystem
from sportarr.rootfolder import list_rootfolder_paths
from app.database import database
from arr_instances.resolution import client_for_instance, default_instance_id

from ..utils import authenticate

api_ns_files_sportarr = Namespace('Files Browser for Sportarr',
                                  description='Browse content of file system as seen by '
                                              'Sportarr')


@api_ns_files_sportarr.route('files/sportarr')
class BrowseSportarrFS(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('path', type=str, default='', help='Path to browse')
    get_request_parser.add_argument('instance_id', type=int, required=False,
                                    help='Owning Sportarr instance id to browse')

    get_response_model = api_ns_files_sportarr.model('SportarrFileBrowserGetResponse', {
        'name': fields.String(),
        'children': fields.Boolean(),
        'path': fields.String(),
    })

    @authenticate
    @api_ns_files_sportarr.response(401, 'Not Authenticated')
    @api_ns_files_sportarr.doc(parser=get_request_parser)
    def get(self):
        """List Sportarr file system content"""
        args = self.get_request_parser.parse_args()
        path = args.get('path')
        # The global sports path-mapping settings leave instance_id unset, so
        # Browse the default Sportarr instance, exactly as the Sonarr and
        # Radarr browsers fall back to the default server.
        instance_id = args.get('instance_id')
        if instance_id is None:
            instance_id = default_instance_id(database, 'sportarr')
        if instance_id is not None and not path:
            # Seed the initial listing with the root folders that have already
            # been synced for this owner: those are the remote paths a sports
            # mapping is actually drawn between, and they answer before any
            # live filesystem probe.
            roots = [
                {'name': os.path.basename(root.rstrip('/\\')) or root,
                 'children': True, 'path': root}
                for root in list_rootfolder_paths(database, instance_id)
            ]
            if roots:
                return marshal(roots, self.get_response_model)
        arr_client = client_for_instance(database, instance_id, enabled_only=False) if instance_id is not None else None
        data = []
        try:
            result = browse_sportarr_filesystem(path, arr_client=arr_client)
            if result is None:
                raise ValueError
        except Exception:
            return []
        for item in result['directories']:
            data.append({'name': item['name'], 'children': True, 'path': item['path']})  # noqa: PERF401
        return marshal(data, self.get_response_model)