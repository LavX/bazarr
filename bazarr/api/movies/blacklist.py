# coding=utf-8

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from app.database import TableMovies, TableBlacklistMovie, database, select
from arr_instances.resolution import scoped
from subtitles.tools.delete import delete_subtitles
from radarr.blacklist import blacklist_log_movie, blacklist_delete_all_movie, blacklist_delete_movie
from utilities.path_mappings import path_mappings
from subtitles.mass_download import movies_download_subtitles
from app.event_handler import event_stream
from api.swaggerui import subtitles_language_model
from utilities.pretty_date import pretty_date

from ..utils import authenticate, postprocess

api_ns_movies_blacklist = Namespace('Movies Blacklist', description='List, add or remove subtitles to or from '
                                                                    'movies blacklist')


@api_ns_movies_blacklist.route('movies/blacklist')
class MoviesBlacklist(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start integer')
    get_request_parser.add_argument('length', type=int, required=False, default=-1, help='Paging length integer')

    get_language_model = api_ns_movies_blacklist.model('subtitles_language_model', subtitles_language_model)

    get_response_model = api_ns_movies_blacklist.model('MovieBlacklistGetResponse', {
        # Owning instance (#156) so the remove action can route.
        'arr_instance_id': fields.Integer(),
        'title': fields.String(),
        'radarrId': fields.Integer(),
        'provider': fields.String(),
        'subs_id': fields.String(),
        'language': fields.Nested(get_language_model),
        'timestamp': fields.String(),
        'parsed_timestamp': fields.String(),
    })

    @authenticate
    @api_ns_movies_blacklist.response(401, 'Not Authenticated')
    @api_ns_movies_blacklist.doc(parser=get_request_parser)
    def get(self):
        """List blacklisted movies subtitles"""
        args = self.get_request_parser.parse_args()
        start = args.get('start')
        length = args.get('length')

        stmt = (
            select(TableMovies.title,
                   TableMovies.radarrId,
                   TableBlacklistMovie.arr_instance_id,
                   TableBlacklistMovie.provider,
                   TableBlacklistMovie.subs_id,
                   TableBlacklistMovie.language,
                   TableBlacklistMovie.timestamp)
            .select_from(TableBlacklistMovie)
            .join(TableMovies)
            .order_by(TableBlacklistMovie.timestamp.desc()))
        # Pagination must be applied to the statement, not the executed Result
        # (a Result has no .limit()). Pre-existing bug: paginated requests 500'd.
        if length > 0:
            stmt = stmt.limit(length).offset(start)
        data = database.execute(stmt)

        return marshal([postprocess({
            'arr_instance_id': x.arr_instance_id,
            'title': x.title,
            'radarrId': x.radarrId,
            'provider': x.provider,
            'subs_id': x.subs_id,
            'language': x.language,
            'timestamp': pretty_date(x.timestamp),
            'parsed_timestamp': x.timestamp.strftime('%x %X'),
        }) for x in data.all()], self.get_response_model, envelope='data')

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('radarrid', type=int, required=True, help='Radarr ID')
    post_request_parser.add_argument('provider', type=str, required=True, help='Provider name')
    post_request_parser.add_argument('subs_id', type=str, required=True, help='Subtitles ID')
    post_request_parser.add_argument('language', type=str, required=True, help='Subtitles language')
    post_request_parser.add_argument('subtitles_path', type=str, required=True, help='Subtitles file path')
    post_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                     help='Owning Radarr instance id (#156)')

    @authenticate
    @api_ns_movies_blacklist.doc(parser=post_request_parser)
    @api_ns_movies_blacklist.response(200, 'Success')
    @api_ns_movies_blacklist.response(401, 'Not Authenticated')
    @api_ns_movies_blacklist.response(404, 'Movie not found')
    @api_ns_movies_blacklist.response(500, 'Subtitles file not found or permission issue.')
    def post(self):
        """Add a movies subtitles to blacklist"""
        args = self.post_request_parser.parse_args()
        radarr_id = args.get('radarrid')
        provider = args.get('provider')
        subs_id = args.get('subs_id')
        language = args.get('language')
        arr_instance_id = args.get('arr_instance_id')
        # TODO
        forced = False
        hi = False

        data = database.execute(
            scoped(
                select(TableMovies.path)
                .where(TableMovies.radarrId == radarr_id),
                TableMovies.arr_instance_id, arr_instance_id))\
            .first()

        if not data:
            return 'Movie not found', 404

        media_path = data.path
        subtitles_path = args.get('subtitles_path')

        blacklist_log_movie(radarr_id=radarr_id,
                            provider=provider,
                            subs_id=subs_id,
                            language=language,
                            arr_instance_id=arr_instance_id)
        if delete_subtitles(media_type='movie',
                            language=language,
                            forced=forced,
                            hi=hi,
                            media_path=path_mappings.path_replace_movie(media_path),
                            subtitles_path=subtitles_path,
                            radarr_id=radarr_id):
            movies_download_subtitles(radarr_id)
            event_stream(type='movie-history')
            return '', 200
        else:
            return 'Subtitles file not found or permission issue.', 500

    delete_request_parser = reqparse.RequestParser()
    delete_request_parser.add_argument('all', type=str, required=False, help='Empty movies subtitles blacklist')
    delete_request_parser.add_argument('provider', type=str, required=False, help='Provider name')
    delete_request_parser.add_argument('subs_id', type=str, required=False, help='Subtitles ID')
    delete_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                       help='Owning Radarr instance id (#156)')

    @authenticate
    @api_ns_movies_blacklist.doc(parser=delete_request_parser)
    @api_ns_movies_blacklist.response(204, 'Success')
    @api_ns_movies_blacklist.response(401, 'Not Authenticated')
    def delete(self):
        """Delete a movies subtitles from blacklist"""
        args = self.delete_request_parser.parse_args()
        if args.get("all") == "true":
            blacklist_delete_all_movie()
        else:
            provider = args.get('provider')
            subs_id = args.get('subs_id')
            blacklist_delete_movie(provider=provider, subs_id=subs_id,
                                   arr_instance_id=args.get('arr_instance_id'))
        return '', 200
