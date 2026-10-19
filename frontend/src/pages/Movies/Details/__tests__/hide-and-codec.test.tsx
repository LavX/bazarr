/* eslint-disable camelcase */

/**
 * Hide track and the codec badge on one movie page. Tracks are told apart by
 * language, HI and forced only, so an SRT and a PGS English track on one
 * remux are two rows with their own badges but a single Hide.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import Table from "@/pages/Movies/Details/table";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

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

const track = (code2: string, name: string, codec: string): Subtitle => ({
  code2,
  name,
  hi: false,
  forced: false,
  path: null,
  codec,
});

function mockCommon(hidden: unknown[] = []) {
  const posts: Record<string, string>[] = [];
  server.use(
    http.get("/api/subtitles/sync-status", () =>
      HttpResponse.json({ data: null }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/subtitles/embedded-hidden", () =>
      HttpResponse.json({ data: hidden }),
    ),
    http.post("/api/subtitles/embedded-hidden", async ({ request }) => {
      const form = Object.fromEntries(
        (await request.formData()).entries(),
      ) as Record<string, string>;
      posts.push(form);
      return HttpResponse.json({
        data: {
          wanted: true,
          hidden: { code2: "en", name: "English", hi: false, forced: false },
          covered_by: [],
        },
      });
    }),
  );
  return posts;
}

describe("Movies Detail Table, Hide track with codecs", () => {
  it("keeps the badges of the tracks left and names the hidden codecs", async () => {
    mockCommon([
      {
        media_type: "movie",
        media_id: 12,
        arr_instance_id: 2,
        language: "en",
        code2: "en",
        name: "English",
        hi: false,
        forced: false,
        codec: "pgs, subrip",
        hidden_at: "2026-09-28T10:00:00",
      },
    ]);
    const user = userEvent.setup();
    customRender(
      <Table movie={movieWith([track("fr", "French", "pgs")])} history={[]} />,
    );

    expect(
      await screen.findByTitle("Subtitle format (PGS, image-based)"),
    ).toHaveTextContent("PGS");
    const disclosure = await screen.findByRole("button", {
      name: "Hidden tracks (1)",
    });
    await user.click(disclosure);
    expect(
      await screen.findByRole("button", { name: "Unhide English" }),
    ).toBeInTheDocument();
    // The hidden list names the codecs as the row badges do, not as stored.
    // jsdom lays nothing out, so once the positioning code has run it counts
    // the list as off screen and hides it from the accessibility tree.
    const dialog = screen.getByRole("dialog", { hidden: true });
    const list = within(dialog);
    expect(
      list.getByTitle("Subtitle format (PGS, image-based)"),
    ).toHaveTextContent(/^PGS$/);
    expect(list.getByTitle("Subtitle format (SRT)")).toHaveTextContent(/^SRT$/);
    expect(screen.queryByText("pgs, subrip")).not.toBeInTheDocument();
    // Under the table the trigger keeps its own width at the end of its line,
    // so the list opens from the words the user clicked.
    expect(disclosure.parentElement?.getAttribute("style")).toContain(
      "--group-justify: flex-end",
    );
    expect(dialog).toHaveAttribute("data-position", "bottom-end");
  });

  it("hides both same-language tracks from the PGS row with one request", async () => {
    const posts = mockCommon();
    const user = userEvent.setup();
    customRender(
      <Table
        movie={movieWith([
          track("en", "English", "subrip"),
          track("en", "English", "pgs"),
        ])}
        history={[]}
      />,
    );
    // Let the page's queries settle first, so the click lands on the menu
    // the final render shows.
    await screen.findByText("PGS");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    await user.click(
      within(screen.getByRole("row", { name: /PGS/ })).getByLabelText(
        "Subtitle Actions",
      ),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Hide track..." }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Hide this embedded track?" }),
    );
    expect(dialog.getAllByText("English (embedded track)")).toHaveLength(2);
    await user.click(dialog.getByRole("button", { name: "Hide track" }));

    await waitFor(() =>
      expect(posts).toEqual([
        {
          media_type: "movie",
          media_id: "12",
          arr_instance_id: "2",
          language: "en",
          hi: "False",
          forced: "False",
        },
      ]),
    );
  });
});
