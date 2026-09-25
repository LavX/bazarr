# coding=utf-8

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from app.config import settings
from app.logger import empty_log

from utilities.central import get_log_file_path
from utilities.log_reader import (DEFAULT_LIMIT, LEVEL_CHOICES, MAX_LIMIT, CountCache, StoredFilter,
                                  read_log_page)
from ..utils import authenticate

api_ns_system_logs = Namespace('System Logs', description='List log file entries or empty log file')

# Shared by every request, so a refresh of the newest page counts only what was
# appended since the last one.
_log_counts = CountCache()


def _limit(value):
    number = int(value)
    if number < 1:
        raise ValueError('limit must be at least 1')
    # Capped rather than refused: a client asking for more gets the most there is.
    return min(number, MAX_LIMIT)


_limit.__schema__ = {'type': 'integer', 'minimum': 1, 'maximum': MAX_LIMIT, 'default': DEFAULT_LIMIT}


def _offset(value):
    number = int(value)
    if number < 0:
        raise ValueError('offset cannot be negative')
    return number


_offset.__schema__ = {'type': 'integer', 'minimum': 0, 'default': 0}


def _baseline_total(value):
    number = int(value)
    if number < 0:
        raise ValueError('baseline_total cannot be negative')
    return number


_baseline_total.__schema__ = {'type': 'integer', 'minimum': 0}


def _level(value):
    name = str(value).strip().lower()
    if name not in LEVEL_CHOICES:
        raise ValueError(f'level must be one of {", ".join(LEVEL_CHOICES)}')
    return name


_level.__schema__ = {'type': 'string', 'enum': list(LEVEL_CHOICES)}


@api_ns_system_logs.route('system/logs')
class SystemLogs(Resource):
    get_response_model = api_ns_system_logs.model('SystemBackupsGetResponse', {
        'timestamp': fields.String(),
        'type': fields.String(),
        'message': fields.String(),
        'exception': fields.String(),
    })

    get_filter_error_model = api_ns_system_logs.model('SystemLogsFilterError', {
        'filter': fields.String(description='include or exclude'),
        'pattern': fields.String(description='The stored pattern'),
        'message': fields.String(description='Why the pattern cannot be applied'),
    })

    get_envelope_model = api_ns_system_logs.model('SystemLogsGetEnvelope', {
        'data': fields.List(fields.Nested(get_response_model), description='Matching entries, newest first'),
        'total': fields.Integer(description='How many entries match, across the whole file'),
        'offset': fields.Integer(),
        'limit': fields.Integer(description='The page size used, after the cap'),
        'filter_errors': fields.List(fields.Nested(get_filter_error_model),
                                     description='Stored filters that are not applied because they are invalid'),
    })

    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('limit', type=_limit, required=False, default=DEFAULT_LIMIT,
                                    help=f'Entries to return, newest first, at most {MAX_LIMIT}')
    get_request_parser.add_argument('offset', type=_offset, required=False, default=0,
                                    help='Matching entries to skip from the newest')
    get_request_parser.add_argument('level', type=_level, required=False, default=None,
                                    help='Minimum severity to return')
    get_request_parser.add_argument('contains', type=str, required=False, default='',
                                    help='Case-insensitive text the entry must contain')
    get_request_parser.add_argument('baseline_total', type=_baseline_total, required=False, default=None,
                                    help='The total from the response paging started from. Entries that '
                                         'matched since are skipped, so an older page does not shift as '
                                         'new lines arrive')

    @authenticate
    @api_ns_system_logs.doc(parser=get_request_parser)
    @api_ns_system_logs.response(200, 'Success', get_envelope_model)
    @api_ns_system_logs.response(400, 'Invalid parameter')
    @api_ns_system_logs.response(401, 'Not Authenticated')
    def get(self):
        """List log entries, newest first, one page at a time"""
        args = self.get_request_parser.parse_args()
        # The stored filters still apply to every read, as they always have. A
        # regex that does not compile is not applied, and is reported below.
        stored = StoredFilter(include=str(settings.log.include_filter),
                              exclude=str(settings.log.exclude_filter),
                              ignore_case=settings.log.ignore_case,
                              use_regex=settings.log.use_regex)
        page = read_log_page(get_log_file_path(), limit=args['limit'], offset=args['offset'],
                             level=args['level'], contains=args['contains'] or '', stored=stored,
                             cache=_log_counts, baseline_total=args['baseline_total'])
        return {
            'data': marshal(page.entries, self.get_response_model),
            'total': page.total,
            'offset': args['offset'],
            'limit': args['limit'],
            'filter_errors': stored.errors,
        }

    @authenticate
    @api_ns_system_logs.doc(parser=None)
    @api_ns_system_logs.response(204, 'Success')
    @api_ns_system_logs.response(401, 'Not Authenticated')
    def delete(self):
        """Force log rotation and create a new log file"""
        empty_log()
        return '', 204
