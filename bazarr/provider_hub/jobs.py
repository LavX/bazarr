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

import threading

from app.job_errors import reason_of
from app.jobs_queue import JobCancelled, JobFailed, jobs_queue

from . import service

JOB_MODULE = "provider_hub.jobs"

# The spooled package of every local install queued since startup. A job
# removed before it runs never removes its package, so each new local install
# discards the ones whose job has left the queue.
_queued_packages = set()
_queued_packages_lock = threading.Lock()


def _checkpoint(job_id):
    """Report a step on the job, and stop there when Stop was pressed.

    The queue only honours Stop when a job reports progress, and these jobs never
    did, so a stopped install carried on through the download, the dependency
    build and the smoke test. The service calls this between its steps, where
    stopping leaves nothing half installed.
    """
    def checkpoint(message):
        try:
            jobs_queue.update_job_progress(job_id=job_id, progress_message=message)
        except JobCancelled as error:
            raise service.ProviderHubStopped(str(error)) from error
    return checkpoint


def _enqueue(label, func, kwargs):
    # An identical job that is already pending or running is what the caller
    # follows instead. The queue names it in the same step that matches it, so
    # it is not lost when that job finishes a moment later.
    return jobs_queue.feed_jobs_pending_queue(
        job_name=label, module=JOB_MODULE, func=func, kwargs=kwargs, is_progress=False,
        return_existing=True)


def queue_install(source_id, provider_id, version, name=None):
    """Queue the install of the entry ``source_id`` lists for ``provider_id``.

    The job carries the reference, not a manifest: it installs what the source
    holds when it runs, bound to that source.
    """
    return _enqueue(f"Installing provider {name or provider_id}", "install_provider",
                    {"source_id": source_id, "provider_id": provider_id, "version": version,
                     "name": name})


def _discard_abandoned_packages():
    """Remove the packages of local installs that left the queue without running.

    Only packages a job was queued for are considered: any other file in the
    spool belongs to an upload that is still arriving.
    """
    with _queued_packages_lock:
        with jobs_queue._queue_lock:
            waiting = {job.kwargs.get("package_path")
                       for job in (*jobs_queue.jobs_pending_queue, *jobs_queue.jobs_running_queue)
                       if job.module == JOB_MODULE and job.func == "install_local_provider"}
        for package_path in _queued_packages - waiting:
            service.discard_local_package(package_path)
        _queued_packages.intersection_update(waiting)


def queue_install_local(package_path, filename=None):
    """Queue the install of a package the upload route spooled to disk.

    The job takes the file over and removes it when it ends. A job removed
    while still pending never runs, so the next local install removes its file
    instead, and startup removes whatever a restart left. The job carries only
    the path: the queue keeps a job's arguments until it runs, and the
    finished-jobs history keeps them after.
    """
    _discard_abandoned_packages()
    label = f"Installing provider package {filename}" if filename else "Installing provider package"
    job_id = _enqueue(label, "install_local_provider",
                      {"package_path": str(package_path), "filename": filename})
    with _queued_packages_lock:
        _queued_packages.add(str(package_path))
    return job_id


def queue_uninstall(provider_id, name=None):
    return _enqueue(f"Uninstalling provider {name or provider_id}", "uninstall_provider",
                    {"provider_id": provider_id, "name": name})


def queue_update(provider_id, name=None):
    return _enqueue(f"Updating provider {name or provider_id}", "update_provider",
                    {"provider_id": provider_id, "name": name})


def queue_catalog_refresh():
    return _enqueue("Refreshing provider catalog", "refresh_catalog", {})


def install_provider(source_id, provider_id, version, name=None, job_id=None):
    label = name or provider_id
    try:
        return service.install_catalog_entry(source_id, provider_id, version,
                                             checkpoint=_checkpoint(job_id))
    except service.ProviderHubStopped as stopped:
        raise JobCancelled(str(stopped)) from stopped
    except Exception as error:
        raise JobFailed(f"Could not install {label}: {reason_of(error)}") from error


def install_local_provider(package_path, filename=None, job_id=None):
    label = filename or "the uploaded package"
    try:
        installation = service.stage_install_local(package_path, checkpoint=_checkpoint(job_id))
    except service.ProviderHubStopped as stopped:
        raise JobCancelled(str(stopped)) from stopped
    except Exception as error:
        raise JobFailed(f"Could not install {label}: {reason_of(error)}") from error
    finally:
        service.discard_local_package(package_path)
    name = None
    if isinstance(installation, dict):
        name = installation.get("name") or installation.get("provider_id")
    if name:
        jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Installing provider {name}")
    return installation


def uninstall_provider(provider_id, name=None, job_id=None):
    label = name or provider_id
    # The removal is a single state write, so the only place to stop is before it.
    jobs_queue.update_job_progress(job_id=job_id, progress_message=f"Removing {label}")
    try:
        removed = service.remove_installation(provider_id)
    except Exception as error:
        raise JobFailed(f"Could not uninstall {label}: {reason_of(error)}") from error
    if not removed:
        raise JobFailed(f"Could not uninstall {label}: it is not installed")


def update_provider(provider_id, name=None, job_id=None):
    label = name or provider_id
    provider = service.get_provider(provider_id, redact=False)
    if not provider:
        raise JobFailed(f"Could not update {label}: it is not installed")
    if provider.get("origin") == "local":
        # A local package is never replaced from a catalog; the request has
        # always been accepted and ignored.
        return service.get_provider(provider_id)
    try:
        result = service.apply_update(provider_id, checkpoint=_checkpoint(job_id))
    except service.ProviderHubStopped as stopped:
        raise JobCancelled(str(stopped)) from stopped
    except Exception as error:
        raise JobFailed(f"Could not update {label}: {reason_of(error)}") from error
    # apply_update reports its failures on the installation rather than by
    # raising: a failed stage or a missing manifest leaves last_error set and
    # nothing staged, while a staged update clears last_error.
    if not result:
        raise JobFailed(f"Could not update {label}: it is not installed")
    if result.get("last_error"):
        raise JobFailed(f"Could not update {label}: {result['last_error']}")
    return result


def refresh_catalog(job_id=None):
    try:
        result = service.refresh_catalog(checkpoint=_checkpoint(job_id))
    except service.ProviderHubStopped as stopped:
        raise JobCancelled(str(stopped)) from stopped
    except Exception as error:
        raise JobFailed(f"Could not refresh the provider catalog: {reason_of(error)}") from error
    # A source that could not be fetched is recorded on the source and the
    # refresh carries on with the others, so its reason is read back here.
    sources = (service.load_state().get("catalog_sources") or {}).values()
    failed = [
        f"{source.get('name') or source.get('id') or 'source'} ({source['last_error']})"
        for source in sources
        if isinstance(source, dict) and source.get("last_error")
    ]
    if failed:
        raise JobFailed(f"Could not refresh the provider catalog from {', '.join(failed)}")
    return result
