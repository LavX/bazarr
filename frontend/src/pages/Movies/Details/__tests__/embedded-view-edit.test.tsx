/* eslint-disable camelcase */

/**
 * View, Edit and Download on a movie's embedded track read the track itself
 * (?source=embedded), never the file that shares its language key, and an
 * image-based track offers none of them. The row's codec travels with the
 * source: two text tracks of one key are two rows.
 */

import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import Table from "@/pages/Movies/Details/table";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import * as files from "@/utilities/files";

function movieWith(subtitles: Subtitle[]): Item.Movie {
  return {
    id: 12,
    radarrId: 7,
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
  };
}

const embedded = (codec: string | null): Subtitle => ({
  code2: "en",
  name: "English",
  hi: false,
  forced: false,
  path: null,
  codec,
});

const file: Subtitle = {
  code2: "en",
  name: "English",
  hi: false,
  forced: false,
  path: "/movies/test.en.srt",
  codec: null,
};

// The table on "/", and anything under /subtitles standing in for the editor,
// so a test reads where a click went without leaving jsdom on that URL.
function mountTable(movie: Item.Movie) {
  const router = createMemoryRouter(
    [
      { path: "/", element: <Table movie={movie} history={[]} /> },
      { path: "/subtitles/*", element: <div>editor</div> },
    ],
    { initialEntries: ["/"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return () =>
    `${router.state.location.pathname}${router.state.location.search}`;
}

function mockCommon() {
  server.use(
    http.get("/api/subtitles/sync-status", () =>
      HttpResponse.json({ data: null }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
  );
}

// Mantine hides a floating dropdown once jsdom reports its target has no
// size, so the rows are found with hidden: true, as the other menu tests do.
const item = (name: string) =>
  screen.findByRole("menuitem", { name, hidden: true });

async function openRowMenu(
  user: ReturnType<typeof userEvent.setup>,
  text: string,
) {
  const row = await screen.findByRole("row", { name: new RegExp(text) });
  await user.click(within(row).getByLabelText("Subtitle Actions"));
}

describe("an embedded movie track", () => {
  it.each([
    ["View", "/subtitles/preview/movie/7/en"],
    ["Edit", "/subtitles/edit/movie/7/en"],
  ])("opens %s on the track itself", async (action, path) => {
    mockCommon();
    const user = userEvent.setup();
    const at = mountTable(movieWith([file, embedded("subrip")]));

    await openRowMenu(user, "Video File Subtitle Track");
    await user.click(await item(action));

    await waitFor(() =>
      expect(at()).toBe(
        `${path}?arr_instance_id=2&source=embedded&codec=subrip`,
      ),
    );
  });

  it("opens the file on the file's own row", async () => {
    mockCommon();
    const user = userEvent.setup();
    const at = mountTable(movieWith([file, embedded("subrip")]));

    await openRowMenu(user, "test.en.srt");
    await user.click(await item("Edit"));

    await waitFor(() =>
      expect(at()).toBe("/subtitles/edit/movie/7/en?arr_instance_id=2"),
    );
  });

  it("downloads the track under the name Save would give it", async () => {
    mockCommon();
    const asked: (string | null)[] = [];
    const codecs: (string | null)[] = [];
    server.use(
      http.get("/api/movies/7/subtitles/en/download", ({ request }) => {
        const url = new URL(request.url);
        asked.push(url.searchParams.get("source"));
        codecs.push(url.searchParams.get("codec"));
        return new HttpResponse("1\n00:00:01,000 --> 00:00:02,000\nHi\n", {
          headers: {
            "Content-Type": "application/x-subrip",
            "Content-Disposition": "attachment; filename=test.en.srt",
          },
        });
      }),
    );
    const save = vi
      .spyOn(files, "saveBlobAs")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    mountTable(movieWith([embedded(null)]));

    await openRowMenu(user, "Video File Subtitle Track");
    await user.click(await item("Download"));

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(asked).toEqual(["embedded"]);
    // An unknown codec sends the source alone: the read takes the first
    // text track, which is all the row can name.
    expect(codecs).toEqual([null]);
    expect(save.mock.calls[0][1]).toBe("test.en.srt");
    save.mockRestore();
  });

  it("offers none of them on an image-based track", async () => {
    mockCommon();
    const user = userEvent.setup();
    mountTable(movieWith([embedded("pgs")]));

    await openRowMenu(user, "Video File Subtitle Track");

    for (const name of ["View", "Edit", "Download"]) {
      expect(await item(name)).toBeDisabled();
    }
  });
});
