# coding=utf-8

from datetime import datetime

from flask_restx import Resource, Namespace, reqparse, fields, marshal

from app.database import TableEpisodes, database, select, update
from app.event_handler import event_stream
from arr_instances.resolution import scoped
from api.swaggerui import subtitles_model, subtitles_language_model, audio_language_model

from ..utils import authenticate, postprocess

api_ns_episodes = Namespace('Episodes', description='List episodes metadata for specific series or episodes.')


@api_ns_episodes.route('episodes')
class Episodes(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('seriesid[]', type=int, action='append', required=False, default=[],
                                    help='Upstream Sonarr series IDs (legacy; not unique across instances)')
    get_request_parser.add_argument('episodeid[]', type=int, action='append', required=False, default=[],
                                    help='Upstream Sonarr episode IDs (legacy; not unique across instances)')
    get_request_parser.add_argument('series_id[]', type=int, action='append', required=False, default=[],
                                    help='Canonical local series IDs (#156; preferred)')
    get_request_parser.add_argument('id[]', type=int, action='append', required=False, default=[],
                                    help='Canonical local episode IDs (#156; preferred)')

    get_subtitles_model = api_ns_episodes.model('subtitles_model', subtitles_model)
    get_subtitles_language_model = api_ns_episodes.model('subtitles_language_model', subtitles_language_model)
    get_audio_language_model = api_ns_episodes.model('audio_language_model', audio_language_model)

    get_response_model = api_ns_episodes.model('EpisodeGetResponse', {
        # Canonical local ids + owning instance (#156); additive alongside the
        # upstream sonarrEpisodeId/sonarrSeriesId. series_id is the local ref.
        'id': fields.Integer(),
        'arr_instance_id': fields.Integer(),
        'series_id': fields.Integer(),
        'audio_language': fields.Nested(get_audio_language_model),
        'episode': fields.Integer(),
        'missing_subtitles': fields.Nested(get_subtitles_language_model),
        'monitored': fields.Boolean(),
        'path': fields.String(),
        'season': fields.Integer(),
        'sonarrEpisodeId': fields.Integer(),
        'sonarrSeriesId': fields.Integer(),
        'subtitles': fields.Nested(get_subtitles_model),
        'title': fields.String(),
        'sceneName': fields.String(),
        'forced_search_pending': fields.Boolean(),
    })

    @authenticate
    @api_ns_episodes.doc(parser=get_request_parser)
    @api_ns_episodes.response(200, 'Success')
    @api_ns_episodes.response(401, 'Not Authenticated')
    @api_ns_episodes.response(404, 'Series or Episode ID not provided')
    def get(self):
        """List episodes metadata for specific series or episodes"""
        args = self.get_request_parser.parse_args()
        seriesId = args.get('seriesid[]')
        episodeId = args.get('episodeid[]')
        localSeriesId = args.get('series_id[]')
        localEpisodeId = args.get('id[]')

        stmt = select(
                TableEpisodes.id,
                TableEpisodes.arr_instance_id,
                TableEpisodes.series_id,
                TableEpisodes.audio_language,
                TableEpisodes.episode,
                TableEpisodes.missing_subtitles,
                TableEpisodes.monitored,
                TableEpisodes.path,
                TableEpisodes.season,
                TableEpisodes.sonarrEpisodeId,
                TableEpisodes.sonarrSeriesId,
                TableEpisodes.subtitles,
                TableEpisodes.title,
                TableEpisodes.sceneName,
                TableEpisodes.forced_search_requested_at,
            )

        # Prefer the canonical local ids (#156); fall back to upstream ids.
        if len(localEpisodeId) > 0:
            stmt_query = database.execute(
                stmt
                .where(TableEpisodes.id.in_(localEpisodeId)))\
                .all()
        elif len(localSeriesId) > 0:
            stmt_query = database.execute(
                stmt
                .where(TableEpisodes.series_id.in_(localSeriesId))
                .order_by(TableEpisodes.season.desc(), TableEpisodes.episode.desc()))\
                .all()
        elif len(episodeId) > 0:
            stmt_query = database.execute(
                stmt
                .where(TableEpisodes.sonarrEpisodeId.in_(episodeId)))\
                .all()
        elif len(seriesId) > 0:
            stmt_query = database.execute(
                stmt
                .where(TableEpisodes.sonarrSeriesId.in_(seriesId))
                .order_by(TableEpisodes.season.desc(), TableEpisodes.episode.desc()))\
                .all()
        else:
            return "Series or Episode ID not provided", 404

        return marshal([postprocess({
                'id': x.id,
                'arr_instance_id': x.arr_instance_id,
                'series_id': x.series_id,
                'audio_language': x.audio_language,
                'episode': x.episode,
                'missing_subtitles': x.missing_subtitles,
                'monitored': x.monitored,
                'path': x.path,
                'season': x.season,
                'sonarrEpisodeId': x.sonarrEpisodeId,
                'sonarrSeriesId': x.sonarrSeriesId,
                'subtitles': x.subtitles,
                'title': x.title,
                'sceneName': x.sceneName,
                'forced_search_pending': x.forced_search_requested_at is not None,
                }) for x in stmt_query], self.get_response_model, envelope='data')


@api_ns_episodes.route('episodes/forced-search')
class EpisodesForcedSearch(Resource):
    """Ask for, or withdraw, a forced duplicate subtitle search of one episode.

    Setting the request only records it: no search runs here. The next search
    run that covers the episode picks it up and clears it once it has finished.
    """
    request_parser = reqparse.RequestParser()
    # The query string wins over a form or JSON body.
    request_parser.add_argument('seriesid', type=int, required=True, location=('args', 'form', 'json'),
                                help='Upstream Sonarr series ID (legacy; not unique across instances)')
    request_parser.add_argument('episodeid', type=int, required=True, location=('args', 'form', 'json'),
                                help='Upstream Sonarr episode ID (legacy; not unique across instances)')
    request_parser.add_argument('arr_instance_id', type=int, required=False, location=('args', 'form', 'json'),
                                help='Owning Sonarr instance id; required when the ids exist in more than one')

    @staticmethod
    def _change(requested_at):
        args = EpisodesForcedSearch.request_parser.parse_args()
        episode_id = args.get('episodeid')
        rows = database.execute(
            scoped(select(TableEpisodes.id)
                   .where(TableEpisodes.sonarrEpisodeId == episode_id,
                          TableEpisodes.sonarrSeriesId == args.get('seriesid')),
                   TableEpisodes.arr_instance_id, args.get('arr_instance_id')).limit(2)).all()
        if not rows:
            return 'Episode not found', 404
        if len(rows) > 1:
            return 'This episode exists in more than one instance; specify arr_instance_id', 409
        row = rows[0]

        # Asking again keeps the time of the first request, and withdrawing a
        # request that is not there changes nothing: each write only touches a
        # row that is in the other state, so both answer success.
        flag = TableEpisodes.forced_search_requested_at
        database.execute(update(TableEpisodes)
                         .where(TableEpisodes.id == row.id,
                                flag.is_(None) if requested_at is not None else flag.is_not(None))
                         .values(forced_search_requested_at=requested_at))
        event_stream(type='series', action='update', payload=args.get('seriesid'))
        return '', 204

    @authenticate
    @api_ns_episodes.doc(parser=request_parser)
    @api_ns_episodes.response(204, 'Forced search requested')
    @api_ns_episodes.response(401, 'Not Authenticated')
    @api_ns_episodes.response(404, 'Episode not found')
    @api_ns_episodes.response(409, 'Owning instance is ambiguous')
    def post(self):
        """Request a forced duplicate search of an episode"""
        return self._change(datetime.now())

    @authenticate
    @api_ns_episodes.doc(parser=request_parser)
    @api_ns_episodes.response(204, 'Forced search request cleared')
    @api_ns_episodes.response(401, 'Not Authenticated')
    @api_ns_episodes.response(404, 'Episode not found')
    @api_ns_episodes.response(409, 'Owning instance is ambiguous')
    def delete(self):
        """Withdraw the forced duplicate search request of an episode"""
        return self._change(None)
