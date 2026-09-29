/* eslint-disable camelcase */

/**
 * Hidden tracks on the sports events table, whose cells read the page's event
 * ids from the table context. One request covers every event, and a hide on
 * another event leaves an open badge menu where it is.
 */

import { useRef, useState } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { Table as TableInstance } from "@tanstack/react-table";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import type { SportsEvent } from "@/apis/raw/sports";
import type { EmbeddedHiddenTrack } from "@/apis/raw/subtitles";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import Table from "./table";

function makeEvent(episode: number, embedded = true): SportsEvent {
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
    subtitles: [
      ["en", `/sports/event${episode}.en.srt`, 10],
      ...(embedded ? [["fr", null, null]] : []),
    ],
    missing_subtitles: [],
  } as unknown as SportsEvent;
}

function hiddenFrench(mediaId: number): EmbeddedHiddenTrack {
  return {
    media_type: "sports",
    media_id: mediaId,
    arr_instance_id: 42,
    language: "fr",
    code2: "fr",
    name: "French",
    hi: false,
    forced: false,
    codec: null,
    hidden_at: null,
  };
}

// The hidden-tracks endpoint, with every request's ids kept.
function serveHidden(hidden: EmbeddedHiddenTrack[]) {
  const asked: number[][] = [];
  server.use(
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/subtitles/embedded-hidden", ({ request }) => {
      const url = new URL(request.url);
      expect(url.searchParams.get("media_type")).toBe("sports");
      asked.push(url.searchParams.getAll("media_id[]").map(Number));
      return HttpResponse.json({ data: [...hidden] });
    }),
  );
  return asked;
}

function mountTable(initial: SportsEvent[]) {
  let setEvents!: (events: SportsEvent[]) => void;
  function Harness() {
    const [events, set] = useState<SportsEvent[]>(initial);
    setEvents = set;
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

// Runs the app's own socket handler, as a hide in another tab reaches this one.
function receiveSportsUpdate() {
  const reducer = createDefaultReducer().find((item) => item.key === "sports");
  reducer?.any?.();
  (reducer?.update as ((ids: number[]) => void) | undefined)?.([62]);
}

function rowOf(element: HTMLElement) {
  return element.closest("tr")!;
}

function rowOfEvent(title: string) {
  return rowOf(screen.getByText(title));
}

function cellContents(row: HTMLElement) {
  return Array.from(row.querySelectorAll(":scope > td")).map(
    (td) => td.firstChild,
  );
}

describe("Sports events Table, hidden tracks", () => {
  it("asks once for every event and shows each track on its own row", async () => {
    const asked = serveHidden([hiddenFrench(62)]);
    mountTable([makeEvent(2, false), makeEvent(1)]);

    const disclosure = await screen.findByRole("button", {
      name: "Hidden tracks (1)",
    });
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(asked).toEqual([[61, 62]]);
    expect(rowOf(disclosure)).toBe(rowOfEvent("Event 2"));
    expect(
      within(rowOfEvent("Event 1")).queryByRole("button", {
        name: /Hidden tracks/,
      }),
    ).not.toBeInTheDocument();
  });

  it("keeps the subtitles line as wide as its badges and trigger", async () => {
    serveHidden([hiddenFrench(61)]);
    mountTable([makeEvent(1)]);

    const disclosure = await screen.findByRole("button", {
      name: "Hidden tracks (1)",
    });
    // The line does not wrap, and the table sizes the column from the cell's
    // minimum width. Without one, a phone-width table cut the labels off.
    // eslint-disable-next-line testing-library/no-node-access
    const line = disclosure.parentElement!;
    expect(line).toContainElement(screen.getByTitle("/sports/event1.en.srt"));
    expect(line.getAttribute("style")).toContain("min-width: max-content");
  });

  it("keeps an open badge menu while a hide on another event changes its row", async () => {
    const hidden: EmbeddedHiddenTrack[] = [];
    serveHidden(hidden);
    const user = userEvent.setup();
    const setEvents = mountTable([makeEvent(2), makeEvent(1)]);

    await screen.findByTitle("/sports/event1.en.srt");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    const row = rowOfEvent("Event 1");
    const contents = cellContents(row);

    await user.click(screen.getByTitle("/sports/event1.en.srt"));
    expect(await screen.findByText("Translate...")).toBeInTheDocument();
    const menu = screen.getByRole("menu", { hidden: true });

    // Event 2's French track is hidden elsewhere: the socket event refreshes
    // the hidden list, and the refetched list drops the track.
    hidden.push(hiddenFrench(62));
    await setEvents([makeEvent(2, false), makeEvent(1)]);
    await act(async () => receiveSportsUpdate());

    const disclosure = await screen.findByRole("button", {
      name: "Hidden tracks (1)",
    });
    expect(rowOf(disclosure)).toBe(rowOfEvent("Event 2"));
    expect(rowOfEvent("Event 1")).toBe(row);
    expect(screen.getByRole("menu", { hidden: true })).toBe(menu);
    const after = cellContents(row);
    expect(after).toHaveLength(contents.length);
    contents.forEach((node, index) => expect(after[index]).toBe(node));
  });
});
