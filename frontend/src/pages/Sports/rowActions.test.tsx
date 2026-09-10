/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import Sports from "@/pages/Sports";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// Its own file on purpose. Mantine renders menus and modals into a portal on
// document.body that outlives a test's container, so a test that leaves a
// modal open puts an overlay over everything a later test in the same file
// tries to click.
beforeEach(() => {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: true } }),
    ),
  );
});

it("acts on one league from its own row menu", async () => {
  const user = userEvent.setup();
  let batched: unknown;
  server.use(
    http.get("/api/system/arr-instances", () => HttpResponse.json([sportarr])),
    http.get("/api/sports/leagues", () =>
      HttpResponse.json({
        data: [
          {
            id: 51,
            arr_instance_id: 42,
            sportarrLeagueId: 7,
            title: "Premier League",
            sport: "Football",
            monitored: true,
            tags: [],
            audio_language: [],
            eventCount: 4,
            eventFileCount: 3,
            profileId: 5,
          },
        ],
        total: 1,
      }),
    ),
    http.post("/api/subtitles/batch", async ({ request }) => {
      batched = await request.json();
      return HttpResponse.json({ queued: 1, skipped: 0, errors: [] });
    }),
  );
  customRender(<Sports />);

  // Series and Movies rows each carry this menu; a league had none, so
  // acting on one meant ticking its checkbox first.
  await user.click(
    await screen.findByRole("button", { name: "Actions" }, { timeout: 8000 }),
  );
  // Queried by text, not by role. Mantine keeps the dropdown mounted and
  // toggles it with display, and jsdom never completes the transition that
  // clears it, so the items stay out of the accessibility tree here while
  // being perfectly clickable in a browser.
  await user.click(
    await screen.findByText("Search Missing", undefined, { timeout: 8000 }),
  );
  await user.click(
    await screen.findByRole(
      "button",
      { name: /Apply to 1 Item/ },
      { timeout: 8000 },
    ),
  );

  await waitFor(() =>
    expect(batched).toEqual({
      items: [
        { type: "sportsLeague", sportsLeagueId: 51, arr_instance_id: 42 },
      ],
      action: "search-missing",
      options: undefined,
    }),
  );
});
