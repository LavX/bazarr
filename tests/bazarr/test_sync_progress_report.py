# coding=utf-8
"""The auto-sync sub-step must keep a cancellation checkpoint without hijacking
the parent download job's progress bar.

``_report_progress`` is the single seam sync uses to talk to the jobs queue:
- as the job owner (standalone sync), it updates value/max/name as usual;
- as a sub-step (called from a download/wanted job), it sends a message-only
  update. That still passes through ``update_job_progress`` -- which raises
  ``JobCancelled`` when the user pressed Stop -- so cancellation is preserved,
  while the parent job's value/max/name are left untouched (no >100% ring).
"""
from unittest.mock import patch


def test_substep_sends_message_only():
    from subtitles import sync
    with patch.object(sync, "jobs_queue") as jq:
        sync._report_progress(5, True, False, "Synchronizing", value=2, total=3, name="Syncing X")
        jq.update_job_progress.assert_called_once_with(job_id=5, progress_message="Synchronizing")
        jq.update_job_name.assert_not_called()


def test_substep_max_sentinel_does_not_complete_parent():
    # Even a 'max' completion from the sync engine must not flow to the parent.
    from subtitles import sync
    with patch.object(sync, "jobs_queue") as jq:
        sync._report_progress(5, True, False, "Sync complete", value="max", name="done")
        jq.update_job_progress.assert_called_once_with(job_id=5, progress_message="Sync complete")
        jq.update_job_name.assert_not_called()


def test_owner_sends_value_max_and_name():
    from subtitles import sync
    with patch.object(sync, "jobs_queue") as jq:
        sync._report_progress(5, True, True, "Working", value=2, total=3, name="Syncing X")
        jq.update_job_progress.assert_called_once_with(
            job_id=5, progress_value=2, progress_max=3, progress_message="Working")
        jq.update_job_name.assert_called_once_with(job_id=5, new_job_name="Syncing X")


def test_owner_without_name_skips_rename():
    from subtitles import sync
    with patch.object(sync, "jobs_queue") as jq:
        sync._report_progress(5, True, True, "Working", value=0, total=3)
        jq.update_job_progress.assert_called_once_with(
            job_id=5, progress_value=0, progress_max=3, progress_message="Working")
        jq.update_job_name.assert_not_called()


def test_noop_without_job_or_tracking():
    from subtitles import sync
    with patch.object(sync, "jobs_queue") as jq:
        sync._report_progress(None, True, True, "x", value=1, total=2)
        sync._report_progress(5, False, True, "x", value=1, total=2)
        jq.update_job_progress.assert_not_called()
        jq.update_job_name.assert_not_called()
