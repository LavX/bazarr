# coding=utf-8

import operator

from flask import request
from flask_restx import Resource, Namespace, reqparse, fields, marshal
from functools import reduce
from sqlalchemy import case

from app.database import get_exclusion_clause, TableEpisodes, TableShows, database, select, update, func
from arr_instances.resolution import scoped
from sonarr.sync.series import update_one_series, update_one_series_for_instance
from subtitles.indexer.series import list_missing_subtitles, series_scan_subtitles
from subtitles.mass_download import series_download_subtitles
from subtitles.tools.combine.main import try_combine_for_video
from subtitles.wanted import wanted_search_missing_subtitles_series, wanted_scan_subtitles_series
from app.event_handler import event_stream
from api.swaggerui import subtitles_model, subtitles_language_model, audio_language_model
from utilities.path_mappings import path_mappings

from api.utils import authenticate, None_Keys, postprocess

api_ns_series = Namespace('Series', description='List series metadata, update series languages profile or run actions '
                                                'for specific series.')


@api_ns_series.route('series')
class Series(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start integer')
    get_request_parser.add_argument('length', type=int, required=False, default=-1, help='Paging length integer')
    get_request_parser.add_argument('seriesid[]', type=int, action='append', required=False, default=[],
                                    help='Upstream Sonarr series IDs (legacy; not unique across instances)')
    get_request_parser.add_argument('id[]', type=int, action='append', required=False, default=[],
                                    help='Canonical local series IDs (#156; preferred, unique across instances)')

    get_subtitles_model = api_ns_series.model('subtitles_model', subtitles_model)
    get_subtitles_language_model = api_ns_series.model('subtitles_language_model', subtitles_language_model)
    get_audio_language_model = api_ns_series.model('audio_language_model', audio_language_model)

    data_model = api_ns_series.model('series_data_model', {
        # Canonical local id + owning instance (#156). Additive: the upstream
        # sonarrSeriesId stays for back-compat; the frontend migrates to id.
        'id': fields.Integer(),
        'arr_instance_id': fields.Integer(),
        'alternativeTitles': fields.List(fields.String),
        'audio_language': fields.Nested(get_audio_language_model),
        'episodeFileCount': fields.Integer(default=0),
        'ended': fields.Boolean(),
        'episodeMissingCount': fields.Integer(default=0),
        'fanart': fields.String(),
        'imdbId': fields.String(),
        'lastAired': fields.String(),
        'monitored': fields.Boolean(),
        'overview': fields.String(),
        'path': fields.String(),
        'poster': fields.String(),
        'profileId': fields.Integer(),
        'seriesType': fields.String(),
        'sonarrSeriesId': fields.Integer(),
        'tags': fields.List(fields.String),
        'title': fields.String(),
        'tvdbId': fields.Integer(),
        'year': fields.String(),
    })

    get_response_model = api_ns_series.model('SeriesGetResponse', {
        'data': fields.Nested(data_model),
        'total': fields.Integer(),
    })

    @authenticate
    @api_ns_series.doc(parser=get_request_parser)
    @api_ns_series.response(200, 'Success')
    @api_ns_series.response(401, 'Not Authenticated')
    def get(self):
        """List series metadata for specific series"""
        args = self.get_request_parser.parse_args()
        start = args.get('start')
        length = args.get('length')
        seriesId = args.get('seriesid[]')
        localId = args.get('id[]')

        episodeFileCount = select(TableEpisodes.series_id,
                                  func.count(TableEpisodes.id).label('episodeFileCount')) \
            .select_from(TableEpisodes) \
            .group_by(TableEpisodes.series_id)\
            .subquery()

        episodes_missing_conditions = [(TableEpisodes.missing_subtitles.is_not(None)),
                                       (TableEpisodes.missing_subtitles != '[]')]
        episodes_missing_conditions += get_exclusion_clause('series')

        episodeMissingCount = select(TableEpisodes.series_id,
                                     func.count(TableEpisodes.id).label('episodeMissingCount')) \
            .select_from(TableEpisodes) \
            .where(reduce(operator.and_, episodes_missing_conditions)) \
            .group_by(TableEpisodes.series_id)\
            .subquery()

        # Correlated subquery: get the first non-empty audio_language from an episode
        # for this series (fallback for Sonarr v4 where series-level audio_language is empty)
        first_episode_audio = (
            select(TableEpisodes.audio_language)
            .where(TableEpisodes.series_id == TableShows.id)
            .where(TableEpisodes.audio_language.is_not(None))
            .where(TableEpisodes.audio_language != '[]')
            .limit(1)
            .correlate(TableShows)
            .scalar_subquery()
        )

        audio_language_col = case(
            (TableShows.audio_language.in_(['[]', None, '']), first_episode_audio),
            else_=TableShows.audio_language
        ).label('audio_language')

        stmt = select(TableShows.id,
                      TableShows.arr_instance_id,
                      TableShows.tvdbId,
                      TableShows.alternativeTitles,
                      audio_language_col,
                      TableShows.fanart,
                      TableShows.imdbId,
                      TableShows.monitored,
                      TableShows.overview,
                      TableShows.path,
                      TableShows.poster,
                      TableShows.profileId,
                      TableShows.seriesType,
                      TableShows.sonarrSeriesId,
                      TableShows.tags,
                      TableShows.title,
                      TableShows.year,
                      TableShows.ended,
                      TableShows.lastAired,
                      episodeFileCount.c.episodeFileCount,
                      episodeMissingCount.c.episodeMissingCount) \
            .select_from(TableShows) \
            .join(episodeFileCount, TableShows.id == episodeFileCount.c.series_id, isouter=True) \
            .join(episodeMissingCount, TableShows.id == episodeMissingCount.c.series_id, isouter=True)\
            .order_by(TableShows.sortTitle)

        # Prefer the canonical local id (#156); fall back to the upstream id.
        if len(localId) != 0:
            stmt = stmt.where(TableShows.id.in_(localId))
        elif len(seriesId) != 0:
            stmt = stmt.where(TableShows.sonarrSeriesId.in_(seriesId))
        elif length > 0:
            stmt = stmt.limit(length).offset(start)

        results = [postprocess({
            'id': x.id,
            'arr_instance_id': x.arr_instance_id,
            'tvdbId': x.tvdbId,
            'alternativeTitles': x.alternativeTitles,
            'audio_language': x.audio_language,
            'fanart': x.fanart,
            'imdbId': x.imdbId,
            'monitored': x.monitored,
            'overview': x.overview,
            'path': x.path,
            'poster': x.poster,
            'profileId': x.profileId,
            'seriesType': x.seriesType,
            'sonarrSeriesId': x.sonarrSeriesId,
            'tags': x.tags,
            'title': x.title,
            'year': x.year,
            'ended': x.ended,
            'lastAired': x.lastAired,
            'episodeFileCount': x.episodeFileCount,
            'episodeMissingCount': x.episodeMissingCount,
        }) for x in database.execute(stmt).all()]

        count = database.execute(
            select(func.count())
            .select_from(TableShows)) \
            .scalar()

        return marshal({'data': results, 'total': count}, self.get_response_model)

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('seriesid', type=int, action='append', required=False, default=[],
                                     help='Sonarr series ID')
    post_request_parser.add_argument('id', type=int, action='append', required=False, default=[],
                                     help='Canonical local series ID(s) (#156; preferred)')
    post_request_parser.add_argument('arr_instance_id', type=int, action='append', required=False, default=[],
                                     help='Owning Sonarr instance id for legacy seriesid fallback (#156)')
    post_request_parser.add_argument('profileid', type=str, action='append', required=False, default=[],
                                     help='Languages profile(s) ID or "none"')

    @authenticate
    @api_ns_series.doc(parser=post_request_parser)
    @api_ns_series.response(204, 'Success')
    @api_ns_series.response(401, 'Not Authenticated')
    @api_ns_series.response(404, 'Languages profile not found')
    def post(self):
        """Update specific series languages profile"""
        args = self.post_request_parser.parse_args()
        seriesIdList = args.get('seriesid')
        localIdList = args.get('id')
        arrInstanceIdList = args.get('arr_instance_id')
        profileIdList = args.get('profileid')
        targetList = localIdList if localIdList else seriesIdList

        for idx in range(len(targetList)):
            profileId = profileIdList[idx]

            if profileId in None_Keys:
                profileId = None
            else:
                try:
                    profileId = int(profileId)
                except Exception:
                    return 'Languages profile not found', 404

            if localIdList:
                localId = targetList[idx]
                series = database.execute(
                    select(TableShows.sonarrSeriesId, TableShows.arr_instance_id)
                    .where(TableShows.id == localId))\
                    .first()
                if not series:
                    continue
                database.execute(
                    update(TableShows)
                    .values(profileId=profileId)
                    .where(TableShows.id == localId))
                seriesId = series.sonarrSeriesId
                arr_instance_id = series.arr_instance_id
                episode_id_query = select(TableEpisodes.sonarrEpisodeId).where(TableEpisodes.series_id == localId)
            else:
                seriesId = targetList[idx]
                arr_instance_id = arrInstanceIdList[idx] if idx < len(arrInstanceIdList) else None
                if arr_instance_id is None:
                    matches = database.execute(
                        select(TableShows.id).where(TableShows.sonarrSeriesId == seriesId)
                    ).all()
                    if len(matches) > 1:
                        return 'Ambiguous Sonarr series ID; pass id or arr_instance_id', 400
                else:
                    series = database.execute(
                        scoped(
                            select(TableShows.id).where(TableShows.sonarrSeriesId == seriesId),
                            TableShows.arr_instance_id,
                            arr_instance_id,
                        )
                    ).first()
                    if not series:
                        continue
                database.execute(
                    scoped(
                        update(TableShows)
                        .values(profileId=profileId)
                        .where(TableShows.sonarrSeriesId == seriesId),
                        TableShows.arr_instance_id,
                        arr_instance_id,
                    ))
                episode_id_query = scoped(
                    select(TableEpisodes.sonarrEpisodeId).where(TableEpisodes.sonarrSeriesId == seriesId),
                    TableEpisodes.arr_instance_id,
                    arr_instance_id,
                )

            list_missing_subtitles(no=seriesId, arr_instance_id=arr_instance_id)

            event_stream(type='series', payload=seriesId)

            episode_id_list = database.execute(episode_id_query).all()

            for item in episode_id_list:
                event_stream(type='episode-wanted', payload=item.sonarrEpisodeId)

        event_stream(type='badges')

        return '', 204

    patch_request_parser = reqparse.RequestParser()
    patch_request_parser.add_argument('seriesid', type=int, required=False, help='Sonarr series ID')
    patch_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                      help='Owning Sonarr instance id (#156)')
    patch_request_parser.add_argument('action', type=str, required=False, help='Action to perform from ["scan-disk", '
                                                                               '"search-missing", "search-wanted", "sync"]')

    @authenticate
    @api_ns_series.doc(parser=patch_request_parser)
    @api_ns_series.response(204, 'Success')
    @api_ns_series.response(400, 'Unknown action')
    @api_ns_series.response(401, 'Not Authenticated')
    @api_ns_series.response(500, 'Series directory not found. Path mapping issue?')
    def patch(self):
        """Run actions on specific series"""
        args = self.patch_request_parser.parse_args()
        seriesid = args.get('seriesid')
        arr_instance_id = args.get('arr_instance_id')
        action = args.get('action')
        if action == "scan-disk":
            series_scan_subtitles(seriesid, arr_instance_id=arr_instance_id)
            return '', 204
        elif action == "search-missing":
            try:
                series_download_subtitles(seriesid, arr_instance_id=arr_instance_id)
            except OSError:
                return 'Series directory not found. Path mapping issue?', 500
            else:
                return '', 204
        elif action == "search-wanted":
            wanted_search_missing_subtitles_series()
            return '', 204
        elif action == "scan-wanted":
            wanted_scan_subtitles_series()
            return '', 204
        elif action == "sync":
            if arr_instance_id is not None:
                update_one_series_for_instance(arr_instance_id, seriesid, 'updated')
            else:
                update_one_series(seriesid, 'updated')
            return '', 204

        return 'Unknown action', 400


def _list_series_episodes(series_id, arr_instance_id=None):
    rows = database.execute(
        scoped(
            select(
                TableEpisodes.sonarrEpisodeId,
                TableEpisodes.sonarrSeriesId,
                TableEpisodes.path,
            ).where(TableEpisodes.sonarrSeriesId == series_id),
            TableEpisodes.arr_instance_id,
            arr_instance_id,
        )
    ).all()
    return [
        {
            'sonarrEpisodeId': r.sonarrEpisodeId,
            'sonarrSeriesId': r.sonarrSeriesId,
            'path': r.path,
        }
        for r in rows
    ]


@api_ns_series.route('series/<int:series_id>/subtitles/combine')
class SeriesSubtitlesCombine(Resource):
    @authenticate
    @api_ns_series.response(200, 'Batch combine summary')
    @api_ns_series.response(401, 'Not Authenticated')
    @api_ns_series.response(404, 'Series not found')
    def post(self, series_id):
        """Build the combined subtitle file for every episode in the series
        that has all required source languages on disk."""
        payload = request.get_json(silent=True) or {}
        languages = payload.get('languages')
        format_ = payload.get('format')
        arr_instance_id = request.args.get('arr_instance_id', type=int)

        episodes = _list_series_episodes(series_id, arr_instance_id=arr_instance_id)
        if not episodes:
            return {'status': 'not_found'}, 404

        built, skipped, failed = 0, 0, 0
        details = []
        for ep in episodes:
            video_path = path_mappings.path_replace(ep['path'])
            r = try_combine_for_video(
                video_path=video_path,
                media_type='series',
                radarr_id=None,
                sonarr_series_id=ep['sonarrSeriesId'],
                sonarr_episode_id=ep['sonarrEpisodeId'],
                languages=languages,
                format=format_,
            )
            details.append({
                'episodeId': ep['sonarrEpisodeId'],
                'status': r.status,
                'path': r.path,
                'reason': r.reason,
                'error': r.error,
            })
            if r.status == 'built':
                built += 1
            elif r.status == 'skipped':
                skipped += 1
            else:
                failed += 1
        return {
            'status': 'batch_complete',
            'built': built,
            'skipped': skipped,
            'failed': failed,
            'details': details,
        }, 200
