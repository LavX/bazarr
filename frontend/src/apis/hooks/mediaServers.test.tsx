/* eslint-disable camelcase */

import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import type { MediaServerKind } from "@/apis/raw/mediaServers";
import { act, renderHook, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import {
  useMediaServerStatus,
  useMediaServerTest,
  useSaveMediaServerInstance,
  useSiloLibraries,
} from "./mediaServers";

function queryWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        networkMode: "offlineFirst",
        staleTime: 60000,
        placeholderData: (previous: unknown) => previous,
      },
      mutations: { networkMode: "offlineFirst" },
    },
  });
  return {
    client,
    wrapper: ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  };
}

it("does not show another server's previous status while its own status is loading", async () => {
  let finish: (() => void) | undefined;
  server.use(
    http.get("/api/system/media-server-instances/a/status", () =>
      HttpResponse.json({ pending: 0, state: "confirmed", error_code: null }),
    ),
    http.get("/api/system/media-server-instances/b/status", async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return HttpResponse.json({
        pending: 1,
        state: "unconfirmed",
        error_code: "timeout",
      });
    }),
  );
  const { wrapper, client } = queryWrapper();
  const { result, rerender, unmount } = renderHook(
    ({ kind }: { kind: MediaServerKind }) =>
      useMediaServerStatus(kind, kind === "emby" ? "a" : "b"),
    { wrapper, initialProps: { kind: "emby" } },
  );
  try {
    await waitFor(() => expect(result.current.data?.state).toBe("confirmed"));
    rerender({ kind: "silo" });
    await waitFor(() => expect(result.current.data).toBeUndefined());
    await waitFor(() => expect(finish).toBeDefined());
    finish?.();
    await waitFor(() => expect(result.current.data?.state).toBe("unconfirmed"));
  } finally {
    finish?.();
    unmount();
    client.clear();
  }
});

it("discards late read-only results after credentials change without caching the API key", async () => {
  let finish: (() => void) | undefined;
  server.use(
    http.post("/api/silo/test-connection", async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return HttpResponse.json({
        success: true,
        server_name: "Old connection",
      });
    }),
  );
  const { wrapper, client } = queryWrapper();
  const { result, rerender, unmount } = renderHook(
    ({ apikey }) =>
      useMediaServerTest("silo", {
        url: "https://silo.example",
        api_key: apikey,
        verify_ssl: true,
      }),
    { wrapper, initialProps: { apikey: "sentinel-hook-secret" } },
  );
  try {
    let pending: Promise<unknown>;
    act(() => {
      pending = result.current.mutateAsync();
    });
    await waitFor(() => expect(finish).toBeDefined());
    rerender({ apikey: "new-key" });
    await act(async () => {
      finish?.();
      await pending;
    });
    await waitFor(() => expect(result.current.isIdle).toBe(true));
    await waitFor(() => expect(result.current.data).toBeUndefined());
    expect(
      JSON.stringify(
        client
          .getMutationCache()
          .getAll()
          .map((mutation) => mutation.state),
      ),
    ).not.toContain("sentinel-hook-secret");
  } finally {
    finish?.();
    unmount();
    client.clear();
  }
});

it("clears loaded Silo library choices after the URL, key or TLS setting changes", async () => {
  server.use(
    http.post("/api/silo/libraries", () =>
      HttpResponse.json({
        data: [{ id: "0007", name: "TV", type: "series", paths: ["/tv"] }],
        error_code: null,
      }),
    ),
  );
  const { wrapper, client } = queryWrapper();
  const initialProps = {
    url: "https://silo.example",
    api_key: "key",
    verify_ssl: true,
  };
  const { result, rerender, unmount } = renderHook(
    (input) => useSiloLibraries(input),
    { wrapper, initialProps },
  );
  try {
    await act(async () => {
      await result.current.mutateAsync();
    });
    await waitFor(() => expect(result.current.data?.[0].id).toBe("0007"));
    rerender({ ...initialProps, verify_ssl: false });
    await waitFor(() => expect(result.current.data).toBeUndefined());
    await act(async () => {
      await result.current.mutateAsync();
    });
    rerender({ ...initialProps, api_key: "changed" });
    await waitFor(() => expect(result.current.data).toBeUndefined());
    await act(async () => {
      await result.current.mutateAsync();
    });
    rerender({ ...initialProps, url: "https://other.example" });
    await waitFor(() => expect(result.current.data).toBeUndefined());
  } finally {
    unmount();
    client.clear();
  }
});

it("isolates saved probes for two UUIDs and retains no secret mutation variables or errors", async () => {
  const requests: unknown[] = [];
  server.use(
    http.post(
      "/api/system/media-server-instances/:id/test-connection",
      async ({ request, params }) => {
        requests.push([params.id, await request.json()]);
        return HttpResponse.json({}, { status: 502 });
      },
    ),
  );
  const { wrapper, client } = queryWrapper();
  const { result, rerender, unmount } = renderHook(
    ({ id }) =>
      useMediaServerTest(
        "emby",
        {
          url: "https://edited.example",
          verify_ssl: false,
          api_key: "sentinel-secret",
        },
        id,
      ),
    { wrapper, initialProps: { id: "a" } },
  );
  try {
    await act(async () => {
      await result.current.mutateAsync().catch(() => undefined);
    });
    rerender({ id: "b" });
    await waitFor(() => expect(result.current.isIdle).toBe(true));
    expect(requests).toEqual([
      [
        "a",
        {
          url: "https://edited.example",
          verify_ssl: false,
          api_key: "sentinel-secret",
        },
      ],
    ]);
    for (const mutation of client.getMutationCache().getAll()) {
      expect(mutation.state.variables).toBeUndefined();
      expect(mutation.state.error).not.toHaveProperty("config");
    }
    expect(JSON.stringify(client.getQueryCache().getAll())).not.toContain(
      "sentinel-secret",
    );
    expect(
      JSON.stringify(
        client
          .getMutationCache()
          .getAll()
          .map((m) => m.state),
      ),
    ).not.toContain("sentinel-secret");
  } finally {
    unmount();
    client.clear();
  }
});

it("saves a credential from its closure and keeps only safe DTO state on success or failure", async () => {
  const writes: unknown[] = [];
  let failed = false;
  const row = {
    id: "2af88684-d7d2-4534-82bb-a6d839cc5c10",
    kind: "emby",
    name: "Room",
    enabled: false,
    url: "https://emby.example",
    verify_ssl: true,
    api_key_set: true,
    path_mappings: [],
  };
  server.use(
    http.patch(
      `/api/system/media-server-instances/${row.id}`,
      async ({ request }) => {
        writes.push(await request.json());
        return failed
          ? HttpResponse.json({}, { status: 502 })
          : HttpResponse.json({ ...row, api_key: "sentinel-save-secret" });
      },
    ),
  );
  const { wrapper, client } = queryWrapper();
  const { result, unmount } = renderHook(
    () =>
      useSaveMediaServerInstance(
        "emby",
        { api_key: "sentinel-save-secret" },
        row.id,
      ),
    { wrapper },
  );
  try {
    await act(async () => {
      await result.current.mutateAsync();
    });
    await waitFor(() => expect(result.current.data).toEqual(row));
    failed = true;
    await act(async () => {
      await result.current.mutateAsync().catch(() => undefined);
    });
    expect(writes).toEqual([
      { api_key: "sentinel-save-secret" },
      { api_key: "sentinel-save-secret" },
    ]);
    const mutations = client.getMutationCache().getAll();
    expect(mutations.every((m) => m.state.variables === undefined)).toBe(true);
    expect(JSON.stringify(mutations.map((m) => m.state))).not.toContain(
      "sentinel-save-secret",
    );
    expect(
      JSON.stringify(
        client
          .getQueryCache()
          .getAll()
          .map((q) => q.queryKey),
      ),
    ).not.toContain("sentinel-save-secret");
  } finally {
    unmount();
    client.clear();
  }
});
