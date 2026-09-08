"""Jobs derived only from enabled Sportarr instances."""

import logging
from datetime import datetime

from app.config import settings
from sportarr.workflows import (
    wanted_search_missing_subtitles_sports,
    upgrade_sports_subtitles,
    cancel_disabled_jobs,
)
from arr_instances.repository import ArrInstanceRepository
from sportarr.settings import get_sports_settings
from sportarr.sync.leagues import update_sports_for_instance
from subtitles.indexer.sports import sports_full_scan_subtitles


def configure_sports_jobs(aps_scheduler, session):
    instances = ArrInstanceRepository(session).list("sportarr", enabled_only=True)
    cancel_disabled_jobs({instance.id for instance in instances})
    prefixes = (
        "update_sports_",
        "sports_full_scan_subtitles_",
        "wanted_search_missing_subtitles_sports_",
        "upgrade_sports_subtitles_",
    )
    wanted = {f"{prefix}{instance.id}" for instance in instances for prefix in prefixes}
    for job in aps_scheduler.get_jobs():
        if job.id.startswith(prefixes) and job.id not in wanted:
            aps_scheduler.remove_job(job.id)
    for instance in instances:
        aps_scheduler.add_job(
            update_sports_for_instance,
            "interval",
            minutes=get_sports_settings(instance)["sync_interval"],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=15,
            id=f"update_sports_{instance.id}",
            name=f"Sync with Sportarr ({instance.name})",
            replace_existing=True,
            kwargs={"arr_instance_id": instance.id},
        )
        scan = get_sports_settings(instance)
        trigger = {"hour": scan["full_scan_hour"]}
        if scan["full_scan"] == "Weekly":
            trigger["day_of_week"] = scan["full_scan_day"]
        elif scan["full_scan"] == "Manually":
            trigger = {"year": datetime.now().year + 100}
        aps_scheduler.add_job(
            sports_full_scan_subtitles,
            "cron",
            **trigger,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=15,
            id=f"sports_full_scan_subtitles_{instance.id}",
            name=f"Index Sports Subtitles ({instance.name})",
            replace_existing=True,
            kwargs={"arr_instance_id": instance.id, "wait_for_completion": True},
        )
        for function, hours, name in (
            (
                wanted_search_missing_subtitles_sports,
                scan["wanted_search_frequency"],
                "Search missing sports subtitles",
            ),
            (
                upgrade_sports_subtitles,
                int(settings.general.upgrade_frequency),
                "Upgrade sports subtitles",
            ),
        ):
            job_id = f"{function.__name__}_{instance.id}"
            if (
                function is upgrade_sports_subtitles
                and not settings.general.upgrade_subs
            ):
                if aps_scheduler.get_job(job_id):
                    aps_scheduler.remove_job(job_id)
                continue
            aps_scheduler.add_job(
                function,
                "interval",
                hours=hours,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=15,
                id=job_id,
                name=f"{name} ({instance.name})",
                replace_existing=True,
                kwargs={"arr_instance_id": instance.id, "wait_for_completion": True},
            )
    from app.get_args import args
    from sportarr.sse_client import refresh_sportarr_clients

    if instances and not args.no_signalr:
        aps_scheduler.add_job(
            refresh_sportarr_clients,
            "interval",
            seconds=30,
            id="sportarr_clients",
            name="Maintain Sportarr event streams",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    elif aps_scheduler.get_job("sportarr_clients") is not None:
        aps_scheduler.remove_job("sportarr_clients")


def refresh_sports_runtime():
    # Stream cancellation must still run if scheduler refresh fails.
    try:
        from sportarr.sse_client import refresh_sportarr_clients
        refresh_sportarr_clients()
    except Exception:
        logging.exception('Could not refresh Sportarr event streams after instance change')
    try:
        from app.database import database
        from app.event_handler import event_stream
        from app.scheduler import scheduler
        configure_sports_jobs(scheduler.aps_scheduler, database)
        event_stream(type='task')
    except Exception:
        logging.exception('Could not refresh Sportarr jobs after instance change')
