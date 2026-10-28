/* eslint-disable camelcase */
import { PropsWithChildren } from "react";
import {
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { useEpisodesProvider, useMoviesProvider } from "@/apis/hooks";
import { QueryKeys } from "@/apis/queries/keys";
import { isJobCompletionClaimed, observeJobSession } from "@/modules/jobs";
import server from "@/tests/mocks/node";

// A manual search is a job: the request queues it, the hook follows it to its
// rows, and the finished job is the modal's to announce, not the toast's.

let client: QueryClient;

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function finished(id: number, status: "completed" | "failed", error?: string) {
  return {
    job_id: id,
    session: "run-search",
    job_name: "Searching subtitles",
    status,
    error: error ? { reason: "providers_unavailable", message: error } : null,
  };
}

const ROWS: SearchResultType[] = [
  {
    provider: "opensubtitles",
    url: "https://example.test/1",
    language: "en",
    forced: "False",
    hearing_impaired: "False",
    score: 90,
    orig_score: 90,
    score_without_hash: 88,
    release_info: ["Show.S01E01"],
    matches: [],
    dont_matches: [],
    subtitle: "abc",
    original_format: "False",
  } as SearchResultType,
];

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  observeJobSession("run-search");
  // The app holds queries until its socket connects; this test has none.
  onlineManager.setOnline(true);
});

describe("manual search jobs", () => {
  it("queues the episode search and returns the rows the job published", async () => {
    const queued: string[] = [];
    server.use(
      http.post("/api/providers/episodes/search", ({ request }) => {
        queued.push(new URL(request.url).searchParams.get("episodeid") ?? "");
        return HttpResponse.json(
          { job_id: 31, session: "run-search" },
          { status: 202 },
        );
      }),
      http.get("/api/system/jobs/result", () =>
        HttpResponse.json({
          job_id: 31,
          session: "run-search",
          status: "completed",
          error: null,
          result: ROWS,
        }),
      ),
    );

    const { result } = renderHook(() => useEpisodesProvider(902), { wrapper });
    // The socket reports the job's end into the jobs cache.
    await waitFor(() => expect(queued).toEqual(["902"]));
    client.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [finished(31, "completed")],
    );

    await waitFor(() => expect(result.current.data).toEqual(ROWS));
    // The modal shows the outcome, so the standard announcement is claimed.
    expect(isJobCompletionClaimed(31, "run-search")).toBe(true);
  });

  it("shows the job's own reason when the movie search fails", async () => {
    server.use(
      http.post("/api/providers/movies/search", () =>
        HttpResponse.json(
          { job_id: 32, session: "run-search" },
          { status: 202 },
        ),
      ),
    );

    const { result } = renderHook(() => useMoviesProvider(801), { wrapper });
    await waitFor(() => expect(result.current.fetchStatus).toBe("fetching"));
    client.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [finished(32, "failed", "All providers are throttled")],
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("All providers are throttled");
  });

  it("does not search without an item", () => {
    const { result } = renderHook(() => useEpisodesProvider(undefined), {
      wrapper,
    });
    return waitFor(() => expect(result.current.data).toEqual([]));
  });
});
