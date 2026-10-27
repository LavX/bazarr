/* eslint-disable camelcase */
import { PropsWithChildren } from "react";
import {
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useMovieAction } from "@/apis/hooks/movies";
import { QueryKeys } from "@/apis/queries/keys";
import server from "@/tests/mocks/node";

// Scan disk and sync answer with the job they queued: the movie list is only
// worth reading again once that job has finished.

let client: QueryClient;

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  // The app holds requests until its socket connects; this test has none.
  onlineManager.setOnline(true);
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
});

describe("movie actions", () => {
  it("refreshes the movies when the sync job ends, not when it is queued", async () => {
    server.use(
      http.patch("/api/movies", () =>
        HttpResponse.json({ job_id: 7, session: "run-a" }, { status: 202 }),
      ),
    );
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useMovieAction(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ radarrid: 8, action: "sync" });
    });
    expect(invalidate).not.toHaveBeenCalledWith({
      queryKey: [QueryKeys.Movies],
    });

    act(() => {
      client.setQueryData(
        [QueryKeys.System, QueryKeys.Jobs],
        [{ job_id: 7, status: "completed" }],
      );
    });
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: [QueryKeys.Movies],
      }),
    );
  });

  it("refreshes at once for an action that answers without a job", async () => {
    server.use(
      http.patch("/api/movies", () => new HttpResponse(null, { status: 204 })),
    );
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useMovieAction(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ action: "search-wanted" });
    });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: [QueryKeys.Movies] });
  });
});
