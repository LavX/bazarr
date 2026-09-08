/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import {
  sportarr,
  sportarrSibling,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import SportsActivity from ".";

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
  action: 1,
  score: 100,
  score_out_of: 180,
};
function owners(enabled = true) {
  server.use(
    http.get("/api/system/arr-instances", () =>
      HttpResponse.json([
        { ...sportarr, enabled },
        { ...sportarrSibling, enabled },
      ]),
    ),
  );
}

it("shows a completed no-result reason from the owned job endpoint", async () => {
  owners();
  let posted: unknown;
  server.use(
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({
        data: [
          {
            id: 11,
            arr_instance_id: 42,
            league_id: 7,
            title: "Final",
            missing_subtitles: ["en"],
          },
        ],
        total: 1,
      }),
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
  customRender(<SportsActivity kind="wanted" />);
  const row = await screen.findByRole("row", { name: /Final/ });
  await userEvent
    .setup()
    .click(within(row).getByRole("button", { name: "Search missing" }));
  expect(
    await screen.findByText(
      "No eligible subtitle met the configured threshold",
    ),
  ).toBeInTheDocument();
  expect(posted).toEqual({ arr_instance_id: 42 });
});

it("filters history and cancels, reopens, then excludes only the selected owner", async () => {
  owners();
  const user = userEvent.setup();
  let filtered = false;
  let posted: unknown;
  server.use(
    http.get("/api/sports/history", ({ request }) => {
      filtered = new URL(request.url).searchParams.get("language") === "en";
      return HttpResponse.json({ data: [record], total: 1 });
    }),
    http.post("/api/sports/history/9/blacklist", async ({ request }) => {
      posted = await request.json();
      return HttpResponse.json({
        queued: true,
        job_id: 100,
        message: "Search queued",
      });
    }),
    http.get("/api/sports/jobs/100", () =>
      HttpResponse.json({
        status: "completed",
        message: "Release excluded",
        result: {
          file_status: "preserved",
          replacement: {
            message:
              "File preserved because its current artifact could not be proven",
          },
        },
      }),
    ),
  );
  customRender(<SportsActivity kind="history" />);
  await screen.findByText("Final");
  await user.type(screen.getByRole("textbox", { name: "Language" }), "en");
  await waitFor(() => expect(filtered).toBe(true));
  await user.click(screen.getByRole("button", { name: "Exclude release" }));
  let dialog = within(await screen.findByRole("dialog"));
  await user.click(dialog.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(posted).toBeUndefined();
  await user.click(screen.getByRole("button", { name: "Exclude release" }));
  dialog = within(await screen.findByRole("dialog"));
  await user.click(dialog.getByRole("button", { name: "Exclude release" }));
  expect(
    await screen.findByText(
      "File preserved because its current artifact could not be proven",
    ),
  ).toBeInTheDocument();
  expect(posted).toEqual({ arr_instance_id: 42 });
});

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
  customRender(<SportsActivity kind="blacklist" />);
  await userEvent
    .setup()
    .click(await screen.findByRole("button", { name: "Remove exclusion" }));
  expect(
    await screen.findByText(
      "Exclusion removed. The release is eligible for future searches.",
    ),
  ).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByText("Final")).toBeNull());
});

it("does not request sports activity when every owner is disabled", async () => {
  owners(false);
  let calls = 0;
  server.use(
    http.get("/api/sports/wanted", () => {
      calls++;
      return HttpResponse.json({ data: [], total: 0 });
    }),
  );
  customRender(<SportsActivity kind="wanted" />);
  expect(
    await screen.findByText(
      "Enable a Sportarr instance in Connections to view sports.",
    ),
  ).toBeInTheDocument();
  expect(calls).toBe(0);
});
