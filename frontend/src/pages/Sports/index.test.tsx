/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import {
  sportarr,
  sportarrSibling,
  sportsProfile,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import Sports from "@/pages/Sports";
import { act, customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

describe("sports library", () => {
  beforeEach(() => {
    // Every sports surface is gated on the master toggle now.
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
    );
  });
  it("renders leagues in the shared table with owner, sport and event counts", async () => {
    const user = userEvent.setup();
    let assigned: unknown;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr, sportarrSibling]),
      ),
      http.get("/api/system/languages/profiles", () =>
        HttpResponse.json([sportsProfile]),
      ),
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
              poster: "https://images.example/poster.jpg",
              eventCount: 4,
              eventFileCount: 4,
              profileId: null,
            },
          ],
          total: 1,
        }),
      ),
      http.patch("/api/sports/leagues/51", async ({ request }) => {
        assigned = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    customRender(<Sports />);

    // The row links to the league by local id, exactly as Series links to a show.
    // Three queries have to settle before a row exists: the instance list, the
    // settings that carry the master toggle, and only then the leagues page.
    // That chain outruns findBy's 1s default when the whole suite is running.
    expect(
      await screen.findByRole(
        "link",
        { name: "Premier League" },
        { timeout: 8000 },
      ),
    ).toHaveAttribute("href", "/sports/51?instance=42");

    // Same table furniture Series and Movies get, which the old poster grid had none of.
    expect(screen.getByRole("table")).toBeInTheDocument();
    for (const header of [
      "Name",
      "Sport",
      "Audio",
      "Languages Profile",
      "Events",
    ]) {
      expect(
        screen.getByRole("columnheader", { name: header }),
      ).toBeInTheDocument();
    }
    expect(screen.getByText("Football")).toBeInTheDocument();
    // A count, not a ratio. Files can never be fewer than events (every row in
    // the sports events table is a playable file), so the only real divergence
    // is a multipart event, which the dedicated test below covers.
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText("Main Sportarr")).toBeInTheDocument();

    // Row selection drives the batch profile toolbar, as on the other two pages.
    await user.click(
      screen.getByRole("checkbox", { name: "Select Premier League" }),
    );
    expect(
      await screen.findByRole("button", { name: /change profile/i }),
    ).toBeInTheDocument();
    expect(assigned).toBeUndefined();
  });

  it("offers the same batch toolbar the other two libraries carry", async () => {
    const user = userEvent.setup();
    let batched: unknown;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
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

    await user.click(
      await screen.findByRole(
        "checkbox",
        { name: "Select Premier League" },
        { timeout: 8000 },
      ),
    );
    // Selecting rows used to offer only Change Profile: the batch endpoint
    // dropped anything that was not an episode, a movie or a series, so none
    // of these buttons had a backend to talk to.
    for (const label of [
      "Sync Subtitles",
      "Subtitle Tools",
      "Translate",
      "Combine",
      "More",
    ]) {
      expect(
        await screen.findByRole("button", { name: label }),
      ).toBeInTheDocument();
    }

    await user.click(screen.getByRole("button", { name: "More" }));
    await user.click(
      await screen.findByRole("menuitem", { name: "Scan Disk" }),
    );
    await user.click(
      await screen.findByRole("button", { name: /Apply to 1 Item/ }),
    );

    await waitFor(() => expect(batched).toBeDefined());
    // A league batches as one item, the way a series does; the backend expands
    // it into its events. The owner travels with it because a sports path
    // mapping is always per instance.
    expect(batched).toEqual({
      items: [
        { type: "sportsLeague", sportsLeagueId: 51, arr_instance_id: 42 },
      ],
      action: "scan-disk",
      options: undefined,
    });
  });

  it("does not present event and file counts as a completion ratio", async () => {
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
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
              // One two-part event: one distinct upstream event, two playable
              // files. As a ratio this is 2/1, a bar past 100% that also
              // colours a complete league yellow.
              eventCount: 1,
              eventFileCount: 2,
              missingLanguageCount: 0,
              profileId: 5,
            },
          ],
          total: 1,
        }),
      ),
    );
    customRender(<Sports />);

    expect(
      await screen.findByText("1 (2 files)", undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText("2/1")).toBeNull();
  });

  it("shows the missing-language count per league and nothing for a covered league", async () => {
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/sports/leagues", () =>
        HttpResponse.json({
          data: [
            {
              id: 51,
              arr_instance_id: 42,
              sportarrLeagueId: 7,
              title: "Partial League",
              sport: "Football",
              monitored: true,
              tags: [],
              audio_language: [],
              eventCount: 1,
              eventFileCount: 1,
              missingLanguageCount: 2,
              profileId: null,
            },
            {
              id: 52,
              arr_instance_id: 42,
              sportarrLeagueId: 8,
              title: "Complete League",
              sport: "Football",
              monitored: true,
              tags: [],
              audio_language: [],
              eventCount: 1,
              eventFileCount: 1,
              missingLanguageCount: 0,
              profileId: null,
            },
          ],
          total: 2,
        }),
      ),
    );
    customRender(<Sports />);
    const rowFor = (label: string): HTMLElement => {
      const link = screen.getByRole("link", { name: label });
      // eslint-disable-next-line testing-library/no-node-access
      const row = link.closest("tr");
      if (!row) throw new Error(`No row for ${label}`);
      return row;
    };
    await screen.findByRole(
      "link",
      { name: "Partial League" },
      { timeout: 8000 },
    );
    await screen.findByRole(
      "link",
      { name: "Complete League" },
      { timeout: 8000 },
    );
    // The aggregate count is rendered next to the events count and hidden
    // while the league has no event still wanting a language.
    expect(within(rowFor("Partial League")).getByText("2")).toBeInTheDocument();
    expect(within(rowFor("Complete League")).queryByText("2")).toBeNull();
  });

  it("does not request the library with no enabled Sportarr", async () => {
    let requests = 0;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([{ ...sportarr, enabled: false }]),
      ),
      http.get("/api/sports/leagues", () => {
        requests++;
        return HttpResponse.json({ data: [], total: 0 });
      }),
    );
    customRender(<Sports />);
    expect(
      await screen.findByText(
        "Enable a Sportarr instance in Connections to view sports.",
      ),
    ).toBeInTheDocument();
    expect(requests).toBe(0);
  });
  it("drops a disabled owner's cached leagues while another owner stays enabled", async () => {
    let enabled = true;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([{ ...sportarr, enabled }, sportarrSibling]),
      ),
      http.get("/api/sports/leagues", () =>
        HttpResponse.json({
          data: enabled
            ? [
                {
                  id: 51,
                  arr_instance_id: 42,
                  sportarrLeagueId: 7,
                  title: "Disabled owner league",
                  sport: "Football",
                  eventCount: 0,
                  eventFileCount: 0,
                  profileId: null,
                },
              ]
            : [],
          total: enabled ? 1 : 0,
        }),
      ),
    );
    customRender(<Sports />);
    expect(
      await screen.findByRole("link", { name: "Disabled owner league" }),
    ).toBeInTheDocument();
    enabled = false;
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: [QueryKeys.ArrInstances],
      });
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("link", { name: "Disabled owner league" }),
      ).toBeNull(),
    );
  });
});
