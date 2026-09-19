/* eslint-disable camelcase -- API fixture fields keep their transport names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import NeedsAttention from "./NeedsAttention";

const healthy = {
  availability: "available",
  observed_at: "2026-09-01T12:00:00Z",
  complete: true,
  unknown_sources: [],
  items: [],
};

function summary(attention: Record<string, unknown> = healthy) {
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
      requirements: 0,
      episode_requirements: 0,
      movie_requirements: 0,
      media_count: 0,
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
    attention,
    onboarding: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      items: [],
    },
  };
}

function throttled(overrides: Record<string, unknown> = {}) {
  return {
    id: "subtitle_providers",
    capability: "subtitle_providers",
    severity: "warning",
    scope: {
      providers: ["opensubtitles"],
      alternatives: true,
      enabled_count: 3,
    },
    summary:
      "1 of 3 providers is cooling down. Searches still use the other 2.",
    detail: "Downloads already in progress are unaffected.",
    freshness: "live",
    recovery: { label: "Review providers", target: "/subtitle-hub" },
    ...overrides,
  };
}

let served: Record<string, unknown> = summary();

beforeEach(() => {
  served = summary();
  server.use(
    http.get("/api/discover/summary", () => HttpResponse.json(served)),
  );
});

function render() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <NeedsAttention /> },
      { path: "/subtitle-hub", element: <div>Subtitle hub page</div> },
      { path: "/settings/connections", element: <div>Connections page</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

it("says nothing at all while there is nothing wrong", async () => {
  render();
  // The heading must never appear, so waiting for its absence would pass
  // instantly on the first empty frame. Wait for the read to have happened.
  await waitFor(() =>
    expect(
      screen.queryByRole("heading", { name: /needs attention/i }),
    ).toBeNull(),
  );
  expect(screen.queryByRole("list")).toBeNull();
});

it("reports a cooling-down provider with its detail and the way to fix it", async () => {
  served = summary({ ...healthy, items: [throttled()] });
  render();
  expect(
    await screen.findByText(
      "1 of 3 providers is cooling down. Searches still use the other 2.",
    ),
  ).toBeInTheDocument();
  expect(
    screen.getByText("Downloads already in progress are unaffected."),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: /review providers/i }),
  ).toHaveAttribute("href", "/subtitle-hub");
});

it("separates a disconnected library from a cooling provider by severity", async () => {
  served = summary({
    ...healthy,
    items: [
      throttled(),
      {
        id: "library_sync:1",
        capability: "library_sync",
        severity: "error",
        scope: { arr_instance_id: 1, instance_name: "Sonarr", kind: "sonarr" },
        summary: "Live sync for Sonarr is disconnected.",
        detail: "Other instances and subtitle downloads are unaffected.",
        freshness: "last_recorded_observation",
        recovery: { label: "Check Sonarr", target: "/settings/connections" },
      },
    ],
  });
  render();
  const items = await screen.findAllByRole("listitem");
  expect(items).toHaveLength(2);
  expect(items[0]).toHaveAttribute("data-severity", "warning");
  expect(items[1]).toHaveAttribute("data-severity", "error");
});

it("admits when a check could not be completed rather than implying all is well", async () => {
  served = summary({
    ...healthy,
    availability: "unknown",
    unknown_sources: ["live_feed"],
  });
  render();
  expect(
    await screen.findByText("This check could not be completed."),
  ).toBeInTheDocument();
});

it("warns that findings are incomplete when only part of the picture was read", async () => {
  served = summary({
    ...healthy,
    items: [throttled()],
    unknown_sources: ["live_feed"],
  });
  render();
  expect(
    await screen.findByText("There may be more than this."),
  ).toBeInTheDocument();
});

// The same distinction one step earlier: a body that is not a summary at all
// leaves this panel with nothing to report, which is not the same fact as
// there being nothing to report.
it("admits it could not check when the answer is not a summary", async () => {
  served = {
    message:
      "The server could not verify that you are authorized to access the URL requested.",
  };
  render();
  expect(
    await screen.findByText("This check could not be completed."),
  ).toBeInTheDocument();
});
