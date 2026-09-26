/* eslint-disable camelcase -- API fixture fields keep their transport names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import { AllProviders } from "@/providers";
import { rawRender, screen } from "@/tests";
import server from "@/tests/mocks/node";
import LibraryActivity from "./LibraryActivity";

function summary(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-09-01T12:00:00Z",
    state: "new_installation",
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

let served: Record<string, unknown> = summary();

beforeEach(() => {
  served = summary();
  server.use(
    http.get("/api/discover/summary", () => HttpResponse.json(served)),
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { theme: "auto" } }),
    ),
  );
});

function render() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <LibraryActivity /> },
      { path: "/history/series", element: <div>Series history page</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

it("says nothing has arrived yet on an install with no library", async () => {
  render();
  expect(
    await screen.findByText("Your next fetched subtitles will appear here."),
  ).toBeInTheDocument();
});

// The strip reads three separate components out of the summary, and a body
// that carries none of them used to be dereferenced as though it did.
it("reports activity unavailable when the answer is not a summary", async () => {
  served = {
    message:
      "The server could not verify that you are authorized to access the URL requested.",
  };
  render();
  expect(
    await screen.findByText("Library activity is temporarily unavailable."),
  ).toBeInTheDocument();
});

it("renders a summary that arrived without its arrivals status or onboarding", async () => {
  const partial: Record<string, unknown> = summary();
  delete partial.arrivals_status;
  delete partial.onboarding;
  served = partial;
  render();
  expect(
    await screen.findByText("Library activity is temporarily unavailable."),
  ).toBeInTheDocument();
});

it("links a sports arrival to its league on the instance that owns it", async () => {
  served = summary({
    arrivals: [
      {
        kind: "sports",
        event_id: "sports:1",
        status: "success",
        action: 1,
        title: "Italian Grand Prix",
        poster_url: null,
        backdrop_url: null,
        library_id: 10,
        season: 2026,
        episode: 14,
        episode_title: null,
        language: "hu",
        languages: ["hu"],
        provider: "example",
        arr_instance_id: 3,
        instance_name: "Sports",
        timestamp: "2026-09-01T11:59:00+00:00",
      },
    ],
  });
  render();
  // Read as a series, it linked to a show page that has nothing to do with it.
  expect(
    await screen.findByRole("link", { name: /Italian Grand Prix/ }),
  ).toHaveAttribute("href", "/sports/10?instance=3");
});
