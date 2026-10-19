/* eslint-disable camelcase */

/**
 * A refetch that adds or removes subtitle rows must leave the other rows'
 * cells in place. The cell components used to be rebuilt on every render of
 * the table, so each change to the movie remounted every cell and closed a
 * row menu the user had open, even on a row whose own data had not changed.
 */

import { useEffect, useState } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import Table from "@/pages/Movies/Details/table";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

function makeMovie(subtitles: Subtitle[]): Item.Movie {
  return {
    id: 1,
    radarrId: 1,
    title: "Test Movie",
    path: "/movies/test.mkv",
    profileId: 1,
    fanart: "",
    overview: "",
    imdbId: "tt0000001",
    alternativeTitles: [],
    poster: "",
    year: "2024",
    monitored: true,
    tags: [],
    audio_language: [],
    subtitles,
    missing_subtitles: [],
  };
}

const englishTrack: Subtitle = {
  code2: "en",
  name: "English",
  hi: false,
  forced: false,
  path: null,
  codec: "subrip",
};

const danishFile: Subtitle = {
  code2: "da",
  name: "Danish",
  hi: false,
  forced: false,
  path: "/movies/test.da.srt",
};

const hungarianFile: Subtitle = {
  code2: "hu",
  name: "Hungarian",
  hi: false,
  forced: false,
  path: "/movies/test.hu.srt",
};

function mountTable(initial: Subtitle[]) {
  let setSubtitles!: (subtitles: Subtitle[]) => void;
  function Harness() {
    const [subtitles, set] = useState<Subtitle[]>(initial);
    useEffect(() => {
      setSubtitles = set;
    }, [set]);
    // A new movie object on every change, as a refetch delivers it.
    return (
      <Table movie={makeMovie(subtitles)} profile={undefined} history={[]} />
    );
  }
  const router = createMemoryRouter([{ path: "/", element: <Harness /> }]);
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return (next: Subtitle[]) => act(async () => setSubtitles(next));
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

describe("Movies Detail Table, an open row menu", () => {
  it("stays open while rows before it are removed and added", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );
    const user = userEvent.setup();
    const setSubtitles = mountTable([englishTrack, hungarianFile]);

    await screen.findByText("test.hu.srt");
    // Let the language lookups settle, so only the list changes below
    // re-render the table.
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    const row = rowOf(screen.getByText("test.hu.srt"));
    const contents = cellContents(row);
    expect(contents.filter(Boolean).length).toBeGreaterThan(0);

    await user.click(
      within(row).getByRole("button", { name: "Subtitle Actions" }),
    );
    // jsdom has no layout, so Mantine keeps the dropdown display:none. Its
    // node is what shows the menu is still the one the user opened.
    expect(await screen.findByText("Translate...")).toBeInTheDocument();
    const menu = screen.getByRole("menu", { hidden: true });

    // A hide or a delete removes the row that sorts before it.
    await setSubtitles([hungarianFile]);
    expect(
      screen.queryByText("Video File Subtitle Track"),
    ).not.toBeInTheDocument();
    expect(rowOf(screen.getByText("test.hu.srt"))).toBe(row);
    expect(screen.getByRole("menu", { hidden: true })).toBe(menu);
    expectSameNodes(contents, cellContents(row));

    // A download or an unhide adds one before it again.
    await setSubtitles([englishTrack, danishFile, hungarianFile]);
    expect(screen.getByText("test.da.srt")).toBeInTheDocument();
    expect(rowOf(screen.getByText("test.hu.srt"))).toBe(row);
    expect(screen.getByRole("menu", { hidden: true })).toBe(menu);
    expectSameNodes(contents, cellContents(row));
  });
});
