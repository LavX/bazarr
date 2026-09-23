import { useMemo } from "react";
import { useSystemJobs } from "@/apis/hooks/system";
import api from "@/apis/raw";
import client from "@/apis/raw/client";

/**
 * The editor's background work (translation, waveform peaks) runs as ordinary
 * Bazarr jobs. Their progress and terminal state arrive on the jobs socket and
 * land in the shared [System, Jobs] cache, which is what this reads. Nothing
 * here polls.
 */
export function useEditorJob(jobId: number | null | undefined) {
  const { data } = useSystemJobs();
  return useMemo(
    () =>
      jobId == null ? undefined : data?.find((job) => job.job_id === jobId),
    [data, jobId],
  );
}

export function isTerminalJob(job: System.Jobs | undefined): boolean {
  return job?.status === "completed" || job?.status === "failed";
}

/** Cancel a running job, or drop it from the queue if it has not started. */
export async function stopEditorJob(job: System.Jobs | undefined, id: number) {
  if (job?.status === "pending") {
    await api.system.deleteJobs(id);
  } else {
    await api.system.actionOnJobs(id, "cancel");
  }
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
