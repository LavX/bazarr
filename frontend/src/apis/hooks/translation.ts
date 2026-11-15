import {
  keepPreviousData,
  notifyManager,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { useSystemStatus } from "@/apis/hooks/system";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import type {
  TranslationConnectionBody,
  TranslationLimitsBody,
  TranslationRefusal,
  TranslationSaveBody,
} from "@/types/translation";

const stateKey = [QueryKeys.Translation, "state"] as const;
const summaryKey = [QueryKeys.Translation, "summary"] as const;

/**
 * Whether the v2 translation runtime is on. The Translation page exists only
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

/**
 * Runs `callback` once the page has been handed the state a write just loaded.
 * A control that shows its new value before the write is confirmed drops that
 * value here, not the moment the write settles: the loaded state reaches the
 * page a moment after the request does, and dropping the value earlier draws
 * the old one for a frame (and moves the focus with it).
 */
export function afterStateDelivered(callback: () => void) {
  notifyManager.schedule(callback);
}

function useRefresh() {
  const client = useQueryClient();
  return () => client.invalidateQueries({ queryKey: stateKey });
}

// A save refused because the row moved on: load the new state, so the form
// holds the current revision and a second try can go through.
function useRefreshOnStale() {
  const refresh = useRefresh();
  return (error: unknown) => {
    if (readRefusal(error)?.code === "stale_revision") {
      void refresh();
    }
  };
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
  const onStale = useRefreshOnStale();
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: TranslationSaveBody }) =>
      withInlineErrors(() => api.translation.saveEngine(id, body)),
    onSuccess: refresh,
    onError: onStale,
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

/**
 * First run in one step: add the engine's first back end, then select the
 * engine. `created` skips the first request on a retry after only the second
 * one failed, so a retry never adds the back end twice. The state is loaded
 * again as soon as the back end exists: if the second request then fails, the
 * rest of the page already shows that back end and does not ask for it again.
 */
export function useStartEngine() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async ({
      instanceId,
      body,
      created,
      onCreated,
    }: {
      instanceId: number;
      body: TranslationConnectionBody;
      created: boolean;
      onCreated: () => void;
    }) => {
      if (!created) {
        await withInlineErrors(() =>
          api.translation.createConnection(instanceId, body),
        );
        onCreated();
        await refresh();
      }
      return withInlineErrors(() =>
        api.translation.setPointers({ engine: instanceId }),
      );
    },
    onSuccess: refresh,
  });
}

export function useUpdateConnection() {
  const refresh = useRefresh();
  const onStale = useRefreshOnStale();
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: number;
      body: TranslationConnectionBody;
    }) => withInlineErrors(() => api.translation.updateConnection(id, body)),
    onSuccess: refresh,
    onError: onStale,
  });
}

export function useDeleteConnection() {
  const refresh = useRefresh();
  const onStale = useRefreshOnStale();
  return useMutation({
    mutationFn: ({ id, revision }: { id: number; revision: number }) =>
      withInlineErrors(() => api.translation.deleteConnection(id, revision)),
    onSuccess: refresh,
    onError: onStale,
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
      // The draft is part of the key: a new base URL asks again instead of showing the old address's models.
      JSON.stringify(values),
    ],
    queryFn: () =>
      api.translation.options(instanceId, backendType, field, values),
    enabled,
    retry: false,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
}

/**
 * Spend this month, the caps and the queue counts. Asked only of a server
 * whose state lists the `summary` capability, so a server without the route is
 * never sent the request.
 */
export function useTranslationSummary(enabled: boolean) {
  return useQuery({
    queryKey: summaryKey,
    queryFn: () => withInlineErrors(() => api.translation.summary()),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: 30_000,
  });
}

/** The waiting jobs, asked only of a server that lists the `queue` capability. */
export function useTranslationQueue(enabled: boolean, live: boolean) {
  return useQuery({
    queryKey: [QueryKeys.Translation, "queue"],
    queryFn: () => withInlineErrors(() => api.translation.queue()),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: live ? 5000 : false,
  });
}

/**
 * Loads the state again and answers with it, for a form that replaces its
 * draft with what is saved now. It rejects when the load fails, so the form
 * never copies a state it could not confirm.
 */
export function useFreshState() {
  const client = useQueryClient();
  return () =>
    client.fetchQuery({
      queryKey: stateKey,
      queryFn: () => api.translation.state(),
      staleTime: 0,
      retry: false,
    });
}

export function useSaveLimits() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: TranslationLimitsBody) =>
      withInlineErrors(() => api.translation.saveLimits(body)),
    onSuccess: (summary) => client.setQueryData(summaryKey, summary),
  });
}

/** Sends one small request through a saved back end and reports what came back. */
export function useTestConnection() {
  return useMutation({
    mutationFn: (id: number) =>
      withInlineErrors(() => api.translation.testConnection(id)),
  });
}

export function useTranslationHistory(
  status: string | null,
  length: number,
  refetchInterval: number | false = false,
  enabled = true,
) {
  return useQuery({
    enabled,
    queryKey: [QueryKeys.Translation, "history", status ?? "all", length],
    queryFn: () =>
      withInlineErrors(() =>
        api.translation.history({
          start: 0,
          length,
          ...(status ? { status } : {}),
        }),
      ),
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval,
    placeholderData: keepPreviousData,
  });
}
