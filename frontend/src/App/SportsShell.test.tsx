/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import { act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { WHATS_NEW_SEEN_KEY } from "@/utilities/whatsNew";
import App from ".";

vi.mock("@/Router", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/Router")>();
  return { ...original, useRouteItems: original.useRoutes };
});

let enabled = true;
let mobile = false;

function renderShell(path = "/discover") {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <App />,
        children: [{ path: "*", element: <h1>Page content</h1> }],
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
  enabled = true;
  mobile = false;
  vi.mocked(window.matchMedia).mockImplementation((query) => ({
    matches: mobile && query === "(max-width: 47.99em)",
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
    new DOMRect(100, 100, 100, 40),
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1024);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(768);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(100);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(40);
  setAuthenticated(true);
  localStorage.setItem(WHATS_NEW_SEEN_KEY, latestWhatsNewVersion);
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {
          theme: "dark",
          setup_complete: true,
          use_sonarr: false,
          use_radarr: false,
          use_sportarr: enabled,
        },
        auth: { type: "form" },
      }),
    ),
    http.get("/api/badges", () =>
      HttpResponse.json({ sportarr_sse: "LIVE", sports: 2 }),
    ),
    http.get("/api/system/status", () =>
      HttpResponse.json({ data: { bazarr_version: "test" } }),
    ),
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/searches", () => HttpResponse.json([])),
  );
});

it("keeps Sports in the rail with its connection state and active detail route", async () => {
  const { user, router } = renderShell("/sports/17");
  const sports = await screen.findByRole("link", { name: "Sports" });
  expect(sports).toHaveAttribute("aria-current", "page");
  await waitFor(() =>
    expect(sports).toHaveAttribute("aria-description", "Connection live"),
  );
  expect(
    within(screen.getByRole("group", { name: "Media" })).getByRole("link", {
      name: "Sports",
    }),
  ).toBe(sports);
  await user.click(sports);
  expect(router.state.location.pathname).toBe("/sports");
});

it.each([
  ["Wanted", "wanted"],
  ["History", "history"],
  ["Excluded", "blacklist"],
])("keeps %s accessible for a Sports-only library", async (label, path) => {
  const { user, router } = renderShell();
  await user.click(await screen.findByRole("button", { name: label }));
  const sports = await screen.findByRole("menuitem", { name: /^Sports/ });
  expect(sports).toHaveAttribute("href", `/${path}/sports`);
  expect(
    screen.queryByRole("menuitem", { name: "Episodes" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("menuitem", { name: "Movies" }),
  ).not.toBeInTheDocument();
  await user.click(sports);
  expect(router.state.location.pathname).toBe(`/${path}/sports`);
});

it("switches disabled Sports to setup in the rail and global search", async () => {
  const { user, router } = renderShell();
  await screen.findByRole("link", { name: "Sports" });
  enabled = false;
  await act(async () => {
    await queryClient.invalidateQueries({
      queryKey: [QueryKeys.System, QueryKeys.Settings],
    });
  });
  const setup = await screen.findByRole("link", {
    name: "Sports, set up a library connection",
  });
  expect(setup).toHaveAttribute("href", "/settings/connections");
  expect(
    screen.queryByRole("button", { name: "Wanted" }),
  ).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Search"), "Sports");
  expect(
    await within(
      await screen.findByRole("region", { name: "Pages" }),
    ).findByRole("link", { name: "Sports" }),
  ).toHaveAttribute("href", "/settings/connections");
  await user.keyboard("{Escape}");
  await user.click(setup);
  expect(router.state.location.pathname).toBe("/settings/connections");
});

it.each([true, false])(
  "keeps Sports navigation usable on mobile when enabled is %s",
  async (isEnabled) => {
    mobile = true;
    enabled = isEnabled;
    const { user, router } = renderShell();
    const menu = await screen.findByRole("button", { name: "Open navigation" });
    await user.click(menu);
    const drawer = await screen.findByRole("dialog", { name: "Navigation" });
    const sports = await within(drawer).findByRole("link", {
      name: isEnabled ? /^Sports/ : "Set up sports",
    });
    expect(sports).toHaveAttribute(
      "href",
      isEnabled ? "/sports" : "/settings/connections",
    );
    await user.click(sports);
    expect(router.state.location.pathname).toBe(
      isEnabled ? "/sports" : "/settings/connections",
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Navigation" }),
      ).not.toBeInTheDocument(),
    );
    expect(menu).toHaveAttribute("aria-expanded", "false");
  },
);
