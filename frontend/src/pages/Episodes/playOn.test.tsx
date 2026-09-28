/* eslint-disable camelcase */
import { createRef } from "react";
import { Table as TableInstance } from "@tanstack/react-table";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import Table from "./table";

const episode = {
  id: 9,
  series_id: 5,
  sonarrSeriesId: 21,
  sonarrEpisodeId: 31,
  arr_instance_id: 1,
  season: 1,
  episode: 2,
  title: "The Train Job",
  monitored: true,
  path: "/tv/Firefly/S01E02.mkv",
  sceneName: "",
  audio_language: [],
  subtitles: [],
  missing_subtitles: [],
} as unknown as Item.Episode;

describe("the episode row's Play on control", () => {
  it("names the episode and stays open when the table renders again", async () => {
    let asked = "";
    server.use(
      http.get("/api/system/media-server-links/destinations", () =>
        HttpResponse.json({
          data: [
            { instance_id: "e1", kind: "emby", display_name: "Emby" },
            { instance_id: "p1", kind: "plex", display_name: "Plex" },
          ],
        }),
      ),
      http.get("/api/system/media-server-links/candidates", ({ request }) => {
        asked = new URL(request.url).search;
        return HttpResponse.json({ data: [] });
      }),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );
    const ref = createRef<TableInstance<Item.Episode>>();
    const props = { onAllRowsExpandedChanged: () => undefined };
    const { rerender } = customRender(
      <Table ref={ref} episodes={[episode]} {...props} />,
    );
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", {
        name: "Play S01E02 on a media server",
        hidden: true,
      }),
    );
    await screen.findByText("No media server covers this file's path");
    // The episode's own local id, not its Sonarr ids.
    expect(asked).toBe("?media_type=episode&id=9");
    rerender(<Table ref={ref} episodes={[{ ...episode }]} {...props} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "Play S01E02 on a media server",
          hidden: true,
        }),
      ).toHaveAttribute("aria-expanded", "true"),
    );
  });
});
