# coding=utf-8

import base64

from flask import jsonify, make_response
from flask_restx import Namespace, Resource, reqparse
from werkzeug.datastructures import FileStorage

from subtitles.tools.archives import (
    ArchiveError,
    extract_subtitles_from_archive,
    is_archive,
)

from ..utils import authenticate

api_ns_subtitle_archive = Namespace(
    'SubtitleArchive',
    description='Extract subtitle files from an uploaded compressed archive')

# Cap the uploaded archive itself; the extractor caps the uncompressed payload.
MAX_ARCHIVE_SIZE = 50 * 1024 * 1024  # 50 MiB


@api_ns_subtitle_archive.route('subtitles/archive')
class SubtitleArchive(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument(
        'file', type=FileStorage, location='files', required=True,
        help='Compressed archive (.zip/.rar/.7z) of subtitle files')

    @authenticate
    @api_ns_subtitle_archive.doc(parser=post_request_parser)
    @api_ns_subtitle_archive.response(200, 'Success')
    @api_ns_subtitle_archive.response(400, 'Not a valid or supported archive')
    @api_ns_subtitle_archive.response(401, 'Not Authenticated')
    def post(self):
        """Extract subtitle files from an uploaded archive (#233).

        Returns the contained subtitle files (non-subtitle entries discarded) as
        base64 so the frontend can rebuild them into the normal per-file upload
        flow, where the user assigns language/forced/HI per file.
        """
        args = self.post_request_parser.parse_args()
        uploaded = args.get('file')
        filename = uploaded.filename or ''

        if not is_archive(filename):
            return 'Unsupported archive type. Use .zip, .rar or .7z.', 400

        data = uploaded.read()
        if len(data) > MAX_ARCHIVE_SIZE:
            return 'Archive is too large.', 400

        try:
            extracted = extract_subtitles_from_archive(filename, data)
        except ArchiveError as e:
            return str(e), 400

        files = [
            {'name': name,
             'content': base64.b64encode(content).decode('ascii')}
            for name, content in extracted
        ]
        return make_response(jsonify({'files': files, 'count': len(files)}), 200)
