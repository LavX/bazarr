# coding=utf-8

import sqlalchemy as sa

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from app.database import get_exclusion_clause, TableEpisodes, TableShows, database, select, func
from api.swaggerui import subtitles_language_model, audio_language_model
from media_sources import repository
from media_sources.api_rows import source_row_fields, wanted_rows
from media_sources.union import SourceKeyFilter, add_source_arguments, includes_sources, union_page
from media_sources.wanted import arr_wanted_condition, wanted_items

from ..utils import authenticate

api_ns_episodes_wanted = Namespace('Episodes Wanted', description='List episodes wanted subtitles')


@api_ns_episodes_wanted.route('episodes/wanted')
class EpisodesWanted(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start integer')
    get_request_parser.add_argument('length', type=int, required=False, default=-1, help='Paging length integer')
    get_request_parser.add_argument('episodeid[]', type=int, action='append', required=False, default=[],
                                    help='Episodes ID to list')

    add_source_arguments(get_request_parser)

    get_subtitles_language_model = api_ns_episodes_wanted.model('subtitles_language_model', subtitles_language_model)
    get_audio_language_model = api_ns_episodes_wanted.model('audio_language_model', audio_language_model)

    data_model = api_ns_episodes_wanted.model('wanted_episodes_data_model', {
        'audio_language': fields.Nested(get_audio_language_model),
        # Canonical local ids + owning instance (#156) so the per-row search
        # routes to the correct Sonarr instance. Upstream ids kept for back-compat.
        'id': fields.Integer(),
        'series_id': fields.Integer(),
        'arr_instance_id': fields.Integer(),
        'seriesTitle': fields.String(),
        'episode_number': fields.String(),
        'episodeTitle': fields.String(),
        'missing_subtitles': fields.Nested(get_subtitles_language_model),
        'sonarrSeriesId': fields.Integer(),
        'sonarrEpisodeId': fields.Integer(),
        'sceneName': fields.String(),
        'tags': fields.List(fields.String),
        'seriesType': fields.String(),
        # True when a release-type mismatch was recorded for this episode: its
        # own release type has no acceptable subtitle while another one does.
        'release_mismatch': fields.Boolean(),
    })

    get_response_model = api_ns_episodes_wanted.model('EpisodeWantedGetResponse', {
        'data': fields.Nested(data_model),
        'total': fields.Integer(),
        # Episodes with a pending forced duplicate search, under the same exclusions as the list. They are
        # not in `data` unless something is also missing: it only tells the page that a search has work.
        'forced_pending': fields.Integer(),
    })

    mixed_data_model = api_ns_episodes_wanted.model(
        'WantedEpisodesSourceRow', {**data_model, **source_row_fields})
    mixed_response_model = {**get_response_model, 'data': fields.Nested(mixed_data_model)}

    @authenticate
    @api_ns_episodes_wanted.response(401, 'Not Authenticated')
    @api_ns_episodes_wanted.doc(parser=get_request_parser)
    def get(self):
        """List episodes wanted subtitles"""
        args = self.get_request_parser.parse_args()
        source_filter = SourceKeyFilter(tuple(args.get('source_key[]') or ()))
        episodeid = args.get('episodeid[]')

        wanted_conditions = [(TableEpisodes.missing_subtitles.is_not(None)),
                             (TableEpisodes.missing_subtitles != '[]')]
        if len(episodeid) > 0:
            wanted_conditions.append((TableEpisodes.sonarrEpisodeId.in_(episodeid)))
            start = 0
            length = 0
        else:
            start = args.get('start')
            length = args.get('length')

        wanted_conditions += get_exclusion_clause('series')
        owner_filter = source_filter.arr(TableEpisodes.arr_instance_id)
        wanted_conditions.append(owner_filter)

        stmt = select(TableEpisodes.audio_language,
                      TableEpisodes.id,
                      TableEpisodes.series_id,
                      TableEpisodes.arr_instance_id,
                      TableShows.title.label('seriesTitle'),
                      sa.cast(TableEpisodes.season, sa.Text).concat('x').concat(TableEpisodes.episode)
                      .label('episode_number'),
                      TableEpisodes.title.label('episodeTitle'),
                      TableEpisodes.missing_subtitles,
                      TableEpisodes.sonarrSeriesId,
                      TableEpisodes.sonarrEpisodeId,
                      TableEpisodes.sceneName,
                      TableShows.tags,
                      TableShows.seriesType) \
            .select_from(TableEpisodes) \
            .join(TableShows) \
            .where(*wanted_conditions)

        items = repository.Items
        mixed = includes_sources(args, database, items, items.c.kind == 'episode') if args.get('sources') == '1' else False
        if mixed:
            arr_condition = arr_wanted_condition('sonarr', TableEpisodes.arr_instance_id)
            source = _source_query(source_filter)
            if episodeid:
                source = source.where(sa.false())  # An explicit upstream ID selects Arr rows only.
            arr = stmt.where(arr_condition).add_columns(TableEpisodes.arr_instance_id.label('_owner'),
                                   TableEpisodes.season.label('_season'),
                                   TableEpisodes.episode.label('_episode'),
                                   sa.cast(sa.null(), sa.Integer).label('source_id'),
                                   sa.cast(sa.null(), sa.Text).label('source_name'),
                                   sa.cast(sa.null(), sa.Text).label('source_kind'),
                                   sa.cast(sa.null(), sa.Text).label('identity_state'),
                                   sa.cast(sa.null(), sa.Text).label('shadowed_by'),
                                   sa.cast(sa.null(), sa.Integer).label('monitored'),
                                   sa.cast(sa.null(), sa.Text).label('poster'))
            rows, count = union_page(database, arr, source, start=start, length=length, sort=lambda c: (
                func.coalesce(c.seriesTitle, ''), func.coalesce(c._season, 0), func.coalesce(c._episode, 0),
                c.origin, c.id))
        else:
            if length > 0:
                stmt = stmt.order_by(TableEpisodes.sonarrEpisodeId.desc()).limit(length).offset(start)
            rows = database.execute(stmt).all()
            count = database.scalar(select(func.count()).select_from(TableEpisodes).join(TableShows)
                                    .where(*wanted_conditions))
        results = wanted_rows(database, 'series', rows, mixed=mixed)

        forced_pending = database.execute(
            select(func.count())
            .select_from(TableEpisodes)
            .join(TableShows)
            .where(TableEpisodes.forced_search_requested_at.is_not(None), owner_filter,
                   arr_condition if mixed else sa.true(), *get_exclusion_clause('series'))) \
            .scalar()

        return marshal({'data': results, 'total': count, 'forced_pending': forced_pending},
                       self.mixed_response_model if mixed else self.get_response_model)


def _source_query(source_filter):
    items, groups, sources = repository.Items.c, repository.Groups.c, repository.Sources.c
    number = sa.cast(items.season, sa.Text).concat('x').concat(sa.cast(items.episode, sa.Text))
    return wanted_items(
        items.audio_language, items.id, items.group_id.label('series_id'),
        groups.title.label('seriesTitle'), number.label('episode_number'),
        func.coalesce(items.episode_title, items.title).label('episodeTitle'),
        items.missing_subtitles, items.scene_name.label('sceneName'),
        items.source_id.label('_owner'), items.season.label('_season'), items.episode.label('_episode'),
        items.source_id, sources.name.label('source_name'), sources.kind.label('source_kind'), items.identity_state, items.shadow_owner.label('shadowed_by'),
        items.monitored, groups.poster, kind='episode').where(source_filter.source(items.source_id))
