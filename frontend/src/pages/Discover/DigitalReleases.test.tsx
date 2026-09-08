/* eslint-disable camelcase -- API fixture fields. */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import Discover from ".";

let regions: string[] = [];
let searches: unknown[] = [];
const stamp = () => new Date().toISOString();
const day = () => stamp().slice(0, 10);
function feed(region: string) {
  return {
    source: "tmdb",
    revision: "digital-one",
    locale: "en-US",
    configured: true,
    status: "live",
    service_status: null,
    region,
    release_type: "digital",
    window: {
      start: new Date(Date.parse(day()) - 29 * 86_400_000)
        .toISOString()
        .slice(0, 10),
      end: day(),
    },
    fetched_at: stamp(),
    attempted_at: stamp(),
    last_success: stamp(),
    expires_at: new Date(Date.now() + 300000).toISOString(),
    stale_until: new Date(Date.now() + 3600000).toISOString(),
    coverage: {
      complete: true,
      truncated: false,
      candidate_limit: 20,
      candidates: 1,
      checked: 1,
      missing_region: 0,
      failed: 0,
    },
    items: [
      {
        source_id: "tmdb:movie:42",
        id: 42,
        media_type: "movie",
        title: "Digital North " + region,
        year: 1980,
        overview: "A film beyond the library.",
        poster_url: null,
        backdrop_url: null,
        release_date: day(),
        region,
        release_type: "digital",
        provenance: {
          source: "tmdb",
          path: "/movie/42/release_dates",
          region,
          type: 4,
          release_date: day(),
        },
      },
    ],
  };
}
function browse() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      { path: "/system/tasks", element: <div>Local activity</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { user: userEvent.setup(), router };
}
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("bazarr.discover.subtitle-language", "hun");
  regions = [];
  searches = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", use_sonarr: false, use_radarr: false },
        discover: {
          tmdb_configured: true,
          metadata_revision: "digital-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "Hungarian", code2: "hu", code3: "hun", enabled: false },
      ]),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({
        data: {
          source: "tmdb",
          status: "available",
          configured: true,
          revision: "digital-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/discover/feeds/trending", () =>
      HttpResponse.json({
        ...feed("US"),
        period: "week",
        scope: "global",
        media_type: "all",
        items: [],
        status: "empty",
      }),
    ),
    http.get("/api/discover/feeds/digital", ({ request }) => {
      const region = new URL(request.url).searchParams.get("region") ?? "US";
      regions.push(region);
      return HttpResponse.json(feed(region));
    }),
    http.get("/api/discover/metadata/movies/42", () =>
      HttpResponse.json({
        data: {
          source: "tmdb",
          status: "available",
          configured: true,
          revision: "digital-one",
          item: {
            ...feed("US").items[0],
            source: "tmdb",
            imdb_id: "tt0080274",
            mapping_status: "resolved",
          },
        },
      }),
    ),
    http.post("/api/discover/search", async ({ request }) => {
      searches.push(await request.json());
      return new HttpResponse(null, { status: 503 });
    }),
  );
});

it("browses recent digital films in US without changing the explicit subtitle language", async () => {
  browse();
  expect(
    await screen.findByRole("heading", { name: "Recent digital releases" }),
  ).toBeInTheDocument();
  expect(
    await screen.findByRole("button", { name: /Digital North US/ }),
  ).toBeEnabled();
  expect(screen.getByLabelText("Film region")).toHaveValue("US");
  expect(screen.getByLabelText("Subtitle language")).toHaveValue("hun");
  expect(searches).toEqual([]);
});

it("changes only film region and caches independent region queries without provider submission", async () => {
  const { user } = browse();
  await screen.findByRole("button", { name: /Digital North US/ });
  await user.selectOptions(screen.getByLabelText("Film region"), "GB");
  await screen.findByRole("button", { name: /Digital North GB/ });
  expect(
    screen.queryByRole("button", { name: /Digital North US/ }),
  ).not.toBeInTheDocument();
  expect(screen.getByLabelText("Subtitle language")).toHaveValue("hun");
  expect(localStorage.getItem("bazarr.discover.subtitle-language")).toBe("hun");
  expect(screen.getByLabelText("Browse media")).toHaveValue("movie");
  await user.selectOptions(screen.getByLabelText("Film region"), "US");
  await screen.findByRole("button", { name: /Digital North US/ });
  expect(regions).toEqual(["US", "GB"]);
  expect(searches).toEqual([]);
});

it("preserves exact date/type/region through details, explicit retrieval, route return and back focus", async () => {
  const { user, router } = browse();
  await screen.findByRole("button", { name: /Digital North US/ });
  await user.selectOptions(screen.getByLabelText("Film region"), "HU");
  await user.click(
    await screen.findByRole("button", { name: /Digital North HU/ }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274"),
  );
  expect(screen.getByLabelText("Selected digital release")).toHaveTextContent(
    `Digital · HU · ${day()} · TMDB regional release record`,
  );
  expect(searches).toEqual([]);
  await user.click(screen.getByLabelText("IMDb ID"));
  await act(async () => {
    await router.navigate("/system/tasks");
  });
  await act(async () => {
    await router.navigate(-1);
  });
  await waitFor(() => expect(screen.getByLabelText("IMDb ID")).toHaveFocus());
  expect(screen.getByLabelText("Selected digital release")).toHaveTextContent(
    `Digital · HU · ${day()}`,
  );
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).toMatchObject({
    language: "hun",
    imdb_id: "tt0080274",
    media_type: "movie",
  });
  expect(searches[0]).not.toHaveProperty("region");
  await user.click(screen.getByRole("button", { name: /Back to/ }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: /Digital North HU/ }),
    ).toHaveFocus(),
  );
  expect(screen.getByLabelText("Film region")).toHaveValue("HU");
  expect(screen.getByLabelText("Subtitle language")).toHaveValue("hun");
});

it("ignores a late prior-region completion", async () => {
  let finish: (() => void) | undefined;
  let started = false;
  server.use(
    http.get("/api/discover/feeds/digital", async ({ request }) => {
      const region = new URL(request.url).searchParams.get("region")!;
      if (region === "US") {
        started = true;
        await new Promise<void>((resolve) => {
          finish = resolve;
        });
      }
      return HttpResponse.json(feed(region));
    }),
  );
  const { user } = browse();
  await waitFor(() => expect(started).toBe(true));
  await user.selectOptions(screen.getByLabelText("Film region"), "GB");
  await screen.findByRole("button", { name: /Digital North GB/ });
  await act(async () => {
    finish!();
  });
  expect(
    screen.queryByRole("button", { name: /Digital North US/ }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /Digital North GB/ }),
  ).toBeEnabled();
  expect(searches).toEqual([]);
});

it.each([
  "empty",
  "unavailable",
  "authentication_failed",
  "partial",
  "cached",
  "expired",
])("shows %s truth without inferring subtitles", async (status) => {
  server.use(
    http.get("/api/discover/feeds/digital", () =>
      HttpResponse.json({
        ...feed("US"),
        status: status === "partial" || status === "expired" ? "live" : status,
        items: ["empty", "unavailable", "authentication_failed"].includes(
          status,
        )
          ? []
          : feed("US").items,
        ...(status === "partial"
          ? {
              coverage: {
                ...feed("US").coverage,
                complete: false,
                candidates: 2,
                failed: 1,
                truncated: true,
              },
              service_status: "unavailable",
            }
          : {}),
        ...(status === "cached" ? { service_status: "unavailable" } : {}),
        ...(status === "expired"
          ? { stale_until: "2000-01-01T00:00:00Z" }
          : {}),
      }),
    ),
  );
  browse();
  const section = within(
    screen.getByRole("region", { name: "Recent digital releases" }),
  );
  if (status === "empty")
    await section.findByText(/No verified digital releases in US/);
  if (status === "unavailable" || status === "expired")
    await section.findByText(/Digital releases are temporarily unavailable/);
  if (status === "authentication_failed")
    await section.findByText(/TMDB rejected/);
  if (status === "partial") {
    await section.findByText(/Incomplete regional coverage: 1 of 2/);
    await section.findByRole("button", { name: /Digital North US/ });
  }
  if (status === "cached")
    await section.findByText(/Cached TMDB regional records/);
  if (
    ["empty", "unavailable", "authentication_failed", "expired"].includes(
      status,
    )
  )
    expect(
      section.queryByRole("button", { name: /Digital North US/ }),
    ).not.toBeInTheDocument();
  expect(
    section.getByText(/does not confirm subtitle availability/),
  ).toBeInTheDocument();
  expect(searches).toEqual([]);
});

it.each([
  { revision: "obsolete" },
  { region: "GB" },
  { window: { start: "2000-01-01", end: "2000-01-30" } },
  { release_type: "theatrical" },
])("rejects obsolete or mismatched feed identity %j", async (overrides) => {
  server.use(
    http.get("/api/discover/feeds/digital", () =>
      HttpResponse.json({ ...feed("US"), ...overrides }),
    ),
  );
  browse();
  await screen.findByText(/Digital releases are temporarily unavailable/);
  expect(
    screen.queryByRole("button", { name: /Digital North US/ }),
  ).not.toBeInTheDocument();
  expect(searches).toEqual([]);
});

it("retains region while metadata configuration rotates and rejects the old revision", async () => {
  const { user } = browse();
  await screen.findByRole("button", { name: /Digital North US/ });
  await user.selectOptions(screen.getByLabelText("Film region"), "HU");
  await screen.findByRole("button", { name: /Digital North HU/ });
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: true,
          metadata_revision: "rotated",
          locale: "hu-HU",
        },
      }),
    ),
  );
  await act(async () => {
    await queryClient.invalidateQueries({
      queryKey: [QueryKeys.System, QueryKeys.Settings],
    });
  });
  await screen.findByText(/Digital releases are temporarily unavailable/);
  expect(
    screen.queryByRole("button", { name: /Digital North HU/ }),
  ).not.toBeInTheDocument();
  expect(screen.getByLabelText("Film region")).toHaveValue("HU");
  expect(screen.getByLabelText("Subtitle language")).toHaveValue("hun");
  expect(searches).toEqual([]);
});

it("does not request regional metadata when TMDB is unconfigured", async () => {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: false,
          metadata_revision: "no-token",
          locale: "en-US",
        },
      }),
    ),
  );
  browse();
  await screen.findByText("Connect TMDB to browse regional digital releases.");
  expect(regions).toEqual([]);
  expect(searches).toEqual([]);
});

it("does not attach a previous digital date when the same movie is opened from trending", async () => {
  server.use(
    http.get("/api/discover/feeds/trending", () =>
      HttpResponse.json({
        ...feed("US"),
        period: "week",
        scope: "global",
        media_type: "all",
        items: [{ ...feed("US").items[0], rank: 1, title: "Trending North" }],
      }),
    ),
  );
  const { user } = browse();
  await user.click(
    await screen.findByRole("button", { name: /Digital North US/ }),
  );
  await screen.findByLabelText("Selected digital release");
  await user.click(screen.getByRole("button", { name: /Back to/ }));
  await user.click(
    await screen.findByRole("button", { name: "Explore Trending North" }),
  );
  await screen.findByRole("heading", { name: "Digital North US" });
  expect(
    screen.queryByLabelText("Selected digital release"),
  ).not.toBeInTheDocument();
  expect(searches).toEqual([]);
});
