/* eslint-disable camelcase */

import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { act, renderHook, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { useMediaServerLinkDestinations } from "./mediaServerLinks";
import { usePlexLogoutMutation, usePlexServerSelectionMutation } from "./plex";
import { useSettingsMutation } from "./system";

const PLEX = { instance_id: "plex-1", kind: "plex", display_name: "Plex" };
const EMBY = { instance_id: "emby-1", kind: "emby", display_name: "Emby" };

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

// The destinations a menu offers, as the server lists them now.
function destinations(initial: object[]) {
  const state = { rows: initial };
  server.use(
    http.get("/api/system/media-server-links/destinations", () =>
      HttpResponse.json({ data: state.rows }),
    ),
  );
  return state;
}

describe("Play on destinations", () => {
  it("are asked again when a Plex server is selected", async () => {
    // Selecting a server creates or moves the Plex destination row, so a
    // menu kept offering what it offered before, for up to a minute.
    const state = destinations([]);
    server.use(
      http.post("/api/plex/select-server", () => {
        state.rows = [PLEX];
        return HttpResponse.json({ data: { name: "Plex" } });
      }),
    );
    const { wrapper, client } = queryWrapper();
    const { result, unmount } = renderHook(
      () => ({
        links: useMediaServerLinkDestinations(),
        select: usePlexServerSelectionMutation(),
      }),
      { wrapper },
    );
    try {
      await waitFor(() => expect(result.current.links.data).toEqual([]));
      await act(async () => {
        await result.current.select.mutateAsync({
          machineIdentifier: "abc",
          name: "Plex",
          uri: "http://10.0.0.9:32400",
          local: true,
        });
      });
      await waitFor(() => expect(result.current.links.data).toEqual([PLEX]));
    } finally {
      unmount();
      client.clear();
    }
  });

  it("are asked again after signing out of Plex", async () => {
    // Signing out switches the account's destination off.
    const state = destinations([PLEX]);
    server.use(
      http.post("/api/plex/oauth/logout", () => {
        state.rows = [];
        return HttpResponse.json({});
      }),
    );
    const { wrapper, client } = queryWrapper();
    const { result, unmount } = renderHook(
      () => ({
        links: useMediaServerLinkDestinations(),
        logout: usePlexLogoutMutation(),
      }),
      { wrapper },
    );
    try {
      await waitFor(() => expect(result.current.links.data).toEqual([PLEX]));
      await act(async () => {
        await result.current.logout.mutateAsync();
      });
      await waitFor(() => expect(result.current.links.data).toEqual([]));
    } finally {
      unmount();
      client.clear();
    }
  });

  it("are asked again when a media server master switch is saved", async () => {
    const state = destinations([]);
    server.use(
      http.post("/api/system/settings", () => {
        state.rows = [EMBY];
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { wrapper, client } = queryWrapper();
    const { result, unmount } = renderHook(
      () => ({
        links: useMediaServerLinkDestinations(),
        save: useSettingsMutation(),
      }),
      { wrapper },
    );
    try {
      await waitFor(() => expect(result.current.links.data).toEqual([]));
      await act(async () => {
        await result.current.save.mutateAsync({
          "settings-general-use_emby": true,
        });
      });
      await waitFor(() => expect(result.current.links.data).toEqual([EMBY]));
    } finally {
      unmount();
      client.clear();
    }
  });
});
