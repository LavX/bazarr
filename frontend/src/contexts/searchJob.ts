import api from "@/apis/raw";
import type {
  DiscoverSearchProgress,
  DiscoverSearchSnapshot,
} from "@/types/discover";
import { isSameRun, JOB_RESULT_GONE } from "@/utilities/jobs";

/** How often the running search is asked for the providers and rows it has so far. */
export const SEARCH_POLL_MS = 600;

/**
 * A search job that ended without a snapshot. `reason` is the sentence to show,
 * or undefined when the job only failed unexpectedly: the generic failure
 * message says that better than the queue's own.
 */
export class SearchJobFailed extends Error {
  readonly reason: string | undefined;

  constructor(reason: string | undefined) {
    super(reason ?? "The search failed.");
    this.name = "SearchJobFailed";
    this.reason = reason;
  }
}

type SearchJobResult = DiscoverSearchProgress & {
  snapshot?: DiscoverSearchSnapshot;
};

/**
 * Follow a queued search to its snapshot. The job publishes what it knows as
 * it goes, the providers heard from and the rows already built, and hands the
 * whole snapshot over at the end; every look is passed to `onProgress`. Losing
 * a look does not cancel the search or discard results, and asking stops once
 * the reader has moved on. `session` is the backend run the search was queued
 * under: a job of another run that shares the id is not this search. A search
 * that completed without a snapshot has nothing more to give, so it is treated
 * as gone rather than asked about again.
 */
export async function followSearch(
  jobId: number | null,
  signal: AbortSignal,
  retired: () => boolean,
  onProgress: (progress: DiscoverSearchProgress) => void,
  session?: string,
): Promise<DiscoverSearchSnapshot> {
  if (jobId === null) throw new SearchJobFailed(undefined);
  await new Promise((resolve) => setTimeout(resolve, 200));
  for (;;) {
    if (signal.aborted || retired()) {
      throw new SearchJobFailed(undefined);
    }
    let outcome: System.JobResult<SearchJobResult> | undefined;
    try {
      outcome = await api.system.jobResult<SearchJobResult>(jobId, signal);
    } catch (error) {
      // The job left the history, so it cannot be followed to an end.
      if (
        (error as { response?: { status?: number } }).response?.status === 404
      )
        throw new SearchJobFailed(undefined);
      // Anything else is a look lost, and the next one tries again.
    }
    if (
      outcome !== undefined &&
      session !== undefined &&
      outcome.session !== session
    ) {
      throw new SearchJobFailed(JOB_RESULT_GONE);
    }
    if (outcome?.status === "completed") {
      if (outcome.result?.snapshot) return outcome.result.snapshot;
      throw new SearchJobFailed(JOB_RESULT_GONE);
    }
    if (outcome?.status === "failed") {
      throw new SearchJobFailed(
        outcome.error?.reason === "unexpected_error"
          ? undefined
          : outcome.error?.message,
      );
    }
    if (outcome?.status === "cancelled") {
      throw new SearchJobFailed("The search was stopped.");
    }
    // The finished look carries the whole snapshot, which the next look hands
    // over with the job's end; it is not an observation to show.
    if (outcome?.result && !outcome.result.snapshot) onProgress(outcome.result);
    await new Promise((resolve) => setTimeout(resolve, SEARCH_POLL_MS));
  }
}

/**
 * Stop a search nobody is reading any more, so it does not hold a slot of the
 * user-search lane for the searches that come after it. A search still waiting
 * for a slot is taken off the queue, and one that is running is asked to stop
 * (it ends at its next check). A job of another backend run that reuses the
 * id, or one that already ended, is left alone, and a failure to ask is
 * ignored: the search simply runs to its end, as it did before.
 */
export async function stopSearchJob(
  jobId: number,
  session?: string,
): Promise<void> {
  try {
    const [job] = await api.system.jobs(jobId);
    if (!job || !isSameRun(session, job.session)) return;
    if (job.status === "pending") await api.system.deleteJobs(jobId);
    else if (job.status === "running")
      await api.system.actionOnJobs(jobId, "cancel");
  } catch {
    // The search runs on, which is what it did before it was stopped.
  }
}
