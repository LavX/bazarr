/* eslint-disable camelcase */
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { customRender, screen, within } from "@/tests";
import server from "@/tests/mocks/node";
import Table, { buildMovieSubtitleToolSelections } from "./table";

describe("buildMovieSubtitleToolSelections", () => {
  it("carries arr_instance_id for existing subtitle tool actions", () => {
    const selections = buildMovieSubtitleToolSelections(
      {
        radarrId: 50,
        arr_instance_id: 7,
      } as Item.Movie,
      {
        code2: "en",
        path: "/movies/movie.en.srt",
        forced: false,
        hi: false,
      } as Subtitle,
    );

    expect(selections).toEqual([
      {
        id: 50,
        type: "movie",
        path: "/movies/movie.en.srt",
        language: "en",
        forced: "False",
        hi: "False",
        from_language: undefined,
        arr_instance_id: 7,
      },
    ]);
  });

  it("carries arr_instance_id for embedded movie track translation", () => {
    const selections = buildMovieSubtitleToolSelections(
      {
        radarrId: 50,
        arr_instance_id: 7,
      } as Item.Movie,
      {
        code2: "ja",
        path: null,
        forced: false,
        hi: true,
      } as Subtitle,
    );

    expect(selections[0]).toMatchObject({
      id: 50,
      type: "movie",
      path: "",
      language: "ja",
      from_language: "ja",
      arr_instance_id: 7,
    });
  });
});

describe("score and provider of a subtitle file", () => {
  const english: Subtitle = {
    code2: "en",
    name: "English",
    hi: false,
    forced: false,
    path: "/movies/Test Movie/Test Movie.en.srt",
  };

  const movie = {
    id: 100,
    radarrId: 42,
    title: "Test Movie",
    path: "/movies/Test Movie/Test Movie.mkv",
    profileId: null,
    fanart: "",
    overview: "",
    imdbId: "tt0000001",
    alternativeTitles: [],
    poster: "",
    year: "2026",
    monitored: true,
    tags: [],
    audio_language: [],
    subtitles: [english],
    missing_subtitles: [],
  } as Item.Movie;

  // The seconds follow history_id, so a higher id is also the newer record.
  function record(
    action: number,
    historyId: number,
    overrides: Partial<History.Movie> = {},
  ): History.Movie {
    return {
      id: movie.id,
      radarrId: movie.radarrId,
      title: movie.title,
      action,
      history_id: historyId,
      timestamp_iso: `2026-09-13T12:00:${String(historyId).padStart(2, "0")}`,
      timestamp: "a moment ago",
      parsed_timestamp: "13.09.2026 12:00:00",
      description: "Subtitle event",
      blacklisted: false,
      upgradable: false,
      monitored: true,
      matches: [],
      dont_matches: [],
      tags: [],
      subtitles_path: english.path!,
      language: english,
      ...overrides,
    };
  }

  const download = record(1, 1, { score: "81.0%", provider: "opensubtitles" });

  async function rowFor(history: History.Movie[]) {
    customRender(<Table movie={movie} history={history} />);
    return screen.findByRole("row", { name: /Test Movie\.en\.srt/ });
  }

  beforeEach(() => {
    server.use(
      http.get("/api/movies/:id/subtitles/:language/sync-status", () =>
        HttpResponse.json({
          synced: true,
          confirmed: true,
          editedAfterSync: false,
          lastModified: 0,
          lastSyncTimestamp: "2026-09-13T12:00:02",
        }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );
  });

  it.each([
    ["an upload", 4],
    ["a translation", 6],
  ])(
    "shows no score or provider once %s replaced the download",
    async (_, action) => {
      const row = await rowFor([
        record(action, 2, { score: undefined, provider: "gemini" }),
        download,
      ]);

      expect(within(row).queryByText("81.0%")).not.toBeInTheDocument();
      expect(within(row).queryByText("opensubtitles")).not.toBeInTheDocument();
      expect(within(row).queryByText("gemini")).not.toBeInTheDocument();
    },
  );

  // Syncing in the default overwrite mode writes back to the same path and
  // logs a record with no score or provider. The file still came from the
  // download, so its score and provider stay.
  it("download then sync still shows score and provider", async () => {
    const row = await rowFor([
      record(5, 2, { score: undefined, provider: undefined }),
      download,
    ]);

    expect(within(row).getByText("81.0%")).toBeInTheDocument();
    expect(within(row).getByText("opensubtitles")).toBeInTheDocument();
  });

  it("shows a later download's score after an upload", async () => {
    const row = await rowFor([
      record(2, 3, { score: "92.0%", provider: "podnapisi" }),
      record(4, 2, { score: undefined, provider: undefined }),
      download,
    ]);

    expect(within(row).getByText("92.0%")).toBeInTheDocument();
    expect(within(row).getByText("podnapisi")).toBeInTheDocument();
  });

  // The order of the list is not the order of the events: the newest record
  // is found by its timestamp, then its history id.
  it.each([
    [
      "oldest",
      [download, record(3, 2, { score: "95.0%", provider: "addic7ed" })],
    ],
    [
      "newest",
      [record(3, 2, { score: "95.0%", provider: "addic7ed" }), download],
    ],
  ])(
    "takes the newest upgrade when the history lists the %s record first",
    async (_, history) => {
      const row = await rowFor(history);

      expect(within(row).getByText("95.0%")).toBeInTheDocument();
      expect(within(row).getByText("addic7ed")).toBeInTheDocument();
      expect(within(row).queryByText("81.0%")).not.toBeInTheDocument();
    },
  );

  it("breaks a timestamp tie by history id", async () => {
    const row = await rowFor([
      download,
      record(4, 2, {
        score: undefined,
        provider: undefined,
        timestamp_iso: download.timestamp_iso,
      }),
    ]);

    expect(within(row).queryByText("81.0%")).not.toBeInTheDocument();
    expect(within(row).queryByText("opensubtitles")).not.toBeInTheDocument();
  });
});
