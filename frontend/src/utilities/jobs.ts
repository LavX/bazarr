import { QueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { claimJobCompletion } from "@/modules/jobs";

// Waiting on a backend job the standard way: the jobs socket keeps the
// [System, Jobs] cache current, and a job has settled once its entry there is
// completed or failed. The cache is read rather than a feature's own status
// endpoint, so everything that queues a job finishes on the same signal the
// jobs drawer shows.
//
// The socket can be down (a proxy that drops websockets, a reconnect in
// progress), so while anything is waiting one shared request re-reads the job
// list every few seconds as a fallback. It is one request for every waiter,
// not one per job, because the wizard waits on dozens at once.

export const JOB_FALLBACK_POLL_MS = 5000;

const JOBS_KEY = [QueryKeys.System, QueryKeys.Jobs];
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

type JobRecord = System.Jobs;

export class JobFailedError extends Error {
  readonly jobId: number;

  constructor(job: JobRecord) {
    super(failureMessage(job));
    this.name = "JobFailedError";
    this.jobId = job.job_id;
  }
}

/**
 * The job is no longer reported, so whether it worked is not known. It is a
 * failure to follow the job, not a failure of the job, and says so: a caller
 * that wants to tell the two apart checks for this class first.
 */
export class JobOutcomeUnknownError extends JobFailedError {
  constructor(jobId: number) {
    super({
      // eslint-disable-next-line camelcase -- the transport field name
      job_id: jobId,
      // eslint-disable-next-line camelcase
      job_name: "",
      status: "failed",
      error: { reason: "unknown", message: UNKNOWN_JOB_OUTCOME },
    } as JobRecord);
    this.name = "JobOutcomeUnknownError";
  }
}

// A job that ended at Stop is recorded as cancelled (and flagged stopped).
// It did not finish, so it is not reported as done.
export class JobStoppedError extends Error {
  readonly jobId: number;

  constructor(job: JobRecord) {
    super(`${job.job_name || "The job"} was stopped`);
    this.name = "JobStoppedError";
    this.jobId = job.job_id;
  }
}

// The reason the job failed with, as the queue records it for the drawer
// and the failure toast.
function failureMessage(job: JobRecord): string {
  if (job.error?.message) {
    return job.error.message;
  }
  return `${job.job_name || "The job"} failed`;
}

interface Waiter {
  client: QueryClient;
  /** The backend run the job belongs to, when the caller knows it. */
  session?: string;
  registeredAt: number;
  resolve: (job: JobRecord | undefined) => void;
  reject: (error: Error) => void;
}

const waiters = new Map<number, Waiter[]>();
const subscriptions = new Map<QueryClient, () => void>();
let timer: ReturnType<typeof setInterval> | null = null;

function settle(jobId: number, job: JobRecord | undefined) {
  const list = waiters.get(jobId);
  if (list === undefined) {
    return;
  }
  waiters.delete(jobId);
  for (const waiter of list) {
    if (job?.status === "failed") {
      waiter.reject(
        job.error?.reason === "unknown"
          ? new JobOutcomeUnknownError(job.job_id)
          : new JobFailedError(job),
      );
    } else if (job?.status === "cancelled" || job?.stopped) {
      // The status is the stop; the flag says the same on a row recorded
      // while a stopped job still read as completed.
      waiter.reject(new JobStoppedError(job));
    } else {
      waiter.resolve(job);
    }
  }
  const clientsInUse = new Set(
    Array.from(waiters.values()).flatMap((entries) =>
      entries.map((waiter) => waiter.client),
    ),
  );
  for (const [client, unsubscribe] of Array.from(subscriptions.entries())) {
    if (!clientsInUse.has(client)) {
      unsubscribe();
      subscriptions.delete(client);
    }
  }
  if (waiters.size === 0) {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }
}

export const UNKNOWN_JOB_OUTCOME =
  "Bazarr+ no longer reports this job, so whether it worked is not known.";

function settleUnknown(jobId: number) {
  const list = waiters.get(jobId);
  if (list === undefined) {
    return;
  }
  // Reuses settle's cleanup with a synthetic failed record.
  settle(jobId, {
    // eslint-disable-next-line camelcase
    job_id: jobId,
    // eslint-disable-next-line camelcase
    job_name: "",
    status: "failed",
    error: { reason: "unknown", message: UNKNOWN_JOB_OUTCOME },
  } as JobRecord);
}

/**
 * A job id only names a job within one backend run. A row from another run
 * that shares the id is not the job that was queued, which matters when the
 * backend restarted and its ids began again. Either side not knowing its run
 * cannot tell them apart, and counts as the same.
 */
export function isSameRun(followed?: string, row?: string) {
  return followed === undefined || row === undefined || followed === row;
}

function sameRun(waiter: Waiter, job: JobRecord) {
  return isSameRun(waiter.session, job.session);
}

function checkCache(client: QueryClient) {
  const jobs = client.getQueryData<JobRecord[]>(JOBS_KEY) ?? [];
  for (const [jobId, entries] of Array.from(waiters.entries())) {
    if (!entries.some((waiter) => waiter.client === client)) {
      continue;
    }
    const job = jobs.find(
      (entry) =>
        entry.job_id === jobId &&
        entries.some(
          (waiter) => waiter.client === client && sameRun(waiter, entry),
        ),
    );
    if (job !== undefined && TERMINAL.has(job.status)) {
      settle(jobId, job);
    }
  }
}

function pollOnce() {
  const startedAt = Date.now();
  void api.system
    .jobs()
    .then((jobs) => {
      const list = (jobs ?? []) as JobRecord[];
      for (const [jobId, pending] of Array.from(waiters.entries())) {
        const job = list.find(
          (entry) =>
            entry.job_id === jobId &&
            pending.some((waiter) => sameRun(waiter, entry)),
        );
        if (job !== undefined) {
          if (TERMINAL.has(job.status)) {
            settle(jobId, job);
          }
          continue;
        }
        // Absent from a list read after the wait began: the job was removed,
        // the backend restarted, or it finished and aged out of the short
        // history. None of those says it worked, so it is not reported as
        // success.
        if (pending.every((waiter) => waiter.registeredAt <= startedAt)) {
          settleUnknown(jobId);
        }
      }
    })
    .catch(() => {
      // Backend unreachable for a moment; the next tick tries again.
    });
}

/**
 * Resolves with the job once it completed, rejects with a JobFailedError
 * carrying the job's reason once it failed, and rejects with its subclass
 * JobOutcomeUnknownError once the backend stops reporting it at all, which says
 * nothing about whether the job worked. It rejects with a JobStoppedError once
 * the job ended at Stop. A caller that knows the backend run the job belongs to
 * passes its `session`, so a row from another run that shares the id is never
 * taken for it. A null id, which the backend answers when it could not queue anything
 * to follow, resolves at once.
 */
export function waitForJob(
  client: QueryClient,
  jobId: number | null | undefined,
  session?: string,
): Promise<JobRecord | undefined> {
  if (jobId === null || jobId === undefined) {
    return Promise.resolve(undefined);
  }
  return new Promise((resolve, reject) => {
    const list = waiters.get(jobId) ?? [];
    list.push({ client, session, registeredAt: Date.now(), resolve, reject });
    waiters.set(jobId, list);
    if (!subscriptions.has(client)) {
      subscriptions.set(
        client,
        client.getQueryCache().subscribe(() => checkCache(client)),
      );
    }
    if (timer === null) {
      timer = setInterval(pollOnce, JOB_FALLBACK_POLL_MS);
    }
    // The job may have finished before its id reached us.
    checkCache(client);
  });
}

/**
 * The message a job result lookup gives when the job published nothing, or has
 * left the history before its result was read.
 */
export const JOB_RESULT_GONE =
  "The result is no longer available. Run it again.";

/**
 * Follow a queued job to its end and hand back what it published. The wait is
 * the standard one, so a failure rejects with the job's own reason and a stop
 * with its own error. A job that ended without a result, or that has left the
 * jobs history since, says so rather than answering with nothing.
 */
export async function awaitJobResult<T>(
  client: QueryClient,
  jobId: number | null | undefined,
  session?: string,
): Promise<T> {
  if (jobId === null || jobId === undefined) {
    throw new Error("The job could not be queued.");
  }
  await waitForJob(client, jobId, session);
  let outcome: System.JobResult<T>;
  try {
    outcome = await api.system.jobResult<T>(jobId);
  } catch (error) {
    if (
      (error as { response?: { status?: number } }).response?.status === 404
    ) {
      throw new Error(JOB_RESULT_GONE, { cause: error });
    }
    throw error;
  }
  // A job of another backend run that shares the id is not the one followed.
  if (
    (session !== undefined && outcome.session !== session) ||
    outcome.result === null
  ) {
    throw new Error(JOB_RESULT_GONE);
  }
  return outcome.result;
}

/** What a route that queues a search answers with. */
export interface QueuedSearch {
  job_id: number | null;
  /** The backend run that owns the job, which ids restart with. */
  session?: string;
}

/**
 * Run a search as a standard job and hand back its rows. The modal that asked
 * shows the outcome itself, so the job's finish is claimed and the standard
 * notification stays quiet; the Jobs drawer still lists it with its progress.
 * Asking again while the search runs follows the same job, so a modal opened
 * again mid-search can pick it up with one more press of Search.
 */
export async function searchAsJob<T>(
  client: QueryClient,
  enqueue: () => Promise<QueuedSearch>,
): Promise<T> {
  const queued = await enqueue();
  if (queued.job_id !== null) {
    claimJobCompletion(queued.job_id, queued.session);
  }
  return awaitJobResult<T>(client, queued.job_id, queued.session);
}
