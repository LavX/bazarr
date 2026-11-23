/* eslint-disable camelcase -- transport names. */
import { FunctionComponent } from "react";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";
import { useSystemStatus } from "@/apis/hooks/system";
import { groupRoutes } from "@/App/Navbar";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { engine, engineInstallations } from "@/pages/Translation/fixtures";
import { AllProviders } from "@/providers";
import { Router, useRoutes } from "@/Router";
import TranslationGate from "@/Router/TranslationGate";
import type { CustomRouteObject } from "@/Router/type";
import { cleanup, customRender, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { WHATS_NEW_SEEN_KEY } from "@/utilities/whatsNew";

// The Translation page belongs to the v2 translation runtime. With the switch
// off it is not in the navigation and its address is Not Found. The route
// object itself is always in the list, at the same place: the router keeps a
// match by position, so a route that appears only once the status has answered
// would make every later route draw its neighbour on a page opened by address.
function Routes() {
  const top: CustomRouteObject[] = useRoutes()[0].children ?? [];
  const settings = top.find((route) => route.path === "settings");
  const navigation = groupRoutes(top).flatMap((group) =>
    group.items.map((item) => item.path),
  );
  const { isFetched } = useSystemStatus();
  return (
    <>
      <span>{isFetched ? "status loaded" : "status loading"}</span>
      <span data-testid="top">{top.map((route) => route.path).join(",")}</span>
      <span data-testid="navigation">{navigation.join(",")}</span>
      <span data-testid="settings">
        {(settings?.children ?? [])
          .filter((route: CustomRouteObject) => !route.hidden)
          .map((route) => route.path)
          .join(",")}
      </span>
    </>
  );
}

function showStatus(translationRuntime: boolean | undefined) {
  server.use(
    http.get("/api/system/status", () =>
      HttpResponse.json({
        data:
          translationRuntime === undefined
            ? {}
            : { translation_runtime_v2: translationRuntime },
      }),
    ),
    http.get("/api/system/arr-instances", () => HttpResponse.json([])),
    http.get("/api/badges", () => HttpResponse.json({})),
  );
}

function showRoutes() {
  rawRender(
    <AllProviders>
      <Routes />
    </AllProviders>,
  );
}

const list = (id: string) =>
  (screen.getByTestId(id).textContent ?? "").split(",");

const Page: FunctionComponent = () => (
  <TranslationGate>
    <span>the translation page</span>
  </TranslationGate>
);

it("keeps Translation out of the navigation while the switch is off, after the status has arrived", async () => {
  showStatus(false);
  showRoutes();
  expect(await screen.findByText("status loaded")).toBeInTheDocument();
  expect(list("navigation")).not.toContain("translation");
  // The old page stays in Settings; the new one was never listed there.
  expect(list("settings")).toContain("translator");
  expect(list("settings")).not.toContain("translation");
});

it("keeps it out on a status that predates the field, after the status has arrived", async () => {
  showStatus(undefined);
  showRoutes();
  expect(await screen.findByText("status loaded")).toBeInTheDocument();
  expect(list("navigation")).not.toContain("translation");
});

it("shows it as a first-level entry in the System group once the switch is on, with the old page still in Settings", async () => {
  showStatus(true);
  showRoutes();
  expect(await screen.findByText("status loaded")).toBeInTheDocument();
  await waitFor(() => expect(list("navigation")).toContain("translation"));
  const navigation = list("navigation");
  // Plugin Hub sits directly after Subtitle Hub, and Translation follows it.
  expect(navigation.indexOf("translation")).toBe(
    navigation.indexOf("plugin-hub") + 1,
  );
  expect(list("settings")).toContain("translator");
  expect(list("settings")).not.toContain("translation");
});

it("holds every route at the same position whether the switch is on or off", async () => {
  showStatus(true);
  showRoutes();
  const before = list("top");
  expect(await screen.findByText("status loaded")).toBeInTheDocument();
  await waitFor(() => expect(list("navigation")).toContain("translation"));
  expect(list("top")).toEqual(before);
});

it("answers Not Found at the page's address while the switch is off, and mounts nothing of the page", async () => {
  showStatus(false);
  customRender(<Page />);
  expect(await screen.findByText(/Page not found/)).toBeVisible();
  expect(screen.queryByText("the translation page")).not.toBeInTheDocument();
});

it("shows neither the page nor Not Found before the status has answered", async () => {
  let answer: (() => void) | undefined;
  server.use(
    http.get("/api/system/status", async () => {
      await new Promise<void>((resolve) => {
        answer = resolve;
      });
      return HttpResponse.json({ data: { translation_runtime_v2: true } });
    }),
  );
  customRender(<Page />);
  await waitFor(() => expect(answer).toBeDefined());
  expect(screen.queryByText(/Page not found/)).not.toBeInTheDocument();
  expect(screen.queryByText("the translation page")).not.toBeInTheDocument();
  answer?.();
  expect(await screen.findByText("the translation page")).toBeVisible();
});

it("lists the Plugin Hub directly after Subtitle Hub whatever the switch says", async () => {
  showStatus(false);
  showRoutes();
  expect(await screen.findByText("status loaded")).toBeInTheDocument();
  const navigation = list("navigation");
  expect(navigation.indexOf("plugin-hub")).toBe(
    navigation.indexOf("subtitle-hub") + 1,
  );
});

describe("Translation addresses in the mounted application router", () => {
  const injectedEnvironment = window.Bazarr;
  const bookmarks = [
    { path: "translator", base: "" },
    { path: "translation", base: "" },
    { path: "translator", base: "/bazarr" },
    { path: "translation", base: "/bazarr" },
  ];
  const deepLink = "?tab=defaults&from=bookmark#backend-2";

  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", "/");
    window.Bazarr = injectedEnvironment;
    localStorage.removeItem(WHATS_NEW_SEEN_KEY);
    setAuthenticated(false);
  });

  function showApplication(address: string, base = "") {
    const instances = [engine()];
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "light", setup_complete: true },
          auth: { type: "form" },
          translator: { default_score: 50, min_source_score: 90 },
        }),
      ),
      http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
      http.get("/api/discover/metadata/status", () =>
        HttpResponse.json({ data: { configured: false } }),
      ),
      http.get("/api/translation/state", () =>
        HttpResponse.json({
          enabled: true,
          engine: 1,
          fallback_engine: null,
          instances,
        }),
      ),
      http.get("/api/plugins/installations", () =>
        HttpResponse.json({ data: engineInstallations(instances) }),
      ),
    );
    window.Bazarr = { baseUrl: base, canUpdate: false, hasUpdate: false };
    window.history.replaceState(null, "", `${base}${address}`);
    setAuthenticated(true);
    localStorage.setItem(WHATS_NEW_SEEN_KEY, latestWhatsNewVersion);
    rawRender(
      <AllProviders>
        <Router />
      </AllProviders>,
    );
  }

  it.each(bookmarks)(
    "redirects $base/settings/$path to Translation with its query and fragment when v2 is on",
    async ({ path, base }) => {
      showStatus(true);
      showApplication(`/settings/${path}${deepLink}`, base);

      expect(
        await screen.findByRole("tab", { name: "Defaults" }),
      ).toHaveAttribute("aria-selected", "true");
      expect(window.location.pathname).toBe(`${base}/translation`);
      expect(window.location.search).toBe("?tab=defaults&from=bookmark");
      expect(window.location.hash).toBe("#backend-2");
      expect(screen.queryByText("Translator Engine")).not.toBeInTheDocument();
    },
  );

  it.each(["", "/bazarr"])(
    "opens a direct %s/translation bookmark on its requested tab",
    async (base) => {
      showStatus(true);
      showApplication(`/translation${deepLink}`, base);

      expect(
        await screen.findByRole("tab", { name: "Defaults" }),
      ).toHaveAttribute("aria-selected", "true");
      expect(window.location.pathname).toBe(`${base}/translation`);
      expect(window.location.search).toBe("?tab=defaults&from=bookmark");
      expect(window.location.hash).toBe("#backend-2");
    },
  );

  it.each([
    { runtime: false, base: "" },
    { runtime: undefined, base: "" },
    { runtime: false, base: "/bazarr" },
    { runtime: undefined, base: "/bazarr" },
  ])(
    "keeps legacy settings at $base/settings/translator when v2 is $runtime",
    async ({ runtime, base }) => {
      showStatus(runtime);
      showApplication(`/settings/translator${deepLink}`, base);

      expect(await screen.findByText("Translator Engine")).toBeVisible();
      expect(window.location.pathname).toBe(`${base}/settings/translator`);
      expect(window.location.search).toBe("?tab=defaults&from=bookmark");
      expect(window.location.hash).toBe("#backend-2");
      expect(
        screen.queryByRole("tab", { name: "Defaults" }),
      ).not.toBeInTheDocument();
    },
  );

  it.each([false, undefined])(
    "keeps the former Settings/Translation address gated when v2 is %s",
    async (runtime) => {
      showStatus(runtime);
      showApplication(`/settings/translation${deepLink}`);

      expect(await screen.findByText(/Page not found/)).toBeVisible();
      expect(window.location.pathname).toBe("/translation");
      expect(window.location.search).toBe("?tab=defaults&from=bookmark");
      expect(window.location.hash).toBe("#backend-2");
      expect(screen.queryByText("Translator Engine")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("tab", { name: "Defaults" }),
      ).not.toBeInTheDocument();
    },
  );

  it.each(["translator", "translation"])(
    "waits for status at /settings/%s without mounting legacy settings or Not Found",
    async (path) => {
      showStatus(true);
      const status = Promise.withResolvers<void>();
      const requested = Promise.withResolvers<void>();
      server.use(
        http.get("/api/system/status", async () => {
          requested.resolve();
          await status.promise;
          return HttpResponse.json({ data: { translation_runtime_v2: true } });
        }),
      );
      showApplication(`/settings/${path}${deepLink}`);

      try {
        await requested.promise;
        // The shell proves settings have arrived while system status is still pending.
        await screen.findByRole("button", { name: "Settings" });
        expect(screen.queryByText("Translator Engine")).not.toBeInTheDocument();
        expect(screen.queryByText(/Page not found/)).not.toBeInTheDocument();
        expect(
          screen.queryByRole("tab", { name: "Defaults" }),
        ).not.toBeInTheDocument();
        status.resolve();
        expect(
          await screen.findByRole("tab", { name: "Defaults" }),
        ).toHaveAttribute("aria-selected", "true");
        expect(window.location.pathname).toBe("/translation");
        expect(window.location.search).toBe("?tab=defaults&from=bookmark");
        expect(window.location.hash).toBe("#backend-2");
      } finally {
        status.resolve();
      }
    },
  );
});
