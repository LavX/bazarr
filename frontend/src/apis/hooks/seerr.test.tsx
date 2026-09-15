import { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { seerrMediaKey, useSeerrRequestMutation } from "./seerr";

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

describe("seerr query keys", () => {
  it("keys movie status by tmdb id", () => {
    expect(seerrMediaKey({ kind: "movie", tmdbId: 550 })).toEqual([
      "seerr",
      "media",
      "movie",
      "tmdb",
      550,
    ]);
  });
  it("keys tvdb-only shows separately from tmdb ones", () => {
    expect(seerrMediaKey({ kind: "tv", tvdbId: 121361 })).toEqual([
      "seerr",
      "media",
      "tv",
      "tvdb",
      121361,
    ]);
  });
});

describe("seerr request mutation", () => {
  it("refreshes each requested title, not only the last one submitted", async () => {
    // Requesting one title, moving to another and requesting that before the
    // first answers. Both requests exist, so both titles have to be refreshed:
    // pinning the identity on the hook rather than on the request lost the
    // first one, which then kept showing its pre-request state for a minute.
    const release: Array<() => void> = [];
    server.use(
      http.post("/api/seerr/request", async () => {
        await new Promise<void>((resolve) => release.push(resolve));
        return HttpResponse.json({ outcome: "requested" });
      }),
    );
    const { client, wrapper } = queryWrapper();
    const refreshed: unknown[] = [];
    vi.spyOn(client, "invalidateQueries").mockImplementation((filters) => {
      refreshed.push(filters?.queryKey);
      return Promise.resolve();
    });
    const { result } = renderHook(() => useSeerrRequestMutation(), { wrapper });

    const movie = { kind: "movie", tmdbId: 550 } as const;
    const show = { kind: "tv", tmdbId: 1399 } as const;
    act(() => {
      result.current.mutate({
        body: { media_type: "movie", tmdb_id: 550, is4k: false },
        identity: movie,
      });
    });
    act(() => {
      result.current.mutate({
        body: { media_type: "tv", tmdb_id: 1399, seasons: "all", is4k: false },
        identity: show,
      });
    });

    await waitFor(() => expect(release).toHaveLength(2));
    await act(async () => {
      release.forEach((resolve) => resolve());
    });
    await waitFor(() => expect(refreshed).toHaveLength(2));
    expect(refreshed).toEqual(
      expect.arrayContaining([seerrMediaKey(movie), seerrMediaKey(show)]),
    );
  });
});
