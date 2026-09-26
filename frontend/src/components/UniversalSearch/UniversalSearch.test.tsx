/* eslint-disable camelcase -- Backend fixtures. */
import { useMemo } from "react";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { DiscoverSetupReturn } from "@/contexts/Discover";
import { useSearchSource } from "@/contexts/UniversalSearch";
import { createSubtitleSearchSource } from "@/pages/SubtitleEditor/searchSource";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import UniversalSearch from ".";

beforeEach(() => {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "dark" },
        auth: { type: "none" },
        discover: {
          tmdb_configured: true,
          metadata_revision: "one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({
        data: { status: "available", source: "tmdb", configured: true },
      }),
    ),
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({
        data: { status: "available", source: "tmdb", items: [] },
      }),
    ),
    http.get("/api/system/searches", () =>
      HttpResponse.json([
        {
          id: 901,
          radarrId: 7,
          arr_instance_id: 2,
          title: "Arrival",
          year: "2016",
          poster: null,
        },
        {
          id: 902,
          radarrId: 7,
          arr_instance_id: 3,
          title: "Arrival",
          year: "2016",
          poster: null,
        },
      ]),
    ),
  );
});

function mount(editor?: React.ReactNode) {
  const router = createMemoryRouter(
    [
      {
        element: (
          <>
            <UniversalSearch />
            <DiscoverSetupReturn>
              <Outlet />
            </DiscoverSetupReturn>
          </>
        ),
        children: [
          { path: "/settings/general", element: <p>Settings page</p> },
          { path: "/movies/:id", element: <p>Library movie</p> },
          { path: "/series/:id", element: <p>Library series</p> },
          { path: "/sports/:id", element: <p>Library league</p> },
          { path: "/discover", element: <p>Discover title</p> },
          { path: "/editor", element: editor },
        ],
      },
    ],
    { initialEntries: [editor ? "/editor" : "/settings/general"] },
  );
  return {
    router,
    user: userEvent.setup(),
    ...rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    ),
  };
}

it("uses canonical library IDs, retains the global query across navigation, and dismisses results", async () => {
  const { router, user } = mount();
  const input = screen.getByLabelText("Search");
  await user.type(input, "Arrival");
  const links = await screen.findAllByRole("link", {
    name: /Arrival \(2016\)/,
  });
  expect(links[0]).toHaveAttribute("href", "/movies/901");
  expect(links[1]).toHaveAttribute("href", "/movies/902");
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
  expect(screen.queryByText(/No movies matched/)).not.toBeInTheDocument();
  expect(screen.queryByText(/No shows matched/)).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Search providers by release name" }),
  ).not.toBeInTheDocument();
  await user.click(links[1]);
  expect(router.state.location.pathname).toBe("/movies/902");
  expect(input).toHaveValue("Arrival");
  expect(
    screen.queryByText("Your Discover selection is saved."),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("region", { name: "In your library" }),
  ).not.toBeInTheDocument();
  await user.keyboard("{Control>}k{/Control}");
  expect(input).toHaveFocus();
  await screen.findByRole("region", { name: "In your library" });
  await user.keyboard("{Escape}");
  expect(input).toHaveAttribute("aria-expanded", "false");
});

// An owned title shows up twice for one query: once in the library group and
// once in the catalog. The library row going to the library page is the
// deliberate choice, and the catalog row still going to the Discover title is
// what keeps that title page reachable for something the reader owns. Pinned
// together so the two groups cannot quietly collapse onto one target.
it.each([
  {
    kind: "movie",
    owned: { id: 901, radarrId: 7, arr_instance_id: 2, year: "2016" },
    library: "/movies/901",
    catalog: { id: 329865, year: 2016 },
    discover: "?movie=329865",
  },
  {
    kind: "show",
    owned: { id: 77, sonarrSeriesId: 5, arr_instance_id: 1, year: "2019" },
    library: "/series/77",
    catalog: { id: 88001, year: 2019 },
    discover: "?show=88001",
  },
] as const)(
  "sends the catalog $kind row for an owned title to Discover and its library row to the library",
  async ({ kind, owned, library, catalog, discover }) => {
    server.use(
      http.get("/api/system/searches", () =>
        HttpResponse.json([{ ...owned, title: "Arrival", poster: null }]),
      ),
      http.get("/api/discover/metadata/search", ({ request }) => {
        const type = new URL(request.url).searchParams.get("type") ?? "movie";
        return HttpResponse.json({
          data: {
            status: "available",
            source: "tmdb",
            revision: "one",
            items:
              type === kind
                ? [
                    {
                      source: "tmdb",
                      source_id: `tmdb:${kind}:${catalog.id}`,
                      id: catalog.id,
                      media_type: kind,
                      title: "Arrival",
                      year: catalog.year,
                      mapping_status: "resolved",
                    },
                  ]
                : [],
          },
        });
      }),
    );
    const { router, user } = mount();
    await user.type(screen.getByLabelText("Search"), "Arrival");

    const ownGroup = await screen.findByRole("region", {
      name: "In your library",
    });
    const label = `Arrival (${catalog.year})`;
    expect(
      within(ownGroup).getByRole("link", {
        name: new RegExp(`^Arrival \\(${catalog.year}\\)`),
      }),
    ).toHaveAttribute("href", library);
    const candidate = await screen.findByRole("button", { name: label });
    expect(ownGroup).not.toContainElement(candidate);

    await user.click(candidate);
    expect(router.state.location.pathname).toBe("/discover");
    expect(router.state.location.search).toBe(discover);
  },
);

it("searches the registered subtitle and retires its results when leaving the editor", async () => {
  const select = vi.fn();
  function Editor() {
    const source = useMemo(
      () =>
        createSubtitleSearchSource(
          [
            {
              id: "cue-1",
              startMs: 84000,
              endMs: 87000,
              text: "There is more to this place than we know?",
            },
          ],
          select,
        ),
      [],
    );
    useSearchSource(source);
    return <p>Open subtitle</p>;
  }
  const { router, user } = mount(<Editor />);
  await user.type(screen.getByLabelText("Search"), "?");
  await screen.findByRole("button", { name: /There is more to this place/ });
  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "Run search" }));
  await user.click(
    await screen.findByRole("button", { name: /There is more to this place/ }),
  );
  expect(select).toHaveBeenCalledWith(0);
  await router.navigate("/settings/general");
  await user.click(screen.getByLabelText("Search"));
  await waitFor(() =>
    expect(
      screen.queryByRole("region", { name: "In this subtitle" }),
    ).not.toBeInTheDocument(),
  );
});

it("keeps both films and shows visible when each catalog source has many matches", async () => {
  server.use(
    http.get("/api/discover/metadata/search", ({ request }) => {
      const kind = new URL(request.url).searchParams.get("type") ?? "movie";
      return HttpResponse.json({
        data: {
          status: "available",
          source: "tmdb",
          revision: "one",
          items: Array.from({ length: 20 }, (_, index) => ({
            source: "tmdb",
            source_id: `tmdb:${kind}:${index}`,
            id: index + 1,
            media_type: kind,
            title: `${kind} match ${index}`,
            year: 2020,
            mapping_status: "resolved",
          })),
        },
      });
    }),
  );
  const { user } = mount();
  await user.type(screen.getByLabelText("Search"), "match");
  await screen.findByRole("button", { name: "show match 0 (2020)" });
  expect(
    screen.getByRole("button", { name: "movie match 0 (2020)" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "movie match 19 (2020)" }),
  ).not.toBeInTheDocument();
});

it("waits for both catalog sources before announcing no matches", async () => {
  let finishShows: (() => void) | undefined;
  server.use(
    http.get("/api/system/searches", () => HttpResponse.json([])),
    http.get("/api/discover/metadata/search", async ({ request }) => {
      if (new URL(request.url).searchParams.get("type") === "show") {
        await new Promise<void>((resolve) => {
          finishShows = resolve;
        });
      }
      return HttpResponse.json({
        data: {
          status: "available",
          source: "tmdb",
          revision: "one",
          items: [],
        },
      });
    }),
  );
  const { user } = mount();
  await user.type(screen.getByLabelText("Search"), "unknown title");
  await waitFor(() => expect(finishShows).toBeDefined());
  expect(
    screen.queryByText("No catalog titles matched this search."),
  ).not.toBeInTheDocument();
  finishShows!();
  expect(
    await screen.findByText("No catalog titles matched this search."),
  ).toBeInTheDocument();
});

it.each(["click", "keyboard"])(
  "opens owned Sports league matches using %s",
  async (selection) => {
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { theme: "dark", use_sportarr: true } }),
      ),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([
          { id: 42, kind: "sportarr", enabled: true },
          { id: 43, kind: "sportarr", enabled: true },
        ]),
      ),
      http.get("/api/system/searches", () =>
        HttpResponse.json([
          {
            id: 51,
            sportarrLeagueId: 7,
            arr_instance_id: 42,
            title: "Formula One",
            sport: "Motorsport",
            year: "",
            poster: null,
          },
          {
            id: 52,
            sportarrLeagueId: 7,
            arr_instance_id: 43,
            title: "Formula One",
            sport: "Motorsport",
            year: "",
            poster: null,
          },
          {
            id: 901,
            radarrId: 7,
            arr_instance_id: 2,
            title: "Formula movie",
            year: "2020",
            poster: null,
          },
          {
            id: 902,
            sonarrSeriesId: 7,
            arr_instance_id: 3,
            title: "Formula series",
            year: "2021",
            poster: null,
          },
        ]),
      ),
    );
    const { router, user } = mount();
    await user.type(screen.getByLabelText("Search"), "Formula");
    const leagues = await screen.findAllByRole("link", { name: /Formula One/ });
    expect(leagues).toHaveLength(2);
    expect(leagues[0]).toHaveAttribute("href", "/sports/51?instance=42");
    expect(leagues[1]).toHaveAttribute("href", "/sports/52?instance=43");
    expect(screen.getByRole("link", { name: /Formula movie/ })).toHaveAttribute(
      "href",
      "/movies/901",
    );
    expect(
      screen.getByRole("link", { name: /Formula series/ }),
    ).toHaveAttribute("href", "/series/902");
    if (selection === "click") await user.click(leagues[1]);
    else {
      await user.keyboard("{ArrowDown}{ArrowDown}");
      expect(leagues[1]).toHaveFocus();
      await user.keyboard("{Enter}");
    }
    expect(router.state.location.pathname).toBe("/sports/52");
    expect(router.state.location.search).toBe("?instance=43");
    expect(
      screen.queryByRole("region", { name: "In your library" }),
    ).not.toBeInTheDocument();
  },
);

it("hides Sports search matches while the master toggle is disabled", async () => {
  server.use(
    http.get("/api/system/searches", () =>
      HttpResponse.json([
        {
          id: 51,
          sportarrLeagueId: 7,
          arr_instance_id: 42,
          title: "Formula One",
          year: "",
          poster: null,
        },
        {
          id: 901,
          radarrId: 7,
          arr_instance_id: 2,
          title: "Formula movie",
          year: "2020",
          poster: null,
        },
      ]),
    ),
  );
  const { user } = mount();
  await user.type(screen.getByLabelText("Search"), "Formula");
  await screen.findByRole("link", { name: /Formula movie/ });
  expect(
    screen.queryByRole("link", { name: /Formula One/ }),
  ).not.toBeInTheDocument();
});
