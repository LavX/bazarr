/* eslint-disable camelcase */

/**
 * The movie table's row menu cell is a module-level component that runs the
 * Hide itself. A hide that takes out a row sorting before an open menu leaves
 * that menu open while the hidden-tracks disclosure below the table updates,
 * and a Hide from the cell still records the movie's local id.
 */

import { useState } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import type { EmbeddedHiddenTrack } from "@/apis/raw/subtitles";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import Table from "@/pages/Movies/Details/table";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

function makeMovie(subtitles: Subtitle[]): Item.Movie {
  return {
    id: 9,
    radarrId: 1,
    arr_instance_id: 2,
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
  } as Item.Movie;
}

const englishTrack: Subtitle = {
  code2: "en",
  name: "English",
  hi: false,
  forced: false,
  path: null,
  codec: "subrip",
};

const hungarianFile: Subtitle = {
  code2: "hu",
  name: "Hungarian",
  hi: false,
  forced: false,
  path: "/movies/test.hu.srt",
};

const hiddenEnglish: EmbeddedHiddenTrack = {
  media_type: "movie",
  media_id: 9,
  arr_instance_id: 2,
  language: "en",
  code2: "en",
  name: "English",
  hi: false,
  forced: false,
  codec: "subrip",
  hidden_at: null,
};

function serve(hidden: EmbeddedHiddenTrack[]) {
  const posted: Record<string, string>[] = [];
  server.use(
    http.get("/api/subtitles/sync-status", () =>
      HttpResponse.json({ data: null }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/subtitles/embedded-hidden", () =>
      HttpResponse.json({ data: [...hidden] }),
    ),
    http.post("/api/subtitles/embedded-hidden", async ({ request }) => {
      posted.push(
        Object.fromEntries((await request.formData()).entries()) as Record<
          string,
          string
        >,
      );
      return HttpResponse.json({
        data: {
          wanted: true,
          hidden: { code2: "en", name: "English", hi: false, forced: false },
          covered_by: [],
        },
      });
    }),
  );
  return posted;
}

function mountTable(initial: Subtitle[]) {
  let setSubtitles!: (subtitles: Subtitle[]) => void;
  function Harness() {
    const [subtitles, set] = useState<Subtitle[]>(initial);
    setSubtitles = set;
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

// Runs the app's own socket handler, as a hide in another tab reaches this one.
function receiveMovieUpdate() {
  const reducer = createDefaultReducer().find((item) => item.key === "movie");
  reducer?.any?.();
  (reducer?.update as ((ids: number[]) => void) | undefined)?.([1]);
}

function rowOf(element: HTMLElement) {
  return element.closest("tr")!;
}

function cellContents(row: HTMLElement) {
  return Array.from(row.querySelectorAll(":scope > td")).map(
    (td) => td.firstChild,
  );
}

describe("Movies Detail Table, Hide from the row menu cell", () => {
  it("records the movie's local id and owner", async () => {
    const posted = serve([]);
    const user = userEvent.setup();
    mountTable([englishTrack, hungarianFile]);

    await screen.findByText("Video File Subtitle Track");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    const row = rowOf(screen.getByText("Video File Subtitle Track"));
    await user.click(
      within(row).getByRole("button", { name: "Subtitle Actions" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Hide track..." }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Hide this embedded track?" }),
    );
    await user.click(dialog.getByRole("button", { name: "Hide track" }));

    await waitFor(() =>
      expect(posted).toEqual([
        {
          media_type: "movie",
          media_id: "9",
          arr_instance_id: "2",
          language: "en",
          hi: "False",
          forced: "False",
        },
      ]),
    );
  });

  it("keeps another row's open menu while a hide takes out a row before it", async () => {
    const hidden: EmbeddedHiddenTrack[] = [];
    serve(hidden);
    const user = userEvent.setup();
    const setSubtitles = mountTable([englishTrack, hungarianFile]);

    await screen.findByText("test.hu.srt");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    const row = rowOf(screen.getByText("test.hu.srt"));
    const contents = cellContents(row);

    await user.click(
      within(row).getByRole("button", { name: "Subtitle Actions" }),
    );
    expect(await screen.findByText("Translate...")).toBeInTheDocument();
    const menu = screen.getByRole("menu", { hidden: true });

    // English is hidden elsewhere: the socket event refreshes the hidden
    // list, and the refetched movie no longer lists the track.
    hidden.push(hiddenEnglish);
    await setSubtitles([hungarianFile]);
    await act(async () => receiveMovieUpdate());

    expect(
      await screen.findByRole("button", { name: "Hidden tracks (1)" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Video File Subtitle Track"),
    ).not.toBeInTheDocument();
    expect(rowOf(screen.getByText("test.hu.srt"))).toBe(row);
    expect(screen.getByRole("menu", { hidden: true })).toBe(menu);
    const after = cellContents(row);
    expect(after).toHaveLength(contents.length);
    contents.forEach((node, index) => expect(after[index]).toBe(node));
  });
});
