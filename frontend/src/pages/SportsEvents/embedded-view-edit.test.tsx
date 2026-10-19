/* eslint-disable camelcase */

/**
 * View, Edit and Download on a sports event's embedded badge read the track
 * itself (?source=embedded), and an image-based track offers none of them.
 * The row's codec travels with the source: two text tracks of one key are
 * two rows.
 */

import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import * as files from "@/utilities/files";
import SportsEvents from ".";

function serveEvent() {
  server.use(
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: true } }),
    ),
    http.get("/api/system/arr-instances", () => HttpResponse.json([sportarr])),
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
            subtitles: [
              ["fr", null, null, "subrip"],
              ["de", null, null, "pgs"],
            ],
            missing_subtitles: [],
          },
        ],
        total: 1,
      }),
    ),
  );
}

function mountEvents() {
  const router = createMemoryRouter(
    [
      { path: "/sports/:id", element: <SportsEvents /> },
      { path: "/subtitles/*", element: <div>editor</div> },
    ],
    { initialEntries: ["/sports/51?instance=42"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return () =>
    `${router.state.location.pathname}${router.state.location.search}`;
}

// Mantine hides a floating dropdown once jsdom reports its target has no
// size, so the rows are found with hidden: true, as the other menu tests do.
const item = (name: string) =>
  screen.findByRole("menuitem", { name, hidden: true });

describe("an embedded sports track", () => {
  it.each([
    ["View", "/subtitles/preview/sports/61/fr"],
    ["Edit", "/subtitles/edit/sports/61/fr"],
  ])("opens %s on the track itself", async (action, path) => {
    serveEvent();
    const user = userEvent.setup();
    const at = mountEvents();

    await user.click(await screen.findByTitle("Embedded (SRT)"));
    await user.click(await item(action));

    await waitFor(() =>
      expect(at()).toBe(
        `${path}?arr_instance_id=42&source=embedded&codec=subrip`,
      ),
    );
  });

  it("downloads the track, not a file", async () => {
    serveEvent();
    const asked: (string | null)[] = [];
    const codecs: (string | null)[] = [];
    server.use(
      http.get("/api/sports/events/61/subtitles/fr/download", ({ request }) => {
        const url = new URL(request.url);
        asked.push(url.searchParams.get("source"));
        codecs.push(url.searchParams.get("codec"));
        return new HttpResponse("1\n00:00:01,000 --> 00:00:02,000\nSalut\n", {
          headers: {
            "Content-Type": "application/x-subrip",
            "Content-Disposition": "attachment; filename=event.fr.srt",
          },
        });
      }),
    );
    const save = vi
      .spyOn(files, "saveBlobAs")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    mountEvents();

    await user.click(await screen.findByTitle("Embedded (SRT)"));
    await user.click(await item("Download"));

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(asked).toEqual(["embedded"]);
    expect(codecs).toEqual(["subrip"]);
    expect(save.mock.calls[0][1]).toBe("event.fr.srt");
    save.mockRestore();
  });

  it("offers none of them on an image-based track", async () => {
    serveEvent();
    const user = userEvent.setup();
    mountEvents();

    await user.click(await screen.findByTitle("Embedded (PGS, image-based)"));

    for (const name of ["View", "Edit", "Download"]) {
      expect(await item(name)).toBeDisabled();
    }
  });
});
