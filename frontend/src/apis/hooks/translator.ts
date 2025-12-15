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

export interface TranslatorModelInfo {
  id: string;
  name: string;
  description?: string;
  context_length?: number;
  pricing?: {
    prompt?: string;
    completion?: string;
  };
  is_default?: boolean;
}

export interface TranslatorModelsResponse {
  models: TranslatorModelInfo[];
  default_model: string;
}

const translatorQueryKeys = {
  all: [QueryKeys.Translator] as const,
  status: () => [...translatorQueryKeys.all, "status"] as const,
  jobs: () => [...translatorQueryKeys.all, "jobs"] as const,
  job: (id: string) => [...translatorQueryKeys.all, "jobs", id] as const,
  models: () => [...translatorQueryKeys.all, "models"] as const,
};

export function useTranslatorStatus(enabled = true) {
  return useQuery({
    queryKey: translatorQueryKeys.status(),
    queryFn: async () => {
      const response =
        await client.axios.get<TranslatorStatus>("/translator/status");
      return response.data;
    },
    // Stop polling on error to avoid spamming the console
    refetchInterval: (query) => (query.state.error ? false : 10000),
    retry: false,
    enabled,
    staleTime: 5000,
    // Suppress console errors - we handle them gracefully in the UI
    throwOnError: false,
  });
}

export function useTranslatorJobs(enabled = true) {
  return useQuery({
    queryKey: translatorQueryKeys.jobs(),
    queryFn: async () => {
      const response =
        await client.axios.get<TranslatorJobsResponse>("/translator/jobs");
      return response.data;
    },
    // Stop polling on error to avoid spamming the console
    refetchInterval: (query) => (query.state.error ? false : 5000),
    retry: false,
    enabled,
    staleTime: 2000,
    // Suppress console errors - we handle them gracefully in the UI
    throwOnError: false,
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
    // Stop polling on error
    refetchInterval: (query) => (query.state.error ? false : 2000),
    enabled: !!jobId,
    retry: false,
    staleTime: 1000,
    throwOnError: false,
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

export function useTranslatorModels(enabled = true) {
  return useQuery({
    queryKey: translatorQueryKeys.models(),
    queryFn: async () => {
      const response =
        await client.axios.get<TranslatorModelsResponse>("/translator/models");
      return response.data;
    },
    retry: false,
    enabled,
    staleTime: 60000, // Cache for 1 minute
    throwOnError: false,
  });
}
