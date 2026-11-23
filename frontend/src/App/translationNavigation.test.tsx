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

it.each([
  { surface: "desktop", runtime: true },
  { surface: "desktop", runtime: false },
  { surface: "mobile", runtime: true },
  { surface: "mobile", runtime: false },
])(
  "keeps translation in its runtime's navigation on $surface with v2 $runtime",
  async ({ surface, runtime }) => {
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
      http.get("/api/system/status", () =>
        HttpResponse.json({ data: { translation_runtime_v2: runtime } }),
      ),
      http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
      http.get("/api/badges", () => HttpResponse.json({})),
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
    const user = userEvent.setup();
    await waitFor(() =>
      expect(
        queryClient.getQueryState([QueryKeys.System, "status"])?.status,
      ).toBe("success"),
    );

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
    expect(
      await within(settings).findByRole(itemRole, { name: "Connections" }),
    ).toBeVisible();
    const translator = within(settings).queryByRole(itemRole, {
      name: "AI Translator",
      hidden: true,
    });
    if (runtime) {
      expect(translator).not.toBeInTheDocument();
    } else {
      expect(translator).toHaveAttribute("href", "/settings/translator");
    }
    if (surface === "desktop") {
      expect(
        within(settings).queryByRole("menuitem", { name: "Translation" }),
      ).not.toBeInTheDocument();
    }
  },
);
