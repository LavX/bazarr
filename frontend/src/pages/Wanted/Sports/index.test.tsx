/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import {
  sportarr,
  sportarrSibling,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import WantedSportsView from "@/pages/Wanted/Sports";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

function owners(enabled = true) {
  server.use(
    http.get("/api/system/arr-instances", () =>
      HttpResponse.json([
        { ...sportarr, enabled },
        { ...sportarrSibling, enabled },
      ]),
    ),
    // Sports surfaces are gated on the master toggle as well as on an enabled
    // owner, so drive both from the same flag.
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: enabled } }),
    ),
  );
}

const event = {
  id: 11,
  arr_instance_id: 42,
  league_id: 7,
  title: "Final",
  missing_subtitles: ["en", "hu:hi"],
};

it("pages through the shared start/length contract instead of a page number", async () => {
  owners();
  let query: URLSearchParams | null = null;
  server.use(
    http.get("/api/sports/wanted", ({ request }) => {
      query = new URL(request.url).searchParams;
      return HttpResponse.json({ data: [event], total: 1 });
    }),
  );
  customRender(<WantedSportsView />);
  // Three queries settle before a row exists: the instance list, the settings
  // carrying the master toggle, and only then the wanted page. That chain
  // outruns findBy's 1s default when the whole suite is running.
  await screen.findByText("Final", undefined, { timeout: 8000 });
  // The old page sent its own page number and a fixed length of 100, so it
  // could not use the shared paginated table at all.
  await waitFor(() => expect(query).not.toBeNull());
  expect(query!.get("start")).toBe("0");
  expect(Number(query!.get("length"))).toBeGreaterThan(0);
});

it("shows a completed no-result reason from the owned job endpoint", async () => {
  owners();
  let posted: unknown;
  server.use(
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [event], total: 1 }),
    ),
    http.post("/api/sports/events/11/automatic", async ({ request }) => {
      posted = await request.json();
      return HttpResponse.json({
        queued: true,
        job_id: 99,
        message: "Search queued",
      });
    }),
    http.get("/api/sports/jobs/99", ({ request }) => {
      expect(new URL(request.url).searchParams.get("arr_instance_id")).toBe(
        "42",
      );
      return HttpResponse.json({
        job_id: 99,
        arr_instance_id: 42,
        status: "completed",
        message: "No eligible subtitle met the configured threshold",
        result: {
          data: [
            {
              status: "no_result",
              message: "No eligible subtitle met the configured threshold",
              downloads: 0,
            },
          ],
        },
      });
    }),
  );
  customRender(<WantedSportsView />);
  const row = await screen.findByRole(
    "row",
    { name: /Final/ },
    { timeout: 8000 },
  );
  // Clicking the missing language searches for it, the way the Series and
  // Movies wanted pages work, rather than a text button in an Actions column.
  await userEvent.setup().click(within(row).getByText("en"));
  await waitFor(() => expect(posted).toEqual({ arr_instance_id: 42 }));
});

it("splits a modified language key into a real language badge", async () => {
  owners();
  server.use(
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [event], total: 1 }),
    ),
  );
  customRender(<WantedSportsView />);
  const row = await screen.findByRole(
    "row",
    { name: /Final/ },
    { timeout: 8000 },
  );
  // "hu:hi" is one opaque string in the sports API. The shared Language badge
  // takes a parsed language and renders its own modifier suffix, so the key has
  // to be split rather than printed as-is.
  expect(within(row).getByText("hu:HI")).toBeInTheDocument();
  expect(within(row).queryByText("hu:hi")).toBeNull();
});

it("does not request the wanted list when every owner is disabled", async () => {
  owners(false);
  let calls = 0;
  server.use(
    http.get("/api/sports/wanted", () => {
      calls++;
      return HttpResponse.json({ data: [], total: 0 });
    }),
  );
  customRender(<WantedSportsView />);
  expect(
    await screen.findByText(
      "Enable a Sportarr instance in Connections to view sports.",
    ),
  ).toBeInTheDocument();
  expect(calls).toBe(0);
});
