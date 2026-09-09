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
    expect(await screen.findByText("fr · Embedded")).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Index subtitles" }),
    );
    expect(
      await screen.findByText(
        "Could not index subtitles. Check the file path and try again.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("No indexed subtitles")).toBeInTheDocument();
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
    const table = await screen.findByRole("table", { name: "Event files" });
    const first = within(table).getByRole("row", { name: /Prelims/ });
    const second = within(table).getByRole("row", { name: /Main/ });
    expect(within(first).getByText("fr · Embedded")).toBeInTheDocument();
    expect(within(first).getByText("en")).toBeInTheDocument();
    expect(within(second).getByText("No language profile")).toBeInTheDocument();
    await userEvent.click(
      within(first).getByRole("button", { name: "Index subtitles" }),
    );
    expect(
      await within(first).findByText("en:hi · External"),
    ).toBeInTheDocument();
    expect(within(first).getByText("None missing")).toBeInTheDocument();
    expect(
      within(second).getByText("No indexed subtitles"),
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
    const table = await screen.findByRole("table", { name: "Event files" });
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    expect(within(table).getByText("Prelims")).toBeInTheDocument();
    expect(within(table).getByText("Main Card")).toBeInTheDocument();
    expect(within(table).getByText("Full event")).toBeInTheDocument();
    expect(within(table).getAllByText("2026-09-01")).toHaveLength(2);
    expect(within(table).getAllByText("Available")).toHaveLength(3);
    expect(screen.queryByText("Part 0")).toBeNull();
    expect(scope).toBe("42");
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
      await screen.findByRole("button", { name: "Search subtitles" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Search" })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Event.Release")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(await screen.findByText("en · External")).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
  },
);
