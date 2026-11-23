/* eslint-disable camelcase -- API fixtures retain transport field names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { AllProviders } from "@/providers";
import { Router } from "@/Router";
import { cleanup, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { WHATS_NEW_SEEN_KEY } from "@/utilities/whatsNew";

const injectedEnvironment = window.Bazarr;

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
    new DOMRect(100, 100, 100, 40),
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1024);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(768);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(100);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(40);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.mocked(window.matchMedia).mockReset();
  window.history.replaceState(null, "", "/");
  window.Bazarr = injectedEnvironment;
  localStorage.removeItem(WHATS_NEW_SEEN_KEY);
  setAuthenticated(false);
});

type Surface = "desktop" | "mobile";

const statusKey = [QueryKeys.System, "status"];
const surfaces: Surface[] = ["desktop", "mobile"];
const successfulStatuses = [
  {
    name: "ready",
    data: { translation_runtime_v2: true },
    runtime: true,
    legacy: false,
  },
  {
    name: "explicitly off",
    data: { translation_runtime_v2: false },
    runtime: false,
    legacy: true,
  },
  { name: "older server", data: {}, runtime: false, legacy: true },
  {
    name: "enabled but unavailable",
    data: { translation_runtime_v2_unavailable: true },
    runtime: false,
    legacy: false,
  },
];

function renderNavigation(surface: Surface) {
  vi.mocked(window.matchMedia).mockImplementation((query) => ({
    matches: surface === "mobile" && query === "(max-width: 47.99em)",
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "light", setup_complete: true },
        auth: { type: "form" },
      }),
    ),
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
    http.get("/api/badges", () => HttpResponse.json({})),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: { configured: false } }),
    ),
  );
  window.Bazarr = { baseUrl: "", canUpdate: false, hasUpdate: false };
  window.history.replaceState(null, "", "/settings");
  setAuthenticated(true);
  localStorage.setItem(WHATS_NEW_SEEN_KEY, latestWhatsNewVersion);
  rawRender(
    <AllProviders>
      <Router />
    </AllProviders>,
  );
  return { user: userEvent.setup() };
}

async function expectTranslationNavigation(
  surface: Surface,
  user: ReturnType<typeof userEvent.setup>,
  { runtime, legacy }: { runtime: boolean; legacy: boolean },
) {
  if (surface === "mobile") {
    await user.click(
      await screen.findByRole("button", { name: "Open navigation" }),
    );
    await screen.findByRole("dialog", { name: "Navigation" });
  }
  const nav = await screen.findByRole("navigation", {
    name: "Main navigation",
  });
  const translation = within(nav).queryByRole("link", {
    name: "Translation",
  });
  if (runtime) {
    expect(translation).toHaveAttribute("href", "/translation");
  } else {
    expect(translation).not.toBeInTheDocument();
  }

  const role = surface === "desktop" ? "button" : "link";
  await user.click(within(nav).getByRole(role, { name: "Settings" }));
  const settings =
    surface === "desktop" ? await screen.findByRole("menu") : nav;
  const itemRole = surface === "desktop" ? "menuitem" : "link";
  await waitFor(() =>
    expect(
      within(settings).getByRole(itemRole, { name: "Connections" }),
    ).toBeVisible(),
  );
  const translator = within(settings).queryByRole(itemRole, {
    name: "AI Translator",
    hidden: true,
  });
  if (legacy) {
    expect(translator).toHaveAttribute("href", "/settings/translator");
  } else {
    expect(translator).not.toBeInTheDocument();
  }
  if (surface === "desktop") {
    expect(
      within(settings).queryByRole("menuitem", { name: "Translation" }),
    ).not.toBeInTheDocument();
  }
}

it.each(
  surfaces.flatMap((surface) =>
    successfulStatuses.map((status) => ({ surface, ...status })),
  ),
)(
  "shows the expected translation navigation on $surface for $name status",
  async ({ surface, data, runtime, legacy }) => {
    server.use(
      http.get("/api/system/status", () => HttpResponse.json({ data })),
    );
    const { user } = renderNavigation(surface);
    await waitFor(() =>
      expect(queryClient.getQueryState(statusKey)?.status).toBe("success"),
    );
    await expectTranslationNavigation(surface, user, {
      runtime,
      legacy,
    });
  },
);

it.each(surfaces)(
  "hides legacy translation while status is unresolved on %s",
  async (surface) => {
    const response = Promise.withResolvers<Response>();
    server.use(http.get("/api/system/status", () => response.promise));
    const { user } = renderNavigation(surface);
    try {
      await waitFor(() =>
        expect(queryClient.getQueryState(statusKey)).toMatchObject({
          status: "pending",
          fetchStatus: "fetching",
        }),
      );
      await expectTranslationNavigation(surface, user, {
        runtime: false,
        legacy: false,
      });
      expect(queryClient.getQueryState(statusKey)?.status).toBe("pending");
    } finally {
      response.resolve(
        HttpResponse.json({ data: { translation_runtime_v2: true } }),
      );
    }
    await waitFor(() =>
      expect(queryClient.getQueryState(statusKey)?.status).toBe("success"),
    );
  },
);

it.each(
  surfaces.flatMap((surface) =>
    [false, true].map((cachedOff) => ({ surface, cachedOff })),
  ),
)(
  "hides legacy translation after status fails on $surface with cached off $cachedOff",
  async ({ surface, cachedOff }) => {
    if (cachedOff) {
      queryClient.setQueryData(
        statusKey,
        { translation_runtime_v2: false },
        { updatedAt: 1 },
      );
    }
    server.use(
      http.get("/api/system/status", () =>
        HttpResponse.json({ message: "Status unavailable" }, { status: 503 }),
      ),
    );
    const { user } = renderNavigation(surface);
    await waitFor(() =>
      expect(queryClient.getQueryState(statusKey)?.status).toBe("error"),
    );
    if (cachedOff) {
      expect(queryClient.getQueryData(statusKey)).toEqual({
        translation_runtime_v2: false,
      });
    }
    await expectTranslationNavigation(surface, user, {
      runtime: false,
      legacy: false,
    });
  },
);
