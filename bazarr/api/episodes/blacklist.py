# coding=utf-8

from flask_restx import Resource, Namespace, reqparse, fields
from sqlalchemy import Text

from app.database import TableEpisodes, TableShows, TableBlacklist, database, select
from subtitles.tools.delete import delete_subtitles
from subtitles.tools.delete_ownership import SubtitleDeletionError, resolve_subtitle_for_deletion
from sonarr.blacklist import blacklist_log, blacklist_delete_all, blacklist_delete
from subtitles.mass_download import episode_download_subtitles
from app.event_handler import event_stream
from api.swaggerui import subtitles_language_model, job_queued_model
from media_sources.blacklist import add_blacklist_delete_arguments, delete_blacklist, list_blacklist
from media_sources.union import add_source_arguments

from ..utils import authenticate

api_ns_episodes_blacklist = Namespace('Episodes Blacklist', description='List, add or remove subtitles to or from '
                                                                        'episodes blacklist')


@api_ns_episodes_blacklist.route('episodes/blacklist')
class EpisodesBlacklist(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('start', type=int, required=False, default=0, help='Paging start integer')
    get_request_parser.add_argument('length', type=int, required=False, default=-1, help='Paging length integer')

    add_source_arguments(get_request_parser)

    get_language_model = api_ns_episodes_blacklist.model('subtitles_language_model', subtitles_language_model)

    get_response_model = api_ns_episodes_blacklist.model('EpisodeBlacklistGetResponse', {
        'id': fields.Integer(),
        # Owning instance (#156) so the remove action can route.
        'arr_instance_id': fields.Integer(),
        'seriesTitle': fields.String(),
        'episode_number': fields.String(),
        'episodeTitle': fields.String(),
        'sonarrSeriesId': fields.Integer(),
        'provider': fields.String(),
        'subs_id': fields.String(),
        'language': fields.Nested(get_language_model),
        'timestamp': fields.String(),
        'parsed_timestamp': fields.String(),
    })

    @authenticate
    @api_ns_episodes_blacklist.response(401, 'Not Authenticated')
    @api_ns_episodes_blacklist.doc(parser=get_request_parser)
    def get(self):
        """List blacklisted episodes subtitles"""
        args = self.get_request_parser.parse_args()
        stmt = select(TableShows.id,
                      TableShows.title.label('seriesTitle'),
                      TableEpisodes.season.cast(Text).concat('x').concat(TableEpisodes.episode.cast(Text)).label('episode_number'),
                      TableEpisodes.title.label('episodeTitle'),
                      TableEpisodes.sonarrSeriesId,
                      TableBlacklist.arr_instance_id,
                      TableBlacklist.provider,
                      TableBlacklist.subs_id,
                      TableBlacklist.language,
                      TableBlacklist.timestamp) \
            .select_from(TableBlacklist) \
            .join(TableShows, onclause=TableBlacklist.series_id == TableShows.id) \
            .join(TableEpisodes, onclause=TableBlacklist.episode_id == TableEpisodes.id)
        return list_blacklist(database, args, stmt, TableBlacklist, 'episode', self.get_response_model)

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('seriesid', type=int, required=True, help='Series ID')
    post_request_parser.add_argument('episodeid', type=int, required=True, help='Episode ID')
    post_request_parser.add_argument('provider', type=str, required=True, help='Provider name')
    post_request_parser.add_argument('subs_id', type=str, required=True, help='Subtitles ID')
    post_request_parser.add_argument('language', type=str, required=True, help='Subtitles language')
    post_request_parser.add_argument('subtitles_path', type=str, required=True, help='Subtitles file path')
    post_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                     help='Owning Sonarr instance id (#156)')

    post_response_model = api_ns_episodes_blacklist.model('JobQueued', job_queued_model)

    @authenticate
    @api_ns_episodes_blacklist.doc(parser=post_request_parser)
    @api_ns_episodes_blacklist.response(200, 'Subtitle excluded; replacement search queued', post_response_model)
    @api_ns_episodes_blacklist.response(401, 'Not Authenticated')
    @api_ns_episodes_blacklist.response(403, "Subtitle is not one of this episode's current subtitles")
    @api_ns_episodes_blacklist.response(404, 'Episode not found')
    @api_ns_episodes_blacklist.response(409, 'Owning instance is ambiguous, or ownership changed before deletion')
    @api_ns_episodes_blacklist.response(500, 'Subtitles file not found or permission issue.')
    def post(self):
        """Add an episodes subtitles to blacklist"""
        args = self.post_request_parser.parse_args()
        sonarr_episode_id = args.get('episodeid')
        provider = args.get('provider')
        subs_id = args.get('subs_id')
        language = args.get('language')

        try:
            # The path only selects one of this episode's indexed subtitles; the
            # file removed is that entry, mapped through the owning instance.
            target = resolve_subtitle_for_deletion('series', sonarr_episode_id, args.get('subtitles_path'),
                                                   args.get('arr_instance_id'), session=database)
            sonarr_series_id = target.row.sonarrSeriesId
            arr_instance_id = target.row.arr_instance_id
            # Recorded by the deletion itself, once the file is really removed
            # and while the write locks are still held, so a refused or failed
            # deletion blacklists nothing.
            removed = delete_subtitles(media_type='series',
                                       language=language,
                                       forced=False,
                                       hi=False,
                                       media_path=target.media_path,
                                       subtitles_path=target.stored_path,
                                       sonarr_series_id=sonarr_series_id,
                                       sonarr_episode_id=sonarr_episode_id,
                                       arr_instance_id=arr_instance_id,
                                       revalidate=target.revalidate,
                                       after_delete=lambda: blacklist_log(sonarr_series_id=sonarr_series_id,
                                                                          sonarr_episode_id=sonarr_episode_id,
                                                                          provider=provider,
                                                                          subs_id=subs_id,
                                                                          language=language,
                                                                          arr_instance_id=arr_instance_id))
        except SubtitleDeletionError as exc:
            return str(exc), exc.status

        if removed:
            job_id = episode_download_subtitles(no=sonarr_episode_id, arr_instance_id=arr_instance_id)
            event_stream(type='episode-history')
            return {'job_id': job_id or None}, 200
        else:
            return 'Subtitles file not found or permission issue.', 500

    delete_request_parser = reqparse.RequestParser()
    delete_request_parser.add_argument('all', type=str, required=False, help='Empty episodes subtitles blacklist')
    delete_request_parser.add_argument('provider', type=str, required=False, help='Provider name')
    delete_request_parser.add_argument('subs_id', type=str, required=False, help='Subtitles ID')
    delete_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                       help='Owning Sonarr instance id (#156)')

    add_blacklist_delete_arguments(delete_request_parser)

    @authenticate
    @api_ns_episodes_blacklist.doc(parser=delete_request_parser)
    @api_ns_episodes_blacklist.response(204, 'Success')
    @api_ns_episodes_blacklist.response(401, 'Not Authenticated')
    def delete(self):
        """Delete an episodes subtitles from blacklist"""
        args = self.delete_request_parser.parse_args()
        refusal = delete_blacklist(args, session=database, kind='episode', arr_table=TableBlacklist,
                                   delete_one=blacklist_delete, delete_all=blacklist_delete_all)
        return refusal or ('', 204)
