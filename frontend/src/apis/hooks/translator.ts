import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import client from "@/apis/raw/client";

export interface TranslatorJob {
  jobId: string;
  status: "queued" | "processing" | "completed" | "failed" | "cancelled";
  progress: number;
  message?: string;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
  error?: string;
  sourceLanguage?: string;
  targetLanguage?: string;
  filename?: string;
}

export interface TranslatorStatus {
  service: string;
  version: string;
  healthy: boolean;
  config: {
    model: string;
    apiKeyConfigured: boolean;
  };
  queue: {
    maxConcurrent: number;
    processing: number;
    queued: number;
    completed: number;
    failed: number;
    total: number;
  };
}

export interface TranslatorJobsResponse {
  jobs: TranslatorJob[];
  total: number;
  processing: number;
  queued: number;
}

const translatorQueryKeys = {
  all: [QueryKeys.Translator] as const,
  status: () => [...translatorQueryKeys.all, "status"] as const,
  jobs: () => [...translatorQueryKeys.all, "jobs"] as const,
  job: (id: string) => [...translatorQueryKeys.all, "jobs", id] as const,
};

export function useTranslatorStatus() {
  return useQuery({
    queryKey: translatorQueryKeys.status(),
    queryFn: async () => {
      const response =
        await client.axios.get<TranslatorStatus>("/translator/status");
      return response.data;
    },
    refetchInterval: 5000, // Refresh every 5 seconds
    retry: false,
  });
}

export function useTranslatorJobs() {
  return useQuery({
    queryKey: translatorQueryKeys.jobs(),
    queryFn: async () => {
      const response =
        await client.axios.get<TranslatorJobsResponse>("/translator/jobs");
      return response.data;
    },
    refetchInterval: 2000, // Refresh every 2 seconds for real-time updates
    retry: false,
  });
}

export function useTranslatorJob(jobId: string) {
  return useQuery({
    queryKey: translatorQueryKeys.job(jobId),
    queryFn: async () => {
      const response = await client.axios.get<TranslatorJob>(
        `/translator/jobs/${jobId}`,
      );
      return response.data;
    },
    refetchInterval: 1000,
    enabled: !!jobId,
    retry: false,
  });
}

export function useCancelTranslatorJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (jobId: string) => {
      const response = await client.axios.delete(`/translator/jobs/${jobId}`);
      return response.data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: translatorQueryKeys.jobs(),
      });
      void queryClient.invalidateQueries({
        queryKey: translatorQueryKeys.status(),
      });
    },
  });
}
