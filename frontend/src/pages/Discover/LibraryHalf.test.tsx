/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { AllProviders } from "@/providers";
import { rawRender, screen } from "@/tests";
import server from "@/tests/mocks/node";
import Discover from "./testHarness";

// Whether the page opens with the reader's own library or with the world's.
//
// An install with no enabled Sonarr or Radarr has nothing behind the library
// half, and four panels each reporting that in their own words is a worse
// answer than the catalog the page can actually fill. An install that has one
// and has not finished indexing keeps the half: the hero is what tells the
// reader a scan is running.

const envelope = {
  source: "tmdb",
  status: "available",
  configured: true,
  revision: "feed-one",
  locale: "en-US",
};

// One episode still missing a subtitle, so "Still missing" is a section that
// would render if the library half were shown. Without it that heading is
// absent on its own terms and asserting its absence proves nothing.
const missingEpisode = {
  id: 701,
  series_id: 801,
  sonarrSeriesId: 101,
  sonarrEpisodeId: 201,
  arr_instance_id: 1,
  seriesTitle: "Northern Light",
  episode_number: "1x1",
  episodeTitle: "Pilot",
  missing_subtitles: [
    { name: "Hungarian", code2: "hu", code3: "hun", forced: false, hi: false },
  ],
  audio_language: [],
  monitored: true,
  tags: [],
  sceneName: null,
  hearing_impaired: false,
  seriesType: "standard",
};

const libraryOnboarding = {
  id: "library",
  summary:
    "No Sonarr or Radarr instance is connected yet. Discover works without one.",
  target: "/settings/connections",
};

function summary(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-09-01T12:00:00Z",
    state: "quiet",
    query_budget: 12,
    activity: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      truncated: false,
      unknown_sources: [],
      running_count: 0,
      queued_count: 0,
      scheduled_count: 0,
      running: [],
      queued: [],
      scheduled: [],
    },
    wanted: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      requirements: 0,
      episode_requirements: 0,
      movie_requirements: 0,
      media_count: 0,
      unknown_media_count: 0,
      complete: true,
      qualifications: [],
      by_instance: [],
    },
    library: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      series: 0,
      movies: 0,
      episodes: 0,
      subtitles_fetched: 0,
    },
    arrivals: [],
    arrivals_status: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      truncated: false,
      candidate_limit: 25,
      display_limit: 4,
      qualifications: [],
    },
    attention: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      unknown_sources: [],
      items: [],
    },
    onboarding: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      items: [],
    },
    ...overrides,
  };
}

function serve(
  general: Record<string, unknown>,
  body: Record<string, unknown> | "unreadable",
) {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", ...general },
        discover: {
          tmdb_configured: true,
          metadata_revision: "feed-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/system/languages/profiles", () => HttpResponse.json([])),
    http.get("/api/episodes/wanted", () =>
      HttpResponse.json({ data: [missingEpisode], total: 1 }),
    ),
    http.get("/api/movies/wanted", () =>
      HttpResponse.json({ data: [], total: 0 }),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: envelope }),
    ),
    http.get("/api/discover/summary", () =>
      body === "unreadable"
        ? HttpResponse.json({
            message:
              "The server could not verify that you are authorized to access the URL requested.",
          })
        : HttpResponse.json(body),
    ),
  );
}

function open() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      { path: "/settings/connections", element: <div>Connections</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

beforeEach(() => {
  localStorage.clear();
  queryClient.clear();
});

/** The global half's own hero, which must be what opens the page. */
async function globalHero() {
  return screen.findByRole("heading", { name: "Beyond your library" });
}

it("opens on the global catalog when no arr instance is enabled", async () => {
  serve(
    { use_sonarr: false, use_radarr: false },
    summary({
      state: "new_installation",
      onboarding: {
        availability: "available",
        observed_at: "2026-09-01T12:00:00Z",
        complete: true,
        items: [libraryOnboarding],
      },
    }),
  );
  open();
  await globalHero();
  expect(screen.queryByText("Your library")).not.toBeInTheDocument();
  expect(screen.queryByText("Recently fetched")).not.toBeInTheDocument();
  expect(screen.queryByText("Needs attention")).not.toBeInTheDocument();
  expect(screen.queryByText("Still missing")).not.toBeInTheDocument();
  // One line in place of the half, carrying the summary's own wording.
  expect(
    await screen.findByRole("link", { name: "Connect a library" }),
  ).toHaveAttribute("href", "/settings/connections");
});

it("hides the half for an enabled integration whose instances are all off", async () => {
  serve(
    { use_sonarr: true },
    summary({
      onboarding: {
        availability: "available",
        observed_at: "2026-09-01T12:00:00Z",
        complete: true,
        items: [libraryOnboarding],
      },
    }),
  );
  open();
  await globalHero();
  expect(
    await screen.findByRole("link", { name: "Connect a library" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("Your library")).not.toBeInTheDocument();
  // Sonarr is on and an episode is missing a subtitle, so this heading is one
  // the page would have rendered had the half been shown.
  expect(screen.queryByText("Still missing")).not.toBeInTheDocument();
});

it("keeps the half for a connected library that has indexed nothing yet", async () => {
  serve({ use_sonarr: true }, summary());
  open();
  expect(await screen.findByText("Your library")).toBeInTheDocument();
  expect(await screen.findByText("Recently fetched")).toBeInTheDocument();
  // The other side of the assertion the two hidden cases make: this is the
  // section whose absence they are pinning.
  expect(await screen.findByText("Still missing")).toBeInTheDocument();
  await globalHero();
  expect(
    screen.queryByRole("link", { name: "Connect a library" }),
  ).not.toBeInTheDocument();
});

it("opens on the global catalog when the summary cannot be read", async () => {
  serve({ use_sonarr: true }, "unreadable");
  open();
  await globalHero();
  // The check that did not happen still gets said, and nothing crashes.
  expect(
    await screen.findByText("This check could not be completed."),
  ).toBeInTheDocument();
  expect(screen.queryByText("Your library")).not.toBeInTheDocument();
  expect(screen.queryByText("Recently fetched")).not.toBeInTheDocument();
  expect(screen.queryByText("Still missing")).not.toBeInTheDocument();
});
