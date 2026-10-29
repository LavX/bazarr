# coding=utf-8

import os

from flask_restx import Resource, Namespace, reqparse, marshal

from app.database import TableEpisodes, TableShows, database, select
from utilities.path_mappings import path_mappings
from app.get_providers import get_providers_sorted
from subtitles.manual import manual_search, queue_manual_search, episode_manually_download_specific_subtitle
from subtitles.search_result import SEARCH_RESULT_FIELDS
from sonarr.history import history_log  # noqa: F401
from app.config import settings  # noqa: F401
from app.jobs_queue import jobs_queue  # noqa: F401
from subtitles.indexer.series import store_subtitles, list_missing_subtitles
from api.swaggerui import job_queued_model

from ..queued_job import queued_job_model, queued_job_response
from ..utils import authenticate

api_ns_providers_episodes = Namespace('Providers Episodes', description='List and download episodes subtitles manually')


def _episode_search_target(episode_id):
    """Look an episode up for a search: its file, profile and names, or the refusal to answer with."""
    stmt = select(TableEpisodes.path,
                  TableEpisodes.sceneName,
                  TableEpisodes.sonarrEpisodeId,
                  TableEpisodes.arr_instance_id,
                  TableEpisodes.season,
                  TableEpisodes.episode,
                  TableShows.title,
                  TableShows.profileId,
                  TableEpisodes.subtitles,
                  TableEpisodes.missing_subtitles) \
        .select_from(TableEpisodes) \
        .join(TableShows) \
        .where(TableEpisodes.id == episode_id)
    episodeInfo = database.execute(stmt).first()

    if not episodeInfo:
        return None, ('Episode not found', 404)
    elif episodeInfo.subtitles is None:
        # subtitles indexing for this episode is incomplete, we'll do it again
        store_subtitles(episodeInfo.path, path_mappings.path_replace_instance(episodeInfo.path, episodeInfo.arr_instance_id, 'episode'),
                        arr_instance_id=episodeInfo.arr_instance_id)
        episodeInfo = database.execute(stmt).first()
    elif episodeInfo.missing_subtitles is None:
        # missing subtitles calculation for this episode is incomplete, we'll do it again
        list_missing_subtitles(epno=episodeInfo.sonarrEpisodeId,
                               arr_instance_id=episodeInfo.arr_instance_id)
        episodeInfo = database.execute(stmt).first()

    episodePath = path_mappings.path_replace(episodeInfo.path)

    if not os.path.exists(episodePath):
        return None, ('Episode file not found. Path mapping issue?', 500)

    return {'path': episodePath, 'title': episodeInfo.title, 'sceneName': episodeInfo.sceneName or "None",
            'profileId': episodeInfo.profileId, 'arr_instance_id': episodeInfo.arr_instance_id,
            'season': episodeInfo.season, 'episode': episodeInfo.episode}, None


@api_ns_providers_episodes.route('providers/episodes')
class ProviderEpisodes(Resource):
    get_request_parser = reqparse.RequestParser()
    get_request_parser.add_argument('episodeid', type=int, required=True, help='Episode ID')

    get_response_model = api_ns_providers_episodes.model('ProviderEpisodesGetResponse', SEARCH_RESULT_FIELDS)

    @authenticate
    @api_ns_providers_episodes.response(401, 'Not Authenticated')
    @api_ns_providers_episodes.response(404, 'Episode not found')
    @api_ns_providers_episodes.response(500, 'Custom error messages')
    @api_ns_providers_episodes.doc(parser=get_request_parser)
    def get(self):
        """Search manually for an episode subtitles"""
        args = self.get_request_parser.parse_args()
        # 'episodeid' is the canonical local episode id (globally unique across
        # instances) that GetItemId and the browse endpoints use. Looking the row
        # up by local id avoids the upstream-id collision between instances; on a
        # single default instance local id == sonarrEpisodeId, so unchanged. (#156)
        target, refusal = _episode_search_target(args.get('episodeid'))
        if refusal:
            return refusal

        providers_list = get_providers_sorted()

        provider_outcomes = []
        data = manual_search(target['path'], target['profileId'], providers_list, target['sceneName'],
                             target['title'], 'series', arr_instance_id=target['arr_instance_id'],
                             provider_outcomes=provider_outcomes)
        if isinstance(data, str):
            return data, 500
        return {'data': marshal(data, self.get_response_model), 'providers': provider_outcomes}

    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('seriesid', type=int, required=True, help='Series ID')
    post_request_parser.add_argument('episodeid', type=int, required=True, help='Episode ID')
    post_request_parser.add_argument('hi', type=str, required=True, help='HI subtitles from ["True", "False"]')
    post_request_parser.add_argument('forced', type=str, required=True, help='Forced subtitles from ["True", "False"]')
    post_request_parser.add_argument('original_format', type=str, required=True,
                                     help='Use original subtitles format from ["True", "False"]')
    post_request_parser.add_argument('provider', type=str, required=True, help='Provider name')
    post_request_parser.add_argument('subtitle', type=str, required=True, help='Subtitle ID as returned by GET')
    post_request_parser.add_argument('arr_instance_id', type=int, required=False,
                                     help='Owning Sonarr instance id (#156); scopes the download to it')

    post_job_model = api_ns_providers_episodes.model('JobQueued', job_queued_model)

    @authenticate
    @api_ns_providers_episodes.doc(parser=post_request_parser)
    @api_ns_providers_episodes.response(202, 'Manual subtitle download queued', post_job_model)
    @api_ns_providers_episodes.response(401, 'Not Authenticated')
    @api_ns_providers_episodes.response(404, 'Episode not found')
    @api_ns_providers_episodes.response(500, 'Custom error messages')
    def post(self):
        """Manually download an episode subtitles"""
        args = self.post_request_parser.parse_args()
        # The frontend sends upstream sonarrSeriesId and sonarrEpisodeId (not
        # local autoincrement ids), so pass them directly to the downstream
        # function which resolves the row by (sonarrEpisodeId, arr_instance_id).
        # A local-id pre-lookup was causing collisions: when a different episode's
        # local id happened to equal the requested upstream episode id the handler
        # routed to the wrong row or 404'd on an instance mismatch. (#156 F8)
        series_id = args.get('seriesid')
        episode_id = args.get('episodeid')
        arr_instance_id = args.get('arr_instance_id')

        job_id = episode_manually_download_specific_subtitle(sonarr_series_id=series_id,
                                                    sonarr_episode_id=episode_id,
                                                    hi=args.get('hi').capitalize(),
                                                    forced=args.get('forced').capitalize(),
                                                    use_original_format=args.get('original_format').capitalize(),
                                                    selected_provider=args.get('provider'),
                                                    subtitle=args.get('subtitle'),
                                                    job_id=None,
                                                    arr_instance_id=arr_instance_id)

        return {'job_id': job_id or None}, 202


@api_ns_providers_episodes.route('providers/episodes/search')
class ProviderEpisodesSearch(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('episodeid', type=int, required=True, help='Episode ID')

    post_job_model = api_ns_providers_episodes.model('EpisodeSearchQueued', queued_job_model)

    @authenticate
    @api_ns_providers_episodes.doc(parser=post_request_parser)
    @api_ns_providers_episodes.response(202, 'Search queued as a job; its rows are the job result', post_job_model)
    @api_ns_providers_episodes.response(401, 'Not Authenticated')
    @api_ns_providers_episodes.response(404, 'Episode not found')
    @api_ns_providers_episodes.response(500, 'Custom error messages')
    def post(self):
        """Search manually for an episode's subtitles as a job

        The same search as the GET, run as a job so it survives the page closing and shows in the jobs
        list. Read its rows from system/jobs/result once the job finishes."""
        args = self.post_request_parser.parse_args()
        target, refusal = _episode_search_target(args.get('episodeid'))
        if refusal:
            return refusal

        label = target['title']
        if isinstance(target['season'], int) and isinstance(target['episode'], int):
            label += f" - S{target['season']:02d}E{target['episode']:02d}"
        job_id = queue_manual_search(target['path'], target['profileId'], target['sceneName'], target['title'],
                                     'series', label, arr_instance_id=target['arr_instance_id'])
        return queued_job_response(job_id), 202
