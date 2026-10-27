/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, Link, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import * as files from "@/utilities/files";
import { downloadJobHandlers, DownloadRequest } from "./downloadJobHarness";
import { pickOption } from "./selectTestHelpers";
import Discover from "./testHarness";

const target = {
  media_type: "movie",
  imdb_id: "tt0133093",
  language: "eng",
  matching_mode: "title",
};

const movie = {
  source: "tmdb",
  source_id: "tmdb:movie:11",
  id: 11,
  media_type: "movie",
  title: "The Matrix",
  year: 1999,
  imdb_id: "tt0133093",
  mapping_status: "resolved",
  overview: "A computer hacker learns about the true nature of reality.",
  poster_url: null,
  backdrop_url: null,
};

const envelope = {
  source: "tmdb",
  status: "available",
  configured: true,
  revision: "metadata-one",
  locale: "en-US",
  message: "TMDB is available.",
  checked_at: "2026-09-08T10:00:00Z",
  fetched_at: "2026-09-08T10:00:00Z",
};

const srt = "1\n00:00:03,000 --> 00:00:04,000\nLive row\n\n";

function row(id: string, release: string) {
  return {
    id,
    search_id: "search-1",
    provider: "catalog-fast",
    language: "en",
    language_variant: null,
    release,
    scope: "unknown" as const,
    hearing_impaired: null,
    matches: ["imdb_id"],
    compatibility_score: 10,
    compatibility_score_max: 119,
    rating: null,
    uploader: null,
    checked_at: "2026-09-08T10:00:00Z",
    expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    stale: false,
  };
}

/** What the running search has published so far, with one provider still out. */
function running(results: ReturnType<typeof row>[]) {
  return {
    phase: "searching",
    search_id: "search-1",
    context: target,
    results,
    providers: [
      {
        provider: "catalog-fast",
        status: "success",
        result_count: results.length,
      },
      { provider: "catalog-slow", status: "pending", result_count: 0 },
    ],
  };
}

function finished(results: ReturnType<typeof row>[]) {
  return {
    search_id: "search-1",
    context: target,
    status: results.length ? "complete" : "complete",
    checked_at: "2026-09-08T10:00:00Z",
    attempted_at: "2026-09-08T10:00:00Z",
    cache_status: "fresh",
    coverage: {
      complete: true,
      configured_count: 2,
      completed_count: 2,
      providers: [
        {
          provider: "catalog-fast",
          status: "success",
          result_count: results.length,
          reason: null,
          retry_at: null,
          elapsed_ms: 2,
        },
        {
          provider: "catalog-slow",
          status: "empty",
          result_count: 0,
          reason: null,
          retry_at: null,
          elapsed_ms: 900,
        },
      ],
    },
    results,
  };
}

let observation: ReturnType<typeof running>;
let releaseSearch: (() => void) | undefined;
let downloads: DownloadRequest[];
let save: ReturnType<typeof vi.spyOn>;

// The search is a job. It is queued at once and keeps running, publishing what
// it has so far as its result, until the test releases it with the snapshot it
// ends on. A look at a job can be held, which is how an answer of a search the
// reader has already left is made to land after they left it.
interface LiveJob {
  snapshot: unknown;
  released: boolean;
}
let liveJobs: Map<number, LiveJob>;
let holdLook: ((jobId: number) => Promise<void> | undefined) | undefined;
let queued: ((jobId: number, release: () => void) => void) | undefined;

function liveSearch(snapshot: () => unknown) {
  return [
    http.post("/api/discover/search", () => {
      const jobId = 700 + liveJobs.size;
      const job: LiveJob = { snapshot: snapshot(), released: false };
      liveJobs.set(jobId, job);
      queued?.(jobId, () => {
        job.released = true;
      });
      return HttpResponse.json(
        { job_id: jobId, session: "run-live" },
        { status: 202 },
      );
    }),
    http.get("/api/system/jobs/result", async ({ request }) => {
      const jobId = Number(new URL(request.url).searchParams.get("id"));
      const job = liveJobs.get(jobId);
      if (job === undefined) return new HttpResponse(null, { status: 404 });
      const held = await holdLook?.(jobId);
      const done = job.released && held === undefined;
      return HttpResponse.json({
        job_id: jobId,
        session: "run-live",
        status: done ? "completed" : "running",
        error: null,
        result: done
          ? {
              phase: "finished",
              providers: [],
              snapshot: job.snapshot,
            }
          : (heldObservation ?? observation),
      });
    }),
  ];
}
let heldObservation: ReturnType<typeof running> | undefined;

function renderDiscover() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      { path: "/subtitle-hub", element: <Link to="/discover">Back</Link> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { user: userEvent.setup() };
}

async function startSearch(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Search"), "Matrix");
  await user.click(
    await screen.findByRole("button", { name: "The Matrix (1999)" }),
  );
  await screen.findByRole("heading", { name: "The Matrix" });
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093"),
  );
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(releaseSearch).toBeDefined());
}

function releases() {
  return screen
    .getAllByRole("heading", { level: 3 })
    .map((heading) => heading.textContent);
}

beforeEach(() => {
  localStorage.clear();
  observation = running([]);
  releaseSearch = undefined;
  liveJobs = new Map();
  holdLook = undefined;
  heldObservation = undefined;
  queued = (_jobId, release) => {
    releaseSearch = release;
  };
  downloads = [];
  save = vi.spyOn(files, "saveBlobAs").mockImplementation(() => undefined);
  save.mockClear();
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: true,
          metadata_revision: "metadata-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "English", code2: "en", code3: "eng", enabled: false },
        { name: "Hungarian", code2: "hu", code3: "hun", enabled: true },
      ]),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: envelope }),
    ),
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({ data: { ...envelope, items: [movie] } }),
    ),
    http.get("/api/discover/metadata/movies/11", () =>
      HttpResponse.json({ data: { ...envelope, item: movie } }),
    ),
    ...downloadJobHandlers({
      body: () => srt,
      filename: () => "live.en.srt",
      requests: downloads,
    }),
  );
});

it("shows each result as it lands and never reorders what is already read", async () => {
  server.use(...liveSearch(() => finished([row("live-1", "First.Release")])));
  const { user } = renderDiscover();
  await startSearch(user);
  expect(
    screen.getByText("No results yet. Providers are still searching."),
  ).toBeVisible();

  observation = running([row("live-1", "First.Release")]);
  expect(
    await screen.findByRole(
      "heading",
      { name: "First.Release" },
      { timeout: 5000 },
    ),
  ).toBeVisible();
  // The bar stays up beside the rows, so the list reads as still growing.
  expect(
    screen.getByRole("progressbar", { name: "Providers checked" }),
  ).toBeInTheDocument();
  expect(screen.getByText(/1 subtitle result so far/)).toBeVisible();

  // A second provider answers. The row already on the page keeps its place.
  observation = running([
    row("live-1", "First.Release"),
    row("live-2", "Second.Release"),
  ]);
  await screen.findByRole(
    "heading",
    { name: "Second.Release" },
    { timeout: 5000 },
  );
  expect(releases()).toEqual(["First.Release", "Second.Release"]);

  releaseSearch?.();
  await waitFor(() =>
    expect(
      screen.queryByRole("progressbar", { name: "Providers checked" }),
    ).toBeNull(),
  );
  expect(releases()).toEqual(["First.Release"]);
});

it("downloads a row the running search has already published", async () => {
  server.use(...liveSearch(() => finished([row("live-1", "First.Release")])));
  const { user } = renderDiscover();
  await startSearch(user);
  observation = running([row("live-1", "First.Release")]);
  await screen.findByRole(
    "heading",
    { name: "First.Release" },
    { timeout: 5000 },
  );
  await user.click(screen.getByRole("button", { name: "Download SRT" }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(downloads[0]).toMatchObject({
    result: "live-1",
    search: "search-1",
  });
  releaseSearch?.();
});

it("never renders partial results of a superseded search", async () => {
  const releases: (() => void)[] = [];
  let firstJobId: number | undefined;
  let releaseStaleLook: (() => void) | undefined;
  let staleLookReleased = false;
  queued = (jobId, release) => {
    firstJobId ??= jobId;
    releases.push(release);
    releaseSearch = release;
  };
  // The first search's look is held open across the change of language, so its
  // answer lands while the replacement search is the live one.
  holdLook = (jobId) => {
    if (jobId !== firstJobId || staleLookReleased) return undefined;
    staleLookReleased = true;
    heldObservation = running([row("live-9", "Superseded.Release")]);
    return new Promise<void>((resolve) => {
      releaseStaleLook = resolve;
    });
  };
  server.use(
    ...liveSearch(() => finished([row("live-9", "Superseded.Release")])),
  );
  const { user } = renderDiscover();
  await startSearch(user);
  await waitFor(() => expect(releaseStaleLook).toBeDefined(), {
    timeout: 5000,
  });

  // The reader changes the language and searches again. The first search is
  // still running on the server and its look is still open.
  await pickOption(user, "Subtitle language", "Hungarian");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(releases).toHaveLength(2));
  expect(
    await screen.findByText("No results yet. Providers are still searching."),
  ).toBeVisible();

  heldObservation = undefined;
  releaseStaleLook?.();
  // The replacement search publishes its own row, which is proof the look at
  // the live search is landing and the assertion below is not just early.
  observation = running([row("live-10", "Replacement.Release")]);
  await screen.findByRole(
    "heading",
    { name: "Replacement.Release" },
    { timeout: 5000 },
  );
  expect(screen.queryByText("Superseded.Release")).toBeNull();
  releases.forEach((release) => release());
  await waitFor(() =>
    expect(screen.queryByText("Superseded.Release")).toBeNull(),
  );
});

it("tells nothing yet apart from nothing found", async () => {
  server.use(...liveSearch(() => finished([])));
  const { user } = renderDiscover();
  await startSearch(user);
  expect(
    screen.getByText("No results yet. Providers are still searching."),
  ).toBeVisible();
  expect(
    screen.queryByText(/No subtitles matched this title and language/),
  ).toBeNull();
  releaseSearch?.();
  expect(
    await screen.findByText(/No subtitles matched this title and language/),
  ).toBeVisible();
  expect(
    screen.queryByText("No results yet. Providers are still searching."),
  ).toBeNull();
});
