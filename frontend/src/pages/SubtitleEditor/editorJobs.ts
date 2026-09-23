import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSystemJobs } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import client from "@/apis/raw/client";

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

export function useEditorJob(jobId: number | null | undefined) {
  const { data } = useSystemJobs();
  const cached = useMemo(
    () =>
      jobId == null ? undefined : data?.find((job) => job.job_id === jobId),
    [data, jobId],
  );
  const cachedTerminal = isTerminalJob(cached);
  const { data: rechecked } = useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Jobs, "editor", jobId],
    queryFn: async () => (await api.system.jobs(jobId ?? undefined))[0] ?? null,
    enabled: jobId != null && !cachedTerminal,
    refetchInterval: MISSED_EVENT_RECHECK_MS,
    staleTime: MISSED_EVENT_RECHECK_MS,
  });
  if (!cachedTerminal && rechecked && isTerminalJob(rechecked)) {
    return rechecked;
  }
  return cached;
}

export function isTerminalJob(job: System.Jobs | undefined): boolean {
  return job?.status === "completed" || job?.status === "failed";
}

/**
 * Stop an editor translation whether it is still queued or already running.
 * The server decides which, so a job that starts in between is still stopped.
 */
export async function cancelEditorTranslation(jobId: number) {
  await client.axios.delete("/translator/editor", { params: { jobId } });
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

export async function queueEditorTranslation(
  params: EditorTranslationRequest,
): Promise<number> {
  const response = await client.axios.post<{ jobId: number }>(
    "/translator/editor",
    params,
  );
  return response.data.jobId;
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
