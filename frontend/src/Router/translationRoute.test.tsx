/* eslint-disable camelcase -- transport names. */
import { FunctionComponent } from "react";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import { useSystemStatus } from "@/apis/hooks/system";
import { groupRoutes } from "@/App/Navbar";
import { AllProviders } from "@/providers";
import { useRoutes } from "@/Router";
import TranslationGate from "@/Router/TranslationGate";
import type { CustomRouteObject } from "@/Router/type";
import { customRender, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

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

it("shows it as a first-level entry after Subtitle Hub once the switch is on, with the old page still in Settings", async () => {
  showStatus(true);
  showRoutes();
  expect(await screen.findByText("status loaded")).toBeInTheDocument();
  await waitFor(() => expect(list("navigation")).toContain("translation"));
  const navigation = list("navigation");
  expect(navigation.indexOf("translation")).toBe(
    navigation.indexOf("subtitle-hub") + 1,
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
