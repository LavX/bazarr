# coding=utf-8

import os

from flask_restx import Resource, Namespace, reqparse, marshal

from app.database import TableMovies, database, select
from utilities.path_mappings import path_mappings
from app.get_providers import get_providers_sorted
from subtitles.manual import manual_search, queue_manual_search, movie_manually_download_specific_subtitle
from subtitles.search_result import SEARCH_RESULT_FIELDS
from radarr.history import history_log_movie  # noqa: F401
from app.config import settings  # noqa: F401
from app.jobs_queue import jobs_queue  # noqa: F401
from subtitles.indexer.movies import store_subtitles_movie, list_missing_subtitles_movies

from ..queued_job import queued_job_model, queued_job_response
from ..utils import authenticate


api_ns_providers_movies = Namespace('Providers Movies', description='List and download movies subtitles manually')


def _movie_search_target(movie_id):
    """Look a movie up for a search: its file, profile and names, or the refusal to answer with."""
    stmt = select(TableMovies.title,
                  TableMovies.year,
                  TableMovies.path,
                  TableMovies.radarrId,
                  TableMovies.arr_instance_id,
                  TableMovies.sceneName,
                  TableMovies.profileId,
                  TableMovies.subtitles,
                  TableMovies.missing_subtitles) \
        .where(TableMovies.id == movie_id)
    movieInfo = database.execute(stmt).first()

    if not movieInfo:
        return None, ('Movie not found', 404)
    elif movieInfo.subtitles is None:
        # subtitles indexing for this movie is incomplete, we'll do it again
        store_subtitles_movie(movieInfo.path, path_mappings.path_replace_instance(movieInfo.path, movieInfo.arr_instance_id, 'movie'),
                              arr_instance_id=movieInfo.arr_instance_id)
        movieInfo = database.execute(stmt).first()
    elif movieInfo.missing_subtitles is None:
        # missing subtitles calculation for this movie is incomplete, we'll do it again
        list_missing_subtitles_movies(no=movieInfo.radarrId, arr_instance_id=movieInfo.arr_instance_id)
        movieInfo = database.execute(stmt).first()

    moviePath = path_mappings.path_replace_movie(movieInfo.path)

    if not os.path.exists(moviePath):
        return None, ('Movie file not found. Path mapping issue?', 500)

    return {'path': moviePath, 'title': movieInfo.title, 'year': movieInfo.year,
            'sceneName': movieInfo.sceneName or "None", 'profileId': movieInfo.profileId,
            'arr_instance_id': movieInfo.arr_instance_id}, None


@api_ns_providers_movies.route('providers/movies')
class ProviderMovies(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('radarrid', type=int, required=True, help='Movie ID')

    get_response_model = api_ns_providers_movies.model('ProviderMoviesGetResponse', SEARCH_RESULT_FIELDS)

    @authenticate
    @api_ns_providers_movies.response(401, 'Not Authenticated')
    @api_ns_providers_movies.response(404, 'Movie not found')
    @api_ns_providers_movies.response(500, 'Custom error messages')
    @api_ns_providers_movies.doc(parser=get_request_parser)
    def get(self):
        """Search manually for a movie subtitles"""
        args = self.get_request_parser.parse_args()
        # 'radarrid' is the canonical local movie id (globally unique across
        # instances) that GetItemId and the browse endpoints use. Looking the row
        # up by local id avoids the upstream-id collision between instances; on a
        # single default instance local id == radarrId, so unchanged. (#156)
        target, refusal = _movie_search_target(args.get('radarrid'))
        if refusal:
            return refusal

        providers_list = get_providers_sorted()

        provider_outcomes = []
        data = manual_search(target['path'], target['profileId'], providers_list, target['sceneName'],
                             target['title'], 'movie', arr_instance_id=target['arr_instance_id'],
                             provider_outcomes=provider_outcomes)
        if isinstance(data, str):
            return data, 500
        return {'data': marshal(data, self.get_response_model), 'providers': provider_outcomes}

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('radarrid', type=int, required=True, help='Movie ID')
    post_request_parser.add_argument('hi', type=str, required=True, help='HI subtitles from ["True", "False"]')
    post_request_parser.add_argument('forced', type=str, required=True, help='Forced subtitles from ["True", "False"]')
    post_request_parser.add_argument('original_format', type=str, required=True,
                                     help='Use original subtitles format from ["True", "False"]')
    post_request_parser.add_argument('provider', type=str, required=True, help='Provider name')
    post_request_parser.add_argument('subtitle', type=str, required=True, help='Subtitle ID as returned by GET')
    post_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                     help='Owning Radarr instance id (#156); scopes the download to it')

    @authenticate
    @api_ns_providers_movies.doc(parser=post_request_parser)
    @api_ns_providers_movies.response(204, 'Success')
    @api_ns_providers_movies.response(401, 'Not Authenticated')
    @api_ns_providers_movies.response(404, 'Movie not found')
    @api_ns_providers_movies.response(500, 'Custom error messages')
    def post(self):
        """Manually download a movie subtitles"""
        args = self.post_request_parser.parse_args()
        # The frontend sends the upstream radarrId (not the local autoincrement
        # id), so pass it directly to the downstream function which resolves the
        # row by (radarrId, arr_instance_id). A local-id pre-lookup was causing
        # collisions: when a different movie's local id happened to equal the
        # requested upstream id the handler routed to the wrong row or 404'd
        # on an instance mismatch. (#156 F8)
        radarr_id = args.get('radarrid')
        arr_instance_id = args.get('arr_instance_id')

        movie_manually_download_specific_subtitle(radarr_id=radarr_id,
                                                  hi=args.get('hi').capitalize(),
                                                  forced=args.get('forced').capitalize(),
                                                  use_original_format=args.get('original_format').capitalize(),
                                                  selected_provider=args.get('provider'),
                                                  subtitle=args.get('subtitle'),
                                                  job_id=None,
                                                  arr_instance_id=arr_instance_id)

        return '', 204


@api_ns_providers_movies.route('providers/movies/search')
class ProviderMoviesSearch(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('radarrid', type=int, required=True, help='Movie ID')

    post_job_model = api_ns_providers_movies.model('MovieSearchQueued', queued_job_model)

    @authenticate
    @api_ns_providers_movies.doc(parser=post_request_parser)
    @api_ns_providers_movies.response(202, 'Search queued as a job; its rows are the job result', post_job_model)
    @api_ns_providers_movies.response(401, 'Not Authenticated')
    @api_ns_providers_movies.response(404, 'Movie not found')
    @api_ns_providers_movies.response(500, 'Custom error messages')
    def post(self):
        """Search manually for a movie's subtitles as a job

        The same search as the GET, run as a job so it survives the page closing and shows in the jobs
        list. Read its rows from system/jobs/result once the job finishes."""
        args = self.post_request_parser.parse_args()
        target, refusal = _movie_search_target(args.get('radarrid'))
        if refusal:
            return refusal

        label = target['title']
        if target['year']:
            label += f" ({target['year']})"
        job_id = queue_manual_search(target['path'], target['profileId'], target['sceneName'], target['title'],
                                     'movie', label, arr_instance_id=target['arr_instance_id'])
        return queued_job_response(job_id), 202
