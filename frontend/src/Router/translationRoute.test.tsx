/* eslint-disable camelcase -- transport names. */
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import { AllProviders } from "@/providers";
import { useRoutes } from "@/Router";
import type { CustomRouteObject } from "@/Router/type";
import { rawRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

// Settings > Translation belongs to the v2 translation runtime. With the
// switch off the route is not in the router at all, so its address is Not
// Found, not a hidden page.
function SettingsPaths() {
  const top: CustomRouteObject[] = useRoutes()[0].children ?? [];
  const settings = top.find((route) => route.path === "settings");
  const paths = (settings?.children ?? []).map((route) => route.path);
  return <span>{paths.join(",")}</span>;
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

it("leaves the Translation route out while the switch is off", async () => {
  showStatus(false);
  rawRender(
    <AllProviders>
      <SettingsPaths />
    </AllProviders>,
  );
  expect(await screen.findByText(/translator/)).toBeInTheDocument();
  expect(screen.getByText(/translator/).textContent).not.toContain(
    "translation",
  );
});

it("leaves it out on a status that predates the field", async () => {
  showStatus(undefined);
  rawRender(
    <AllProviders>
      <SettingsPaths />
    </AllProviders>,
  );
  expect(await screen.findByText(/translator/)).toBeInTheDocument();
  expect(screen.getByText(/translator/).textContent).not.toContain(
    "translation",
  );
});

it("adds the route, beside the old translator page, once the switch is on", async () => {
  showStatus(true);
  rawRender(
    <AllProviders>
      <SettingsPaths />
    </AllProviders>,
  );
  const paths = await screen.findByText(/translation/);
  expect(paths.textContent).toContain("translator");
  expect(paths.textContent).toContain("translation");
});
