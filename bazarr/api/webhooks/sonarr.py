# coding=utf-8
import logging

from flask_restx import Resource, Namespace, fields

from app.database import TableEpisodes, TableShows, database, select
from arr_instances.repository import ArrInstanceRepository
from arr_instances.resolution import scoped
from sonarr.sync.episodes import sync_one_episode, sync_one_episode_for_instance
from subtitles.mass_download import episode_download_subtitles
from subtitles.indexer.series import store_subtitles
from utilities.path_mappings import path_mappings


from ..utils import authenticate


api_ns_webhooks_sonarr = Namespace(
    "Webhooks Sonarr",
    description="Webhooks to trigger subtitles search based on Sonarr webhooks",
)


@api_ns_webhooks_sonarr.route("webhooks/sonarr")
@api_ns_webhooks_sonarr.route("webhooks/sonarr/<string:stable_key>")
class WebHooksSonarr(Resource):
    episode_model = api_ns_webhooks_sonarr.model(
        "SonarrEpisode",
        {
            "id": fields.Integer(required=True, description="Episode ID"),
        },
        strict=False,
    )

    episode_file_model = api_ns_webhooks_sonarr.model(
        "SonarrEpisodeFile",
        {
            "id": fields.Integer(required=True, description="Episode file ID"),
        },
        strict=False,
    )

    sonarr_webhook_model = api_ns_webhooks_sonarr.model(
        "SonarrWebhook",
        {
            "episodes": fields.List(
                fields.Nested(episode_model),
                required=False,
                description="List of episodes. Can be used to sync episodes from Sonarr if not found in Bazarr.",
            ),
            "episodeFiles": fields.List(
                fields.Nested(episode_file_model),
                required=False,
                description="List of episode files; required for anything other than test hooks",
            ),
            "eventType": fields.String(
                required=True,
                description="Type of Sonarr event (e.g. Test, Download, etc.)",
            ),
        },
        strict=False,
    )

    @authenticate
    @api_ns_webhooks_sonarr.expect(sonarr_webhook_model, validate=True)
    @api_ns_webhooks_sonarr.response(200, "Success")
    @api_ns_webhooks_sonarr.response(401, "Not Authenticated")
    def post(self, stable_key=None):
        """Search for missing subtitles based on Sonarr webhooks.

        The optional <stable_key> path segment (#156) identifies the owning
        Sonarr instance so each instance can use its own webhook URL
        (/api/webhooks/sonarr/<stable_key>). With no key this is the legacy
        single-instance path: arr_instance_id stays None, so every lookup and
        action is unscoped exactly as before (byte-identical).
        """
        args = api_ns_webhooks_sonarr.payload
        event_type = args.get("eventType")

        logging.debug(f"Received Sonarr webhook event: {event_type}")  # noqa: G004

        arr_instance_id = None
        if stable_key is not None:
            instance = ArrInstanceRepository(database).get_by_key("sonarr", stable_key)
            if instance is None or not instance.enabled:
                # Return 200 so Sonarr does not flag the webhook unhealthy.
                logging.warning("Sonarr webhook for unknown/disabled instance key %s; ignoring.", stable_key)
                return "Unknown or disabled instance.", 200
            arr_instance_id = instance.id

        if event_type == "Test":
            message = "Received test hook, skipping database search."
            logging.debug(message)
            return message, 200

        # Sonarr hooks only differentiate a download starting vs. ending by
        # the inclusion of episodeFiles in the payload.
        sonarr_episode_file_ids = [e.get("id") for e in args.get("episodeFiles", [])]

        if not sonarr_episode_file_ids:
            message = "No episode file IDs found in the webhook request. Nothing to do."
            logging.debug(message)
            # Sonarr reports the webhook as 'unhealthy' and requires
            # user interaction if we return anything except 200s.
            return message, 200

        sonarr_episode_ids = [e.get("id") for e in args.get("episodes", [])]

        if len(sonarr_episode_ids) != len(sonarr_episode_file_ids):
            logging.debug(
                "Episode IDs and episode file IDs are different lengths, ignoring episode IDs."
            )
            sonarr_episode_ids = []

        for i, efid in enumerate(sonarr_episode_file_ids):
            # Scope by the owning instance: episode_file_id is per-Sonarr, so it
            # collides across instances. scoped() is a no-op when arr_instance_id
            # is None (legacy URL), keeping the default path byte-identical.
            q = scoped(
                select(TableEpisodes.sonarrEpisodeId, TableEpisodes.path)
                .select_from(TableEpisodes)
                .join(TableShows)
                .where(TableEpisodes.episode_file_id == efid),
                TableEpisodes.arr_instance_id, arr_instance_id,
            )

            episode = database.execute(q).first()
            if not episode and sonarr_episode_ids:
                logging.debug(
                    "No episode found for episode file ID %s, attempting to sync from Sonarr.",
                    efid,
                )
                if arr_instance_id is not None:
                    sync_one_episode_for_instance(arr_instance_id, sonarr_episode_ids[i])
                else:
                    sync_one_episode(sonarr_episode_ids[i])
                episode = database.execute(q).first()
            if not episode:
                logging.debug(
                    "No episode found for episode file ID %s, skipping.", efid
                )
                continue

            store_subtitles(episode.path, path_mappings.path_replace(episode.path))
            episode_download_subtitles(no=episode.sonarrEpisodeId, arr_instance_id=arr_instance_id)

        return "Finished processing subtitles.", 200
