import { useEffect, useRef } from "react";
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

export function useTranslationRouteStatus() {
  const { data, isSuccess, fetchStatus, isError, error, isFetching, refetch } =
    useSystemStatus();
  // A first visit still waits for stale cached status to be verified.
  const legacyConfirmed = useRef(false);
  let runtime: "ready" | "unavailable" | "legacy" | undefined;

  if (isSuccess) {
    if (data.translation_runtime_v2 === true) {
      runtime = "ready";
    } else if (data.translation_runtime_v2_unavailable === true) {
      runtime = "unavailable";
    } else if (fetchStatus === "idle" || legacyConfirmed.current) {
      // Keep admitted legacy forms mounted during background refreshes.
      runtime = "legacy";
    }
  }

  useEffect(() => {
    legacyConfirmed.current = runtime === "legacy";
  }, [runtime]);

  return { runtime, isError, error, isFetching, refetch };
}

/**
 * The page's state. `enabled` is for a caller outside the page that reads it
 * only while the runtime is on: the route does not exist with it off.
 * The page and setup refresh on entry even when other callers have a fresh cache.
 */
export function useTranslationState(enabled = true, refetchOnMount?: "always") {
  return useQuery({
    queryKey: stateKey,
    queryFn: () => api.translation.state(),
    enabled,
    retry: false,
    refetchOnMount,
    refetchOnWindowFocus: false,
  });
}

/**
 * A refusal from the typed API in the shape the page shows; null for anything else. `message` is the
 * server's own sentence, or empty when it sent none: the caller then says what failed in its own words,
 * and a bare "Request failed with status code 500" never reaches the page.
 */
export function readRefusal(error: unknown): TranslationRefusal | null {
  if (!isAxiosError(error) || !error.response) {
    return null;
  }
  const data = (error.response.data ?? {}) as Record<string, unknown>;
  return {
    status: error.response.status,
    code: typeof data.error === "string" ? data.error : "",
    message: typeof data.message === "string" ? data.message : "",
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
 * A start that got as far as the engine's switch and no further: the back end
 * is saved, and the engine is still off because the server would not turn it
 * on. Nothing is selected, so the start is not reported as done.
 */
export class EngineStillOffError extends Error {
  /** The server's refusal, when it gave one. */
  readonly refusal: TranslationRefusal | null;

  constructor(cause: unknown) {
    super("The engine is still switched off.");
    this.name = "EngineStillOffError";
    this.refusal = readRefusal(cause);
  }
}

/**
 * First run in one step: add the engine's first back end, turn the engine on
 * when it is off, then select it. `created` skips the first request on a retry
 * after only a later one failed, so a retry never adds the back end twice. The
 * state is loaded again as soon as the back end exists: if a later request then
 * fails, the rest of the page already shows that back end and does not ask for
 * it again.
 *
 * An engine can be off when the start is pressed: turned off by hand in the
 * full page, or by a plugin update. Selecting it as it is would report a start
 * that translates nothing, so it is turned on first, from the state as it is
 * after the back end was added, and a refusal stops the start before anything
 * is selected.
 */
export function useStartEngine() {
  const refresh = useRefresh();
  const fresh = useFreshState();
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
      const engine = (await fresh()).instances.find(
        (item) => item.id === instanceId,
      );
      let turnedOn = false;
      if (engine && !engine.enabled) {
        try {
          await withInlineErrors(() =>
            api.translation.saveEngine(instanceId, {
              enabled: true,
              // eslint-disable-next-line camelcase -- transport name
              expected_revision: engine.revision,
            }),
          );
          turnedOn = true;
        } catch (error) {
          throw new EngineStillOffError(error);
        }
      }
      try {
        return await withInlineErrors(() =>
          api.translation.setPointers({ engine: instanceId }),
        );
      } catch (error) {
        // The engine was turned on a moment ago and is not selected. The page is shown that, and
        // not the engine as it was: off, at a revision that is gone.
        if (turnedOn) {
          await refresh();
        }
        throw error;
      }
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
