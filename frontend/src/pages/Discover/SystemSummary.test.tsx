/* eslint-disable camelcase -- API fixture fields keep their transport names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import { focusManager } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { findSelectInput, pickOption, selectInput } from "./selectTestHelpers";
import SystemSummary from "./SystemSummary";
import Discover from ".";

/**
 * The figure standing under one stat's label.
 *
 * Asserting the value and the label separately proves only that both strings
 * are somewhere in the group: three figures and three labels in one tile pass
 * that test in any pairing, including every wrong one. This walks from the
 * label to the figure it actually belongs to.
 */
function statValue(scope: HTMLElement, label: string) {
  const labelled = within(scope).getByText(label);
  // Direct node access on purpose: the pairing of a figure with the label
  // beside it is the thing under test, and no query expresses "the figure in
  // this one stat" without walking to it.
  // eslint-disable-next-line testing-library/no-node-access
  const value = labelled.parentElement?.querySelector("strong");
  if (!value) throw new Error(`No figure paired with the label ${label}`);
  return value;
}

const emptyActivity = {
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
};

const emptyWanted = {
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
};

function summary(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-09-01T12:00:00Z",
    state: "quiet",
    activity: emptyActivity,
    wanted: emptyWanted,
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

function runningTranslation(overrides: Record<string, unknown> = {}) {
  return {
    activity_id: "run-1",
    operation: "translation",
    state: "running",
    phase: "running",
    name: "Translating Northern Light (EN to HU)",
    scope_kind: "media",
    arr_instance_id: 2,
    instance_name: "Anime",
    media_type: "episode",
    title: "Northern Light",
    season: 2,
    episode: 5,
    language: "hu",
    progress: { unit: "percent", value: 40, total: 100 },
    remote: {
      service_id: "translator",
      job_id: "abc",
      phase: "processing",
      observed_at: "2026-09-01T12:00:00Z",
      stale: false,
    },
    parent_activity_id: null,
    scheduler_run_id: null,
    observed_at: "2026-09-01T12:00:00Z",
    ...overrides,
  };
}

let served: unknown = summary();
let requests = 0;
function setViewport(narrow: boolean) {
  vi.mocked(window.matchMedia).mockImplementation(
    (query: string) =>
      ({
        matches: narrow && query.includes("max-width"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }) as unknown as MediaQueryList,
  );
}

beforeEach(() => {
  focusManager.setFocused(true);
  // A viewport a previous case set must not leak into the next one.
  setViewport(false);
  localStorage.clear();
  localStorage.setItem("bazarr.discover.subtitle-language", "eng");
  served = summary();
  requests = 0;
  server.use(
    http.get("/api/discover/summary", () => {
      requests += 1;
      if (served === null) {
        return new HttpResponse(null, { status: 503 });
      }
      return HttpResponse.json(served);
    }),
  );
});

function renderSummary() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <SystemSummary /> },
      { path: "/system/tasks", element: <div>Local activity page</div> },
      { path: "/wanted/series", element: <div>Wanted series page</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

it("shows running work with its owning instance, language and measured progress", async () => {
  served = summary({
    state: "busy",
    activity: {
      ...emptyActivity,
      running_count: 1,
      queued_count: 2,
      scheduled_count: 3,
      running: [runningTranslation()],
      queued: [
        {
          ...runningTranslation(),
          activity_id: "queued-1",
          state: "queued",
          phase: "queued",
          name: "Manually downloading Subtitles",
          progress: null,
          remote: null,
        },
      ],
      scheduled: [
        {
          job_id: "update_series_1",
          name: "Sync with Sonarr (Main)",
          interval: "every 5 minutes",
          next_run_in: "in 3 minutes",
        },
      ],
    },
  });
  renderSummary();

  const running = await screen.findByRole("group", { name: "Running now" });
  expect(within(running).getByText(/Translating Northern Light/)).toBeVisible();
  expect(within(running).getByText(/Anime/)).toBeVisible();
  expect(within(running).getByText(/hu/)).toBeVisible();
  expect(within(running).getByText("40%")).toBeVisible();

  // Three states, three figures, each under its own label, and each figure
  // read from the label it is paired with rather than from the tile at large.
  const now = screen.getByRole("group", { name: "Current work" });
  expect(statValue(now, "running")).toHaveTextContent("1");
  expect(statValue(now, "queued")).toHaveTextContent("2");
  expect(statValue(now, "scheduled")).toHaveTextContent("3");
  expect(statValue(now, "running")).toBeVisible();
});

it("never invents progress the source does not measure", async () => {
  served = summary({
    state: "busy",
    activity: {
      ...emptyActivity,
      running_count: 1,
      running: [
        runningTranslation({
          progress: null,
          phase: "waiting_for_service",
          remote: {
            service_id: "translator",
            job_id: "abc",
            phase: "queued",
            observed_at: "2026-09-01T12:00:00Z",
            stale: false,
          },
        }),
      ],
    },
  });
  renderSummary();

  const running = await screen.findByRole("group", { name: "Running now" });
  expect(
    within(running).getByText(/Waiting for the translation service/),
  ).toBeVisible();
  expect(within(running).queryByRole("progressbar")).toBeNull();
  expect(within(running).queryByText("%", { exact: false })).toBeNull();
});

it("describes Wanted as outstanding subtitle requirements, not a title count", async () => {
  served = summary({
    wanted: {
      ...emptyWanted,
      requirements: 3,
      episode_requirements: 2,
      movie_requirements: 1,
      media_count: 2,
    },
  });
  renderSummary();

  const wanted = await screen.findByRole("group", { name: "Wanted subtitles" });
  expect(within(wanted).getByText("3")).toBeVisible();
  expect(
    within(wanted).getByText(/subtitle languages still wanted/i),
  ).toBeVisible();
  expect(within(wanted).getByText(/across 2 library items/i)).toBeVisible();
  expect(within(wanted).queryByRole("alert")).toBeNull();
  expect(
    within(wanted).getByRole("link", { name: /Open Wanted/ }),
  ).toHaveAttribute("href", "/wanted/series");
});

it("qualifies an incomplete Wanted aggregate instead of showing a confident total", async () => {
  served = summary({
    wanted: {
      ...emptyWanted,
      requirements: 2,
      media_count: 1,
      unknown_media_count: 4,
      complete: false,
      qualifications: ["uncomputed_media", "malformed_requirements"],
    },
  });
  renderSummary();

  const wanted = await screen.findByRole("group", { name: "Wanted subtitles" });
  expect(within(wanted).getByText(/at least/i)).toBeVisible();
  expect(
    within(wanted).getByText(/4 items have no computed requirement list yet/i),
  ).toBeVisible();
  expect(
    within(wanted).getByText(
      /Some stored requirement lists could not be read/i,
    ),
  ).toBeVisible();
});

it("lists only successful arrivals with their exact title, language and time", async () => {
  served = summary({
    arrivals: [
      {
        kind: "episode",
        event_id: "episode:9",
        status: "success",
        action: 2,
        title: "Northern Light",
        season: 2,
        episode: 5,
        episode_title: "Home",
        language: "hu:forced",
        provider: "opensubtitles",
        arr_instance_id: 1,
        instance_name: "Main",
        timestamp: "2026-09-01T11:59:00Z",
      },
    ],
  });
  renderSummary();

  const arrivals = await screen.findByRole("group", { name: "Recently added" });
  expect(within(arrivals).getByText(/Northern Light/)).toBeVisible();
  expect(within(arrivals).getByText(/S02E05/)).toBeVisible();
  expect(within(arrivals).getByText(/Home/)).toBeVisible();
  expect(within(arrivals).getByText(/hu:forced/)).toBeVisible();
  const stamp = within(arrivals).getByText(
    (_content, element) => element?.tagName === "TIME",
  );
  expect(stamp).toHaveAttribute("datetime", "2026-09-01T11:59:00Z");
});

it("says nothing has arrived rather than hiding the section", async () => {
  renderSummary();
  const arrivals = await screen.findByRole("group", { name: "Recently added" });
  expect(
    within(arrivals).getByText(/No subtitles have arrived recently/i),
  ).toBeVisible();
});

it("names the affected capability and instance and one scoped recovery", async () => {
  served = summary({
    state: "degraded",
    attention: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      unknown_sources: [],
      items: [
        {
          id: "library_sync:2",
          capability: "library_sync",
          severity: "warning",
          scope: { arr_instance_id: 2, instance_name: "Anime", kind: "sonarr" },
          summary: "Live sync for Anime is disconnected.",
          detail: "Other instances and subtitle downloads are unaffected.",
          freshness: "last_recorded_observation",
          recovery: { label: "Check Anime", target: "/settings/connections" },
        },
        {
          id: "subtitle_providers",
          capability: "subtitle_providers",
          severity: "warning",
          scope: { providers: ["example"], alternatives: true },
          summary: "1 provider is cooling down. Others are still searched.",
          detail: null,
          freshness: "live",
          recovery: { label: "Provider status", target: "/system/providers" },
        },
      ],
    },
  });
  renderSummary();

  const attention = await screen.findByRole("group", {
    name: "Needs attention",
  });
  expect(within(attention).getByText(/Live sync for Anime/)).toBeVisible();
  expect(
    within(attention).getByText(/Other instances and subtitle downloads/),
  ).toBeVisible();
  expect(
    within(attention).getByText(/Others are still searched/),
  ).toBeVisible();
  expect(
    within(attention).getByRole("link", { name: "Check Anime" }),
  ).toHaveAttribute("href", "/settings/connections");
  expect(
    within(attention).getByRole("link", { name: "Provider status" }),
  ).toHaveAttribute("href", "/system/providers");
  expect(
    within(attention).getByText(/Last recorded observation/i),
  ).toBeVisible();
});

it("keeps an unavailable status unknown rather than zero or healthy", async () => {
  served = null;
  renderSummary();

  await waitFor(() =>
    expect(screen.getByText(/Local status is unavailable/i)).toBeVisible(),
  );
  expect(screen.getByText(/Counts are unknown, not zero/i)).toBeVisible();
  expect(screen.queryByText(/No local work running/i)).toBeNull();
  expect(screen.queryByText(/Everything is healthy/i)).toBeNull();
});

// A figure the source reported as null must not be drawn as a zero: "nothing
// is queued" and "the queue could not be read" are different things to tell a
// reader, and only one of them is reassuring.
it("says a figure it could not read is unknown rather than drawing a zero", async () => {
  served = summary({
    state: "unknown",
    activity: {
      ...emptyActivity,
      running_count: null,
      queued_count: 2,
      scheduled_count: null,
    },
  });
  renderSummary();

  const now = await screen.findByRole("group", { name: "Current work" });
  expect(statValue(now, "running")).toHaveTextContent("Unknown");
  expect(statValue(now, "running")).toHaveAttribute("data-unknown", "true");
  expect(statValue(now, "scheduled")).toHaveTextContent("Unknown");
  // The figure that was readable keeps its value and is not marked unknown.
  expect(statValue(now, "queued")).toHaveTextContent("2");
  expect(statValue(now, "queued")).toHaveAttribute("data-unknown", "false");
  expect(within(now).queryByText("0")).toBeNull();
});

it("marks one unavailable component unknown while the rest keep their values", async () => {
  served = summary({
    state: "unknown",
    wanted: {
      ...emptyWanted,
      availability: "unknown",
      requirements: null,
      episode_requirements: null,
      movie_requirements: null,
      media_count: null,
      unknown_media_count: null,
      complete: false,
      observed_at: null,
    },
    arrivals: [
      {
        kind: "movie",
        event_id: "movie:1",
        status: "success",
        action: 1,
        title: "Example",
        season: null,
        episode: null,
        episode_title: null,
        language: "en",
        provider: "example",
        arr_instance_id: 1,
        instance_name: "Main",
        timestamp: "2026-09-01T11:00:00Z",
      },
    ],
  });
  renderSummary();

  const wanted = await screen.findByRole("group", { name: "Wanted subtitles" });
  // A count that could not be read says so in full, and never as a figure.
  expect(
    within(wanted).getByText(
      /Unknown\. The outstanding requirement count could not be read\./,
    ),
  ).toBeVisible();
  expect(within(wanted).queryByText("0")).toBeNull();
  expect(within(wanted).queryByText(/still wanted/)).toBeNull();
  const arrivals = screen.getByRole("group", { name: "Recently added" });
  expect(within(arrivals).getByText(/Example/)).toBeVisible();
});

it("retries local status on request without disturbing anything else", async () => {
  served = null;
  renderSummary();
  await waitFor(() =>
    expect(screen.getByText(/Local status is unavailable/i)).toBeVisible(),
  );
  const attempts = requests;
  served = summary({
    wanted: { ...emptyWanted, requirements: 5, media_count: 3 },
  });
  await userEvent.click(
    screen.getByRole("button", { name: "Retry local status" }),
  );
  await waitFor(() => expect(requests).toBeGreaterThan(attempts));
  const wanted = await screen.findByRole("group", { name: "Wanted subtitles" });
  expect(within(wanted).getByText("5")).toBeVisible();
});

it("labels a failed refresh as the last reading rather than as current", async () => {
  served = summary({
    wanted: { ...emptyWanted, requirements: 5, media_count: 3 },
  });
  renderSummary();
  const wanted = await screen.findByRole("group", { name: "Wanted subtitles" });
  expect(within(wanted).getByText("5")).toBeVisible();

  served = null;
  await userEvent.click(
    screen.getByRole("button", { name: "Refresh local status" }),
  );
  await waitFor(() =>
    expect(screen.getByText(/could not be refreshed/i)).toBeVisible(),
  );
  expect(screen.getByText(/last reading/i)).toBeVisible();
  // The retained numbers stay, explicitly labelled, instead of dropping to zero.
  expect(within(wanted).getByText("5")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Retry local status" }),
  ).toBeVisible();
});

it("treats an unused library and translator as optional setup, not failure", async () => {
  served = summary({
    state: "new_installation",
    onboarding: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: true,
      items: [
        {
          id: "library",
          summary: "No Sonarr or Radarr instance is connected yet.",
          target: "/settings/connections",
        },
        {
          id: "translator",
          summary: "The optional AI translator is not configured.",
          target: "/settings/translator",
        },
      ],
    },
  });
  renderSummary();

  const onboarding = await screen.findByRole("group", {
    name: "Optional setup",
  });
  expect(within(onboarding).getByText(/No Sonarr or Radarr/)).toBeVisible();
  expect(within(onboarding).getByText(/optional AI translator/)).toBeVisible();
  expect(screen.queryByRole("group", { name: "Needs attention" })).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("opens the detail on a wide panel and collapses it on a narrow viewport", async () => {
  served = summary({
    wanted: { ...emptyWanted, requirements: 3, media_count: 2 },
  });
  renderSummary();

  const detail = await screen.findByRole("group", {
    name: "Local work detail",
  });
  expect(detail).toHaveAttribute("open");
  expect(screen.getByText(/No local work running/i)).toBeVisible();

  setViewport(true);
  renderSummary();
  const panels = await screen.findAllByRole("group", {
    name: "Local work detail",
  });
  expect(panels[panels.length - 1]).not.toHaveAttribute("open");
  // The short current-work summary and its retry stay outside the disclosure.
  const headlines = screen.getAllByText(/No local work running/i);
  expect(headlines[headlines.length - 1]).toBeVisible();
});

function browseDiscover() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      { path: "/system/tasks", element: <div>Local activity page</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return router;
}

it("shows search, the start of discovery and the summary together", async () => {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", use_sonarr: false, use_radarr: false },
        discover: {
          tmdb_configured: false,
          metadata_revision: "summary-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "English", code3: "eng", code2: "en", enabled: false },
      ]),
    ),
  );
  served = summary({
    wanted: { ...emptyWanted, requirements: 3, media_count: 2 },
  });
  browseDiscover();

  expect(await screen.findByText("Your Bazarr+")).toBeVisible();
  expect(screen.getByRole("heading", { name: "Discover" })).toBeVisible();
  expect(selectInput("Subtitle language")).toBeVisible();
  expect(
    screen.getByRole("button", { name: /Find subtitles/ }),
  ).toBeInTheDocument();
});

it("records a return target so leaving for local work restores Discover", async () => {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", use_sonarr: false, use_radarr: false },
        discover: {
          tmdb_configured: false,
          metadata_revision: "summary-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "English", code3: "eng", code2: "en", enabled: false },
      ]),
    ),
  );
  const router = browseDiscover();
  const imdb = await screen.findByLabelText("IMDb ID");
  await userEvent.type(imdb, "tt0133093");

  await userEvent.click(screen.getByRole("link", { name: "Activity" }));
  await waitFor(() =>
    expect(screen.getByText("Local activity page")).toBeVisible(),
  );

  await waitFor(() => router.navigate(-1));
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093"),
  );
  expect(screen.getByText("Your Bazarr+")).toBeVisible();
});

function discoverFixtures() {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", use_sonarr: false, use_radarr: false },
        discover: {
          tmdb_configured: false,
          metadata_revision: "summary-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "English", code3: "eng", code2: "en", enabled: false },
      ]),
    ),
  );
}

/**
 * Drives window scroll, paint frames and element geometry the way the page
 * owner sees them. jsdom reports a zero scroll offset and never paints, so a
 * restoration that runs in a requestAnimationFrame is invisible without this.
 */
function scrollHarness(controlTop: number, headerHeight = 112) {
  const scrollDescriptor = Object.getOwnPropertyDescriptor(window, "scrollY");
  const heightDescriptor = Object.getOwnPropertyDescriptor(
    window,
    "innerHeight",
  );
  // The page reconciles against the real app shell header, which these focused
  // renders do not mount. Give it one so the reveal path is actually exercised.
  const shell = document.createElement("header");
  shell.className = "mantine-AppShell-header";
  document.body.append(shell);
  const nativeRect = HTMLElement.prototype.getBoundingClientRect;
  const setScroll = (top: number) =>
    Object.defineProperty(window, "scrollY", {
      configurable: true,
      value: top,
    });
  const rect = vi
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockImplementation(function (this: HTMLElement) {
      if (["SELECT", "INPUT", "BUTTON"].includes(this.tagName))
        return new DOMRect(0, controlTop - window.scrollY, 200, 44);
      if (this.tagName === "HEADER")
        return new DOMRect(0, 0, 320, headerHeight);
      return nativeRect.call(this);
    });
  const scrollTo = vi
    .spyOn(window, "scrollTo")
    .mockImplementation((options: number | ScrollToOptions) => {
      if (typeof options === "object") setScroll(options.top ?? 0);
    });
  const frames = new Map<number, FrameRequestCallback>();
  let frameId = 0;
  const requestFrame = vi
    .spyOn(window, "requestAnimationFrame")
    .mockImplementation((callback) => {
      frames.set(++frameId, callback);
      return frameId;
    });
  const cancelFrame = vi
    .spyOn(window, "cancelAnimationFrame")
    .mockImplementation((id) => {
      frames.delete(id);
    });
  Object.defineProperty(window, "innerHeight", {
    configurable: true,
    value: 900,
  });
  setScroll(0);
  return {
    setScroll,
    paint: () =>
      act(() => {
        const pending = [...frames.values()];
        frames.clear();
        pending.forEach((callback) => callback(performance.now()));
      }),
    pending: () => frames.size,
    restore: () => {
      shell.remove();
      rect.mockRestore();
      scrollTo.mockRestore();
      requestFrame.mockRestore();
      cancelFrame.mockRestore();
      if (scrollDescriptor)
        Object.defineProperty(window, "scrollY", scrollDescriptor);
      if (heightDescriptor)
        Object.defineProperty(window, "innerHeight", heightDescriptor);
    },
  };
}

it("restores the retrieval control and the scroll offset after returning from local work", async () => {
  discoverFixtures();
  // The control sits comfortably in view at the saved offset, so the exact
  // offset survives and no reveal adjustment is needed.
  const harness = scrollHarness(3338);
  try {
    const router = browseDiscover();
    const language = await findSelectInput("Subtitle language");
    await act(async () => {
      language.focus();
      language.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    });
    harness.setScroll(3038);
    await act(async () => {
      window.dispatchEvent(new Event("scroll"));
    });

    await userEvent.click(screen.getByRole("link", { name: "Activity" }));
    await waitFor(() =>
      expect(screen.getByText("Local activity page")).toBeVisible(),
    );
    // The browser applies its own offset on a history return.
    harness.setScroll(742);

    await waitFor(() => router.navigate(-1));
    await findSelectInput("Subtitle language");
    harness.paint();

    const restored = selectInput("Subtitle language");
    expect(restored).toHaveFocus();
    expect(window.scrollY).toBe(3038);
    const bounds = restored.getBoundingClientRect();
    expect(bounds.top).toBeGreaterThanOrEqual(112);
    expect(bounds.bottom).toBeLessThanOrEqual(900);
  } finally {
    harness.restore();
  }
});

it("reveals the restored control when the saved offset would hide it behind the shell", async () => {
  discoverFixtures();
  // The control sits under the app shell header at the saved offset.
  const harness = scrollHarness(3000);
  try {
    const router = browseDiscover();
    const imdb = await screen.findByLabelText("IMDb ID");
    await act(async () => {
      imdb.focus();
      imdb.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    });
    harness.setScroll(2980);
    await act(async () => {
      window.dispatchEvent(new Event("scroll"));
    });
    await userEvent.click(screen.getByRole("link", { name: "Activity" }));
    await waitFor(() =>
      expect(screen.getByText("Local activity page")).toBeVisible(),
    );
    harness.setScroll(0);
    await waitFor(() => router.navigate(-1));
    await screen.findByLabelText("IMDb ID");
    harness.paint();

    const restored = screen.getByLabelText("IMDb ID");
    expect(restored).toHaveFocus();
    // Reconciled up from the saved 2980 so the control clears the header.
    expect(window.scrollY).toBeLessThan(2980);
    expect(restored.getBoundingClientRect().top).toBeGreaterThanOrEqual(112);
  } finally {
    harness.restore();
  }
});

it("never steals focus or scroll on a first visit", async () => {
  discoverFixtures();
  const harness = scrollHarness(3040);
  try {
    browseDiscover();
    await findSelectInput("Subtitle language");
    // No frame was requested at all, so the page owner did not try to restore.
    expect(harness.pending()).toBe(0);
    harness.paint();
    expect(selectInput("Subtitle language")).not.toHaveFocus();
    expect(screen.getByLabelText("IMDb ID")).not.toHaveFocus();
    expect(window.scrollY).toBe(0);
  } finally {
    harness.restore();
  }
});

it("cancels the restoration when the page leaves again before paint", async () => {
  discoverFixtures();
  const harness = scrollHarness(3040);
  try {
    const router = browseDiscover();
    const language = await findSelectInput("Subtitle language");
    await act(async () => {
      language.focus();
      language.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    });
    harness.setScroll(3038);
    await act(async () => {
      window.dispatchEvent(new Event("scroll"));
    });
    await userEvent.click(screen.getByRole("link", { name: "Activity" }));
    await waitFor(() =>
      expect(screen.getByText("Local activity page")).toBeVisible(),
    );
    harness.setScroll(0);
    await waitFor(() => router.navigate(-1));
    await findSelectInput("Subtitle language");
    await waitFor(() => router.navigate("/system/tasks"));
    await waitFor(() =>
      expect(screen.getByText("Local activity page")).toBeVisible(),
    );
    harness.paint();
    expect(window.scrollY).toBe(0);
  } finally {
    harness.restore();
  }
});

it("restores a control in the results region, which no other owner claims", async () => {
  discoverFixtures();
  server.use(
    http.post("/api/discover/search", async ({ request }) => {
      const context = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json({
        search_id: "return-search",
        context: { ...context, matching_mode: "title" },
        status: "complete",
        checked_at: "2026-09-08T10:00:00Z",
        attempted_at: "2026-09-08T10:00:00Z",
        cache_status: "fresh",
        coverage: {
          complete: true,
          configured_count: 1,
          completed_count: 1,
          providers: [
            {
              provider: "catalog-fixture",
              status: "success",
              result_count: 1,
              elapsed_ms: 5,
              reason: null,
              retry_at: null,
            },
          ],
        },
        results: [
          {
            id: "return-result",
            search_id: "return-search",
            provider: "catalog-fixture",
            language: "eng",
            language_variant: null,
            release: "Northern.Light.S02E05.1080p.WEB-DL",
            uploader: null,
            scope: "unknown",
            hearing_impaired: null,
            matches: ["imdb_id"],
            compatibility_score: 10,
            compatibility_score_max: 119,
            rating: null,
            checked_at: "2026-09-08T10:00:00Z",
            expires_at: "2099-01-01T00:00:00Z",
            stale: false,
          },
        ],
      });
    }),
  );
  const harness = scrollHarness(3338);
  try {
    const router = browseDiscover();
    const imdb = await screen.findByLabelText("IMDb ID");
    await userEvent.type(imdb, "tt1234567");
    await pickOption(userEvent, "Subtitle language", "English");
    await userEvent.click(
      screen.getByRole("button", { name: /Find subtitles/ }),
    );
    const preview = await screen.findByRole("button", { name: "Preview" });
    await act(async () => {
      preview.focus();
      preview.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    });
    harness.setScroll(3038);
    await act(async () => {
      window.dispatchEvent(new Event("scroll"));
    });

    await userEvent.click(screen.getByRole("link", { name: "Activity" }));
    await waitFor(() =>
      expect(screen.getByText("Local activity page")).toBeVisible(),
    );
    harness.setScroll(759);
    await waitFor(() => router.navigate(-1));
    await screen.findByRole("button", { name: "Preview" });
    harness.paint();

    expect(screen.getByRole("button", { name: "Preview" })).toHaveFocus();
    expect(window.scrollY).toBe(3038);
  } finally {
    harness.restore();
  }
});

it("never renders an unknown attention component as a healthy system", async () => {
  served = summary({
    state: "unknown",
    attention: {
      availability: "unknown",
      observed_at: null,
      complete: false,
      unknown_sources: ["attention"],
      items: [],
    },
  });
  renderSummary();

  const attention = await screen.findByRole("group", {
    name: "Needs attention",
  });
  expect(
    within(attention).getByText(/Scoped problems could not be read/i),
  ).toBeVisible();
  expect(
    within(attention).getByText(/not a report that nothing is wrong/i),
  ).toBeVisible();
  expect(
    screen.getByText(/What is missing is unknown, not healthy/i),
  ).toBeVisible();
});

it("says an unchecked attention source leaves the list incomplete", async () => {
  served = summary({
    attention: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: false,
      unknown_sources: ["live_feed"],
      items: [],
    },
  });
  renderSummary();

  const attention = await screen.findByRole("group", {
    name: "Needs attention",
  });
  expect(
    within(attention).getByText(/Some sources could not be checked/i),
  ).toBeVisible();
});

it("reports an unreadable schedule as unknown rather than as none scheduled", async () => {
  served = summary({
    activity: {
      ...emptyActivity,
      scheduled_count: null,
      complete: false,
      unknown_sources: ["schedules"],
    },
  });
  renderSummary();

  const scheduled = await screen.findByRole("group", {
    name: "Scheduled work",
  });
  expect(
    within(scheduled).getByText(/unknown rather than none/i),
  ).toBeVisible();
  // The headline drops the scheduled figure rather than printing a zero.
  expect(screen.getByText(/No local work running\./)).toBeVisible();
});

it("reports an unreadable optional setup state instead of hiding it", async () => {
  served = summary({
    onboarding: {
      availability: "unknown",
      observed_at: null,
      complete: false,
      items: [],
    },
  });
  renderSummary();

  const onboarding = await screen.findByRole("group", {
    name: "Optional setup",
  });
  expect(within(onboarding).getByText(/could not be read/i)).toBeVisible();
});

it("remembers the reader's disclosure choice across leaving and returning", async () => {
  served = summary({
    wanted: { ...emptyWanted, requirements: 3, media_count: 2 },
  });
  // A narrow viewport, where the panel starts short.
  setViewport(true);
  const router = browseDiscover();
  const detail = await screen.findByRole("group", {
    name: "Local work detail",
  });
  expect(detail).not.toHaveAttribute("open");

  await userEvent.click(screen.getByText("Local work detail"));
  await waitFor(() =>
    expect(
      screen.getByRole("group", { name: "Local work detail" }),
    ).toHaveAttribute("open"),
  );

  await userEvent.click(screen.getByRole("link", { name: "Activity" }));
  await waitFor(() =>
    expect(screen.getByText("Local activity page")).toBeVisible(),
  );
  await waitFor(() => router.navigate(-1));

  // The panel comes back the height it left, which is what makes the saved
  // return offset reachable on a narrow viewport.
  await waitFor(() =>
    expect(
      screen.getByRole("group", { name: "Local work detail" }),
    ).toHaveAttribute("open"),
  );
});

it("stores no disclosure preference the reader never expressed", async () => {
  served = summary();
  renderSummary();
  await screen.findByRole("group", { name: "Local work detail" });
  expect(localStorage.getItem("bazarr.discover.local-work-detail")).toBeNull();
});

it("keeps the overflow count when a source could not be checked", async () => {
  const item = (id: number) => ({
    id: `library_sync:${id}`,
    capability: "library_sync",
    severity: "warning",
    scope: {
      arr_instance_id: id,
      instance_name: `Instance ${id}`,
      kind: "sonarr",
    },
    summary: `Live sync for Instance ${id} is disconnected.`,
    detail: null,
    freshness: "last_recorded_observation",
    recovery: { label: `Check ${id}`, target: "/settings/connections" },
  });
  served = summary({
    state: "degraded",
    attention: {
      availability: "available",
      observed_at: "2026-09-01T12:00:00Z",
      complete: false,
      unknown_sources: ["live_feed"],
      items: [item(1), item(2), item(3), item(4), item(5)],
    },
  });
  renderSummary();

  const attention = await screen.findByRole("group", {
    name: "Needs attention",
  });
  expect(
    within(attention).getByText(/Some sources could not be checked/i),
  ).toBeVisible();
  // The same overflow line the fully readable branch shows.
  expect(within(attention).getByText(/and 2 more to review/i)).toBeVisible();
  expect(within(attention).getAllByRole("listitem")).toHaveLength(3);
});
