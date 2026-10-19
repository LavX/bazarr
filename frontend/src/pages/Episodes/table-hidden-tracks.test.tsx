/* eslint-disable camelcase */

/**
 * Hidden tracks on the episodes table, whose cells read the page's episode ids
 * and the local episode id from the table context. One request covers every
 * episode, a Hide from a row records that episode's local id, and a hide on
 * another episode leaves an open badge menu where it is.
 */

import { useRef, useState } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { Table as TableInstance } from "@tanstack/react-table";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import type { EmbeddedHiddenTrack } from "@/apis/raw/subtitles";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import Table from "./table";

const frenchTrack: Subtitle = {
  code2: "fr",
  name: "French",
  hi: false,
  forced: false,
  path: null,
};

function makeEpisode(episode: number, embedded = true): Item.Episode {
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
      ...(embedded ? [frenchTrack] : []),
    ],
    missing_subtitles: [],
  } as unknown as Item.Episode;
}

function hiddenFrench(mediaId: number): EmbeddedHiddenTrack {
  return {
    media_type: "episode",
    media_id: mediaId,
    arr_instance_id: 1,
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
      expect(url.searchParams.get("media_type")).toBe("episode");
      asked.push(url.searchParams.getAll("media_id[]").map(Number));
      return HttpResponse.json({ data: [...hidden] });
    }),
  );
  return asked;
}

function mountTable(initial: Item.Episode[]) {
  let setEpisodes!: (episodes: Item.Episode[]) => void;
  function Harness() {
    const [episodes, set] = useState<Item.Episode[]>(initial);
    setEpisodes = set;
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

// Runs the app's own socket handler, as a hide in another tab reaches this one.
function receiveSeriesUpdate() {
  const reducer = createDefaultReducer().find((item) => item.key === "series");
  reducer?.any?.();
  (reducer?.update as ((ids: number[]) => void) | undefined)?.([7]);
}

function rowOf(element: HTMLElement) {
  return element.closest("tr")!;
}

function rowOfEpisode(title: string) {
  return rowOf(screen.getByText(title));
}

function cellContents(row: HTMLElement) {
  return Array.from(row.querySelectorAll(":scope > td")).map(
    (td) => td.firstChild,
  );
}

describe("Episodes Table, hidden tracks", () => {
  it("asks once for every episode and shows each track on its own row", async () => {
    const asked = serveHidden([hiddenFrench(12)]);
    mountTable([makeEpisode(2, false), makeEpisode(1)]);

    const disclosure = await screen.findByRole("button", {
      name: "Hidden tracks (1)",
    });
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(asked).toEqual([[11, 12]]);
    expect(rowOf(disclosure)).toBe(rowOfEpisode("Episode 2"));
    expect(
      within(rowOfEpisode("Episode 1")).queryByRole("button", {
        name: /Hidden tracks/,
      }),
    ).not.toBeInTheDocument();
  });

  it("hides a row's track by that episode's local id", async () => {
    serveHidden([]);
    const posted: Record<string, string>[] = [];
    server.use(
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
            hidden: { code2: "fr", name: "French", hi: false, forced: false },
            covered_by: [],
          },
        });
      }),
    );
    const user = userEvent.setup();
    mountTable([makeEpisode(2), makeEpisode(1)]);

    await screen.findByText("Episode 1");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await user.click(
      within(rowOfEpisode("Episode 1")).getByTitle(
        "Embedded in the video file",
      ),
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
          media_type: "episode",
          media_id: "11",
          arr_instance_id: "1",
          language: "fr",
          hi: "False",
          forced: "False",
        },
      ]),
    );
  });

  it("keeps an open badge menu while a hide on another episode changes its row", async () => {
    const hidden: EmbeddedHiddenTrack[] = [];
    serveHidden(hidden);
    const user = userEvent.setup();
    const setEpisodes = mountTable([makeEpisode(2), makeEpisode(1)]);

    await screen.findByTitle("File: s01e01.en.srt");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    const row = rowOfEpisode("Episode 1");
    const contents = cellContents(row);

    await user.click(screen.getByTitle("File: s01e01.en.srt"));
    expect(await screen.findByText("Translate...")).toBeInTheDocument();
    const menu = screen.getByRole("menu", { hidden: true });

    // Episode 2's French track is hidden elsewhere: the socket event
    // refreshes the hidden list, and the refetched list drops the track.
    hidden.push(hiddenFrench(12));
    await setEpisodes([makeEpisode(2, false), makeEpisode(1)]);
    await act(async () => receiveSeriesUpdate());

    const disclosure = await screen.findByRole("button", {
      name: "Hidden tracks (1)",
    });
    expect(rowOf(disclosure)).toBe(rowOfEpisode("Episode 2"));
    expect(rowOfEpisode("Episode 1")).toBe(row);
    expect(screen.getByRole("menu", { hidden: true })).toBe(menu);
    const after = cellContents(row);
    expect(after).toHaveLength(contents.length);
    contents.forEach((node, index) => expect(after[index]).toBe(node));
  });
});
