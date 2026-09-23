import { QueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

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
const TERMINAL = new Set(["completed", "failed"]);

type JobRecord = System.Jobs & { error?: string | null };

export class JobFailedError extends Error {
  readonly jobId: number;

  constructor(job: JobRecord) {
    super(failureMessage(job));
    this.name = "JobFailedError";
    this.jobId = job.job_id;
  }
}

// The reason the job raised with. Newer backends carry it as `error`; until
// then the job function writes the same sentence into its progress message.
function failureMessage(job: JobRecord): string {
  if (typeof job.error === "string" && job.error.length > 0) {
    return job.error;
  }
  if (job.progress_message) {
    return job.progress_message;
  }
  return `${job.job_name || "The job"} failed`;
}

interface Waiter {
  client: QueryClient;
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
      waiter.reject(new JobFailedError(job));
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

function checkCache(client: QueryClient) {
  const jobs = client.getQueryData<JobRecord[]>(JOBS_KEY) ?? [];
  for (const [jobId, entries] of Array.from(waiters.entries())) {
    if (!entries.some((waiter) => waiter.client === client)) {
      continue;
    }
    const job = jobs.find((entry) => entry.job_id === jobId);
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
        const job = list.find((entry) => entry.job_id === jobId);
        if (job !== undefined) {
          if (TERMINAL.has(job.status)) {
            settle(jobId, job);
          }
          continue;
        }
        // Absent from a list read after the wait began: the backend keeps
        // pending and running jobs in full and only the last few finished
        // ones, so this job finished and has aged out. Its outcome is gone
        // with it; the caller refetches whatever the job changed.
        if (pending.every((waiter) => waiter.registeredAt <= startedAt)) {
          settle(jobId, undefined);
        }
      }
    })
    .catch(() => {
      // Backend unreachable for a moment; the next tick tries again.
    });
}

/**
 * Resolves with the job once it completed, rejects with a JobFailedError
 * carrying the job's reason once it failed. A null id, which the backend
 * answers when it could not queue anything to follow, resolves at once.
 */
export function waitForJob(
  client: QueryClient,
  jobId: number | null | undefined,
): Promise<JobRecord | undefined> {
  if (jobId === null || jobId === undefined) {
    return Promise.resolve(undefined);
  }
  return new Promise((resolve, reject) => {
    const list = waiters.get(jobId) ?? [];
    list.push({ client, registeredAt: Date.now(), resolve, reject });
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
