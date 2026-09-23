import { QueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

const JOBS_KEY = [QueryKeys.System, QueryKeys.Jobs];
const TERMINAL = new Set(["completed", "failed"]);

// How often the job is asked for directly, in case its terminal socket event
// never reaches the cache (a dropped socket, a failed refetch in the reducer).
export const JOB_WATCH_POLL_MS = 15_000;

type CachedJob = { job_id?: number; status?: string };

function cachedStatus(client: QueryClient, jobId: number) {
  const jobs = client.getQueryData<CachedJob[]>(JOBS_KEY);
  return jobs?.find((job) => job.job_id === jobId)?.status;
}

function isJobsQuery(queryKey: readonly unknown[]) {
  return (
    queryKey.length === JOBS_KEY.length &&
    queryKey.every((part, index) => part === JOBS_KEY[index])
  );
}

/**
 * Run `onFinished` once the queued job reaches a terminal state.
 *
 * The socket reducer writes every job's terminal event into the
 * [System, Jobs] cache, so this watches that cache rather than the socket. A
 * job that already finished before the watch started is caught by the first
 * check. Returns a function that stops watching.
 */
export function whenJobFinishes(
  client: QueryClient,
  jobId: number,
  onFinished: (status: string) => void,
): () => void {
  const current = cachedStatus(client, jobId);
  if (current && TERMINAL.has(current)) {
    onFinished(current);
    return () => undefined;
  }

  let done = false;
  const finish = (status: string) => {
    if (done) return;
    done = true;
    unsubscribe();
    clearInterval(timer);
    onFinished(status);
  };

  const unsubscribe = client.getQueryCache().subscribe((event) => {
    if (done || event.type !== "updated" || !isJobsQuery(event.query.queryKey))
      return;
    const status = cachedStatus(client, jobId);
    if (status && TERMINAL.has(status)) finish(status);
  });

  const timer = setInterval(() => {
    api.system
      .jobs(jobId)
      .then((jobs) => {
        const job = Array.isArray(jobs) ? jobs[0] : undefined;
        if (!job) {
          // Gone from the queue (trimmed, or the server restarted): whatever
          // it did is done, so refresh rather than wait forever.
          finish("completed");
        } else if (TERMINAL.has(job.status)) {
          finish(job.status);
        }
      })
      .catch(() => undefined);
  }, JOB_WATCH_POLL_MS);

  return () => {
    done = true;
    unsubscribe();
    clearInterval(timer);
  };
}

/**
 * Refresh the data a queued job changes when that job finishes, or at once
 * when no job was queued (the backend answers without an id when an identical
 * job is already pending, and older routes answer without one at all).
 */
export function refreshWhenJobFinishes(
  client: QueryClient,
  jobId: number | null | undefined,
  refresh: () => void,
) {
  if (typeof jobId === "number") {
    whenJobFinishes(client, jobId, refresh);
  } else {
    refresh();
  }
}
