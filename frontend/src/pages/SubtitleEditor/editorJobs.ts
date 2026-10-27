import { useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSystemJobs } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import client from "@/apis/raw/client";
import { currentJobSession } from "@/modules/jobs";

/**
 * The editor's background work (translation, waveform peaks) runs as ordinary
 * Bazarr jobs. Their progress and terminal state arrive on the jobs socket and
 * land in the shared [System, Jobs] cache, which is what this reads.
 *
 * A terminal event sent while the socket was down is never replayed, so the
 * tracked job is also re-read on a slow interval. That is a safety net for a
 * missed event, not the progress source.
 */
const MISSED_EVENT_RECHECK_MS = 30_000;

// A job id only names a job within one backend run. A row, cached or read
// again, that belongs to another run shares the id by chance.
function belongsTo(job: System.Jobs, session: string | undefined) {
  return (
    session === undefined ||
    job.session === undefined ||
    job.session === session
  );
}

/**
 * Follow one editor job. `session` is the backend run it was queued under: a
 * job of an earlier run that shares its id is never taken for it, which a
 * restart mid-job would otherwise allow.
 */
export function useEditorJob(
  jobId: number | null | undefined,
  session?: string,
): System.Jobs | null | undefined {
  const { data } = useSystemJobs();
  const cached = useMemo(
    () =>
      jobId == null
        ? undefined
        : data?.find((job) => job.job_id === jobId && belongsTo(job, session)),
    [data, jobId, session],
  );
  const cachedTerminal = isTerminalJob(cached);
  // What was last seen of the job, so a job that was there and then is not can
  // be told from one that never arrived.
  const seen = useRef<{ jobId: number; terminal: boolean } | null>(null);
  const vanished = jobId != null && !cached && seen.current?.jobId === jobId;
  useEffect(() => {
    if (jobId != null && cached) {
      seen.current = { jobId, terminal: cachedTerminal };
    } else if (jobId == null) {
      seen.current = null;
    }
  }, [jobId, cached, cachedTerminal]);
  const { data: rechecked, refetch } = useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Jobs, "editor", jobId, session],
    queryFn: async () =>
      (await api.system.jobs(jobId ?? undefined)).find((job) =>
        belongsTo(job, session),
      ) ?? null,
    enabled: jobId != null && !cachedTerminal,
    refetchInterval: MISSED_EVENT_RECHECK_MS,
    staleTime: MISSED_EVENT_RECHECK_MS,
  });
  // A job that left the cache is asked for at once rather than at the next
  // slow recheck: a queued job removed from the queue leaves exactly so.
  useEffect(() => {
    if (vanished) void refetch();
  }, [vanished, refetch]);
  if (!cachedTerminal && rechecked && isTerminalJob(rechecked)) {
    return rechecked;
  }
  if (!cachedTerminal && jobId != null && rechecked === null) {
    // The queue no longer knows the job. One that was seen waiting or running
    // and is gone was removed from the queue, which is how a queued job is
    // cancelled. One that was never seen ended and aged out of the history
    // before its terminal event was read; the caller reads the real outcome
    // from its own endpoint, which keeps editor results longer than the queue
    // does.
    return {
      // eslint-disable-next-line camelcase
      ...(cached ?? ({ job_id: jobId } as System.Jobs)),
      status: vanished && !seen.current?.terminal ? "cancelled" : "completed",
    };
  }
  return cached;
}

export function isTerminalJob(job: System.Jobs | null | undefined): boolean {
  return (
    job?.status === "completed" ||
    job?.status === "failed" ||
    job?.status === "cancelled"
  );
}

/**
 * Stop an editor translation whether it is still queued or already running.
 * The server decides which, so a job that starts in between is still stopped.
 */
export async function cancelEditorTranslation(jobId: number) {
  await client.axios.delete("/translator/editor", { params: { jobId } });
}

/**
 * Stop following an editor translation without stopping it, as an editor that
 * is closed does. The job goes on; the server only drops this editor from the
 * ones it counts, so the Stop of one still waiting is the stop.
 */
export async function releaseEditorTranslation(jobId: number) {
  await client.axios.delete("/translator/editor", {
    params: { jobId, release: true },
  });
}

export interface EditorTranslationRequest {
  lines: Array<{ position: number; line: string }>;
  sourceLanguage: string;
  targetLanguage: string;
  title: string;
  mediaType: string;
}

export interface EditorTranslationState {
  jobId: number;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  lines?: Array<{ position: number; line: string }>;
  partial?: string | null;
  error?: string;
}

export interface QueuedEditorTranslation {
  jobId: number;
  /** The backend run the job was queued under. Null before the socket has said. */
  session: string | undefined;
}

export async function queueEditorTranslation(
  params: EditorTranslationRequest,
): Promise<QueuedEditorTranslation> {
  const response = await client.axios.post<{ jobId: number }>(
    "/translator/editor",
    params,
  );
  return {
    jobId: response.data.jobId,
    session: currentJobSession() ?? undefined,
  };
}

export async function readEditorTranslation(
  jobId: number,
): Promise<EditorTranslationState> {
  const response = await client.axios.get<EditorTranslationState>(
    "/translator/editor",
    { params: { jobId } },
  );
  return response.data;
}
