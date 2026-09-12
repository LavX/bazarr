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
import { rawRender, screen, waitFor } from "@/tests";
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
