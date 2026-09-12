/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { WHATS_NEW_SEEN_KEY } from "@/utilities/whatsNew";
import App from ".";

const state = vi.hoisted(() => ({ sonarr: true, radarr: true }));

vi.mock("@/Router", () => ({
  useRouteItems: () => [
    {
      path: "/",
      children: [
        { path: "discover", name: "Discover", element: <div /> },
        {
          path: "series",
          name: "Series",
          hidden: !state.sonarr,
          children: [{ index: true, element: <div /> }],
        },
        {
          path: "movies",
          name: "My movies",
          hidden: !state.radarr,
          children: [{ index: true, element: <div /> }],
        },
        {
          path: "wanted",
          name: "Missing",
          hidden: !state.sonarr && !state.radarr,
          children: [
            {
              path: "series",
              name: "Episodes",
              hidden: !state.sonarr,
              element: <div />,
            },
            {
              path: "movies",
              name: "Movies",
              hidden: !state.radarr,
              element: <div />,
            },
          ],
        },
        {
          path: "history",
          name: "History",
          hidden: !state.sonarr && !state.radarr,
          children: [
            {
              path: "series",
              name: "Episodes",
              hidden: !state.sonarr,
              element: <div />,
            },
            {
              path: "movies",
              name: "Movies",
              hidden: !state.radarr,
              element: <div />,
            },
          ],
        },
        { path: "subtitle-hub", name: "Subtitle Hub", element: <div /> },
        {
          path: "settings",
          name: "Settings",
          children: [
            { path: "connections", name: "Connections", element: <div /> },
            { path: "general", name: "General", element: <div /> },
          ],
        },
        {
          path: "system",
          name: "System",
          children: [
            { path: "tasks", name: "Tasks", element: <div /> },
            { path: "status", name: "Status", element: <div /> },
          ],
        },
      ],
    },
  ],
}));

function renderShell(path = "/discover") {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <App />,
        children: [
          {
            path: "*",
            element: (
              <>
                <h1>Page content</h1>
              </>
            ),
          },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { router, user: userEvent.setup() };
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.mocked(window.matchMedia).mockImplementation((query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  // Floating menus need real layout bounds; jsdom otherwise reports zero-area anchors.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
    new DOMRect(100, 100, 100, 40),
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1024);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(768);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(100);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(40);
  state.sonarr = true;
  state.radarr = true;
  setAuthenticated(true);
  localStorage.setItem(WHATS_NEW_SEEN_KEY, latestWhatsNewVersion);
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {
          theme: "dark",
          setup_complete: true,
          use_sonarr: state.sonarr,
          use_radarr: state.radarr,
        },
        auth: { type: "form" },
      }),
    ),
    http.get("/api/system/status", () =>
      HttpResponse.json({ data: { bazarr_version: "test" } }),
    ),
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/searches", () => HttpResponse.json([])),
  );
});

describe("Discover application shell", () => {
  it("keeps the same header, search and compact navigation across routes", async () => {
    const { router, user } = renderShell();
    await screen.findByRole("heading", { name: "Page content" });
    const header = screen.getByRole("banner");
    const search = screen.getByLabelText("Search");
    expect(header).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Subtitle Hub" }));
    expect(router.state.location.pathname).toBe("/subtitle-hub");
    expect(await screen.findByRole("banner")).toBe(header);
    expect(screen.getByLabelText("Search")).toBe(search);
  });

  it("sends Wanted to an enabled library and keeps Activity on the real tasks route", async () => {
    state.sonarr = false;
    const { user, router } = renderShell();
    const nav = await screen.findByRole("navigation", {
      name: "Main navigation",
    });
    await user.click(within(nav).getByRole("button", { name: "Wanted" }));
    expect(
      await screen.findByRole("menuitem", { name: "Movies" }),
    ).toHaveAttribute("href", "/wanted/movies");
    expect(
      screen.queryByRole("menuitem", { name: "Episodes" }),
    ).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("menu")).not.toBeInTheDocument(),
    );
    await user.click(within(nav).getByRole("button", { name: "System" }));
    await user.click(await screen.findByRole("menuitem", { name: "Tasks" }));
    expect(router.state.location.pathname).toBe("/system/tasks");
  });

  it("uses the compact shell on Discover settings", async () => {
    renderShell("/settings/discover");
    expect(await screen.findByRole("banner")).toBeInTheDocument();
    expect(screen.getByLabelText("Search")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Series" })).toBeInTheDocument();
  });

  it("offers setup instead of presenting disabled libraries as configured", async () => {
    state.sonarr = false;
    state.radarr = false;
    const { user, router } = renderShell();
    const nav = await screen.findByRole("navigation", {
      name: "Main navigation",
    });
    expect(
      within(nav).getByRole("link", {
        name: "Series, set up a library connection",
      }),
    ).toHaveAttribute("href", "/settings/connections");
    expect(
      within(nav).getByRole("link", {
        name: "Movies, set up a library connection",
      }),
    ).toHaveAttribute("href", "/settings/connections");
    expect(
      within(nav).queryByRole("link", { name: "Wanted" }),
    ).not.toBeInTheDocument();
    await user.click(
      within(nav).getByRole("link", {
        name: "Series, set up a library connection",
      }),
    );
    expect(router.state.location.pathname).toBe("/settings/connections");
  });

  it("keeps settings and the jobs manager reachable through System", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      new DOMRect(100, 100, 100, 40),
    );
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1024);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(768);
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(100);
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(40);
    const { user } = renderShell();
    await screen.findByRole("heading", { name: "Page content" });
    await user.click(screen.getByRole("button", { name: "Settings" }));
    expect(
      await screen.findByRole("menuitem", { name: "General" }),
    ).toHaveAttribute("href", "/settings/general");
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("menu")).not.toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "Jobs Manager" }));
    expect(
      await screen.findByRole("dialog", { name: "Jobs Manager" }),
    ).toBeInTheDocument();
  });

  it("opens the mobile drawer, dismisses it with Escape, and closes it after navigation", async () => {
    vi.mocked(window.matchMedia).mockImplementation((query) => ({
      matches: query === "(max-width: 47.99em)",
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    const { user, router } = renderShell();
    const menu = await screen.findByRole("button", { name: "Open navigation" });
    await user.click(menu);
    await screen.findByRole("dialog", { name: "Navigation" });
    expect(menu).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Navigation" }),
      ).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(menu).toHaveFocus());
    expect(menu).toHaveAttribute("aria-expanded", "false");
    await user.click(menu);
    const drawer = await screen.findByRole("dialog", { name: "Navigation" });
    await user.click(
      await within(drawer).findByRole("link", { name: "Subtitle Hub" }),
    );
    expect(router.state.location.pathname).toBe("/subtitle-hub");
    expect(menu).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps Jobs Manager available after closing the mobile navigation drawer", async () => {
    vi.mocked(window.matchMedia).mockImplementation((query) => ({
      matches: query === "(max-width: 47.99em)",
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    const { user } = renderShell();
    await user.click(
      await screen.findByRole("button", { name: "Open navigation" }),
    );
    const drawer = await screen.findByRole("dialog", { name: "Navigation" });
    await user.click(
      within(drawer).getByRole("button", { name: "Jobs Manager" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Jobs Manager" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Navigation" }),
      ).not.toBeInTheDocument(),
    );
    expect(
      screen.getByRole("dialog", { name: "Jobs Manager" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Open navigation", hidden: true }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("switches appearance from the bottom app controls", async () => {
    const { user } = renderShell();
    await user.click(
      await screen.findByRole("button", { name: "App controls" }),
    );
    await user.click(
      await screen.findByRole("menuitem", {
        name: "Switch to light appearance",
      }),
    );
    await waitFor(() =>
      expect(document.documentElement).toHaveAttribute(
        "data-mantine-color-scheme",
        "light",
      ),
    );
  });
});

it("uses the same disabled-library setup destination in global search and navigation", async () => {
  state.sonarr = false;
  const { user } = renderShell();
  const search = await screen.findByLabelText("Search");
  await user.type(search, "Series");
  expect(
    await within(screen.getByRole("region", { name: "Pages" })).findByRole(
      "link",
      { name: "Series" },
    ),
  ).toHaveAttribute("href", "/settings/connections");
});

it("identifies the current System subpage and preserves running-job feedback in bottom controls", async () => {
  server.use(
    http.get("/api/system/jobs", () =>
      HttpResponse.json({
        data: [{ name: "Subtitle search", status: "running" }],
      }),
    ),
  );
  renderShell("/system/tasks");
  const header = await screen.findByRole("banner");
  expect(within(header).getByText("Tasks")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "System" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Jobs Manager" }),
    ).toHaveAttribute("aria-description", "1 running"),
  );
});
