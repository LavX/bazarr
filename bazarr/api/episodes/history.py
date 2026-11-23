# coding=utf-8

import operator
import ast

import sqlalchemy as sa
from functools import reduce

from api.swaggerui import subtitles_language_model
from app.database import TableEpisodes, TableShows, TableHistory, TableBlacklist, database, select, func
from subtitles.upgrade import get_upgradable_episode_subtitles,  _language_still_desired
from utilities.pretty_date import pretty_date

from media_sources import repository
from media_sources.history_rows import arr_history_rows, history_origin_fields, merge_history_rows, mixed_history_page
from media_sources.union import SourceKeyFilter, add_source_arguments, includes_sources

from flask_restx import Resource, Namespace, inputs, reqparse, fields, marshal
from ..utils import authenticate, postprocess

api_ns_episodes_history = Namespace('Episodes History', description='List episodes history events')


@api_ns_episodes_history.route('episodes/history')
class EpisodesHistory(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start integer')
    get_request_parser.add_argument('length', type=int, required=False, default=-1, help='Paging length integer')
    get_request_parser.add_argument('id', type=int, required=False, help='Local episode ID')
    get_request_parser.add_argument('episodeid', type=int, required=False, help='Episode ID')
    get_request_parser.add_argument('series_id', type=int, required=False, help='Local series ID')
    get_request_parser.add_argument('include_embedded', type=inputs.boolean, required=False, default=False,
                                    help='Include Embedded Source records (default excludes them)')
    get_request_parser.add_argument('include_upgradable', type=inputs.boolean, required=False, default=True,
                                    help='Include the upgradable computation (default includes it)')

    add_source_arguments(get_request_parser, require_opt_in=True)

    get_language_model = api_ns_episodes_history.model('subtitles_language_model', subtitles_language_model)

    data_model = api_ns_episodes_history.model('history_episodes_data_model', {
        'id': fields.Integer(),
        'history_id': fields.Integer(),
        'series_id': fields.Integer(),
        # Owning instance (#156) so secondary actions (blacklist) can route.
        'arr_instance_id': fields.Integer(),
        'seriesTitle': fields.String(),
        'monitored': fields.Boolean(),
        'episode_number': fields.String(),
        'episodeTitle': fields.String(),
        'timestamp': fields.String(),
        'timestamp_iso': fields.String(),
        'subs_id': fields.String(),
        'description': fields.String(),
        'sonarrSeriesId': fields.Integer(),
        'language': fields.Nested(get_language_model),
        'score': fields.String(),
        'ai_translated': fields.Boolean(),
        'tags': fields.List(fields.String),
        'action': fields.Integer(),
        'subtitles_path': fields.String(),
        'sonarrEpisodeId': fields.Integer(),
        'provider': fields.String(),
        'upgradable': fields.Boolean(),
        'parsed_timestamp': fields.String(),
        'blacklisted': fields.Boolean(),
        'matches': fields.List(fields.String),
        'dont_matches': fields.List(fields.String),
    })

    get_response_model = api_ns_episodes_history.model('EpisodeHistoryGetResponse', {
        'data': fields.Nested(data_model),
        'total': fields.Integer(),
    })

    mixed_data_model = api_ns_episodes_history.model(
        'EpisodeHistorySourceRow', {**data_model, **history_origin_fields})
    mixed_response_model = {
        **get_response_model, 'data': fields.Nested(mixed_data_model), 'source_keys': fields.List(fields.String),
    }

    @authenticate
    @api_ns_episodes_history.response(401, 'Not Authenticated')
    @api_ns_episodes_history.doc(parser=get_request_parser)
    def get(self):
        """List episodes history events"""
        args = self.get_request_parser.parse_args()
        source_filter = SourceKeyFilter(tuple(args.get('source_key[]') or ()))
        history = repository.History
        mixed = includes_sources(args, database, history, history.c.item_kind == 'episode')
        start = args.get('start')
        length = args.get('length')
        local_episode_id = args.get('id')
        episodeid = args.get('episodeid')
        local_series_id = args.get('series_id')
        include_embedded = args.get('include_embedded')
        include_upgradable = args.get('include_upgradable')

        blacklisted_subtitles = select(TableBlacklist.provider,
                                       TableBlacklist.subs_id,
                                       TableBlacklist.arr_instance_id) \
            .subquery()

        query_conditions = [TableEpisodes.title.is_not(None)]
        if source_filter.keys:
            query_conditions.append(source_filter.arr(TableHistory.arr_instance_id))
        if not include_embedded:
            # "Treat Embedded Subtitles as Downloaded" writes one action=7 row
            # per episode/language combination; a large library carries six
            # figures of them and they bury real events. They stay in the table
            # (scoring and upgrade logic read them) but out of the listing and
            # its total unless the caller asks.
            query_conditions.append((TableHistory.action != 7))
        if local_episode_id:
            query_conditions.append((TableEpisodes.id == local_episode_id))
        elif episodeid:
            query_conditions.append((TableEpisodes.sonarrEpisodeId == episodeid))
        # The series detail table reads every episode's scores in one request.
        # Filter on TableEpisodes columns (not TableShows) so the count query,
        # which only joins TableEpisodes, stays valid.
        elif local_series_id:
            query_conditions.append((TableEpisodes.series_id == local_series_id))

        stmt = select(TableHistory.id.label('history_id'),
                      TableEpisodes.id,
                      TableShows.id.label('series_id'),
                      TableHistory.arr_instance_id,
                      TableShows.title.label('seriesTitle'),
                      TableEpisodes.monitored,
                      TableEpisodes.season.concat('x').concat(TableEpisodes.episode).label('episode_number'),
                      TableEpisodes.title.label('episodeTitle'),
                      TableHistory.timestamp,
                      TableHistory.subs_id,
                      TableHistory.description,
                      TableHistory.sonarrSeriesId,
                      TableEpisodes.path,
                      TableHistory.language,
                      TableHistory.score,
                      TableHistory.ai_translated,
                      TableHistory.score_out_of,
                      TableShows.tags,
                      TableHistory.action,
                      TableHistory.video_path,
                      TableHistory.subtitles_path,
                      TableHistory.sonarrEpisodeId,
                      TableHistory.provider,
                      TableShows.seriesType,
                      TableShows.profileId,
                      TableHistory.matched,
                      TableHistory.not_matched,
                      TableEpisodes.subtitles.label('external_subtitles'),
                      blacklisted_subtitles.c.subs_id.label('blacklisted')) \
            .select_from(TableHistory) \
            .join(TableShows, onclause=TableHistory.series_id == TableShows.id) \
            .join(TableEpisodes, onclause=TableHistory.episode_id == TableEpisodes.id) \
            .join(blacklisted_subtitles,
                  onclause=((TableHistory.subs_id == blacklisted_subtitles.c.subs_id) &
                            (func.coalesce(TableHistory.provider, '') ==
                             func.coalesce(blacklisted_subtitles.c.provider, '')) &
                            (func.coalesce(TableHistory.arr_instance_id, -1) ==
                             func.coalesce(blacklisted_subtitles.c.arr_instance_id, -1))),
                  isouter=True) \
            .where(reduce(operator.and_, query_conditions))
        if mixed:
            arr_keys = select(TableHistory.id, TableHistory.arr_instance_id.label('_owner'),
                              sa.cast(TableHistory.timestamp, sa.Text).label('timestamp')).select_from(TableHistory)
            arr_keys = arr_keys.join(TableEpisodes).join(
                TableShows, onclause=TableHistory.series_id == TableShows.id).where(*query_conditions)
            timeline, count = mixed_history_page(database, arr_keys, 'episode', args,
                                                 arr_detail=bool(local_episode_id or episodeid or local_series_id))
            rows = arr_history_rows(database, stmt.add_columns(TableHistory.upgradedFromId), TableHistory.id, timeline)
        else:
            stmt = stmt.order_by(TableHistory.timestamp.desc(), TableHistory.id.desc())
            if length > 0:
                stmt = stmt.limit(length).offset(start)
            rows = database.execute(stmt).all()
        episode_history = [{
            'history_id': x.history_id,
            'id': x.id,
            'series_id': x.series_id,
            'arr_instance_id': x.arr_instance_id,
            'seriesTitle': x.seriesTitle,
            'monitored': x.monitored,
            'episode_number': x.episode_number,
            'episodeTitle': x.episodeTitle,
            'timestamp': x.timestamp,
            'timestamp_iso': x.timestamp.isoformat() if x.timestamp else None,
            'subs_id': x.subs_id,
            'description': x.description,
            'sonarrSeriesId': x.sonarrSeriesId,
            'path': x.path,
            'language': x.language,
            'profileId': x.profileId,
            'score': x.score,
            'ai_translated': x.ai_translated is True,
            'score_out_of': x.score_out_of,
            'tags': x.tags,
            'action': x.action,
            'video_path': x.video_path,
            'subtitles_path': x.subtitles_path,
            'sonarrEpisodeId': x.sonarrEpisodeId,
            'provider': x.provider,
            'matches': x.matched,
            'dont_matches': x.not_matched,
            'external_subtitles': [y[1] for y in ast.literal_eval(x.external_subtitles) if y[1]],
            'blacklisted': bool(x.blacklisted),
            **({'upgradedFromId': x.upgradedFromId} if mixed else {}),
        } for x in rows]

        # The upgradable computation aggregates the whole history table and
        # materializes every library-wide candidate. The series detail table
        # reads only score, provider and matches, so callers that never read
        # upgradable can skip it with include_upgradable=false.
        if include_upgradable and (episode_history or not mixed):
            upgradable_episodes_not_perfect = get_upgradable_episode_subtitles(history_id_list=[x['history_id'] for x in
                                                                                                episode_history])

        for item in episode_history:
            # is this language still desired or should we simply skip this subtitles from upgrade logic?
            still_desired = _language_still_desired(item['language'], item['profileId']) if include_upgradable else None

            item.update(postprocess(item))

            if include_upgradable:
                # Mark upgradable and get original_id
                item.update({'original_id': upgradable_episodes_not_perfect.get(item['history_id'])})
                item.update({'upgradable': item['history_id'] in upgradable_episodes_not_perfect.keys()})

                # Mark not upgradable if video/subtitles file doesn't exist anymore or if language isn't desired anymore
                if item['upgradable']:
                    if (item['subtitles_path'] not in item['external_subtitles'] or item['video_path'] != item['path'] or
                            not still_desired):
                        item.update({"upgradable": False})
            else:
                item.update({'upgradable': False})

            del item['path']
            if not mixed:
                del item['video_path']
            del item['external_subtitles']
            del item['profileId']

            # A score of 0 is a score, so it is formatted like any other when its scale is known.
            if item['score'] is not None and item['score_out_of']:
                item['score'] = f"{round((int(item['score']) * 100 / item['score_out_of']), 2)}%"

            # Make timestamp pretty
            if item['timestamp']:
                item["parsed_timestamp"] = item['timestamp'].strftime('%x %X')
                item['timestamp'] = pretty_date(item["timestamp"])

            # Parse matches and dont_matches
            if item['matches']:
                item.update({'matches': ast.literal_eval(item['matches'])})
            else:
                item.update({'matches': []})

            if item['dont_matches']:
                item.update({'dont_matches': ast.literal_eval(item['dont_matches'])})
            else:
                item.update({'dont_matches': []})

        if mixed:
            data, source_keys = merge_history_rows(database, timeline, episode_history, 'episode',
                                                   arr_item_column=TableHistory.__table__.c.episode_id,
                                                   include_upgradable=include_upgradable)
            return marshal({'data': data, 'total': count, 'source_keys': source_keys}, self.mixed_response_model)

        count = database.execute(
            select(func.count())
            .select_from(TableHistory)
            .join(TableEpisodes)
            .where(reduce(operator.and_, query_conditions))) \
            .scalar()

        return marshal({'data': episode_history, 'total': count}, self.get_response_model)
