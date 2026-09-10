/* eslint-disable camelcase */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import BlacklistSportsView from "@/pages/Blacklist/Sports";
import {
  sportarr,
  sportarrSibling,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import { AllProviders } from "@/providers";
import { customRender, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

// Rendered on a real route so the ?instance= the page reads is the same one a
// link from the event page would set. The instance select writes to the URL for
// exactly that reason.
function renderScoped(search: string) {
  const router = createMemoryRouter(
    [{ path: "/blacklist/sports", element: <BlacklistSportsView /> }],
    { initialEntries: [`/blacklist/sports${search}`] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

function owners(enabled = true) {
  server.use(
    http.get("/api/system/arr-instances", () =>
      HttpResponse.json([
        { ...sportarr, enabled },
        { ...sportarrSibling, enabled },
      ]),
    ),
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: enabled } }),
    ),
  );
}

const record = {
  id: 9,
  arr_instance_id: 42,
  league_id: 7,
  event_id: 11,
  title: "Final",
  timestamp: "2026-09-08T10:00:00",
  language: "en",
  provider: "fixture",
  subs_id: "release",
};

it("removes a release exclusion for its owner", async () => {
  owners();
  let removed = false;
  server.use(
    http.get("/api/sports/blacklist", () =>
      HttpResponse.json({
        data: removed ? [] : [record],
        total: removed ? 0 : 1,
      }),
    ),
    http.delete("/api/sports/blacklist/9", ({ request }) => {
      expect(new URL(request.url).searchParams.get("arr_instance_id")).toBe(
        "42",
      );
      removed = true;
      return HttpResponse.json({ removed: true });
    }),
  );
  customRender(<BlacklistSportsView />);
  await userEvent
    .setup()
    .click(
      await screen.findByRole(
        "button",
        { name: "Remove exclusion" },
        { timeout: 8000 },
      ),
    );
  expect(
    await screen.findByText(
      "Exclusion removed. The release is eligible for future searches.",
    ),
  ).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByText("Final")).toBeNull());
});

it("asks before clearing every exclusion for an instance", async () => {
  owners();
  const user = userEvent.setup();
  let cleared = false;
  server.use(
    http.get("/api/sports/blacklist", () =>
      HttpResponse.json({ data: [record], total: 1 }),
    ),
    http.delete("/api/sports/blacklist", () => {
      cleared = true;
      return HttpResponse.json({ removed: 1 });
    }),
  );
  renderScoped(`?instance=${sportarr.id}`);
  await screen.findByText("Final", undefined, { timeout: 8000 });
  await user.click(screen.getByRole("button", { name: "Remove All" }));
  const dialog = within(await screen.findByRole("dialog"));
  await user.click(dialog.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(cleared).toBe(false);
});

it("does not request exclusions when every owner is disabled", async () => {
  owners(false);
  let calls = 0;
  server.use(
    http.get("/api/sports/blacklist", () => {
      calls++;
      return HttpResponse.json({ data: [], total: 0 });
    }),
  );
  customRender(<BlacklistSportsView />);
  expect(
    await screen.findByText(
      "Enable a Sportarr instance in Connections to view sports.",
    ),
  ).toBeInTheDocument();
  expect(calls).toBe(0);
});
