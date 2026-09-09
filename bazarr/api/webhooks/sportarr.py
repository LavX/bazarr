# coding=utf-8
"""Sportarr import webhook.

Sonarr and Radarr have both had one since forever, so a user whose arr can
reach Bazarr but not the other way round still gets an immediate index and
search on import. Sports had none, leaving the SSE stream as the only live
path: a setup where Bazarr cannot hold an outbound connection to Sportarr had
to wait for the next scheduled sync.

Sportarr is a Sonarr fork and its notification model carries Sonarr's exact
event set (onGrab / onDownload / onUpgrade / onRename / onSeriesDelete /
onEpisodeFileDelete), so the payload contract here is Sonarr's: an eventType,
an episodes list and an episodeFiles list. Sportarr 4.1.6 stores a webhook
notification but never sends one, verified against a real import that reached
Bazarr over SSE and fired no hook, so this route cannot be exercised end to end
until that side is wired up. It costs nothing to be ready, and the SSE path
already covers the common setup.
"""

import logging

from flask_restx import Resource, Namespace, fields

from app.database import TableSportsEvents, database, select
from arr_instances.repository import ArrInstanceRepository
from arr_instances.resolution import scoped

from ..utils import authenticate


api_ns_webhooks_sportarr = Namespace(
    "Webhooks Sportarr",
    description="Webhooks to trigger subtitles search based on Sportarr webhooks",
)


def _resolve_owner(stable_key):
    """The owning Sportarr instance, or None with a reason to log.

    Unlike Sonarr and Radarr there is no unscoped legacy path to fall back on:
    a sports path mapping is always per instance, so an unowned sports action
    cannot resolve a file at all. With no key in the URL the default instance
    is used, which is the single-instance setup.
    """
    repository = ArrInstanceRepository(database)
    if stable_key is not None:
        instance = repository.get_by_key("sportarr", stable_key)
        if instance is None or not instance.enabled:
            return None, f"unknown or disabled instance key {stable_key}"
        return instance.id, None
    instance = repository.get_default("sportarr")
    if instance is None or not instance.enabled:
        return None, "no enabled default Sportarr instance"
    return instance.id, None


@api_ns_webhooks_sportarr.route("webhooks/sportarr")
@api_ns_webhooks_sportarr.route("webhooks/sportarr/<string:stable_key>")
class WebHooksSportarr(Resource):
    event_model = api_ns_webhooks_sportarr.model(
        "SportarrEvent",
        {"id": fields.Integer(required=True, description="Sportarr event ID")},
        strict=False,
    )

    event_file_model = api_ns_webhooks_sportarr.model(
        "SportarrEventFile",
        {"id": fields.Integer(required=True, description="Sportarr event file ID")},
        strict=False,
    )

    sportarr_webhook_model = api_ns_webhooks_sportarr.model(
        "SportarrWebhook",
        {
            "episodes": fields.List(
                fields.Nested(event_model),
                required=False,
                description="Sportarr events; used to sync an event Bazarr has not seen yet",
            ),
            "episodeFiles": fields.List(
                fields.Nested(event_file_model),
                required=False,
                description="Sportarr event files; required for anything other than test hooks",
            ),
            "eventType": fields.String(
                required=True,
                description="Type of Sportarr event (e.g. Test, Download)",
            ),
        },
        strict=False,
    )

    @authenticate
    @api_ns_webhooks_sportarr.expect(sportarr_webhook_model, validate=True)
    @api_ns_webhooks_sportarr.response(200, "Success")
    @api_ns_webhooks_sportarr.response(401, "Not Authenticated")
    def post(self, stable_key=None):
        """Index and search subtitles for the sports files Sportarr just imported."""
        from sportarr.automatic import search_event
        from subtitles.indexer.sports import store_subtitles_sports

        args = api_ns_webhooks_sportarr.payload
        event_type = args.get("eventType")
        logging.debug("Received Sportarr webhook event: %s", event_type)

        arr_instance_id, reason = _resolve_owner(stable_key)
        if arr_instance_id is None:
            # 200 throughout, like the Sonarr and Radarr hooks: an arr marks a
            # webhook unhealthy and demands user interaction on anything else,
            # and none of these outcomes is something the user can fix there.
            logging.warning("Sportarr webhook ignored: %s.", reason)
            return "Unknown or disabled instance.", 200

        if event_type == "Test":
            message = "Received test hook, skipping database search."
            logging.debug(message)
            return message, 200

        # Same tell as Sonarr: a download starting and a download finishing are
        # distinguished only by whether file entries are present.
        file_ids = [entry.get("id") for entry in args.get("episodeFiles", [])]
        if not file_ids:
            message = "No event file IDs found in the webhook request. Nothing to do."
            logging.debug(message)
            return message, 200

        upstream_event_ids = [entry.get("id") for entry in args.get("episodes", [])]

        def _local_events():
            # file_id is unique per owner, which is what the payload names.
            return database.execute(
                scoped(
                    select(TableSportsEvents.id)
                    .where(TableSportsEvents.file_id.in_(file_ids)),
                    TableSportsEvents.arr_instance_id, arr_instance_id,
                )
            ).scalars().all()

        local_ids = _local_events()
        if len(local_ids) != len(set(file_ids)) and upstream_event_ids:
            # An import Bazarr has not synced yet. Reuse the stream's own
            # reconciliation rather than a second resolution path, so a webhook
            # and a stream frame converge on the same rows.
            logging.debug("Syncing Sportarr owner %s before indexing the webhook's files.",
                          arr_instance_id)
            try:
                _reconcile(arr_instance_id, upstream_event_ids)
            except Exception:
                logging.exception("Could not sync Sportarr owner %s for its webhook.",
                                  arr_instance_id)
            local_ids = _local_events()

        if not local_ids:
            message = "No sports event found for the webhook's files. Nothing to do."
            logging.debug(message)
            return message, 200

        for event_id in local_ids:
            try:
                store_subtitles_sports(event_id, arr_instance_id)
                search_event(event_id, arr_instance_id)
            except Exception:
                # One unreadable file must not cost the rest of the batch.
                logging.exception("Sportarr webhook processing failed for event %s.", event_id)

        return "Finished processing subtitles.", 200


def _reconcile(arr_instance_id, upstream_event_ids):
    from sportarr.connection import connection_identity
    from sportarr.sse_client import PendingWork, reconcile_work
    from sportarr.sync.leagues import require_sportarr

    instance = require_sportarr(database, arr_instance_id)
    batch = PendingWork(event_ids={
        event_id for event_id in upstream_event_ids if isinstance(event_id, int) and event_id > 0
    })
    reconcile_work(arr_instance_id, batch, cancel=None,
                   expected_connection=connection_identity(instance))
