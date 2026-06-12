import { useMemo } from "react";
import { showNotification } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import type {
  ArrInstanceCreate,
  ArrInstanceTest,
  ArrInstanceTestOverrides,
  ArrInstanceUpdate,
} from "@/apis/raw/arrInstances";

const arrKey = [QueryKeys.ArrInstances];

export function getArrInstanceErrorMessage(error: unknown, fallback: string) {
  if (error instanceof AxiosError) {
    const data = error.response?.data as { message?: string } | undefined;
    if (data?.message) {
      return data.message;
    }
  }
  return fallback;
}

export function isArrInstanceConflict(error: unknown) {
  return error instanceof AxiosError && error.response?.status === 409;
}

export function useArrInstances() {
  return useQuery({
    queryKey: arrKey,
    queryFn: () => api.arrInstances.list(),
  });
}

/**
 * Multi-instance UI helpers (#156) for a given kind: a name lookup by
 * arr_instance_id, MultiSelect options for that kind's instances, and whether
 * there is more than one (so the instance badge/filter only show when relevant).
 */
export function useArrInstanceLabels(kind: "sonarr" | "radarr") {
  const { data } = useArrInstances();
  return useMemo(() => {
    const all = data ?? [];
    const ofKind = all.filter((i) => i.kind === kind);
    const nameById = new Map(all.map((i) => [i.id, i.name]));
    return {
      multiInstance: ofKind.length > 1,
      nameById,
      options: ofKind.map((i) => ({ value: String(i.id), label: i.name })),
    };
  }, [data, kind]);
}

export function useArrInstance(id: number) {
  return useQuery({
    queryKey: [...arrKey, id],
    queryFn: () => api.arrInstances.getOne(id),
  });
}

export function useCreateArrInstance() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...arrKey, QueryKeys.Actions, "create"],
    mutationFn: (body: ArrInstanceCreate) => api.arrInstances.create(body),
    onSuccess: (created) => {
      showNotification({
        color: "green",
        message: `Instance "${created.name}" created`,
      });
      client.invalidateQueries({ queryKey: arrKey });
    },
    onError: (error) =>
      showNotification({
        color: "red",
        message: getArrInstanceErrorMessage(
          error,
          "Failed to create the instance",
        ),
      }),
  });
}

export function useUpdateArrInstance() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...arrKey, QueryKeys.Actions, "update"],
    mutationFn: ({ id, body }: { id: number; body: ArrInstanceUpdate }) =>
      api.arrInstances.update(id, body),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: arrKey });
    },
    onError: (error) =>
      showNotification({
        color: "red",
        message: getArrInstanceErrorMessage(
          error,
          "Failed to update the instance",
        ),
      }),
  });
}

export function useDeleteArrInstance() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [...arrKey, QueryKeys.Actions, "delete"],
    mutationFn: (id: number) => api.arrInstances.remove(id),
    onSuccess: () => {
      showNotification({ color: "green", message: "Instance deleted" });
      client.invalidateQueries({ queryKey: arrKey });
    },
    onError: (error) => {
      // A 409 conflict (instance still owns synced media) is surfaced inline
      // by the delete confirmation dialog with actionable guidance.
      if (isArrInstanceConflict(error)) {
        return;
      }
      showNotification({
        color: "red",
        message: getArrInstanceErrorMessage(
          error,
          "Failed to delete the instance",
        ),
      });
    },
  });
}

export function useTestArrInstanceConnection() {
  return useMutation({
    // The key never appears here: mutation keys are static strings and the
    // request body is the only place credentials travel.
    mutationKey: [...arrKey, QueryKeys.Actions, "test"],
    mutationFn: (body: ArrInstanceTest) => api.arrInstances.test(body),
  });
}

// Tests a saved instance using its stored key (decrypted server-side). The key
// never reaches the browser, so this is how the card "Test" and the edit
// modal's "Keep current key" mode verify the connection.
export function useTestArrInstanceById() {
  return useMutation({
    mutationKey: [...arrKey, QueryKeys.Actions, "test-existing"],
    mutationFn: ({
      id,
      overrides,
    }: {
      id: number;
      overrides?: ArrInstanceTestOverrides;
    }) => api.arrInstances.testExisting(id, overrides),
  });
}
