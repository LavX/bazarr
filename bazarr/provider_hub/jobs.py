# coding=utf-8
"""Provider Hub lifecycle actions as queued jobs.

Install, update, uninstall and catalog refresh used to run inside the HTTP
request: a bundle download, a hash check, a venv build and a worker smoke test,
all while the button spun, and none of it visible in the jobs list. Each action
is now a job on the application's queue. The Hub keeps writing its own activity
log exactly as before, because the service functions below still record it;
the queue is what reports progress and failure. A failure raises with the
provider's name and the reason, which is what marks the job failed.
"""

from app.job_errors import fail_job, reason_of
from app.jobs_queue import jobs_queue

from . import service

JOB_MODULE = "provider_hub.jobs"


def _manifest_name(manifest):
    if isinstance(manifest, dict):
        name = manifest.get("name") or manifest.get("provider_id")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return "provider"


def _existing_job_id(func, kwargs):
    """The pending or running job the queue matched a duplicate against."""
    for job in list(jobs_queue.jobs_pending_queue) + list(jobs_queue.jobs_running_queue):
        if job.module != JOB_MODULE or job.func != func:
            continue
        job_kwargs = dict(job.kwargs)
        job_kwargs.pop("job_id", None)
        if job_kwargs == kwargs:
            return job.job_id
    return None


def _enqueue(label, func, kwargs):
    job_id = jobs_queue.feed_jobs_pending_queue(
        job_name=label, module=JOB_MODULE, func=func, kwargs=kwargs, is_progress=False)
    # The queue answers False for an identical job that is already pending or
    # running. The caller still wants something to follow, and that job is it.
    return job_id or _existing_job_id(func, kwargs)


def queue_install(manifest):
    return _enqueue(f"Installing provider {_manifest_name(manifest)}", "install_provider",
                    {"manifest": manifest})


def queue_install_local(archive_bytes, filename=None):
    label = f"Installing provider package {filename}" if filename else "Installing provider package"
    return _enqueue(label, "install_local_provider",
                    {"archive_bytes": bytes(archive_bytes), "filename": filename})


def queue_uninstall(provider_id, name=None):
    return _enqueue(f"Uninstalling provider {name or provider_id}", "uninstall_provider",
                    {"provider_id": provider_id, "name": name})


def queue_update(provider_id, name=None):
    return _enqueue(f"Updating provider {name or provider_id}", "update_provider",
                    {"provider_id": provider_id, "name": name})


def queue_catalog_refresh():
    return _enqueue("Refreshing provider catalog", "refresh_catalog", {})


def install_provider(manifest, job_id=None):
    name = _manifest_name(manifest)
    try:
        return service.stage_install(manifest)
    except Exception as error:
        fail_job(job_id, f"Could not install {name}: {reason_of(error)}", error)


def install_local_provider(archive_bytes, filename=None, job_id=None):
    label = filename or "the uploaded package"
    try:
        installation = service.stage_install_local(archive_bytes)
    except Exception as error:
        fail_job(job_id, f"Could not install {label}: {reason_of(error)}", error)
    name = None
    if isinstance(installation, dict):
        name = installation.get("name") or installation.get("provider_id")
    if name:
        jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Installing provider {name}")
    return installation


def uninstall_provider(provider_id, name=None, job_id=None):
    label = name or provider_id
    try:
        removed = service.remove_installation(provider_id)
    except Exception as error:
        fail_job(job_id, f"Could not uninstall {label}: {reason_of(error)}", error)
    if not removed:
        fail_job(job_id, f"Could not uninstall {label}: it is not installed")


def update_provider(provider_id, name=None, job_id=None):
    label = name or provider_id
    provider = service.get_provider(provider_id, redact=False)
    if not provider:
        fail_job(job_id, f"Could not update {label}: it is not installed")
    if provider.get("origin") == "local":
        # A local package is never replaced from a catalog; the request has
        # always been accepted and ignored.
        return service.get_provider(provider_id)
    try:
        result = service.apply_update(provider_id)
    except Exception as error:
        fail_job(job_id, f"Could not update {label}: {reason_of(error)}", error)
    # apply_update reports its failures on the installation rather than by
    # raising: a failed stage or a missing manifest leaves last_error set and
    # nothing staged, while a staged update clears last_error.
    if not result:
        fail_job(job_id, f"Could not update {label}: it is not installed")
    if result.get("last_error"):
        fail_job(job_id, f"Could not update {label}: {result['last_error']}")
    return result


def refresh_catalog(job_id=None):
    try:
        result = service.refresh_catalog()
    except Exception as error:
        fail_job(job_id, f"Could not refresh the provider catalog: {reason_of(error)}", error)
    # A source that could not be fetched is recorded on the source and the
    # refresh carries on with the others, so its reason is read back here.
    sources = (service.load_state().get("catalog_sources") or {}).values()
    failed = [
        f"{source.get('name') or source.get('id') or 'source'} ({source['last_error']})"
        for source in sources
        if isinstance(source, dict) and source.get("last_error")
    ]
    if failed:
        fail_job(job_id, f"Could not refresh the provider catalog from {', '.join(failed)}")
    return result
