/* eslint-disable camelcase */

/**
 * A refetch that adds or removes episodes must leave the other rows' cells in
 * place. The subtitles cell used to be declared inside the table's render, so
 * every change to the list remounted every cell and closed a badge menu the
 * user had open on an episode that had not changed.
 */

import { useEffect, useRef, useState } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { Table as TableInstance } from "@tanstack/react-table";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import Table from "./table";

function makeEpisode(episode: number): Item.Episode {
  return {
    id: 10 + episode,
    series_id: 3,
    sonarrSeriesId: 7,
    sonarrEpisodeId: 100 + episode,
    arr_instance_id: 1,
    season: 1,
    episode,
    title: `Episode ${episode}`,
    path: `/tv/show/s01e0${episode}.mkv`,
    monitored: true,
    sceneName: null,
    audio_language: [],
    subtitles: [
      {
        code2: "en",
        name: "English",
        hi: false,
        forced: false,
        path: `/tv/show/s01e0${episode}.en.srt`,
      },
    ],
    missing_subtitles: [],
  } as unknown as Item.Episode;
}

function mountTable(initial: Item.Episode[]) {
  let setEpisodes!: (episodes: Item.Episode[]) => void;
  function Harness() {
    const [episodes, set] = useState<Item.Episode[]>(initial);
    useEffect(() => {
      setEpisodes = set;
    }, [set]);
    const ref = useRef<TableInstance<Item.Episode> | null>(null);
    return (
      <Table
        ref={ref}
        episodes={episodes}
        profile={undefined}
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
  return (next: Item.Episode[]) => act(async () => setEpisodes(next));
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

describe("Episodes Table, an open badge menu", () => {
  it("stays open while episodes before it are removed and added", async () => {
    server.use(http.get("/api/system/languages", () => HttpResponse.json([])));
    const user = userEvent.setup();
    const first = makeEpisode(1);
    const second = makeEpisode(2);
    const third = makeEpisode(3);
    // Rows sort by episode, newest first, so episode 2 sorts before 1.
    const setEpisodes = mountTable([second, first]);

    await screen.findByTitle("File: s01e01.en.srt");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    const badge = screen.getByTitle("File: s01e01.en.srt");
    const row = rowOf(badge);
    const contents = cellContents(row);
    expect(contents.filter(Boolean).length).toBeGreaterThan(0);

    await user.click(badge);
    // jsdom has no layout, so Mantine keeps the dropdown display:none. Its
    // node is what shows the menu is still the one the user opened.
    expect(await screen.findByText("Translate...")).toBeInTheDocument();
    const menu = screen.getByRole("menu", { hidden: true });

    await setEpisodes([first]);
    expect(screen.queryByText("Episode 2")).not.toBeInTheDocument();
    expect(rowOf(screen.getByTitle("File: s01e01.en.srt"))).toBe(row);
    expect(screen.getByRole("menu", { hidden: true })).toBe(menu);
    expectSameNodes(contents, cellContents(row));

    await setEpisodes([third, second, first]);
    expect(screen.getByText("Episode 3")).toBeInTheDocument();
    expect(rowOf(screen.getByTitle("File: s01e01.en.srt"))).toBe(row);
    expect(screen.getByRole("menu", { hidden: true })).toBe(menu);
    expectSameNodes(contents, cellContents(row));
  });
});
