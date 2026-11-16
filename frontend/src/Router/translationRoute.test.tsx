/* eslint-disable camelcase -- transport names. */
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import { useSystemStatus } from "@/apis/hooks/system";
import { AllProviders } from "@/providers";
import { useRoutes } from "@/Router";
import type { CustomRouteObject } from "@/Router/type";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// Settings > Translation belongs to the v2 translation runtime. With the
// switch off the route is not in the router at all, so its address is Not
// Found, not a hidden page.
function SettingsPaths() {
  const top: CustomRouteObject[] = useRoutes()[0].children ?? [];
  const settings = top.find((route) => route.path === "settings");
  const paths = (settings?.children ?? []).map((route) => route.path);
  // The assertions wait for the status to arrive, so a flag that is only
  // ignored after loading cannot pass on the first render.
  const { isFetched } = useSystemStatus();
  return (
    <>
      <span>{isFetched ? "status loaded" : "status loading"}</span>
      <span data-testid="paths">{paths.join(",")}</span>
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

function showPaths() {
  rawRender(
    <AllProviders>
      <SettingsPaths />
    </AllProviders>,
  );
}

it("leaves the Translation route out while the switch is off, after the status has arrived", async () => {
  showStatus(false);
  showPaths();
  expect(await screen.findByText("status loaded")).toBeInTheDocument();
  const paths = screen.getByTestId("paths").textContent ?? "";
  expect(paths).toContain("translator");
  expect(paths.split(",")).not.toContain("translation");
});

it("leaves it out on a status that predates the field, after the status has arrived", async () => {
  showStatus(undefined);
  showPaths();
  expect(await screen.findByText("status loaded")).toBeInTheDocument();
  expect(screen.getByTestId("paths").textContent?.split(",")).not.toContain(
    "translation",
  );
});

it("adds the route, beside the old translator page, once the switch is on", async () => {
  showStatus(true);
  showPaths();
  expect(await screen.findByText("status loaded")).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByTestId("paths").textContent?.split(",")).toContain(
      "translation",
    ),
  );
  expect(screen.getByTestId("paths").textContent).toContain("translator");
});
