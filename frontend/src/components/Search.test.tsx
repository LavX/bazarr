/* eslint-disable camelcase -- API fixtures use the server's field names. */

import { createMemoryRouter, RouterProvider } from "react-router";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import type { ArrInstance, ArrKind } from "@/apis/raw/arrInstances";
import Search from "@/components/Search";
import { rawRender, screen, within } from "@/tests";
import server from "@/tests/mocks/node";

function instance(
  id: number,
  kind: ArrKind,
  name: string,
  isDefault: boolean,
): ArrInstance {
  return {
    id,
    kind,
    name,
    display_name: name,
    stable_key: `${kind}-${id}`,
    enabled: true,
    is_default: isDefault,
    ip: "localhost",
    port: { sonarr: 8989, radarr: 7878, sportarr: 1867 }[kind] ?? 7878,
    base_url: "/",
    ssl: false,
    verify_ssl: true,
    http_timeout: 60,
    api_key_set: true,
  };
}

const instances = [
  instance(1, "sonarr", "HD Series", true),
  instance(2, "sonarr", "4K Series", false),
  instance(3, "radarr", "HD Movies", true),
  instance(4, "radarr", "4K Movies", false),
  instance(5, "sportarr", "HD Sports", true),
  instance(6, "sportarr", "4K Sports", false),
];

const results: ItemSearchResult[] = [
  {
    id: 101,
    sonarrSeriesId: 7,
    arr_instance_id: 1,
    title: "Café",
    year: "2020",
    poster: null,
  },
  {
    id: 102,
    sonarrSeriesId: 7,
    arr_instance_id: 2,
    title: "Café",
    year: "2020",
    poster: null,
  },
  {
    id: 201,
    radarrId: 7,
    arr_instance_id: 3,
    title: "Amélie",
    year: "2001",
    poster: null,
  },
  {
    id: 202,
    radarrId: 7,
    arr_instance_id: 4,
    title: "Amélie",
    year: "2001",
    poster: null,
  },
];

const leagues: ItemSearchResult[] = [
  {
    id: 301,
    sportarrLeagueId: 7,
    arr_instance_id: 5,
    title: "Formula 1",
    sport: "Motorsport",
    year: "",
    poster: null,
  },
  {
    id: 302,
    sportarrLeagueId: 7,
    arr_instance_id: 6,
    title: "Formula 1",
    sport: "Motorsport",
    year: "",
    poster: null,
  },
];

function renderSearch(
  configuredInstances = instances,
  searchResults = results,
) {
  server.use(
    http.get("/api/system/searches", () => HttpResponse.json(searchResults)),
    http.get("/api/system/arr-instances", () =>
      HttpResponse.json(configuredInstances),
    ),
  );

  const router = createMemoryRouter([
    { path: "/", element: <Search /> },
    { path: "/movies/:id", element: <p>Movie details</p> },
    { path: "/series/:id", element: <p>Series details</p> },
    { path: "/sports/:id", element: <p>League details</p> },
  ]);

  rawRender(
    <QueryClientProvider client={queryClient}>
      <MantineProvider env="test">
        <RouterProvider router={router} />
      </MantineProvider>
    </QueryClientProvider>,
  );

  return { router };
}

describe("Search Bar", () => {
  it("should render the closed empty state", () => {
    renderSearch([], []);

    expect(screen.getByPlaceholderText("Search")).toHaveValue("");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it.each([
    { query: "CAFE", owner: "HD Series", path: "/series/101" },
    { query: "CAFE", owner: "4K Series", path: "/series/102" },
    { query: "amelie", owner: "HD Movies", path: "/movies/201" },
    { query: "amelie", owner: "4K Movies", path: "/movies/202" },
  ])(
    "selects the duplicate owned by $owner using its local ID",
    async ({ query, owner, path }) => {
      const user = userEvent.setup();
      const { router } = renderSearch();

      await user.type(screen.getByPlaceholderText("Search"), query);

      const option = await screen.findByRole("option", {
        name: new RegExp(owner),
      });
      expect(screen.getAllByRole("option")).toHaveLength(2);
      await user.click(option);

      expect(router.state.location.pathname).toBe(path);
    },
  );

  it.each([
    { ids: [1, 3], seriesBadge: false, movieBadge: false },
    { ids: [1, 3, 4], seriesBadge: false, movieBadge: true },
    { ids: [1, 2, 3], seriesBadge: true, movieBadge: false },
  ])(
    "only labels kinds with multiple configured instances: $ids",
    async ({ ids, seriesBadge, movieBadge }) => {
      const user = userEvent.setup();
      renderSearch(
        instances.filter((item) => ids.includes(item.id)),
        [results[0], results[2]],
      );

      await user.type(screen.getByPlaceholderText("Search"), "a");

      const series = await screen.findByRole("option", { name: /Café/ });
      const movie = await screen.findByRole("option", { name: /Amélie/ });
      expect(within(series).queryByText("HD Series") !== null).toBe(
        seriesBadge,
      );
      expect(within(movie).queryByText("HD Movies") !== null).toBe(movieBadge);
    },
  );

  it("does not blow up the dropdown on a sports league", async () => {
    const user = userEvent.setup();
    const { router } = renderSearch(instances, leagues);

    // The backend has returned leagues since sports gained a nav tab, and the
    // dropdown threw "Unknown search result" on every one of them: typing
    // anything matching a league name broke the whole search.
    await user.type(screen.getByPlaceholderText("Search"), "formula");

    const option = await screen.findByRole("option", { name: /4K Sports/ });
    // A league carries a sport where a show or film carries a year.
    expect(option).toHaveTextContent("Formula 1 (Motorsport)");
    await user.click(option);
    expect(router.state.location.pathname).toBe("/sports/302");
    expect(router.state.location.search).toBe("?instance=6");
  });

  it("labels a league with its own Sportarr instances, not Radarr's", async () => {
    const user = userEvent.setup();
    // One Radarr, two Sportarr: "not a show means a movie" would read the
    // single-Radarr labels and show no badge at all.
    renderSearch(
      [instances[0], instances[2], instances[4], instances[5]],
      leagues,
    );

    await user.type(screen.getByPlaceholderText("Search"), "formula");

    const option = await screen.findByRole("option", { name: /HD Sports/ });
    expect(within(option).getByText("HD Sports")).toBeInTheDocument();
  });

  it("filters by title without matching the instance name", async () => {
    const user = userEvent.setup();
    renderSearch(
      [instances[2], instance(4, "radarr", "Amelie 4K", false)],
      [results[2], { ...results[3], title: "Inception", year: "2010" }],
    );

    await user.type(screen.getByPlaceholderText("Search"), "amelie");

    expect(
      await screen.findByRole("option", { name: /Amélie/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: /Inception/ }),
    ).not.toBeInTheDocument();
  });
});
