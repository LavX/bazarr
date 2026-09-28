/* eslint-disable camelcase */
import { createRef } from "react";
import { Table as TableInstance } from "@tanstack/react-table";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { SportsEvent } from "@/apis/raw/sports";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import Table from "./table";

const event = {
  id: 11,
  arr_instance_id: 3,
  league_id: 5,
  title: "Game 12",
  season: 2026,
  episode: 12,
  path: "/sports/NHL/Game 12.mkv",
  monitored: true,
  hasFile: true,
  audio_language: [],
  subtitles: [],
  missing_subtitles: [],
} as unknown as SportsEvent;

describe("the sports event row's Play on control", () => {
  it("asks about the event's local id and stays open when the page renders again", async () => {
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
    const ref = createRef<TableInstance<SportsEvent>>();
    const props = {
      events: [event],
      onAllRowsExpandedChanged: () => undefined,
    };
    const { rerender } = customRender(
      <Table ref={ref} onIndex={() => undefined} {...props} />,
    );
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", {
        name: "Play Game 12 on a media server",
        hidden: true,
      }),
    );
    await screen.findByText("No media server covers this file's path");
    expect(asked).toBe("?media_type=sports&id=11");
    // The page hands the table a new onIndex on every render, which rebuilds
    // its columns; the control's own cell must not be remounted by that.
    rerender(<Table ref={ref} onIndex={() => undefined} {...props} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "Play Game 12 on a media server",
          hidden: true,
        }),
      ).toHaveAttribute("aria-expanded", "true"),
    );
  });
});
