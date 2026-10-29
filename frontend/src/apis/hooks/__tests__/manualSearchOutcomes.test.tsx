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
import { observeJobSession } from "@/modules/jobs";
import server from "@/tests/mocks/node";

let client: QueryClient;

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function finished(id: number) {
  return {
    job_id: id,
    session: "run-search",
    job_name: "Searching subtitles",
    status: "completed",
    error: null,
  };
}

const ROWS: SearchResultType[] = [
  {
    provider: "found-provider",
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
  },
];

const OUTCOMES = [
  { name: "found-provider", status: "found", results: 1 },
  { name: "empty-provider", status: "empty", results: 0 },
  { name: "failed-provider", status: "failed", results: 0 },
];

type ManualSearchResult = ReturnType<typeof useEpisodesProvider> & {
  providers?: typeof OUTCOMES;
};

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  observeJobSession("run-search");
  onlineManager.setOnline(true);
});

describe("manual search provider outcomes", () => {
  it("returns episode rows and provider outcomes from the completed job", async () => {
    const queued: string[] = [];
    server.use(
      http.post("/api/providers/episodes/search", ({ request }) => {
        queued.push(new URL(request.url).searchParams.get("episodeid") ?? "");
        return HttpResponse.json(
          { job_id: 41, session: "run-search" },
          { status: 202 },
        );
      }),
      http.get("/api/system/jobs/result", () =>
        HttpResponse.json({
          job_id: 41,
          session: "run-search",
          status: "completed",
          error: null,
          result: { data: ROWS, providers: OUTCOMES },
        }),
      ),
    );

    const { result } = renderHook<ManualSearchResult, void>(
      () => useEpisodesProvider(902),
      { wrapper },
    );
    await waitFor(() => expect(queued).toEqual(["902"]));
    client.setQueryData([QueryKeys.System, QueryKeys.Jobs], [finished(41)]);

    await waitFor(() => expect(result.current.data).toEqual(ROWS));
    expect(result.current.providers).toEqual(OUTCOMES);
  });

  it("returns movie rows and provider outcomes from the completed job", async () => {
    const queued: string[] = [];
    server.use(
      http.post("/api/providers/movies/search", ({ request }) => {
        queued.push(new URL(request.url).searchParams.get("radarrid") ?? "");
        return HttpResponse.json(
          { job_id: 42, session: "run-search" },
          { status: 202 },
        );
      }),
      http.get("/api/system/jobs/result", () =>
        HttpResponse.json({
          job_id: 42,
          session: "run-search",
          status: "completed",
          error: null,
          result: { data: ROWS, providers: OUTCOMES },
        }),
      ),
    );

    const { result } = renderHook<ManualSearchResult, void>(
      () => useMoviesProvider(801),
      { wrapper },
    );
    await waitFor(() => expect(queued).toEqual(["801"]));
    client.setQueryData([QueryKeys.System, QueryKeys.Jobs], [finished(42)]);

    await waitFor(() => expect(result.current.data).toEqual(ROWS));
    expect(result.current.providers).toEqual(OUTCOMES);
  });
});
