/* eslint-disable camelcase -- API fixture fields keep their transport names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import WantedQueue from "./WantedQueue";

function summary(requirements: number, mediaCount: number) {
  return {
    generated_at: "2026-09-01T12:00:00Z",
    state: "quiet",
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
      requirements,
      // Split across both kinds, because the strip renders both and the
      // header links are driven by which kinds actually have something left.
      episode_requirements: requirements - 1,
      movie_requirements: 1,
      episode_media_count: mediaCount - 1,
      movie_media_count: 1,
      media_count: mediaCount,
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
  };
}

const hungarian = {
  name: "Hungarian",
  code2: "hu",
  code3: "hun",
  forced: false,
  hi: false,
};

function episode(id: number, seriesTitle: string) {
  return {
    sonarrSeriesId: 100 + id,
    sonarrEpisodeId: 200 + id,
    arr_instance_id: 4,
    seriesTitle,
    episode_number: "1x1",
    episodeTitle: "Pilot",
    missing_subtitles: [hungarian],
    audio_language: [],
    monitored: true,
    tags: [],
    sceneName: null,
    hearing_impaired: false,
    seriesType: "standard",
  };
}

function movie(id: number, title: string) {
  return {
    radarrId: 300 + id,
    arr_instance_id: 2,
    title,
    missing_subtitles: [hungarian],
    audio_language: [],
    monitored: true,
    tags: [],
    sceneName: null,
    hearing_impaired: false,
  };
}

function sportsEvent(id: number, title: string) {
  return {
    id,
    title,
    league_id: 7,
    partName: null,
    // Sportarr reports bare codes, not the objects the other two endpoints
    // return, so the component has to name them itself.
    missing_subtitles: ["hu", "en"],
    arr_instance_id: 5,
  };
}

let episodes: unknown[] = [];
let movies: unknown[] = [];
let sports: unknown[] = [];
let connections = {
  use_sonarr: true,
  use_radarr: true,
  use_sportarr: false,
};
let patched: URL[] = [];

beforeEach(() => {
  episodes = [episode(1, "Northern Light")];
  movies = [movie(1, "Child of God")];
  sports = [sportsEvent(1, "Italian Grand Prix")];
  connections = { use_sonarr: true, use_radarr: true, use_sportarr: false };
  patched = [];
  server.use(
    http.get("/api/discover/summary", () => HttpResponse.json(summary(9, 4))),
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { theme: "auto", ...connections } }),
    ),
    http.get("/api/episodes/wanted", () =>
      HttpResponse.json({ data: episodes, total: episodes.length }),
    ),
    http.get("/api/movies/wanted", () =>
      HttpResponse.json({ data: movies, total: movies.length }),
    ),
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: sports, total: sports.length }),
    ),
    http.patch("/api/episodes/subtitles", ({ request }) => {
      patched.push(new URL(request.url));
      return new HttpResponse(null, { status: 204 });
    }),
    http.patch("/api/movies/subtitles", ({ request }) => {
      patched.push(new URL(request.url));
      return new HttpResponse(null, { status: 204 });
    }),
  );
});

function render() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <WantedQueue /> },
      { path: "/wanted/series", element: <div>Wanted series page</div> },
      { path: "/series/:id", element: <div>Series detail page</div> },
      { path: "/movies/:id", element: <div>Movie detail page</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

it("shows what is missing with the count behind it", async () => {
  render();
  expect(await screen.findByText("Northern Light")).toBeInTheDocument();
  // Sonarr says "1x1" and the history strip says "S01E01"; one page should
  // not spell the same thing two ways.
  expect(screen.getByText("S01E01 · Pilot")).toBeInTheDocument();
  expect(screen.getByText("Child of God")).toBeInTheDocument();
  // Both units named: an item can need several languages, so the larger figure
  // is subtitles and the smaller one is media items.
  expect(
    screen.getByText("9 subtitles missing across 4 episodes and movies"),
  ).toBeInTheDocument();
});

it("starts the search for that exact episode on the instance that owns it", async () => {
  render();
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("button", {
      name: "Search providers for Hungarian subtitles for Northern Light",
    }),
  );
  await waitFor(() => expect(patched).toHaveLength(1));
  expect(patched[0].pathname).toBe("/api/episodes/subtitles");
  expect(patched[0].searchParams.get("seriesid")).toBe("101");
  expect(patched[0].searchParams.get("episodeid")).toBe("201");
  // Routing to the owning instance is the whole point of #156: a search sent
  // to the default Sonarr would look for a file that server has never heard of.
  expect(patched[0].searchParams.get("arr_instance_id")).toBe("4");
});

it("starts the search for a movie against its own instance", async () => {
  render();
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("button", {
      name: "Search providers for Hungarian subtitles for Child of God",
    }),
  );
  await waitFor(() => expect(patched).toHaveLength(1));
  expect(patched[0].pathname).toBe("/api/movies/subtitles");
  expect(patched[0].searchParams.get("radarrid")).toBe("301");
  expect(patched[0].searchParams.get("arr_instance_id")).toBe("2");
});

it("keeps films visible when episodes could fill the list on their own", async () => {
  episodes = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => episode(n, `Series ${n}`));
  movies = [movie(1, "Child of God")];
  render();
  // Concatenating would bury the single film under eight episodes and show
  // none of it, which is exactly the install this is most useful on.
  expect(await screen.findByText("Child of God")).toBeInTheDocument();
});

it("says which group is which and where each one continues", async () => {
  render();
  // The heading and its link render while the queue is still being read, so
  // the count has to be waited for rather than asserted beside the link.
  // Two independent previews, each with its own heading and its own link, so
  // neither the grouping nor the destination has to be inferred from content.
  expect(
    await screen.findByRole("heading", { name: "Episodes" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Movies" })).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: /all missing episodes/i }),
  ).toHaveAttribute("href", "/wanted/series");
  expect(
    screen.getByRole("link", { name: /all missing movies/i }),
  ).toHaveAttribute("href", "/wanted/movies");
});

it("shows no group at all for a kind with nothing missing", async () => {
  server.use(
    http.get("/api/discover/summary", () =>
      HttpResponse.json({
        ...summary(9, 4),
        wanted: {
          availability: "available",
          observed_at: "2026-09-01T12:00:00Z",
          requirements: 9,
          episode_requirements: 9,
          movie_requirements: 0,
          media_count: 4,
          unknown_media_count: 0,
          complete: true,
          qualifications: [],
          by_instance: [],
        },
      }),
    ),
  );
  movies = [];
  render();
  expect(
    await screen.findByRole("link", { name: /all missing episodes/i }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Movies" })).toBeNull();
});

it("adds a sports group only where Sportarr is configured", async () => {
  connections = { use_sonarr: true, use_radarr: true, use_sportarr: true };
  render();
  expect(
    await screen.findByRole("heading", { name: "Sports" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Italian Grand Prix")).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: /all missing sports/i }),
  ).toHaveAttribute("href", "/wanted/sports");
  // Three configured libraries, three groups: the section follows what is
  // actually connected rather than a fixed pair.
  expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(3);
});

it("never shows a sports group when Sportarr is off", async () => {
  render();
  expect(
    await screen.findByRole("heading", { name: "Episodes" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Sports" })).toBeNull();
  expect(screen.queryByText("Italian Grand Prix")).toBeNull();
});

it("shows sports languages without pretending they start a search", async () => {
  connections = { use_sonarr: false, use_radarr: false, use_sportarr: true };
  render();
  // Sportarr's per-event endpoint returns candidates to choose between, so a
  // control shaped like the series pills would promise an action that never
  // happens. The languages are stated, and the row links to where to act.
  expect(await screen.findByText("Hungarian")).toBeInTheDocument();
  expect(screen.getByText("English")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /search .* hungarian/i }),
  ).toBeNull();
  expect(
    screen.getByRole("link", { name: "Italian Grand Prix" }),
  ).toHaveAttribute("href", "/sports/7");
});

it("stays away entirely when no library is connected", async () => {
  connections = { use_sonarr: false, use_radarr: false, use_sportarr: false };
  render();
  await waitFor(() =>
    expect(
      screen.queryByRole("heading", { name: /still missing/i }),
    ).toBeNull(),
  );
});
