# coding=utf-8

from flask_restx import Resource, Namespace, reqparse
from unidecode import unidecode

from app.config import base_url, settings
from app.database import TableShows, TableMovies, database, select

from ..utils import authenticate, image_proxy_path_with_instance

import textdistance 

api_ns_system_searches = Namespace('System Searches', description='Search for series or movies by name')


def _poster_url(media, path, arr_instance_id):
    if not path:
        return None
    path = image_proxy_path_with_instance(path, arr_instance_id)
    return f"{base_url}/images/{media}{path}"


@api_ns_system_searches.route('system/searches')
class Searches(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('query', type=str, required=True, help='Series or movie name to search for')

    @authenticate
    @api_ns_system_searches.doc(parser=get_request_parser)
    @api_ns_system_searches.response(200, 'Success')
    @api_ns_system_searches.response(401, 'Not Authenticated')
    def get(self):
        """List results from query"""
        args = self.get_request_parser.parse_args()
        query = unidecode(args.get('query')).lower()
        search_list = []

        if query:
            if settings.general.use_sonarr:
                # Get matching series
                search_list += database.execute(
                    select(TableShows.title,
                           TableShows.id,
                           TableShows.arr_instance_id,
                           TableShows.sonarrSeriesId,
                           TableShows.poster,
                           TableShows.year)
                    .order_by(TableShows.title)) \
                    .all()

            if settings.general.use_radarr:
                # Get matching movies
                search_list += database.execute(
                    select(TableMovies.title,
                           TableMovies.id,
                           TableMovies.arr_instance_id,
                           TableMovies.radarrId,
                           TableMovies.poster,
                           TableMovies.year)
                    .order_by(TableMovies.title)) \
                    .all()

        results = []

        for x in search_list:
            if query in unidecode(x.title).lower():
                result = {
                    'title': x.title,
                    'year': x.year,
                    # Canonical local id + owning instance (#156); the frontend
                    # routes by id, id == upstream id on a single instance.
                    'id': x.id,
                    'arr_instance_id': x.arr_instance_id,
                }

                if hasattr(x, 'sonarrSeriesId'):
                    result['sonarrSeriesId'] = x.sonarrSeriesId
                    result['poster'] = _poster_url('series', x.poster, x.arr_instance_id)

                else:
                    result['radarrId'] = x.radarrId
                    result['poster'] = _poster_url('movies', x.poster, x.arr_instance_id)

                results.append(result)

        # sort results by how closely they match the query
        results = sorted(results, key=lambda x: textdistance.hamming.distance(query, x['title']))
        return results
