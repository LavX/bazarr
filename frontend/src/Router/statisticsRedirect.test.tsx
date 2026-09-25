import { createMemoryRouter, RouterProvider } from "react-router";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import { AllProviders } from "@/providers";
import { useRoutes } from "@/Router";
import type { CustomRouteObject } from "@/Router/type";
import { rawRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

// Statistics moved from History to System in 2.7.0. A bookmark to the old
// page has to land on the new one, not on Not Found.
function OldStatisticsBookmark() {
  const top: CustomRouteObject[] = useRoutes()[0].children ?? [];
  const history = top.find((route) => route.path === "history");
  const old = (history?.children ?? []).find(
    (route: CustomRouteObject) => route.path === "stats",
  ) as CustomRouteObject | undefined;
  if (old === undefined) {
    return <span>No route for the old statistics page</span>;
  }
  const router = createMemoryRouter(
    [
      { path: "/history/stats", element: old.element },
      { path: "/system/statistics", element: <span>Statistics page</span> },
    ],
    { initialEntries: ["/history/stats"] },
  );
  return (
    <>
      {old.hidden && <span>Hidden from navigation</span>}
      <RouterProvider router={router} />
    </>
  );
}

it("sends the 2.6 statistics bookmark to the System page", async () => {
  server.use(
    http.get("/api/system/arr-instances", () => HttpResponse.json([])),
    http.get("/api/badges", () => HttpResponse.json({})),
  );
  // Raw render: the component mounts its own router, and the custom render
  // would wrap it in a second one.
  rawRender(
    <AllProviders>
      <OldStatisticsBookmark />
    </AllProviders>,
  );

  expect(await screen.findByText("Statistics page")).toBeInTheDocument();
  expect(screen.getByText("Hidden from navigation")).toBeInTheDocument();
});
