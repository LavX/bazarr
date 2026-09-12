/* eslint-disable camelcase -- API fixture fields. */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import { METADATA_QUERY_KEY } from "@/apis/hooks/discover";
import queryClient from "@/apis/queries";
import { DiscoverSetupReturn } from "@/contexts/Discover";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import * as files from "@/utilities/files";
import {
  findSelectInput,
  openReleaseSearch,
  openSearchOptions,
  pickOption,
  selectInput,
} from "./selectTestHelpers";
import Discover from "./testHarness";

const envelope = {
  source: "tmdb",
  status: "available",
  configured: true,
  revision: "episodes-one",
  locale: "en-US",
  message: "Available",
  checked_at: null,
  fetched_at: null,
};
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
  overview: "A non-library show.",
  poster_url: null,
  backdrop_url: null,
  seasons: [{ id: 201, season: 2, title: "Season 2", episode_count: 2 }],
};
const episode = {
  source: "tmdb",
  source_id: "tmdb:show:100:episode:401",
  show_id: 100,
  season_id: 201,
  id: 401,
  season: 2,
  episode: 1,
  title: "Home",
  air_date: "2026-09-01",
  imdb_id: "tt7654321",
  tvdb_id: 501,
  show_imdb_id: "tt1234567",
  show_tvdb_id: 300,
  show_title: "Northern Light",
  show_year: 2020,
  target_season: 2,
  target_episode: 1,
  numbering: "tvdb_default",
  identity_status: "resolved",
  absolute_episode: null,
  tvdb_absolute_number: null,
  mapping_updated_at: "2026-09-02",
};
let requests: unknown[] = [];
beforeEach(() => {
  localStorage.clear();
  requests = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: true,
          metadata_revision: "episodes-one",
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
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({ data: { ...envelope, items: [show] } }),
    ),
    http.get("/api/discover/metadata/shows/100", () =>
      HttpResponse.json({ data: { ...envelope, item: show } }),
    ),
    http.get("/api/discover/metadata/shows/100/seasons/2", () =>
      HttpResponse.json({
        data: {
          ...envelope,
          season: {
            id: 201,
            season: 2,
            episodes: [
              episode,
              { ...episode, id: 402, episode: 2, air_date: null },
            ],
          },
        },
      }),
    ),
    http.get("/api/discover/metadata/shows/100/seasons/2/episodes/1", () =>
      HttpResponse.json({ data: { ...envelope, episode } }),
    ),
    http.post("/api/discover/search", async ({ request }) => {
      requests.push(await request.json());
      return new HttpResponse(null, { status: 503 });
    }),
  );
});
function browse(initial = "/discover") {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      {
        path: "/subtitle-hub",
        element: (
          <DiscoverSetupReturn>
            <div>Settings</div>
          </DiscoverSetupReturn>
        ),
      },
    ],
    { initialEntries: [initial] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { router, user: userEvent.setup() };
}
it("opens an exact episode link with original name, number and date without searching providers", async () => {
  const { user } = browse("/discover?show=100&season=2&episode=1");
  expect(
    await screen.findByRole("heading", { name: "Northern Light" }),
  ).toBeInTheDocument();
  expect(await screen.findByDisplayValue("1. Home")).toBeInTheDocument();
  expect(requests).toEqual([]);
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]).toMatchObject({
    media_type: "episode",
    imdb_id: "tt1234567",
    title: "Northern Light",
    year: 2020,
    season: 2,
    episode: 1,
    episode_identity: episode,
  });
});
it("adopts a show without selecting an episode and offers its real season list", async () => {
  const { user } = browse();
  // The search mock answers with the show whatever the scope, so no tab
  // switch is needed to reach it here. Scope switching lives in the
  // trending tab test.
  await user.type(screen.getByLabelText("Search"), "Northern");
  await user.click(
    await screen.findByRole("button", { name: "Northern Light (2020)" }),
  );
  const season = await findSelectInput("Season");
  await waitFor(() => expect(season).toHaveValue("Season 2"));
  expect(await findSelectInput("Episode")).toHaveValue("");
  await pickOption(user, "Subtitle language", "English");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
  expect(requests).toEqual([]);
});

it("distinguishes an empty season from a metadata outage and offers confirmed manual recovery", async () => {
  server.use(
    http.get("/api/discover/metadata/shows/100/seasons/2", () =>
      HttpResponse.json({
        data: { ...envelope, status: "unavailable", season: null },
      }),
    ),
  );
  const { user } = browse("/discover?show=100&season=2");
  expect(
    await screen.findByText(/Episodes could not be loaded/),
  ).toBeInTheDocument();
  await user.click(
    screen.getByRole("button", { name: "Enter episode numbers manually" }),
  );
  await user.type(screen.getByRole("textbox", { name: "Season" }), "0");
  await user.type(screen.getByRole("textbox", { name: "Episode" }), "3");
  await pickOption(user, "Subtitle language", "English");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
  await user.click(
    screen.getByLabelText(
      "I confirm this series IMDb ID and the manual season and episode numbers",
    ),
  );
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]).toMatchObject({
    show_id: 100,
    imdb_id: "tt1234567",
    season: 0,
    episode: 3,
    manual_confirmed: true,
  });
  expect(requests[0]).not.toHaveProperty("episode_identity");
});

it("keeps conflicts blocked and preserves original source numbering for manual recovery", async () => {
  server.use(
    http.get("/api/discover/metadata/shows/100/seasons/2/episodes/1", () =>
      HttpResponse.json({
        data: {
          ...envelope,
          episode: {
            ...episode,
            identity_status: "conflict",
            target_season: null,
            target_episode: null,
            numbering: null,
          },
        },
      }),
    ),
  );
  const { user } = browse("/discover?show=100&season=2&episode=1");
  expect(
    await screen.findByText(
      /Episode numbering does not match between metadata sources/,
    ),
  ).toBeInTheDocument();
  await pickOption(user, "Subtitle language", "English");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: "Enter episode numbers manually" }),
  ).not.toBeInTheDocument();
  expect(requests).toEqual([]);
});

it("retires results and pending responses when an exact episode changes while retaining language", async () => {
  let finish: (() => void) | undefined;
  const second = {
    ...episode,
    id: 402,
    source_id: "tmdb:show:100:episode:402",
    episode: 2,
    target_episode: 2,
    air_date: null,
  };
  server.use(
    http.get("/api/discover/metadata/shows/100/seasons/2/episodes/2", () =>
      HttpResponse.json({ data: { ...envelope, episode: second } }),
    ),
    http.post("/api/discover/search", async ({ request }) => {
      const context = await request.json();
      requests.push(context);
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return HttpResponse.json({
        search_id: "obsolete",
        context,
        status: "complete",
        results: [],
        coverage: {
          providers: [],
          configured_count: 0,
          complete: true,
          completed_count: 0,
        },
      });
    }),
  );
  const { user } = browse("/discover?show=100&season=2&episode=1");
  await screen.findByDisplayValue("1. Home");
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(finish).toBeDefined());
  await pickOption(user, "Episode", "2. Home");
  await screen.findByDisplayValue("2. Home");
  await openSearchOptions(user);
  expect(screen.getByRole("textbox", { name: "Episode" })).toHaveValue("2");
  expect(selectInput("Subtitle language")).toHaveValue("English");
  finish?.();
  await waitFor(() =>
    expect(
      screen.queryByRole("heading", { name: "Subtitle results" }),
    ).not.toBeInTheDocument(),
  );
  expect(screen.queryByDisplayValue("1. Home")).not.toBeInTheDocument();
});

it("ignores a late episode metadata response after an exact link change", async () => {
  let finish: (() => void) | undefined;
  const second = {
    ...episode,
    id: 402,
    source_id: "tmdb:show:100:episode:402",
    episode: 2,
    target_episode: 2,
    air_date: null,
  };
  server.use(
    http.get(
      "/api/discover/metadata/shows/100/seasons/2/episodes/1",
      async () => {
        await new Promise<void>((resolve) => {
          finish = resolve;
        });
        return HttpResponse.json({ data: { ...envelope, episode } });
      },
    ),
    http.get("/api/discover/metadata/shows/100/seasons/2/episodes/2", () =>
      HttpResponse.json({ data: { ...envelope, episode: second } }),
    ),
  );
  const { user } = browse("/discover?show=100&season=2&episode=1");
  await waitFor(() => expect(finish).toBeDefined());
  await pickOption(user, "Episode", "2. Home");
  await screen.findByDisplayValue("2. Home");
  finish?.();
  await openSearchOptions(user);
  expect(screen.getByRole("textbox", { name: "Episode" })).toHaveValue("2");
  expect(requests).toEqual([]);
});

it("keeps an active raw query isolated from episode-link adoption", async () => {
  const { user, router } = browse();
  // The homepage carries no retrieval controls, so the release entry lives on
  // the detail. Enter the detail first via title search, then switch modes.
  // The search mock answers with the show whatever the scope.
  await user.type(screen.getByLabelText("Search"), "Northern");
  await user.click(
    await screen.findByRole("button", { name: "Northern Light (2020)" }),
  );
  await screen.findByRole("button", { name: "Search options" });
  await openReleaseSearch(user);
  await user.type(screen.getByLabelText("Release name"), "Other.Movie.2024");
  await pickOption(user, "Subtitle language", "English");
  await router.navigate("/discover?show=100&season=2&episode=1&mode=release");
  expect(screen.getByLabelText("Release name")).toHaveValue("Other.Movie.2024");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0]).toEqual({
    mode: "release",
    query: "Other.Movie.2024",
    language: "eng",
    refresh: false,
  });
  await user.click(
    screen.getByRole("button", { name: "Return to identified title" }),
  );
  expect(await screen.findByDisplayValue("1. Home")).toBeInTheDocument();
});

it.each([
  "?show=100&episode=1",
  "?show=100&season=2&episode=0",
  "?show=bad&season=2&episode=1",
])(
  "rejects incomplete episode links %s without a provider search",
  async (suffix) => {
    browse(`/discover${suffix}`);
    expect(
      await screen.findByText(
        /This title or episode link is incomplete or invalid/,
      ),
    ).toBeInTheDocument();
    // The homepage carries no retrieval controls, so there is no Find button
    // to disable. The pin is that an invalid link never searches providers.
    expect(
      screen.queryByRole("button", { name: "Find subtitles" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Subtitle language")).toBeNull();
    expect(screen.queryByLabelText("IMDb ID")).toBeNull();
    expect(requests).toEqual([]);
  },
);

it("preserves the exact episode through source outages and settings return", async () => {
  const { user, router } = browse("/discover?show=100&season=2&episode=1");
  await screen.findByDisplayValue("1. Home");
  await pickOption(user, "Subtitle language", "English");
  server.use(
    http.get("/api/discover/metadata/shows/100", () =>
      HttpResponse.json({
        data: { ...envelope, status: "unavailable", item: null },
      }),
    ),
    http.get("/api/discover/metadata/shows/100/seasons/2/episodes/1", () =>
      HttpResponse.json({
        data: { ...envelope, status: "unavailable", episode: null },
      }),
    ),
  );
  await queryClient.invalidateQueries({ queryKey: METADATA_QUERY_KEY });
  expect(screen.getByDisplayValue("1. Home")).toBeInTheDocument();
  // The application shell owns settings navigation; this fixture renders
  // only the page and the real interrupted-task return.
  await router.navigate("/subtitle-hub");
  await act(async () => {
    await router.navigate(-1);
  });
  expect(await screen.findByDisplayValue("1. Home")).toBeInTheDocument();
  await openSearchOptions(user);
  expect(screen.getByRole("textbox", { name: "Episode" })).toHaveValue("1");
  expect(selectInput("Subtitle language")).toHaveValue("English");
  expect(requests).toEqual([]);
});

function episodeSnapshot(context: unknown, id = "accepted") {
  return {
    search_id: id,
    context: { ...(context as object), matching_mode: "title" },
    status: "complete",
    checked_at: new Date().toISOString(),
    attempted_at: new Date().toISOString(),
    cache_status: "fresh",
    coverage: {
      complete: true,
      configured_count: 1,
      completed_count: 1,
      providers: [],
    },
    results: [
      {
        id: `${id}-row`,
        search_id: id,
        provider: "catalog-example",
        language: "en",
        language_variant: null,
        release: `${id} episode subtitle`,
        scope: "full",
        hearing_impaired: false,
        matches: [],
        compatibility_score: 0,
        compatibility_score_max: 357,
        rating: null,
        uploader: null,
        checked_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3600000).toISOString(),
        stale: false,
      },
    ],
  };
}

it.each([
  { child: "missing", manual: false },
  { child: "stale", manual: false },
  { child: "missing", manual: true },
  { child: "stale", manual: true },
])(
  "retires results, feedback and active refresh when the parent changes with a $child child and manual=$manual",
  async ({ child, manual }) => {
    let finish: (() => void) | undefined;
    const save = vi
      .spyOn(files, "saveBlobAs")
      .mockImplementation(() => undefined);
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        const context = await request.json();
        requests.push(context);
        if (requests.length > 1)
          await new Promise<void>((resolve) => {
            finish = resolve;
          });
        return HttpResponse.json(
          episodeSnapshot(context, requests.length > 1 ? "late" : "accepted"),
        );
      }),
      http.get(
        "/api/discover/download",
        () =>
          new HttpResponse("1\n00:00:01,000 --> 00:00:02,000\nHome\n\n", {
            headers: {
              "Content-Type": "application/x-subrip",
              "Content-Disposition": 'attachment; filename="Home.srt"',
            },
          }),
      ),
    );
    try {
      const { user } = browse("/discover?show=100&season=2&episode=1");
      await screen.findByDisplayValue("1. Home");
      if (manual) {
        await openSearchOptions(user);
        await user.click(
          screen.getByRole("button", {
            name: "Enter episode numbers manually",
          }),
        );
        await user.type(screen.getByRole("textbox", { name: "Season" }), "0");
        await user.type(screen.getByRole("textbox", { name: "Episode" }), "3");
        await user.click(
          screen.getByLabelText(
            "I confirm this series IMDb ID and the manual season and episode numbers",
          ),
        );
      }
      await pickOption(user, "Subtitle language", "English");
      await user.click(screen.getByRole("button", { name: "Find subtitles" }));
      await user.click(
        await screen.findByRole("button", { name: "Download SRT" }),
      );
      await screen.findByText(/Download started for/);
      await user.click(screen.getByRole("button", { name: "Search again" }));
      await waitFor(() => expect(finish).toBeDefined());
      server.use(
        http.get("/api/discover/metadata/shows/100", () =>
          HttpResponse.json({
            data: { ...envelope, item: { ...show, tvdb_id: 999 } },
          }),
        ),
        http.get("/api/discover/metadata/shows/100/seasons/2/episodes/1", () =>
          HttpResponse.json({
            data: {
              ...envelope,
              status: child === "missing" ? "unavailable" : "cached",
              episode: child === "missing" ? null : episode,
            },
          }),
        ),
      );
      await queryClient.invalidateQueries({ queryKey: METADATA_QUERY_KEY });
      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "Download SRT" }),
        ).not.toBeInTheDocument(),
      );
      expect(
        screen.queryByText(/Download started for/),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Find subtitles" }),
      ).toBeDisabled();
      expect(
        screen.queryByText(/Verified TVDB default order/),
      ).not.toBeInTheDocument();
      expect(screen.getByText(/Show details changed/)).toBeInTheDocument();
      expect(
        screen.queryByRole("button", {
          name: "Enter episode numbers manually",
        }),
      ).not.toBeInTheDocument();
      expect(screen.getByDisplayValue("1. Home")).toBeInTheDocument();
      expect(selectInput("Subtitle language")).toHaveValue("English");
      finish?.();
      await waitFor(() =>
        expect(
          screen.queryByRole("heading", { name: "late episode subtitle" }),
        ).not.toBeInTheDocument(),
      );
      expect(save).toHaveBeenCalledTimes(1);
    } finally {
      finish?.();
      save.mockRestore();
    }
  },
);

it.each([
  { outage: "unavailable", manual: false },
  { outage: "partial", manual: false },
  { outage: "unavailable", manual: true },
  { outage: "partial", manual: true },
])(
  "preserves accepted results, feedback and active refresh for an unchanged parent during a $outage outage and manual=$manual",
  async ({ outage, manual }) => {
    let finish: (() => void) | undefined;
    const save = vi
      .spyOn(files, "saveBlobAs")
      .mockImplementation(() => undefined);
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        const context = await request.json();
        requests.push(context);
        const id = requests.length > 1 ? "refreshed" : "accepted";
        if (requests.length > 1)
          await new Promise<void>((resolve) => {
            finish = resolve;
          });
        return HttpResponse.json(episodeSnapshot(context, id));
      }),
      http.get(
        "/api/discover/download",
        () =>
          new HttpResponse("1\n00:00:01,000 --> 00:00:02,000\nHome\n\n", {
            headers: { "Content-Type": "application/x-subrip" },
          }),
      ),
    );
    try {
      const { user } = browse("/discover?show=100&season=2&episode=1");
      await screen.findByDisplayValue("1. Home");
      if (manual) {
        await openSearchOptions(user);
        await user.click(
          screen.getByRole("button", {
            name: "Enter episode numbers manually",
          }),
        );
        await user.type(screen.getByRole("textbox", { name: "Season" }), "0");
        await user.type(screen.getByRole("textbox", { name: "Episode" }), "3");
        await user.click(
          screen.getByLabelText(
            "I confirm this series IMDb ID and the manual season and episode numbers",
          ),
        );
      }
      await pickOption(user, "Subtitle language", "English");
      await user.click(screen.getByRole("button", { name: "Find subtitles" }));
      await user.click(
        await screen.findByRole("button", { name: "Download SRT" }),
      );
      await screen.findByText(/Download started for/);
      await user.click(screen.getByRole("button", { name: "Search again" }));
      await waitFor(() => expect(finish).toBeDefined());
      server.use(
        http.get("/api/discover/metadata/shows/100", () =>
          HttpResponse.json({
            data: {
              ...envelope,
              status: "cached",
              service_status: "unavailable",
              unavailable_dependency: "show_external_ids",
              item: show,
            },
          }),
        ),
        http.get("/api/discover/metadata/shows/100/seasons/2/episodes/1", () =>
          HttpResponse.json({
            data: {
              ...envelope,
              status: outage === "partial" ? "cached" : "unavailable",
              service_status: "unavailable",
              unavailable_dependency: "episode_external_ids",
              episode: outage === "partial" ? episode : null,
            },
          }),
        ),
      );
      await queryClient.invalidateQueries({ queryKey: METADATA_QUERY_KEY });
      expect(
        screen.getByRole("heading", { name: "accepted episode subtitle" }),
      ).toBeInTheDocument();
      expect(screen.getByText(/Download started for/)).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Finding subtitles" }),
      ).toBeInTheDocument();
      if (manual) {
        expect(
          screen.getByLabelText(
            "I confirm this series IMDb ID and the manual season and episode numbers",
          ),
        ).toBeChecked();
        expect(screen.getByRole("textbox", { name: "Season" })).toHaveValue(
          "0",
        );
        expect(screen.getByRole("textbox", { name: "Episode" })).toHaveValue(
          "3",
        );
        expect(requests[1]).toMatchObject({
          manual_confirmed: true,
          episode_identity: episode,
        });
      }
      finish?.();
      expect(
        await screen.findByRole("heading", {
          name: "refreshed episode subtitle",
        }),
      ).toBeInTheDocument();
    } finally {
      finish?.();
      save.mockRestore();
    }
  },
);
