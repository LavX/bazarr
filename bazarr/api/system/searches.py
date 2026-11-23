# coding=utf-8

from itertools import zip_longest

import sqlalchemy as sa
from flask_restx import Resource, Namespace, reqparse
from unidecode import unidecode

from app.config import base_url, settings
from app.database import (TableArrInstances, TableShows, TableMovies, TableSportsLeagues,
                          database, select)
from media_sources import repository

from ..utils import authenticate, image_proxy_path_with_instance

api_ns_system_searches = Namespace('System Searches', description='Search for series or movies by name')


def _hamming_distance(left, right):
    """Count the positions at which two strings differ.

    The tail of the longer string counts as a difference at every position past
    the end of the shorter one. This is the same measure textdistance.hamming
    gave, kept local because textdistance pulls in numpy, and numpy aborts the
    process on CPUs without SSE4.2.
    """
    return sum(a != b for a, b in zip_longest(left, right))


def _poster_url(media, path, arr_instance_id):
    if not path:
        return None
    path = image_proxy_path_with_instance(path, arr_instance_id)
    return f"{base_url}/images/{media}{path}"


def _source_search_results(query):
    groups, items = repository.Groups.c, repository.Items.c
    media = sa.union_all(
        select(groups.id, groups.title, groups.year, groups.poster, groups.kind).where(groups.kind == 'show'),
        select(items.id, items.title, items.year, items.poster, items.kind).where(items.kind == 'movie'),
    ).subquery()
    rows = database.execute(select(media).order_by(media.c.title, media.c.kind, media.c.id))
    return [
        {
            'id': row.id, 'title': row.title, 'year': row.year, 'poster': row.poster,
            'origin': 'source', 'source_kind': row.kind,
            'detail_url': f"/{'series' if row.kind == 'show' else 'movies'}/source/{row.id}",
        }
        for row in rows if query in unidecode(row.title).lower()
    ]


@api_ns_system_searches.route('system/searches')
class Searches(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('query', type=str, required=True, help='Series or movie name to search for')
    get_request_parser.add_argument('sources', location='args', help='Set to 1 to include media sources')

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

            if settings.general.use_sportarr:
                # Sports leagues were absent from the global search entirely,
                # so a Sportarr user's library was unreachable from the search
                # bar even though the tab existed in the nav.
                # Filtered to enabled owners, the way the sports library and
                # detail queries are. A disabled instance keeps its league
                # rows, so an unfiltered search kept offering them while the
                # pages they link to refuse to load, giving the user a result
                # that goes nowhere.
                search_list += database.execute(
                    select(TableSportsLeagues.title,
                           TableSportsLeagues.id,
                           TableSportsLeagues.arr_instance_id,
                           TableSportsLeagues.sportarrLeagueId,
                           TableSportsLeagues.poster,
                           TableSportsLeagues.sport)
                    .join(TableArrInstances,
                          TableSportsLeagues.arr_instance_id == TableArrInstances.id)
                    .where(TableArrInstances.kind == 'sportarr',
                           TableArrInstances.enabled == 1)
                    .order_by(TableSportsLeagues.title)) \
                    .all()

        results = []

        for x in search_list:
            if query in unidecode(x.title).lower():
                result = {
                    'title': x.title,
                    # A league has a sport, not a year.
                    'year': getattr(x, 'year', None),
                    # Canonical local id + owning instance (#156); the frontend
                    # routes by id, id == upstream id on a single instance.
                    'id': x.id,
                    'arr_instance_id': x.arr_instance_id,
                }

                if hasattr(x, 'sonarrSeriesId'):
                    result['sonarrSeriesId'] = x.sonarrSeriesId
                    result['poster'] = _poster_url('series', x.poster, x.arr_instance_id)

                elif hasattr(x, 'sportarrLeagueId'):
                    result['sportarrLeagueId'] = x.sportarrLeagueId
                    result['sport'] = x.sport
                    # Sportarr serves its own poster URLs, so there is no local
                    # proxy path to build the way series and movies have.
                    result['poster'] = x.poster

                else:
                    result['radarrId'] = x.radarrId
                    result['poster'] = _poster_url('movies', x.poster, x.arr_instance_id)

                results.append(result)

        if query and args.get('sources') == '1':
            results.extend(_source_search_results(query))

        # sort results by how closely they match the query
        results = sorted(results, key=lambda x: _hamming_distance(query, x['title']))
        return results
