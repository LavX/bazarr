/* eslint-disable camelcase -- API fixture fields keep their transport names. */
/// <reference types="node" />
import { createMemoryRouter, RouterProvider } from "react-router";
import { http, HttpResponse } from "msw";
import { readFileSync } from "node:fs";
import { beforeEach, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import LibraryHero from "./LibraryHero";

const discoverStyles = readFileSync(
  "src/pages/Discover/Discover.module.scss",
  "utf8",
);

/**
 * The block that restyles library-hero descendants when the panel has no
 * backdrop. jsdom does not apply this module's rules to computed styles, so
 * the stylesheet itself is what we can assert: cream over artwork stays
 * elsewhere, and this block has to exist for the three copy-only descendants
 * to take page ink on a light card.
 */
function noArtworkHeroBlock(source: string): string {
  const marker = '.libraryFeature[data-artwork="false"]';
  const start = source.indexOf(marker);
  if (start < 0) return "";
  let depth = 0;
  let started = false;
  for (let i = start; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === "{") {
      depth += 1;
      started = true;
    } else if (ch === "}") {
      depth -= 1;
      if (started && depth === 0) return source.slice(start, i + 1);
    }
  }
  return "";
}

const quietActivity = {
  availability: "available",
  observed_at: "2026-09-01T12:00:00Z",
  complete: true,
  truncated: false,
  unknown_sources: [],
  running_count: 0,
  queued_count: 0,
  scheduled_count: 24,
  running: [],
  queued: [],
  scheduled: [
    {
      job_id: "update_series",
      name: "Sync series",
      interval: "every hour",
      next_run_in: "in 12 minutes",
    },
  ],
};

const countedLibrary = {
  availability: "available",
  observed_at: "2026-09-01T12:00:00Z",
  complete: true,
  series: 312,
  movies: 1265,
  episodes: 9481,
  subtitles_fetched: 5207,
};

function summary(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-09-01T12:00:00Z",
    state: "quiet",
    activity: quietActivity,
    library: countedLibrary,
    wanted: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      requirements: 185,
      episode_requirements: 176,
      movie_requirements: 9,
      media_count: 146,
      episode_media_count: 140,
      movie_media_count: 6,
      unknown_media_count: 0,
      complete: true,
      qualifications: [],
      by_instance: [],
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

function arrival(overrides: Record<string, unknown> = {}) {
  return {
    kind: "episode",
    event_id: "episode:1",
    status: "success",
    action: 1,
    title: "Northern Light",
    poster_url: "/images/series/MediaCover/1/poster-250.jpg",
    backdrop_url: "/images/series/MediaCover/1/fanart.jpg",
    library_id: 1,
    season: 2,
    episode: 5,
    episode_title: "Home",
    language: "hu",
    provider: "opensubtitles",
    arr_instance_id: 1,
    instance_name: "Sonarr",
    timestamp: "2026-09-01T11:00:00+00:00",
    ...overrides,
  };
}

let served: Record<string, unknown> = summary();
let sportarr = false;
let sportsTotal = 0;

beforeEach(() => {
  served = summary();
  sportarr = false;
  sportsTotal = 0;
  server.use(
    http.get("/api/discover/summary", () => HttpResponse.json(served)),
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", use_sportarr: sportarr },
      }),
    ),
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [], total: sportsTotal }),
    ),
  );
});

function render() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <LibraryHero /> },
      { path: "/wanted/series", element: <div>Wanted series page</div> },
      { path: "/system/tasks", element: <div>Tasks page</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

/** The figure standing under one stat's label, not merely somewhere near it. */
async function statFor(label: string) {
  const labelled = await screen.findByText(label);
  // Direct node access on purpose: pairing a figure with its own label is the
  // thing under test, and no query expresses "the figure in this one stat".

  const value = labelled.parentElement?.querySelector("strong");
  if (!value) throw new Error(`No figure paired with the label ${label}`);
  return value;
}

it("counts the library with each figure under its own label", async () => {
  render();
  // The labels are painted before the counts arrive, so finding a label proves
  // only that the tile exists. Wait for the figure itself to be the read one.
  await waitFor(async () =>
    expect(await statFor("Series")).toHaveTextContent("312"),
  );
  expect(await statFor("Movies")).toHaveTextContent("1,265");
  expect(await statFor("Episodes")).toHaveTextContent("9,481");
  expect(await statFor("Subtitles fetched, all time")).toHaveTextContent(
    "5,207",
  );
});

it("counts sports only where Sportarr is switched on", async () => {
  served = summary({
    library: { ...countedLibrary, sports_leagues: 2, sports_events: 3 },
  });
  render();
  await waitFor(async () =>
    expect(await statFor("Sports events")).toHaveTextContent("3"),
  );
});

it("shows no sports tile at all rather than a zero", async () => {
  render();
  // The count is absent, not zero, when Sportarr is off or not in this build.
  // A "0 sports" tile would invite a reader to go looking for a feature they
  // have not turned on.
  await waitFor(async () =>
    expect(await statFor("Series")).toHaveTextContent("312"),
  );
  expect(screen.queryByText("Sports events")).toBeNull();
});

it("says a count is unknown rather than drawing it as none", async () => {
  served = summary({
    library: {
      ...countedLibrary,
      availability: "unknown",
      complete: false,
      series: null,
      movies: null,
      episodes: null,
      subtitles_fetched: null,
    },
  });
  render();
  // Zero work and no answer are different facts, and only one is reassuring.
  // The pending state also reads Unknown, so wait for the read to land first.
  expect(
    await screen.findByText("Next: Sync series in 12 minutes"),
  ).toBeInTheDocument();
  expect(await statFor("Series")).toHaveTextContent("Unknown");
  expect(await statFor("Series")).not.toHaveTextContent("0");
});

it("names the job that is running rather than counting it", async () => {
  served = summary({
    state: "busy",
    activity: {
      ...quietActivity,
      running_count: 3,
      running: [
        { activity_id: "a", name: "Translating Northern Light (EN to HU)" },
        { activity_id: "b", name: "Downloading Dune" },
      ],
    },
  });
  render();
  expect(
    await screen.findByText("Translating Northern Light (EN to HU) and 2 more"),
  ).toBeInTheDocument();
});

it("reports waiting work when nothing has started yet", async () => {
  served = summary({
    activity: { ...quietActivity, running_count: 0, queued_count: 1 },
  });
  render();
  expect(
    await screen.findByText("1 subtitle job waiting to start"),
  ).toBeInTheDocument();
});

it("surfaces findings when the queue is idle but something is wrong", async () => {
  served = summary({
    attention: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      unknown_sources: [],
      items: [{ id: "x" }, { id: "y" }],
    },
  });
  render();
  expect(
    await screen.findByText("2 things need attention"),
  ).toBeInTheDocument();
});

it("admits an unreadable queue instead of calling it quiet", async () => {
  served = summary({
    activity: {
      ...quietActivity,
      availability: "unknown",
      running_count: null,
      queued_count: null,
      scheduled_count: null,
    },
  });
  render();
  expect(
    await screen.findByText("Local activity could not be read."),
  ).toBeInTheDocument();
  expect(
    screen.getByText("Scheduled work could not be read."),
  ).toBeInTheDocument();
});

it("never calls a job that will not run again the next one", async () => {
  served = summary({
    activity: {
      ...quietActivity,
      scheduled_count: 2,
      scheduled: [
        {
          job_id: "backup",
          name: "Backup Database",
          interval: "manually",
          next_run_in: "Never",
        },
        {
          job_id: "update_series",
          name: "Sync series",
          interval: "every hour",
          next_run_in: "in 12 minutes",
        },
      ],
    },
  });
  render();
  // The list is not ordered by next run, so the first entry is not the next
  // job, and "next: Backup Database Never" would be a sentence that is false.
  expect(
    await screen.findByText("Next: Sync series in 12 minutes"),
  ).toBeInTheDocument();
});

it("reports only the count when nothing is scheduled to run again", async () => {
  served = summary({
    activity: {
      ...quietActivity,
      scheduled_count: 1,
      scheduled: [
        {
          job_id: "backup",
          name: "Backup Database",
          interval: "manually",
          next_run_in: "Never",
        },
      ],
    },
  });
  render();
  expect(await screen.findByText("1 scheduled job")).toBeInTheDocument();
});

it("states the queue before offering the way into it", async () => {
  render();
  const schedule = await screen.findByText("Next: Sync series in 12 minutes");
  const allJobs = screen.getByRole("link", { name: /all jobs/i });
  // Reading order is the subject: a link ahead of the fact reads as a heading
  // for something it does not describe.
  expect(
    schedule.compareDocumentPosition(allJobs) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
});

it("says what is scheduled next and when", async () => {
  render();
  expect(
    await screen.findByText("Next: Sync series in 12 minutes"),
  ).toBeInTheDocument();
});

it("dresses itself in the most recent thing this library actually fetched", async () => {
  served = summary({ arrivals: [arrival()] });
  render();
  const hero = await screen.findByRole("region", { name: /your library/i });
  await waitFor(() =>
    expect(
      within(hero).getByRole("presentation", { hidden: true }),
    ).toHaveAttribute("src", "/images/series/MediaCover/1/fanart.jpg"),
  );
});

it("gives each arrival's cover a turn, ten seconds apart", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  served = summary({
    arrivals: [
      arrival(),
      arrival({
        event_id: "episode:2",
        backdrop_url: "/images/series/MediaCover/2/fanart.jpg",
      }),
    ],
  });
  render();
  const hero = await screen.findByRole("region", { name: /your library/i });
  const cover = () =>
    within(hero)
      .getByRole("presentation", { hidden: true })
      .getAttribute("src");
  await waitFor(() =>
    expect(cover()).toBe("/images/series/MediaCover/1/fanart.jpg"),
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(cover()).toBe("/images/series/MediaCover/2/fanart.jpg");
  // And back round, rather than stopping on the last one.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(cover()).toBe("/images/series/MediaCover/1/fanart.jpg");
  vi.useRealTimers();
});

it("holds still when there is only one cover to show", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  served = summary({ arrivals: [arrival()] });
  render();
  const hero = await screen.findByRole("region", { name: /your library/i });
  const cover = () =>
    within(hero)
      .getByRole("presentation", { hidden: true })
      .getAttribute("src");
  await waitFor(() =>
    expect(cover()).toBe("/images/series/MediaCover/1/fanart.jpg"),
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(cover()).toBe("/images/series/MediaCover/1/fanart.jpg");
  vi.useRealTimers();
});

it("keeps its shape when nothing in the library has artwork", async () => {
  served = summary({ arrivals: [arrival({ backdrop_url: null })] });
  render();
  const hero = await screen.findByRole("region", { name: /your library/i });
  expect(hero).toHaveAttribute("data-artwork", "false");
});

it("paints the status, schedule and jobs link in page ink when there is no artwork", async () => {
  served = summary({ arrivals: [arrival({ backdrop_url: null })] });
  render();
  const hero = await screen.findByRole("region", { name: /your library/i });
  const panel = hero.querySelector("article");
  if (!panel) throw new Error("Library hero has no panel");
  expect(panel).toHaveAttribute("data-artwork", "false");

  const status = await screen.findByText(/subtitle jobs idle · last fetched/i);
  const schedule = screen.getByText("Next: Sync series in 12 minutes");
  const allJobs = screen.getByRole("link", { name: /all jobs/i });
  expect(panel).toContainElement(status);
  expect(panel).toContainElement(schedule);
  expect(panel).toContainElement(allJobs);

  // jsdom does not apply this CSS module to computed styles (styleSheets
  // stay empty for it, getComputedStyle cannot see the cream). The
  // stylesheet is the thing that has to change: the no-artwork panel must
  // restyle these three to page ink, because their cream is more specific
  // than the panel rule and vanishes them on a light card.
  const block = noArtworkHeroBlock(discoverStyles);
  expect(block).toMatch(/\.heroStatus[\s\S]*--bz-text-primary/);
  expect(block).toMatch(/\.heroStatus[\s\S]*background/);
  expect(block).toMatch(/\.heroSchedule[\s\S]*--bz-text-secondary/);
  expect(block).toMatch(/\.heroActions[\s\S]*--bz-text-secondary/);
  expect(block).not.toMatch(/#fff8f0/);
});

it("counts media items, not language requirements, per kind", async () => {
  render();
  // Media items, not language requirements: 140 episodes need 176 subtitles,
  // and calling the larger figure "episodes" overstated the work by a third.
  expect(
    await screen.findByRole("link", { name: /140 episodes need subtitles/i }),
  ).toHaveAttribute("href", "/wanted/series");
  expect(
    screen.getByRole("link", { name: /6 movies need subtitles/i }),
  ).toHaveAttribute("href", "/wanted/movies");
  expect(screen.getByRole("link", { name: /all jobs/i })).toHaveAttribute(
    "href",
    "/system/tasks",
  );
});

it("offers sports the same control as the other kinds", async () => {
  sportarr = true;
  sportsTotal = 3;
  render();
  // Every kind is the same sort of thing: a queue of work with a page behind
  // it, so none of them gets a quieter control than the others.
  const missing = await screen.findByRole("link", {
    name: /3 sports events need subtitles/i,
  });
  expect(missing).toHaveAttribute("href", "/wanted/sports");
  const episodes = screen.getByRole("link", {
    name: /140 episodes need subtitles/i,
  });
  expect(missing.className).toBe(episodes.className);
  expect(
    screen.getByRole("link", { name: /6 movies need subtitles/i }).className,
  ).toBe(episodes.className);
});

it("says nothing about sports where Sportarr is not configured", async () => {
  render();
  expect(
    await screen.findByRole("link", { name: /140 episodes need subtitles/i }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /sports event/i })).toBeNull();
});

it("stops counting sports once Sportarr is switched off", async () => {
  sportarr = true;
  sportsTotal = 3;
  render();
  expect(
    await screen.findByRole("link", {
      name: /3 sports events need subtitles/i,
    }),
  ).toBeInTheDocument();
  sportarr = false;
  await act(async () => {
    await queryClient.invalidateQueries({
      queryKey: [QueryKeys.System, QueryKeys.Settings],
    });
  });
  // The count query stops, but its last answer stays cached, and the link
  // would open a Wanted page the navigation no longer offers.
  await waitFor(() =>
    expect(screen.queryByRole("link", { name: /sports event/i })).toBeNull(),
  );
});

it("drops a configured kind that has nothing left to do", async () => {
  sportarr = true;
  sportsTotal = 0;
  render();
  expect(
    await screen.findByRole("link", { name: /140 episodes need subtitles/i }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /sports event/i })).toBeNull();
});

it("leads with the larger queue and drops a kind with nothing missing", async () => {
  served = summary({
    wanted: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      requirements: 4,
      episode_requirements: 0,
      movie_requirements: 4,
      media_count: 4,
      episode_media_count: 0,
      movie_media_count: 3,
      unknown_media_count: 0,
      complete: true,
      qualifications: [],
      by_instance: [],
    },
  });
  render();
  expect(
    await screen.findByRole("link", { name: /3 movies need subtitles/i }),
  ).toHaveAttribute("href", "/wanted/movies");
  expect(screen.queryByRole("link", { name: /episodes need/i })).toBeNull();
});

it("says when it last did something rather than leading with an absence", async () => {
  served = summary({ arrivals: [arrival()] });
  render();
  // An idle install's most interesting status is the last thing it fetched.
  expect(
    await screen.findByText(/subtitle jobs idle · last fetched/i),
  ).toBeInTheDocument();
});

it("still reports idle when it has never fetched anything", async () => {
  render();
  expect(await screen.findByText("Subtitle jobs idle")).toBeInTheDocument();
});

// A 200 is not a promise that the body is a summary. A reverse proxy's sign-in
// page, an authentication message and the SPA's own index.html all arrive as a
// truthy body with none of the summary's components in it, and reaching into
// one of those took the whole page down with "Cannot read properties of
// undefined (reading 'availability')".
it("says local activity is unreadable when the answer is not a summary", async () => {
  served = {
    message:
      "The server could not verify that you are authorized to access the URL requested.",
  };
  render();
  expect(
    await screen.findByText("Local activity could not be read."),
  ).toBeInTheDocument();
  // Counts stay unknown rather than being drawn as a reassuring zero.
  await waitFor(async () =>
    expect(await statFor("Series")).toHaveTextContent("Unknown"),
  );
});

it("renders a summary whose activity is missing the lists it usually carries", async () => {
  served = summary({
    activity: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      truncated: false,
      unknown_sources: [],
      running_count: 0,
      queued_count: 0,
      scheduled_count: null,
    },
  });
  render();
  expect(await screen.findByText("Subtitle jobs idle")).toBeInTheDocument();
});
