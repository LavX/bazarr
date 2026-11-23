/* eslint-disable camelcase */

/**
 * A refetch that adds or removes events must leave the other rows' cells in
 * place. The page hands the table a new index callback on every render, and
 * the columns were rebuilt whenever it changed, so every change to the list
 * remounted every cell and closed a badge menu the user had open on an event
 * that had not changed.
 */

import { useEffect, useRef, useState } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { Table as TableInstance } from "@tanstack/react-table";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { SportsEvent } from "@/apis/raw/sports";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import Table from "./table";

function makeEvent(episode: number): SportsEvent {
  return {
    id: 60 + episode,
    arr_instance_id: 42,
    league_id: 51,
    season: 2026,
    episode,
    title: `Event ${episode}`,
    path: `/sports/event${episode}.mkv`,
    eventDate: "2026-09-01",
    broadcastDate: null,
    hasFile: true,
    monitored: true,
    mapped_path: `/sports/event${episode}.mkv`,
    file_size: 1,
    profileId: 1,
    audio_language: [],
    subtitles: [["en", `/sports/event${episode}.en.srt`, 10]],
    missing_subtitles: [],
  } as unknown as SportsEvent;
}

function mountTable(initial: SportsEvent[]) {
  let setEvents!: (events: SportsEvent[]) => void;
  function Harness() {
    const [events, set] = useState<SportsEvent[]>(initial);
    useEffect(() => {
      setEvents = set;
    }, [set]);
    const ref = useRef<TableInstance<SportsEvent> | null>(null);
    return (
      <Table
        ref={ref}
        events={events}
        profile={undefined}
        // A new callback on every render, as the page passes it.
        onIndex={vi.fn()}
        onAllRowsExpandedChanged={vi.fn()}
      />
    );
  }
  const router = createMemoryRouter([{ path: "/", element: <Harness /> }]);
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return (next: SportsEvent[]) => act(async () => setEvents(next));
}

function rowOf(element: HTMLElement) {
  return element.closest("tr")!;
}

function cellContents(row: HTMLElement) {
  return Array.from(row.querySelectorAll(":scope > td")).map(
    (td) => td.firstChild,
  );
}

function expectSameNodes(before: (Node | null)[], after: (Node | null)[]) {
  expect(after).toHaveLength(before.length);
  before.forEach((node, index) => expect(after[index]).toBe(node));
}

describe("Sports events Table, an open badge menu", () => {
  it("sets the score of the chosen sports file using its local event id and owner", async () => {
    const user = userEvent.setup();
    const bodies: FormData[] = [];
    server.use(
      http.get("/api/system/languages", () => HttpResponse.json([])),
      http.post("/api/subtitles/score-override", async ({ request }) => {
        bodies.push(await request.formData());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const event = makeEvent(1);
    event.subtitles = [
      ["en", "/sports/event1.en.srt", 10],
      ["en", "/sports/event1.alt.en.srt", 20],
    ];
    event.manual_subtitle_scores = { "/sports/event1.alt.en.srt": 0 };
    mountTable([event]);

    const badge = await screen.findByTitle("/sports/event1.alt.en.srt");
    expect(within(badge).getByText("0%")).toBeInTheDocument();
    expect(
      within(screen.getByTitle("/sports/event1.en.srt")).queryByText("0%"),
    ).toBeNull();
    await user.click(badge);
    await user.click(
      await screen.findByRole("menuitem", { name: "Set Score..." }),
    );
    const input = await screen.findByRole("textbox", { name: "Score (%)" });
    await user.clear(input);
    await user.type(input, "0");
    await user.click(screen.getByRole("button", { name: "Set score" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0].get("sportsEventId")).toBe("61");
    expect(bodies[0].get("arr_instance_id")).toBe("42");
    expect(bodies[0].get("subtitlesPath")).toBe("/sports/event1.alt.en.srt");
    expect(bodies[0].get("score")).toBe("0");
    expect(bodies[0].has("radarrMovieId")).toBe(false);
    expect(bodies[0].has("sonarrEpisodeId")).toBe(false);
  });

  it("stays open while events before it are removed and added", async () => {
    server.use(http.get("/api/system/languages", () => HttpResponse.json([])));
    const user = userEvent.setup();
    const first = makeEvent(1);
    const second = makeEvent(2);
    const third = makeEvent(3);
    // Rows sort by event number, newest first, so event 2 sorts before 1.
    const setEvents = mountTable([second, first]);

    await screen.findByTitle("/sports/event1.en.srt");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    const badge = screen.getByTitle("/sports/event1.en.srt");
    const row = rowOf(badge);
    const contents = cellContents(row);
    expect(contents.filter(Boolean).length).toBeGreaterThan(0);

    await user.click(badge);
    // The menu node is what shows it is still the one the user opened.
    // hidden: true only widens the query; the test theme keeps the dropdown
    // visible.
    expect(await screen.findByText("Translate...")).toBeInTheDocument();
    const menu = screen.getByRole("menu", { hidden: true });

    await setEvents([first]);
    expect(screen.queryByText("Event 2")).not.toBeInTheDocument();
    expect(rowOf(screen.getByTitle("/sports/event1.en.srt"))).toBe(row);
    expect(screen.getByRole("menu", { hidden: true })).toBe(menu);
    expectSameNodes(contents, cellContents(row));

    await setEvents([third, second, first]);
    expect(screen.getByText("Event 3")).toBeInTheDocument();
    expect(rowOf(screen.getByTitle("/sports/event1.en.srt"))).toBe(row);
    expect(screen.getByRole("menu", { hidden: true })).toBe(menu);
    expectSameNodes(contents, cellContents(row));
  });
});
