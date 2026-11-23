# coding=utf-8

import sqlalchemy as sa

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from app.database import get_exclusion_clause, TableMovies, database, select, func
from api.swaggerui import subtitles_language_model, audio_language_model
from media_sources import repository
from media_sources.api_rows import source_row_fields, wanted_rows
from media_sources.union import SourceKeyFilter, add_source_arguments, includes_sources, union_page
from media_sources.wanted import arr_wanted_condition, wanted_items

from api.utils import authenticate

api_ns_movies_wanted = Namespace('Movies Wanted', description='List movies wanted subtitles')


@api_ns_movies_wanted.route('movies/wanted')
class MoviesWanted(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start integer')
    get_request_parser.add_argument('length', type=int, required=False, default=-1, help='Paging length integer')
    get_request_parser.add_argument('radarrid[]', type=int, action='append', required=False, default=[],
                                    help='Movies ID to list')

    add_source_arguments(get_request_parser)

    get_subtitles_language_model = api_ns_movies_wanted.model('subtitles_language_model', subtitles_language_model)
    get_audio_language_model = api_ns_movies_wanted.model('audio_language_model', audio_language_model)

    data_model = api_ns_movies_wanted.model('wanted_movies_data_model', {
        'audio_language': fields.Nested(get_audio_language_model),
        # Canonical local id + owning instance (#156) so the per-row search
        # routes to the correct Radarr instance. Upstream id kept for back-compat.
        'id': fields.Integer(),
        'arr_instance_id': fields.Integer(),
        'title': fields.String(),
        'missing_subtitles': fields.Nested(get_subtitles_language_model),
        'radarrId': fields.Integer(),
        'sceneName': fields.String(),
        'tags': fields.List(fields.String),
        # True when a release-type mismatch was recorded for this movie: its own
        # release type has no acceptable subtitle while another one does.
        'release_mismatch': fields.Boolean(),
    })

    get_response_model = api_ns_movies_wanted.model('MovieWantedGetResponse', {
        'data': fields.Nested(data_model),
        'total': fields.Integer(),
    })

    mixed_data_model = api_ns_movies_wanted.model(
        'WantedMoviesSourceRow', {**data_model, **source_row_fields})
    mixed_response_model = {**get_response_model, 'data': fields.Nested(mixed_data_model)}

    @authenticate
    @api_ns_movies_wanted.response(401, 'Not Authenticated')
    @api_ns_movies_wanted.doc(parser=get_request_parser)
    def get(self):
        """List movies wanted subtitles"""
        args = self.get_request_parser.parse_args()
        source_filter = SourceKeyFilter(tuple(args.get('source_key[]') or ()))
        radarrid = args.get("radarrid[]")

        wanted_conditions = [(TableMovies.missing_subtitles.is_not(None)),
                             (TableMovies.missing_subtitles != '[]')]
        if len(radarrid) > 0:
            wanted_conditions.append((TableMovies.radarrId.in_(radarrid)))
            start = 0
            length = 0
        else:
            start = args.get('start')
            length = args.get('length')

        wanted_conditions += get_exclusion_clause('movie')
        owner_filter = source_filter.arr(TableMovies.arr_instance_id)
        wanted_conditions.append(owner_filter)

        stmt = select(TableMovies.audio_language,
                      TableMovies.id,
                      TableMovies.arr_instance_id,
                      TableMovies.title,
                      TableMovies.missing_subtitles,
                      TableMovies.radarrId,
                      TableMovies.sceneName,
                      TableMovies.tags) \
            .where(*wanted_conditions)

        items = repository.Items
        mixed = includes_sources(args, database, items, items.c.kind == 'movie') if args.get('sources') == '1' else False
        if mixed:
            arr_condition = arr_wanted_condition('radarr', TableMovies.arr_instance_id)
            source = _source_query(source_filter)
            if radarrid:
                source = source.where(sa.false())  # An explicit upstream ID selects Arr rows only.
            arr = stmt.where(arr_condition).add_columns(TableMovies.arr_instance_id.label('_owner'),
                                   sa.cast(sa.null(), sa.Integer).label('source_id'),
                                   sa.cast(sa.null(), sa.Text).label('source_name'),
                                   sa.cast(sa.null(), sa.Text).label('source_kind'),
                                   sa.cast(sa.null(), sa.Text).label('identity_state'),
                                   sa.cast(sa.null(), sa.Text).label('shadowed_by'),
                                   sa.cast(sa.null(), sa.Integer).label('monitored'),
                                   sa.cast(sa.null(), sa.Text).label('poster'))
            rows, count = union_page(database, arr, source, start=start, length=length,
                                     sort=lambda c: (func.coalesce(c.title, ''), c.origin, c.id))
        else:
            if length > 0:
                stmt = stmt.order_by(TableMovies.radarrId.desc()).limit(length).offset(start)
            rows = database.execute(stmt).all()
            count = database.scalar(select(func.count()).select_from(TableMovies).where(*wanted_conditions))
        results = wanted_rows(database, 'movie', rows, mixed=mixed)

        return marshal({'data': results, 'total': count},
                       self.mixed_response_model if mixed else self.get_response_model)


def _source_query(source_filter):
    items, sources = repository.Items.c, repository.Sources.c
    return wanted_items(
        items.audio_language, items.id, items.title, items.missing_subtitles, items.scene_name.label('sceneName'),
        items.source_id.label('_owner'), items.source_id, sources.name.label('source_name'), sources.kind.label('source_kind'), items.identity_state,
        items.shadow_owner.label('shadowed_by'), items.monitored, items.poster, kind='movie',
    ).where(source_filter.source(items.source_id))
