/* eslint-disable camelcase */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import SportsEvents from ".";

function renderDetail() {
  const router = createMemoryRouter(
    [{ path: "/sports/:id", element: <SportsEvents /> }],
    { initialEntries: ["/sports/51?instance=42"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

describe("sports event detail", () => {
  beforeEach(() => {
    // Every sports surface is gated on the master toggle now.
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
    );
  });
  it("refreshes cleared subtitle state when a file becomes unavailable", async () => {
    let unavailable = false;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/sports/leagues/51", () =>
        HttpResponse.json({
          id: 51,
          arr_instance_id: 42,
          title: "Fixture League",
          eventCount: 1,
          eventFileCount: 1,
        }),
      ),
      http.get("/api/sports/leagues/51/events", () =>
        HttpResponse.json({
          data: [
            {
              id: 61,
              arr_instance_id: 42,
              league_id: 51,
              title: "Event",
              path: "/sports/event.mkv",
              profileId: null,
              hasFile: true,
              subtitles: unavailable ? [] : [["fr", null, null]],
              missing_subtitles: [],
            },
          ],
          total: 1,
        }),
      ),
      http.post("/api/sports/events/61/subtitles", () => {
        unavailable = true;
        return HttpResponse.json(
          { message: "File unavailable" },
          { status: 409 },
        );
      }),
    );
    renderDetail();
    expect(await screen.findByText("fr")).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Index Subtitles" }),
    );
    expect(
      await screen.findByText(
        "Could not index subtitles. Check the file path and try again.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("No profile")).toBeInTheDocument();
  });

  it("shows subtitles per part and refreshes the selected local file", async () => {
    let indexed = false;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/sports/leagues/51", () =>
        HttpResponse.json({
          id: 51,
          arr_instance_id: 42,
          title: "Fixture League",
          eventCount: 1,
          eventFileCount: 2,
        }),
      ),
      http.get("/api/sports/leagues/51/events", () =>
        HttpResponse.json({
          data: [
            {
              id: 61,
              arr_instance_id: 42,
              league_id: 51,
              title: "Prelims",
              partNumber: 1,
              path: "/sports/prelims.mkv",
              hasFile: true,
              profileId: 5,
              subtitles: indexed
                ? [
                    ["fr", null, null],
                    ["en:hi", "/sports/prelims.en.hi.srt", 50],
                  ]
                : [["fr", null, null]],
              missing_subtitles: indexed ? [] : ["en"],
            },
            {
              id: 62,
              arr_instance_id: 42,
              league_id: 51,
              title: "Main",
              partNumber: 2,
              path: "/sports/main.mkv",
              hasFile: true,
              profileId: null,
              subtitles: [],
              missing_subtitles: [],
            },
          ],
          total: 2,
        }),
      ),
      http.post("/api/sports/events/61/subtitles", async ({ request }) => {
        expect(await request.json()).toEqual({ arr_instance_id: 42 });
        indexed = true;
        return HttpResponse.json({ id: 61, arr_instance_id: 42 });
      }),
    );
    renderDetail();
    const table = await screen.findByRole("table");
    const rowFor = (label: string | RegExp): HTMLElement => {
      const cell = within(table).getByText(label);
      // eslint-disable-next-line testing-library/no-node-access
      const row = cell.closest("tr");
      if (!row) throw new Error(`No row for ${String(label)}`);
      return row;
    };
    // The table renders before the events query resolves, so wait for the row
    // to exist rather than reading an empty table synchronously.
    await within(table).findByText(/Prelims/);
    expect(within(rowFor(/Prelims/)).getByText("fr")).toBeInTheDocument();
    expect(within(rowFor(/Prelims/)).getByText("en")).toBeInTheDocument();
    // No profile on the second part, so there is nothing wanted to show.
    expect(within(rowFor(/Main/)).getByText("No profile")).toBeInTheDocument();
    await userEvent.click(
      within(rowFor(/Prelims/)).getByRole("button", {
        name: "Index Subtitles",
      }),
    );
    expect(
      await within(rowFor(/Prelims/)).findByText("en:hi"),
    ).toBeInTheDocument();
  });

  it("lists each playable part with date and file state without inventing numbering", async () => {
    let scope = "";
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/sports/leagues/51", () =>
        HttpResponse.json({
          id: 51,
          arr_instance_id: 42,
          title: "Fixture League",
          eventCount: 2,
          eventFileCount: 3,
        }),
      ),
      http.get("/api/sports/leagues/51/events", ({ request }) => {
        scope = new URL(request.url).searchParams.get("arr_instance_id") ?? "";
        return HttpResponse.json({
          data: [
            {
              id: 61,
              arr_instance_id: 42,
              league_id: 51,
              title: "Fixture Card",
              partName: "Prelims",
              partNumber: 1,
              broadcastDate: "2026-09-01T00:00:00",
              hasFile: true,
              path: "/sports/prelims.mkv",
            },
            {
              id: 62,
              arr_instance_id: 42,
              league_id: 51,
              title: "Fixture Card",
              partName: "Main Card",
              partNumber: 2,
              broadcastDate: "2026-09-01T00:00:00",
              hasFile: true,
              path: "/sports/main.mkv",
            },
            {
              id: 63,
              arr_instance_id: 42,
              league_id: 51,
              title: "Unknown Number",
              partName: null,
              partNumber: null,
              eventDate: "2026-09-02T12:00:00Z",
              hasFile: true,
              path: "/sports/unknown.mkv",
            },
          ],
          total: 3,
        });
      }),
    );
    renderDetail();
    const table = await screen.findByRole("table");
    // The shared table folds the part into the title, the way the episodes
    // table shows an episode title, rather than giving parts their own column.
    expect(
      await within(table).findByText("Fixture Card (Prelims)"),
    ).toBeInTheDocument();
    expect(
      within(table).getByText("Fixture Card (Main Card)"),
    ).toBeInTheDocument();
    // A part-less event keeps its bare title: no invented "Part 0".
    expect(within(table).getByText("Unknown Number")).toBeInTheDocument();
    expect(within(table).queryByText(/Part 0/)).toBeNull();
    expect(within(table).getAllByText("2026-09-01")).toHaveLength(2);
    expect(scope).toBe("42");
  });

  it("opens the shared Subtitle Tools on the event's own subtitles", async () => {
    let removed: unknown;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/sports/leagues/51", () =>
        HttpResponse.json({
          id: 51,
          arr_instance_id: 42,
          title: "Fixture League",
          eventCount: 1,
          eventFileCount: 1,
        }),
      ),
      http.get("/api/sports/leagues/51/events", () =>
        HttpResponse.json({
          data: [
            {
              id: 61,
              arr_instance_id: 42,
              league_id: 51,
              title: "Event",
              path: "/sports/event.mkv",
              hasFile: true,
              profileId: 5,
              subtitles: [
                ["en:hi", "/sports/event.en.hi.srt", 50],
                // An embedded track has no file, so there is nothing for the
                // tools to act on and it must not reach the list.
                ["fr", null, null],
              ],
              missing_subtitles: [],
            },
          ],
          total: 1,
        }),
      ),
      http.delete("/api/sports/events/61/subtitles", async ({ request }) => {
        removed = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderDetail();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Mass Edit" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText("event.en.hi.srt")).toBeInTheDocument();
    expect(dialog.queryByText(/event\.fr/)).toBeNull();

    // The tools table checkboxes carry ids rather than labels, so the row
    // is selected by position: there is exactly one file-backed subtitle.
    await user.click(dialog.getAllByRole("checkbox")[1]);
    await user.click(dialog.getByRole("button", { name: "Select Action" }));
    await user.click(
      await screen.findByRole("menuitem", { name: "Delete..." }),
    );
    // Deleting a file asks first, and the confirmation lists what goes.
    const confirm = within(
      await screen.findByRole("dialog", { name: /deleted/ }),
    );
    expect(confirm.getByText("/sports/event.en.hi.srt")).toBeInTheDocument();
    await user.click(confirm.getByRole("button", { name: "Delete" }));

    // The local event id and its owner, not the series/movie id pair the
    // other two media types build.
    await waitFor(() =>
      expect(removed).toEqual({
        arr_instance_id: 42,
        language: "en",
        path: "/sports/event.en.hi.srt",
        hi: true,
        forced: false,
      }),
    );
  });

  it("gives a present subtitle the shared tools menu", async () => {
    let removed: unknown;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/sports/leagues/51", () =>
        HttpResponse.json({
          id: 51,
          arr_instance_id: 42,
          title: "Fixture League",
          eventCount: 1,
          eventFileCount: 1,
        }),
      ),
      http.get("/api/sports/leagues/51/events", () =>
        HttpResponse.json({
          data: [
            {
              id: 61,
              arr_instance_id: 42,
              league_id: 51,
              title: "Event",
              path: "/sports/event.mkv",
              hasFile: true,
              profileId: 5,
              subtitles: [["en:hi", "/sports/event.en.hi.srt", 50]],
              missing_subtitles: [],
            },
          ],
          total: 1,
        }),
      ),
      http.delete("/api/sports/events/61/subtitles", async ({ request }) => {
        removed = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderDetail();
    const user = userEvent.setup();

    // A subtitle that exists used to render as a badge with no handler at all,
    // so nothing could be done with it from the only page that lists it.
    await user.click(await screen.findByText("en:hi"));
    // The dropdown mounts through a portal behind a transition, so it is waited
    // for by its contents rather than read synchronously or by role.
    await user.click(await screen.findByText("Delete..."));
    const confirm = within(
      await screen.findByRole("dialog", { name: /deleted/ }),
    );
    // The menu carries this subtitle's own file, not the league's.
    expect(confirm.getByText("/sports/event.en.hi.srt")).toBeInTheDocument();
    await user.click(confirm.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(removed).toEqual({
        arr_instance_id: 42,
        language: "en",
        path: "/sports/event.en.hi.srt",
        hi: true,
        forced: false,
      }),
    );
  });

  it("searches one missing language from its badge", async () => {
    let searched: unknown;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/system/languages", () =>
        HttpResponse.json([
          { code2: "en", code3: "eng", name: "English", enabled: true },
        ]),
      ),
      http.get("/api/sports/leagues/51", () =>
        HttpResponse.json({ id: 51, arr_instance_id: 42, title: "League" }),
      ),
      http.get("/api/sports/leagues/51/events", () =>
        HttpResponse.json({
          data: [
            {
              id: 61,
              arr_instance_id: 42,
              league_id: 51,
              title: "Event",
              path: "/sports/event.mkv",
              hasFile: true,
              profileId: 5,
              subtitles: [],
              missing_subtitles: ["en:hi"],
            },
          ],
          total: 1,
        }),
      ),
      http.post("/api/sports/events/61/search", async ({ request }) => {
        searched = await request.json();
        return HttpResponse.json({ data: [] });
      }),
    );
    renderDetail();
    const user = userEvent.setup();
    // A badge now opens the same tools menu episodes and movies show, so the
    // search is the menu's own action rather than the badge's click handler.
    await user.click(await screen.findByText("en:hi"));
    await user.click(await screen.findByRole("menuitem", { name: "Search" }));
    const dialog = within(await screen.findByRole("dialog"));
    await user.click(dialog.getByRole("button", { name: "Search" }));

    // Opened on the clicked language and its modifier, not the profile's
    // first entry.
    await waitFor(() =>
      expect(searched).toEqual({
        arr_instance_id: 42,
        language: "en",
        hi: true,
        forced: false,
      }),
    );
  });

  it("shows the audio languages and combines one event from its row", async () => {
    let combined: unknown;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/sports/leagues/51", () =>
        HttpResponse.json({ id: 51, arr_instance_id: 42, title: "League" }),
      ),
      http.get("/api/sports/leagues/51/events", () =>
        HttpResponse.json({
          data: [
            {
              id: 61,
              arr_instance_id: 42,
              league_id: 51,
              title: "Event",
              path: "/sports/event.mkv",
              hasFile: true,
              profileId: 5,
              audio_language: ["hu", "en"],
              subtitles: [["hu", "/sports/event.hu.srt", 40]],
              missing_subtitles: [],
            },
          ],
          total: 1,
        }),
      ),
      http.post(
        "/api/sports/events/61/subtitles/combine",
        async ({ request }) => {
          combined = await request.json();
          return HttpResponse.json({
            status: "built",
            path: "/sports/out.srt",
          });
        },
      ),
    );
    renderDetail();
    const table = await screen.findByRole("table");
    // The indexer reads these off the file and the table never showed them,
    // though the audio is exactly what decides whether a subtitle is wanted.
    await within(table).findByText("Event");
    expect(
      within(table).getByRole("columnheader", { name: "Audio" }),
    ).toBeInTheDocument();
    // "en" is audio only here; the event's one indexed subtitle is Hungarian,
    // so this cannot be satisfied by the Subtitles column.
    expect(within(table).getByText("en")).toBeInTheDocument();

    await userEvent
      .setup()
      .click(within(table).getByRole("button", { name: "Combine Subtitles" }));

    // No languages or format: a sports composition follows its league
    // profile's rule, and the engine refuses an override alongside it.
    await waitFor(() => expect(combined).toEqual({}));
  });

  it("does not load events for a disabled owner", async () => {
    let requests = 0;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([{ ...sportarr, enabled: false }]),
      ),
      http.get("/api/sports/leagues/51/events", () => {
        requests++;
        return HttpResponse.json({ data: [], total: 0 });
      }),
    );
    renderDetail();
    expect(
      await screen.findByText(
        "Enable a Sportarr instance in Connections to view sports.",
      ),
    ).toBeInTheDocument();
    expect(requests).toBe(0);
  });
});

it.each([false, true])(
  "shows committed sports publication and refresh outcome, warning=%s",
  async (warning) => {
    let saved = false;
    const event = {
      id: 61,
      arr_instance_id: 42,
      league_id: 51,
      title: "Event",
      path: "/sports/event.mkv",
      profileId: null,
      hasFile: true,
      missing_subtitles: [],
    };
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      // This case sits outside the describe block, so it registers the master
      // toggle itself rather than inheriting the suite's beforeEach.
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
      http.get("/api/sports/leagues/51", () =>
        HttpResponse.json({ id: 51, arr_instance_id: 42, title: "League" }),
      ),
      http.get("/api/system/languages", () =>
        HttpResponse.json([
          { code2: "en", code3: "eng", name: "English", enabled: true },
        ]),
      ),
      http.get("/api/sports/leagues/51/events", () =>
        HttpResponse.json({
          data: [
            {
              ...event,
              subtitles: saved ? [["en", "/sports/event.en.srt", 80]] : [],
            },
          ],
          total: 1,
        }),
      ),
      http.post("/api/sports/events/61/search", async ({ request }) => {
        expect(await request.json()).toEqual({
          arr_instance_id: 42,
          language: "en",
          hi: false,
          forced: false,
        });
        return HttpResponse.json({
          data: [
            {
              provider: "fixture",
              subtitle: "owned-candidate",
              language: "en",
              forced: "False",
              hearing_impaired: "False",
              score: 80,
              orig_score: 144,
              score_without_hash: 144,
              release_info: ["Event.Release"],
              matches: ["title"],
              dont_matches: [],
              original_format: false,
            },
          ],
        });
      }),
      http.post("/api/sports/events/61/download", async ({ request }) => {
        const body = (await request.json()) as {
          arr_instance_id: number;
          candidate: { subtitle: string };
        };
        expect(body.arr_instance_id).toBe(42);
        expect(body.candidate.subtitle).toBe("owned-candidate");
        saved = true;
        return HttpResponse.json({
          event: { ...event, subtitles: [["en", "/sports/event.en.srt", 80]] },
          publication: {
            published: true,
            status: warning ? "published_with_warnings" : "published",
            message: warning
              ? "Subtitle published; index did not complete. History committed. Index refresh failed; no refresh queued."
              : "Subtitle published. Processing and history completed; subtitle index refreshed.",
            history: "committed",
            index: warning ? "failed" : "completed",
            refresh_attempts: warning ? 2 : 1,
            refresh_queued: false,
          },
        });
      }),
    );
    renderDetail();
    await userEvent.click(
      await screen.findByRole("button", { name: "Manual Search" }),
    );
    // Scoped to the modal: the page toolbox now carries its own Search button,
    // exactly as the episodes page does.
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() =>
      expect(dialog.getByRole("button", { name: "Search" })).toBeEnabled(),
    );
    await userEvent.click(dialog.getByRole("button", { name: "Search" }));
    expect(await dialog.findByText("Event.Release")).toBeInTheDocument();
    // findBy, not getBy: the results table is re-rendered as the sports
    // queries settle, so a one-shot query can land between the old rows going
    // and the new ones mounting, with the release text already matched.
    await userEvent.click(
      await dialog.findByRole("button", { name: "Download" }),
    );
    // Scoped to the page table: the search modal renders a results table too,
    // and "en" appears in both, so anything wider is ambiguous.
    await waitFor(() => {
      const pageTable = screen
        .getAllByRole("table")
        // eslint-disable-next-line testing-library/no-node-access
        .find((t) => t.closest('[role="dialog"]') === null);
      if (!pageTable) throw new Error("No page table");
      expect(within(pageTable).getByText("en")).toBeInTheDocument();
    });
    expect(
      await screen.findByText(
        warning
          ? /Index refresh failed; no refresh queued/
          : /subtitle index refreshed/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Download failed. Search again and retry."),
    ).not.toBeInTheDocument();
    // Re-queried rather than reusing the handle captured above: the modal
    // re-renders when the sports query is invalidated, which can replace the
    // dialog node and leave the old handle pointing at a detached tree.
    // Scoped to it either way, because the page toolbox carries its own
    // Download button for the league's subtitle bundle.
    // The whole lookup retries, not just the dialog: the modal re-renders when
    // the sports query is invalidated, so a one-shot query inside it can land
    // between the results table being replaced and the new one mounting.
    await waitFor(() =>
      expect(
        within(screen.getByRole("dialog")).getByRole("button", {
          name: "Download",
        }),
      ).toBeDisabled(),
    );
  },
);
