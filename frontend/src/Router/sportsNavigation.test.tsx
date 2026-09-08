import { act } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import { useRoutes } from "@/Router";
import type { CustomRouteObject } from "@/Router/type";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

function Navigation() {
  // These children are route descriptors, not DOM nodes.
  // eslint-disable-next-line testing-library/no-node-access
  const children: CustomRouteObject[] = useRoutes()[0].children ?? [];
  return (
    <nav>
      {children
        .filter((route) => route.name && !route.hidden)
        .map((route) => (
          <span key={route.name}>{route.name}</span>
        ))}
    </nav>
  );
}

it("adds and removes Sports navigation as the last enabled owner changes", async () => {
  let enabled = false;
  server.use(
    http.get("/api/system/arr-instances", () =>
      HttpResponse.json([{ ...sportarr, enabled }]),
    ),
    http.get("/api/badges", () => HttpResponse.json({})),
  );
  customRender(<Navigation />);
  await screen.findByText("Settings");
  expect(screen.queryByText("Sports")).toBeNull();
  enabled = true;
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: [QueryKeys.ArrInstances] });
  });
  expect(await screen.findByText("Sports")).toBeInTheDocument();
  enabled = false;
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: [QueryKeys.ArrInstances] });
  });
  await waitFor(() => expect(screen.queryByText("Sports")).toBeNull());
});

function SportsTabs() {
  // eslint-disable-next-line testing-library/no-node-access
  const children: CustomRouteObject[] = useRoutes()[0].children ?? [];
  return (
    <nav>
      {children
        .filter((route) => !route.hidden)
        .flatMap((route) =>
          ((route.children ?? []) as CustomRouteObject[])
            .filter((child) => child.path === "sports" && !child.hidden)
            .map((child) => (
              <span key={route.path}>
                {route.name}: {child.name}
              </span>
            )),
        )}
    </nav>
  );
}

it("exposes sports wanted, history and exclusion tabs only for enabled owners", async () => {
  let enabled = true;
  server.use(
    http.get("/api/system/arr-instances", () =>
      HttpResponse.json([{ ...sportarr, enabled }]),
    ),
    http.get("/api/badges", () => HttpResponse.json({ sports: 2 })),
  );
  customRender(<SportsTabs />);
  expect(await screen.findByText("Missing: Sports")).toBeInTheDocument();
  expect(screen.getByText("History: Sports")).toBeInTheDocument();
  expect(screen.getByText("Excluded: Sports")).toBeInTheDocument();
  enabled = false;
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: [QueryKeys.ArrInstances] });
  });
  await waitFor(() => expect(screen.queryByText("Missing: Sports")).toBeNull());
  expect(screen.queryByText("History: Sports")).toBeNull();
  expect(screen.queryByText("Excluded: Sports")).toBeNull();
});
