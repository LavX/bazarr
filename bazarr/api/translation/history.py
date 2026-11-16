# coding=utf-8
"""Translation history of the v2 translation runtime.

Dark: while the ``translator.runtime_v2`` switch is off, or its tables are missing,
every route here answers 404 before it touches the database. The old
``/api/translator/*`` routes are not related to these and do not change.
"""
from flask_restx import Resource, Namespace, reqparse
from werkzeug.exceptions import NotFound

from app.database import database
from subtitles.tools.translate.runtime.history import HistoryStore

from ..utils import authenticate
from .guard import runtime_v2_only

api_ns_translation_history = Namespace('Translation History', description='History of translation jobs')

MAX_PAGE = 200


@api_ns_translation_history.route('translation/history')
class TranslationHistory(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start integer')
    get_request_parser.add_argument('length', type=int, required=False, default=50,
                                    help=f'Paging length integer, at most {MAX_PAGE}')
    get_request_parser.add_argument('status', type=str, required=False, help='Only jobs with this status')
    get_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                    help='Only jobs of this Sonarr or Radarr instance')

    @runtime_v2_only
    @authenticate
    @api_ns_translation_history.doc(parser=get_request_parser)
    @api_ns_translation_history.response(401, 'Not Authenticated')
    @api_ns_translation_history.response(404, 'Translation runtime is off')
    def get(self):
        """List translation jobs, newest first, each with the connections that contributed to it"""
        args = self.get_request_parser.parse_args()
        length = args.get('length')
        length = MAX_PAGE if length is None or length < 0 else min(length, MAX_PAGE)
        rows, total = HistoryStore(database).list_jobs(
            start=max(0, args.get('start') or 0), length=length, status=args.get('status') or None,
            arr_instance_id=args.get('arr_instance_id'))
        return {'data': rows, 'total': total}


@api_ns_translation_history.route('translation/history/<int:history_id>')
class TranslationHistoryItem(Resource):
    @runtime_v2_only
    @authenticate
    @api_ns_translation_history.response(401, 'Not Authenticated')
    @api_ns_translation_history.response(404, 'Not found, or the translation runtime is off')
    def get(self, history_id):
        """Get one translation job with its per-connection rows"""
        row = HistoryStore(database).get(history_id)
        if row is None:
            raise NotFound()
        return row
