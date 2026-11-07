import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { useSystemStatus } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import type {
  TranslationConnectionBody,
  TranslationRefusal,
  TranslationSaveBody,
} from "@/types/translation";

const stateKey = [QueryKeys.Translation, "state"] as const;

/**
 * Whether the v2 translation runtime is on. Settings > Translation exists only
 * while this is true; it is false while the status has not answered, so a
 * loading app never shows the page.
 */
export function useTranslationRuntime(): boolean {
  const { data } = useSystemStatus();
  return data?.translation_runtime_v2 === true;
}

export function useTranslationState() {
  return useQuery({
    queryKey: stateKey,
    queryFn: () => api.translation.state(),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/** A refusal from the typed API in the shape the page shows; null for anything else. */
export function readRefusal(error: unknown): TranslationRefusal | null {
  if (!isAxiosError(error) || !error.response) {
    return null;
  }
  const data = (error.response.data ?? {}) as Record<string, unknown>;
  return {
    status: error.response.status,
    code: typeof data.error === "string" ? data.error : "",
    message: typeof data.message === "string" ? data.message : error.message,
    fieldErrors:
      typeof data.field_errors === "object" && data.field_errors !== null
        ? (data.field_errors as Record<string, string>)
        : {},
    destinations: Array.isArray(data.destinations)
      ? (data.destinations as string[])
      : [],
  };
}

function useRefresh() {
  const client = useQueryClient();
  return () => client.invalidateQueries({ queryKey: stateKey });
}

// Every write runs with inline errors: the page shows the refusal next to the
// form that made it (field errors, a conflict, the consent question), so the
// shared client's toast would only say it a second time.
export function usePointersMutation() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (body: {
      engine?: number | null;
      fallback_engine?: number | null;
    }) => withInlineErrors(() => api.translation.setPointers(body)),
    onSettled: refresh,
  });
}

export function useSaveEngine() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: TranslationSaveBody }) =>
      withInlineErrors(() => api.translation.saveEngine(id, body)),
    onSuccess: refresh,
  });
}

export function useCreateConnection() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({
      instanceId,
      body,
    }: {
      instanceId: number;
      body: TranslationConnectionBody;
    }) =>
      withInlineErrors(() =>
        api.translation.createConnection(instanceId, body),
      ),
    onSuccess: refresh,
  });
}

export function useUpdateConnection() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: number;
      body: TranslationConnectionBody;
    }) => withInlineErrors(() => api.translation.updateConnection(id, body)),
    onSuccess: refresh,
  });
}

export function useDeleteConnection() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (id: number) =>
      withInlineErrors(() => api.translation.deleteConnection(id)),
    onSuccess: refresh,
  });
}

export function useReorderConnections() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: ({
      instanceId,
      order,
    }: {
      instanceId: number;
      order: number[];
    }) =>
      withInlineErrors(() =>
        api.translation.reorderConnections(instanceId, order),
      ),
    onSettled: refresh,
  });
}

/** The live choices of one `options` field, fetched when the form for it is open. */
export function useFieldOptions(
  instanceId: number,
  backendType: string,
  field: string,
  values: object,
  enabled: boolean,
) {
  return useQuery({
    queryKey: [
      QueryKeys.Translation,
      "options",
      instanceId,
      backendType,
      field,
    ],
    queryFn: () =>
      api.translation.options(instanceId, backendType, field, values),
    enabled,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
}
