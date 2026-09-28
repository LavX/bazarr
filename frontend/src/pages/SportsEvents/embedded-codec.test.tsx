/* eslint-disable camelcase */
import { createMemoryRouter, RouterProvider } from "react-router";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import { AllProviders } from "@/providers";
import { rawRender, screen } from "@/tests";
import server from "@/tests/mocks/node";
import { toSportsSubtitle } from "./table";
import SportsEvents from ".";

describe("toSportsSubtitle", () => {
  it("reads the codec of a four element embedded entry", () => {
    expect(toSportsSubtitle(["en:hi", null, null, "pgs"])).toMatchObject({
      code2: "en",
      hi: true,
      path: null,
      codec: "pgs",
    });
  });

  it("leaves the codec null for legacy entries and files", () => {
    expect(toSportsSubtitle(["fr", null, null]).codec).toBeNull();
    expect(
      toSportsSubtitle(["fr", "/sports/event.fr.srt", 12]).codec,
    ).toBeNull();
  });
});

describe("sports embedded badge", () => {
  it("names the codec of an embedded track on hover", async () => {
    server.use(
      http.get("/api/system/languages", () => HttpResponse.json([])),
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
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
              subtitles: [
                ["fr", null, null, "pgs"],
                ["de", null, null],
              ],
              missing_subtitles: [],
            },
          ],
          total: 1,
        }),
      ),
    );
    const router = createMemoryRouter(
      [{ path: "/sports/:id", element: <SportsEvents /> }],
      { initialEntries: ["/sports/51?instance=42"] },
    );
    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );

    expect(
      await screen.findByTitle("Embedded (PGS, image-based)"),
    ).toHaveTextContent("fr");
    expect(screen.getByTitle("Embedded")).toHaveTextContent("de");
  });
});
