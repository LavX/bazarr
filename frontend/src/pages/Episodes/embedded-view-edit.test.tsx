/**
 * View, Edit and Download on an episode's embedded badge read the track
 * itself (?source=embedded), and an image-based track offers none of them.
 * The row's codec travels with the source: two text tracks of one key are
 * two rows.
 */

import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import * as files from "@/utilities/files";
import { Subtitle } from "./components";

const track = (codec: string | null): Subtitle => ({
  code2: "en",
  name: "English",
  hi: true,
  forced: false,
  path: null,
  codec,
});

function mountBadge(subtitle: Subtitle) {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: (
          <Subtitle
            seriesId={5}
            episodeId={51}
            localEpisodeId={2}
            arrInstanceId={1}
            subtitle={subtitle}
            availableSubtitles={[subtitle]}
          />
        ),
      },
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

// Mantine hides a floating dropdown once jsdom reports its target has no
// size, so the rows are found with hidden: true, as the other menu tests do.
const item = (name: string) =>
  screen.findByRole("menuitem", { name, hidden: true });

describe("an embedded episode track", () => {
  it.each([
    ["View", "/subtitles/preview/episode/51/en%3Ahi"],
    ["Edit", "/subtitles/edit/episode/51/en%3Ahi"],
  ])("opens %s on the track itself", async (action, path) => {
    const user = userEvent.setup();
    const at = mountBadge(track("subrip"));

    await user.click(await screen.findByTitle(/^Embedded in the video file/));
    await user.click(await item(action));

    await waitFor(() =>
      expect(at()).toBe(
        `${path}?arr_instance_id=1&source=embedded&codec=subrip`,
      ),
    );
  });

  it("downloads the track, not a file", async () => {
    const asked: (string | null)[] = [];
    const codecs: (string | null)[] = [];
    server.use(
      http.get(
        "/api/episodes/51/subtitles/:language/download",
        ({ request }) => {
          const url = new URL(request.url);
          asked.push(url.searchParams.get("source"));
          codecs.push(url.searchParams.get("codec"));
          return new HttpResponse("1\n00:00:01,000 --> 00:00:02,000\nHi\n", {
            headers: {
              "Content-Type": "application/x-subrip",
              "Content-Disposition":
                "attachment; filename=Show.S01E01.en.hi.srt",
            },
          });
        },
      ),
    );
    const save = vi
      .spyOn(files, "saveBlobAs")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    mountBadge(track("subrip"));

    await user.click(await screen.findByTitle(/^Embedded in the video file/));
    await user.click(await item("Download"));

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(asked).toEqual(["embedded"]);
    expect(codecs).toEqual(["subrip"]);
    expect(save.mock.calls[0][1]).toBe("Show.S01E01.en.hi.srt");
    save.mockRestore();
  });

  it("offers none of them on an image-based track", async () => {
    const user = userEvent.setup();
    mountBadge(track("vobsub"));

    await user.click(await screen.findByTitle(/^Embedded in the video file/));

    for (const name of ["View", "Edit", "Download"]) {
      expect(await item(name)).toBeDisabled();
    }
  });
});
