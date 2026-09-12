/* eslint-disable camelcase */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import SportsHistoryView from "@/pages/History/Sports";
import {
  sportarr,
  sportarrSibling,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import { AllProviders } from "@/providers";
import { customRender, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

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
  timestamp: "2 hours ago",
  parsed_timestamp: "09/08/26 10:00:00",
  language: "en",
  provider: "fixture",
  subs_id: "release",
  action: 1,
  score: 100,
  score_out_of: 180,
};

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
  customRender(<SportsHistoryView />);
  await screen.findByText("Final", undefined, { timeout: 8000 });
  await user.type(screen.getByRole("textbox", { name: "Language" }), "en");
  await waitFor(() => expect(filtered).toBe(true));
  await user.click(screen.getByRole("button", { name: "Exclude" }));
  let dialog = within(await screen.findByRole("dialog"));
  await user.click(dialog.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(posted).toBeUndefined();
  await user.click(screen.getByRole("button", { name: "Exclude" }));
  dialog = within(await screen.findByRole("dialog"));
  await user.click(dialog.getByRole("button", { name: "Exclude release" }));
  expect(
    await screen.findByText(
      "File preserved because its current artifact could not be proven",
    ),
  ).toBeInTheDocument();
  expect(posted).toEqual({ arr_instance_id: 42 });
});

it("offers no exclusion for a record that names no release", async () => {
  owners();
  server.use(
    http.get("/api/sports/history", () =>
      HttpResponse.json({
        // A deletion. Excluding names a provider release, and a deletion,
        // upload or sync has none to name.
        data: [{ ...record, action: 0, provider: null, subs_id: null }],
        total: 1,
      }),
    ),
  );
  customRender(<SportsHistoryView />);
  await screen.findByText("Final", undefined, { timeout: 8000 });
  expect(screen.queryByRole("button", { name: "Exclude" })).toBeNull();
});

it("disables the exclusion on a row the instance already excluded", async () => {
  owners();
  const user = userEvent.setup();
  let posts = 0;
  server.use(
    http.get("/api/sports/history", () =>
      HttpResponse.json({
        data: [{ ...record, blacklisted: true }],
        total: 1,
      }),
    ),
    http.post("/api/sports/history/9/blacklist", () => {
      posts++;
      return HttpResponse.json({
        queued: true,
        job_id: 100,
        message: "Search queued",
      });
    }),
  );
  customRender(<SportsHistoryView />);
  await screen.findByText("Final", undefined, { timeout: 8000 });
  const exclude = screen.getByRole("button", { name: "Exclude" });
  expect(exclude).toBeDisabled();
  // The repeat would queue another job and another replacement search for a
  // release the instance has already been told to skip.
  await user.click(exclude);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(posts).toBe(0);
});

it("keeps the exclusion enabled on a row that is not excluded yet", async () => {
  owners();
  server.use(
    http.get("/api/sports/history", () =>
      HttpResponse.json({ data: [record], total: 1 }),
    ),
  );
  customRender(<SportsHistoryView />);
  await screen.findByText("Final", undefined, { timeout: 8000 });
  expect(screen.getByRole("button", { name: "Exclude" })).toBeEnabled();
});

it("shows the relative date the other history pages show", async () => {
  owners();
  server.use(
    http.get("/api/sports/history", () =>
      HttpResponse.json({ data: [record], total: 1 }),
    ),
  );
  customRender(<SportsHistoryView />);
  // The raw ISO string used to go out under both names, so this column printed
  // a machine timestamp where Series and Movies read "2 hours ago".
  expect(
    await screen.findByText("2 hours ago", undefined, { timeout: 8000 }),
  ).toBeInTheDocument();
});

it("shows the score as a percentage of what was available", async () => {
  owners();
  server.use(
    http.get("/api/sports/history", () =>
      HttpResponse.json({ data: [record], total: 1 }),
    ),
  );
  customRender(<SportsHistoryView />);
  // 100 of a possible 180.
  expect(
    await screen.findByText("56%", undefined, { timeout: 8000 }),
  ).toBeInTheDocument();
});

it("does not request history when every owner is disabled", async () => {
  owners(false);
  let calls = 0;
  server.use(
    http.get("/api/sports/history", () => {
      calls++;
      return HttpResponse.json({ data: [], total: 0 });
    }),
  );
  customRender(<SportsHistoryView />);
  expect(
    await screen.findByText(
      "Enable a Sportarr instance in Connections to view sports.",
    ),
  ).toBeInTheDocument();
  expect(calls).toBe(0);
});

it("narrows the history to the league the toolbox opened the page for", async () => {
  owners();
  let leagueFilter = "";
  server.use(
    http.get("/api/sports/history", ({ request }) => {
      leagueFilter = new URL(request.url).searchParams.get("league_id") ?? "";
      return HttpResponse.json({ data: [record], total: 1 });
    }),
  );
  const router = createMemoryRouter(
    [{ path: "/history/sports", element: <SportsHistoryView /> }],
    { initialEntries: ["/history/sports?instance=42&league=7"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  await screen.findByText("Final", undefined, { timeout: 8000 });
  expect(leagueFilter).toBe("7");
});

it("renders the Match popover for a record with recorded criteria", async () => {
  owners();
  const user = userEvent.setup();
  server.use(
    http.get("/api/sports/history", () =>
      HttpResponse.json({
        data: [
          {
            ...record,
            matches: ["title", "year"],
            dont_matches: ["release_group"],
          },
        ],
        total: 1,
      }),
    ),
  );
  customRender(<SportsHistoryView />);
  await screen.findByText("Final", undefined, { timeout: 8000 });
  // The shared StateIcon cell: a list-check icon whose popover lists the
  // criteria that matched and did not match.
  // eslint-disable-next-line testing-library/no-node-access
  const icon = document.querySelector(".fa-list-check");
  expect(icon).not.toBeNull();
  await user.hover(icon as Element);
  expect(
    await screen.findByText("Scoring Criteria", undefined, { timeout: 8000 }),
  ).toBeInTheDocument();
  expect(screen.getByText("title")).toBeInTheDocument();
  expect(screen.getByText("year")).toBeInTheDocument();
  expect(screen.getByText("release_group")).toBeInTheDocument();
});

it("marks an upgradable row with the recycle indicator", async () => {
  owners();
  const user = userEvent.setup();
  server.use(
    http.get("/api/sports/history", () =>
      HttpResponse.json({
        data: [{ ...record, upgradable: true }],
        total: 1,
      }),
    ),
  );
  customRender(<SportsHistoryView />);
  await screen.findByText("Final", undefined, { timeout: 8000 });
  // eslint-disable-next-line testing-library/no-node-access
  const icon = document.querySelector(".fa-recycle");
  expect(icon).not.toBeNull();
  await user.hover(icon as Element);
  expect(
    await screen.findByText(
      "This Subtitle File Is Eligible For An Upgrade.",
      undefined,
      { timeout: 8000 },
    ),
  ).toBeInTheDocument();
});

it("hides Embedded Source records by default and shows them with the switch on", async () => {
  owners();
  const user = userEvent.setup();
  server.use(
    http.get("/api/sports/history", ({ request }) => {
      const include = new URL(request.url).searchParams.get("include_embedded");
      return HttpResponse.json(
        include === "true"
          ? {
              data: [
                record,
                {
                  ...record,
                  id: 10,
                  action: 7,
                  language: "fr",
                  provider: "embedded",
                  description: "fr embedded subtitles detected.",
                  subs_id: null,
                },
              ],
              total: 2,
            }
          : { data: [record], total: 1 },
      );
    }),
  );
  customRender(<SportsHistoryView />);
  await screen.findByText("Final", undefined, { timeout: 8000 });
  // The embedded row's description lives in the Info tooltip, so the visible
  // proof is the provider cell, which only the action=7 row carries.
  expect(screen.queryByText("embedded")).toBeNull();
  await user.click(
    screen.getByRole("switch", { name: "Show Embedded Source records" }),
  );
  expect(
    await screen.findByText("embedded", undefined, { timeout: 8000 }),
  ).toBeInTheDocument();
});
