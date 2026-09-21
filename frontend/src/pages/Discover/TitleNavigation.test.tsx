/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import Discover from "./testHarness";

/**
 * Leaving a title for another one.
 *
 * Metadata queries keep the previous answer as placeholder data while the next
 * one is in flight, so for a render after the reader picks a new title the
 * page still holds the departed one. A single-season series answers that
 * moment with its own season redirect, which used to land on top of the
 * incoming title and put the reader back where they started. These cases pin
 * the departure for every combination of kinds, and the case below them pins
 * the redirect that is supposed to happen.
 */

const envelope = {
  source: "tmdb",
  status: "available",
  configured: true,
  revision: "navigation-one",
  locale: "en-US",
  message: "Available",
  checked_at: null,
  fetched_at: null,
};
const common = {
  source: "tmdb",
  mapping_status: "resolved",
  poster_url: null,
  backdrop_url: null,
  overview: "A title.",
};
// One season, so opening it puts a season in the URL by itself.
const soleSeasonShow = {
  ...common,
  source_id: "tmdb:show:100",
  id: 100,
  media_type: "show",
  title: "Northern Light",
  year: 2020,
  imdb_id: "tt1000000",
  tvdb_id: 300,
  seasons: [{ id: 201, season: 2, title: "Season 2", episode_count: 2 }],
};
const otherShow = {
  ...common,
  source_id: "tmdb:show:200",
  id: 200,
  media_type: "show",
  title: "Southern Cross",
  year: 2021,
  imdb_id: "tt2000000",
  tvdb_id: 301,
  seasons: [
    { id: 301, season: 1, title: "Season 1", episode_count: 2 },
    { id: 302, season: 2, title: "Season 2", episode_count: 2 },
  ],
};
const movie = {
  ...common,
  source_id: "tmdb:movie:11",
  id: 11,
  media_type: "movie",
  title: "Eastern Front",
  year: 1999,
  imdb_id: "tt1100000",
};

beforeEach(() => {
  localStorage.clear();
  queryClient.clear();
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: true,
          metadata_revision: "navigation-one",
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
    http.get("/api/discover/metadata/search", ({ request }) => {
      const query = (
        new URL(request.url).searchParams.get("q") ?? ""
      ).toLowerCase();
      return HttpResponse.json({
        data: {
          ...envelope,
          items: [soleSeasonShow, otherShow, movie].filter((item) =>
            item.title.toLowerCase().includes(query),
          ),
        },
      });
    }),
    http.get("/api/discover/metadata/shows/100", () =>
      HttpResponse.json({ data: { ...envelope, item: soleSeasonShow } }),
    ),
    http.get("/api/discover/metadata/shows/200", () =>
      HttpResponse.json({ data: { ...envelope, item: otherShow } }),
    ),
    http.get("/api/discover/metadata/movies/11", () =>
      HttpResponse.json({ data: { ...envelope, item: movie } }),
    ),
    http.get(
      "/api/discover/metadata/shows/:show/seasons/:season",
      ({ params }) =>
        HttpResponse.json({
          data: {
            ...envelope,
            season: {
              id: 201,
              season: Number(params.season),
              episodes: [],
            },
          },
        }),
    ),
  );
});

function browse(initial: string) {
  const router = createMemoryRouter(
    [{ path: "/discover", element: <Discover /> }],
    { initialEntries: [initial] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { router, user: userEvent.setup() };
}

function url(router: ReturnType<typeof browse>["router"]) {
  return router.state.location.pathname + router.state.location.search;
}

async function pickFromSearch(
  user: ReturnType<typeof userEvent.setup>,
  query: string,
  name: string,
) {
  const field = screen.getByLabelText("Search");
  await user.clear(field);
  await user.type(field, query);
  await user.click(await screen.findByRole("button", { name }));
}

it("opens a different series from a single-season series page", async () => {
  const { router, user } = browse("/discover?show=100&season=2");
  expect(
    await screen.findByRole("heading", { name: "Northern Light" }),
  ).toBeInTheDocument();
  await pickFromSearch(user, "Southern", "Southern Cross (2021)");
  expect(
    await screen.findByRole("heading", { name: "Southern Cross" }),
  ).toBeInTheDocument();
  await waitFor(() => expect(url(router)).toBe("/discover?show=200"));
  expect(screen.queryByRole("heading", { name: "Northern Light" })).toBeNull();
});

it("opens a movie from a single-season series page", async () => {
  const { router, user } = browse("/discover?show=100&season=2");
  expect(
    await screen.findByRole("heading", { name: "Northern Light" }),
  ).toBeInTheDocument();
  await pickFromSearch(user, "Eastern", "Eastern Front (1999)");
  expect(
    await screen.findByRole("heading", { name: "Eastern Front" }),
  ).toBeInTheDocument();
  await waitFor(() => expect(url(router)).toBe("/discover?movie=11"));
});

it("opens a series from a movie page", async () => {
  const { router, user } = browse("/discover?movie=11");
  expect(
    await screen.findByRole("heading", { name: "Eastern Front" }),
  ).toBeInTheDocument();
  await pickFromSearch(user, "Northern", "Northern Light (2020)");
  expect(
    await screen.findByRole("heading", { name: "Northern Light" }),
  ).toBeInTheDocument();
  // The incoming series has one season, so its own default lands in the URL.
  await waitFor(() => expect(url(router)).toBe("/discover?show=100&season=2"));
});

it("stays on the same series when it is picked again", async () => {
  const { router, user } = browse("/discover?show=100&season=2");
  expect(
    await screen.findByRole("heading", { name: "Northern Light" }),
  ).toBeInTheDocument();
  await pickFromSearch(user, "Northern", "Northern Light (2020)");
  expect(
    await screen.findByRole("heading", { name: "Northern Light" }),
  ).toBeInTheDocument();
  await waitFor(() => expect(url(router)).toBe("/discover?show=100&season=2"));
  await pickFromSearch(user, "Northern", "Northern Light (2020)");
  expect(
    await screen.findByRole("heading", { name: "Northern Light" }),
  ).toBeInTheDocument();
  await waitFor(() => expect(url(router)).toBe("/discover?show=100&season=2"));
});

it("keeps the only season of a series in the URL when it is opened", async () => {
  const { router } = browse("/discover?show=100");
  expect(
    await screen.findByRole("heading", { name: "Northern Light" }),
  ).toBeInTheDocument();
  await waitFor(() => expect(url(router)).toBe("/discover?show=100&season=2"));
});
