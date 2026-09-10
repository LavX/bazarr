/* eslint-disable camelcase -- API fixture fields. */
import { createMemoryRouter, RouterProvider } from "react-router";
import { focusManager } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it } from "vitest";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { selectInput } from "./selectTestHelpers";
import Discover from ".";
import styles from "./Discover.module.scss";

const day = () => new Date().toISOString().slice(0, 10);
const sourceEpisode = () => ({
  source: "tmdb",
  source_id: "tmdb:show:100:episode:401",
  show_id: 100,
  season_id: 201,
  id: 401,
  season: 2,
  episode: 1,
  title: "Home",
  air_date: day(),
  identity_status: "unverified",
  show_title: "Northern Light",
  show_year: 2020,
  poster_url: null,
  provenance: { source: "tmdb", path: "/tv/100/season/2", air_date: day() },
});
const show = {
  source: "tmdb",
  source_id: "tmdb:show:100",
  id: 100,
  media_type: "show",
  title: "Northern Light",
  year: 2020,
  imdb_id: "tt1234567",
  tvdb_id: 300,
  mapping_status: "resolved",
  overview: "A show beyond the library.",
  poster_url: null,
  backdrop_url: null,
  seasons: [{ id: 201, season: 2, title: "Season 2", episode_count: 1 }],
};
function resolved() {
  return {
    ...sourceEpisode(),
    imdb_id: "tt7654321",
    tvdb_id: 501,
    show_imdb_id: "tt1234567",
    show_tvdb_id: 300,
    target_season: 3,
    target_episode: 7,
    numbering: "tvdb_default",
    identity_status: "resolved",
    absolute_episode: null,
    tvdb_absolute_number: null,
    mapping_updated_at: day(),
  };
}
const envelope = {
  source: "tmdb",
  configured: true,
  revision: "recent-one",
  locale: "en-US",
  status: "available",
};
function feed() {
  return {
    ...envelope,
    status: "live",
    service_status: null,
    period: "week",
    scope: "trending_shows",
    window: {
      start: new Date(Date.parse(day()) - 29 * 86400000)
        .toISOString()
        .slice(0, 10),
      end: day(),
    },
    fetched_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 300000).toISOString(),
    stale_until: new Date(Date.now() + 3600000).toISOString(),
    last_success: new Date().toISOString(),
    attempted_at: new Date().toISOString(),
    coverage: {
      complete: false,
      show_limit: 6,
      shows: 2,
      shows_checked: 1,
      season_limit: 3,
      seasons: 1,
      seasons_checked: 1,
      episode_limit: 1000,
      output_limit: 120,
      episodes_checked: 1,
      missing_dates: 0,
      failed: 1,
      truncated: false,
    },
    items: [sourceEpisode()],
  };
}
let searches: unknown[] = [];
beforeEach(() => {
  focusManager.setFocused(true);
  localStorage.clear();
  localStorage.setItem("bazarr.discover.subtitle-language", "eng");
  searches = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", use_sonarr: false, use_radarr: false },
        discover: {
          tmdb_configured: true,
          metadata_revision: "recent-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "English", code3: "eng", code2: "en", enabled: false },
      ]),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: envelope }),
    ),
    http.get("/api/discover/feeds/trending", () =>
      HttpResponse.json({
        ...feed(),
        scope: "global",
        media_type: "all",
        items: [],
        status: "empty",
      }),
    ),
    http.get("/api/discover/feeds/digital", () =>
      HttpResponse.json({
        ...feed(),
        region: "US",
        release_type: "digital",
        items: [],
        status: "empty",
      }),
    ),
    http.get("/api/discover/feeds/recent-episodes", () =>
      HttpResponse.json(feed()),
    ),
    http.get("/api/discover/metadata/shows/100", () =>
      HttpResponse.json({ data: { ...envelope, item: show } }),
    ),
    http.get("/api/discover/metadata/shows/100/seasons/2", () =>
      HttpResponse.json({
        data: {
          ...envelope,
          season: { id: 201, season: 2, episodes: [sourceEpisode()] },
        },
      }),
    ),
    http.get("/api/discover/metadata/shows/100/seasons/2/episodes/1", () =>
      HttpResponse.json({ data: { ...envelope, episode: resolved() } }),
    ),
    http.post("/api/discover/search", async ({ request }) => {
      searches.push(await request.json());
      return new HttpResponse(null, { status: 503 });
    }),
  );
});
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
it("browses true recent episode dates and partial coverage without provider requests", async () => {
  const { user } = browse();
  expect(
    await screen.findByRole("button", { name: /Northern Light.*Home/ }),
  ).toBeEnabled();
  expect(screen.getByText(/Incomplete episode coverage/)).toBeInTheDocument();
  expect(screen.getByText(/not a complete schedule/)).toBeInTheDocument();
  await user.click(
    screen.getByRole("button", { name: "Refresh recent episodes" }),
  );
  expect(searches).toEqual([]);
});
it("carries the show poster and keeps the missing-art tile in place", async () => {
  server.use(
    http.get("/api/discover/feeds/recent-episodes", () =>
      HttpResponse.json({
        ...feed(),
        items: [
          sourceEpisode(),
          {
            ...sourceEpisode(),
            source_id: "tmdb:show:100:episode:402",
            id: 402,
            episode: 2,
            title: "Away",
            poster_url: "https://image.tmdb.org/t/p/w342/show100.jpg",
          },
        ],
      }),
    ),
  );
  browse();
  const withArt = await screen.findByRole("button", {
    name: /Northern Light.*Away/,
  });
  expect(within(withArt).getByRole("presentation")).toHaveAttribute(
    "src",
    "https://image.tmdb.org/t/p/w342/show100.jpg",
  );
  const withoutArt = screen.getByRole("button", {
    name: /Northern Light.*Home/,
  });
  expect(
    within(withoutArt).queryByRole("presentation"),
  ).not.toBeInTheDocument();
  expect(withoutArt).toHaveTextContent("Artwork unavailable");
});
it("keeps a source date whole rather than breaking it across lines", async () => {
  browse();
  const item = await screen.findByRole("button", {
    name: /Northern Light.*Home/,
  });
  const stamp = within(item).getByText(day());
  expect(stamp.tagName).toBe("TIME");
  expect(stamp).toHaveAttribute("datetime", day());
  expect(stamp).toHaveClass(styles.dateValue);
});
it("opens the exact source episode and retains date, explicit target mapping and return focus", async () => {
  const { user, router } = browse();
  await user.click(
    await screen.findByRole("button", { name: /Northern Light.*Home/ }),
  );
  await screen.findByText(/Verified TVDB default order: season 3, episode 7/);
  expect(screen.getByLabelText("Selected recent episode")).toHaveTextContent(
    day(),
  );
  expect(searches).toEqual([]);
  await user.click(selectInput("Subtitle language"));
  await act(async () => {
    await router.navigate("/system/tasks");
  });
  await screen.findByText("Local activity");
  expect(
    screen.queryByRole("heading", { name: "Discover" }),
  ).not.toBeInTheDocument();
  await act(async () => {
    await router.navigate(-1);
  });
  await waitFor(() => expect(selectInput("Subtitle language")).toHaveFocus());
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).toMatchObject({
    media_type: "episode",
    season: 3,
    episode: 7,
    episode_identity: { episode: 1, season: 2, id: 401 },
    language: "eng",
  });
  await user.click(screen.getByRole("button", { name: /Back to/ }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: /Northern Light.*Home/ }),
    ).toHaveFocus(),
  );
});

it.each(["unverified", "conflict"])(
  "does not infer a provider episode for %s mapping",
  async (identityStatus) => {
    server.use(
      http.get("/api/discover/metadata/shows/100/seasons/2/episodes/1", () =>
        HttpResponse.json({
          data: {
            ...envelope,
            episode: {
              ...resolved(),
              identity_status: identityStatus,
              target_season: null,
              target_episode: null,
              numbering: null,
            },
          },
        }),
      ),
    );
    const { user } = browse();
    await user.click(
      await screen.findByRole("button", { name: /Northern Light.*Home/ }),
    );
    await screen.findByLabelText("Selected episode");
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    if (identityStatus === "unverified") {
      await user.click(
        screen.getByRole("button", { name: "Enter a manual episode" }),
      );
      await user.type(screen.getByLabelText("Season"), "3");
      await user.type(screen.getByRole("textbox", { name: "Episode" }), "7");
      expect(
        screen.getByRole("button", { name: "Find subtitles" }),
      ).toBeDisabled();
      await user.click(
        screen.getByRole("checkbox", { name: /I confirm this series IMDb ID/ }),
      );
      await user.click(screen.getByRole("button", { name: "Find subtitles" }));
      await waitFor(() => expect(searches).toHaveLength(1));
      expect(searches[0]).toMatchObject({
        manual_confirmed: true,
        season: 3,
        episode: 7,
        episode_identity: { id: 401, air_date: day() },
      });
    } else {
      expect(
        screen.getByText(/Source episode identities conflict/),
      ).toBeInTheDocument();
      expect(searches).toEqual([]);
    }
  },
);

it.each([
  { id: 402, source_id: "tmdb:show:100:episode:402" },
  { air_date: "2001-01-01" },
  { title: "Changed title" },
])(
  "requires explicit recovery when current detail differs from the source record %j",
  async (changed) => {
    server.use(
      http.get("/api/discover/metadata/shows/100/seasons/2/episodes/1", () =>
        HttpResponse.json({
          data: { ...envelope, episode: { ...resolved(), ...changed } },
        }),
      ),
    );
    const { user } = browse();
    await user.click(
      await screen.findByRole("button", { name: /Northern Light.*Home/ }),
    );
    const recovery = await screen.findByRole("button", {
      name: "Use current episode details",
    });
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    expect(screen.getByLabelText("Selected recent episode")).toHaveTextContent(
      day(),
    );
    expect(searches).toEqual([]);
    await user.click(recovery);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await waitFor(() => expect(searches).toHaveLength(1));
    expect(searches[0]).toMatchObject({ episode_identity: changed });
  },
);

it("keeps a source numbering conflict explicit until current details are deliberately accepted", async () => {
  server.use(
    http.get("/api/discover/feeds/recent-episodes", () =>
      HttpResponse.json({
        ...feed(),
        items: [{ ...sourceEpisode(), identity_status: "conflict" }],
      }),
    ),
  );
  const { user } = browse();
  await user.click(
    await screen.findByRole("button", { name: /Northern Light.*Home/ }),
  );
  await screen.findByRole("button", { name: "Use current episode details" });
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
  expect(searches).toEqual([]);
});

it.each(["empty", "unavailable", "authentication_failed", "cached", "expired"])(
  "presents %s freshness and availability without provider work",
  async (status) => {
    server.use(
      http.get("/api/discover/feeds/recent-episodes", () =>
        HttpResponse.json({
          ...feed(),
          status: status === "expired" ? "live" : status,
          items: ["cached", "expired"].includes(status) ? feed().items : [],
          ...(status === "expired"
            ? { stale_until: "2000-01-01T00:00:00Z" }
            : {}),
          ...(status === "cached" ? { service_status: "unavailable" } : {}),
        }),
      ),
    );
    browse();
    if (status === "empty")
      await screen.findByText(/No qualifying episodes were found/);
    if (status === "unavailable" || status === "expired")
      await screen.findByText(/Recent episodes are temporarily unavailable/);
    if (status === "authentication_failed")
      await screen.findByText(/TMDB rejected the key Discover is using/);
    if (status === "cached")
      await screen.findByText(/Cached TMDB episode records/);
    if (status !== "cached")
      expect(
        screen.queryByRole("button", { name: /Northern Light.*Home/ }),
      ).not.toBeInTheDocument();
    expect(searches).toEqual([]);
  },
);

it.each([
  { revision: "obsolete" },
  { window: { start: "2001-01-01", end: "2001-01-30" } },
  { scope: "global" },
  { period: "day" },
])("rejects obsolete feed identity %j", async (changed) => {
  server.use(
    http.get("/api/discover/feeds/recent-episodes", () =>
      HttpResponse.json({ ...feed(), ...changed }),
    ),
  );
  browse();
  await screen.findByText(/Recent episodes are temporarily unavailable/);
  expect(
    screen.queryByRole("button", { name: /Northern Light.*Home/ }),
  ).not.toBeInTheDocument();
  expect(searches).toEqual([]);
});

it("automatically recovers one busy startup admission without provider requests", async () => {
  let calls = 0;
  server.use(
    http.get("/api/discover/feeds/recent-episodes", () => {
      calls++;
      return HttpResponse.json(
        calls === 1
          ? {
              ...feed(),
              status: "unavailable",
              items: [],
              retry_after_ms: 1000,
            }
          : feed(),
      );
    }),
  );
  browse();
  expect(
    await screen.findByRole(
      "button",
      { name: /Northern Light.*Home/ },
      { timeout: 3000 },
    ),
  ).toBeEnabled();
  expect(calls).toBe(2);
  expect(searches).toEqual([]);
});

afterEach(() => focusManager.setFocused(undefined));
