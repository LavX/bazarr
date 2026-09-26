/* eslint-disable camelcase */

import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { act, renderHook, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { useMediaServerInstances } from "./mediaServers";
import { usePlexServerSelectionMutation } from "./plex";
import { useSystemSettings } from "./system";

function queryWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, networkMode: "offlineFirst" },
      mutations: { retry: false, networkMode: "offlineFirst" },
    },
  });
  return {
    client,
    wrapper: ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  };
}

it("refreshes the Plex instance list when a server is selected", async () => {
  // Choosing a server is what creates the Plex destination row
  // (media_servers/plex_account.py). The mutation used to invalidate the Plex
  // account queries only, so every list of Plex media server instances on
  // screen kept showing the state from before the row existed.
  let rows: unknown[] = [];
  server.use(
    http.get("/api/system/media-server-instances", () =>
      HttpResponse.json({ data: rows }),
    ),
    http.post("/api/plex/select-server", () => {
      rows = [
        {
          id: "plex-1",
          kind: "plex",
          name: "Plex",
          enabled: true,
          url: "http://10.0.0.9:32400",
          verify_ssl: true,
          api_key_set: true,
          path_mappings: [],
          refresh_movies: true,
          refresh_episodes: true,
          options: {},
        },
      ];
      return HttpResponse.json({ data: { name: "Plex" } });
    }),
  );
  const { wrapper, client } = queryWrapper();
  const { result, unmount } = renderHook(
    () => ({
      instances: useMediaServerInstances("plex"),
      select: usePlexServerSelectionMutation(),
    }),
    { wrapper },
  );

  try {
    await waitFor(() => expect(result.current.instances.data).toEqual([]));

    await act(async () => {
      await result.current.select.mutateAsync({
        machineIdentifier: "abc",
        name: "Plex",
        uri: "http://10.0.0.9:32400",
        local: true,
      });
    });

    await waitFor(() =>
      expect(result.current.instances.data?.[0]?.id).toBe("plex-1"),
    );
  } finally {
    unmount();
    client.clear();
  }
});

it("refreshes the settings that say which Plex row the account owns", async () => {
  // plex.instance_id is the only thing that says the account owns a row, and
  // the settings query never goes stale on its own. Left cached, the wizard's
  // picker read the row the account had just made as one somebody added by
  // hand: disconnecting it would have deleted it without signing out, and the
  // credential left behind rebuilds it on the next reconcile.
  let instanceId = "";
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {},
        translator: {},
        plex: { instance_id: instanceId },
      }),
    ),
    http.post("/api/plex/select-server", () => {
      instanceId = "plex-1";
      return HttpResponse.json({ data: { name: "Plex" } });
    }),
  );
  const { wrapper, client } = queryWrapper();
  const { result, unmount } = renderHook(
    () => ({
      settings: useSystemSettings(),
      select: usePlexServerSelectionMutation(),
    }),
    { wrapper },
  );

  try {
    await waitFor(() =>
      expect(result.current.settings.data?.plex?.instance_id).toBe(""),
    );

    await act(async () => {
      await result.current.select.mutateAsync({
        machineIdentifier: "abc",
        name: "Plex",
        uri: "http://10.0.0.9:32400",
        local: true,
      });
    });

    await waitFor(() =>
      expect(result.current.settings.data?.plex?.instance_id).toBe("plex-1"),
    );
  } finally {
    unmount();
    client.clear();
  }
});
