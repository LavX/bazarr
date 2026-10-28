/* eslint-disable camelcase */
import { useEffect, useState } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import Table from "@/pages/Movies/Details/table";
import { AllProviders } from "@/providers";
import { act, customRender, rawRender, screen } from "@/tests";
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

const embedded = (code2: string, codec?: string | null): Subtitle => ({
  code2,
  name: code2,
  hi: false,
  forced: false,
  path: null,
  codec,
});

describe("Movies Detail Table, embedded track codec", () => {
  it("names the codec next to the track and marks bitmap tracks", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );

    customRender(
      <Table
        movie={makeMovie([embedded("en", "pgs"), embedded("fr", "subrip")])}
        profile={undefined}
        history={[]}
      />,
    );

    const pgs = await screen.findByText("PGS");
    expect(pgs.closest("[title]")).toHaveAttribute(
      "title",
      "Subtitle format (PGS, image-based)",
    );
    expect(screen.getByText("SRT").closest("[title]")).toHaveAttribute(
      "title",
      "Subtitle format (SRT)",
    );
    expect(screen.getAllByText("Video File Subtitle Track")).toHaveLength(2);
  });

  it("shows no codec for a track indexed before the codec was stored", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );

    customRender(
      <Table
        movie={makeMovie([embedded("en"), embedded("de", null)])}
        profile={undefined}
        history={[]}
      />,
    );

    expect(
      await screen.findAllByText("Video File Subtitle Track"),
    ).toHaveLength(2);
    expect(screen.queryByTitle(/^Subtitle format/)).not.toBeInTheDocument();
  });

  it("keeps two same-language tracks with different codecs apart", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );
    const consoleError = vi.spyOn(console, "error");

    customRender(
      <Table
        movie={makeMovie([embedded("en", "subrip"), embedded("en", "pgs")])}
        profile={undefined}
        history={[]}
      />,
    );

    expect(
      await screen.findAllByText("Video File Subtitle Track"),
    ).toHaveLength(2);
    expect(screen.getAllByText("SRT")).toHaveLength(1);
    expect(screen.getAllByText("PGS")).toHaveLength(1);
    expect(
      consoleError.mock.calls.filter((call) =>
        String(call[0]).includes("same key"),
      ),
    ).toEqual([]);
    consoleError.mockRestore();
  });

  it("keeps a track's row mounted when a row is added before it", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );
    const track = embedded("en", "pgs");
    const danish: Subtitle = {
      code2: "da",
      name: "Danish",
      hi: false,
      forced: false,
      path: "/movies/test.da.srt",
    };
    let setSubtitles!: (subtitles: Subtitle[]) => void;
    function Harness() {
      const [subtitles, set] = useState<Subtitle[]>([track]);
      useEffect(() => {
        setSubtitles = set;
      }, [set]);
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

    const before = (await screen.findByText("PGS")).closest("tr");
    await act(async () => setSubtitles([danish, track]));
    await screen.findByText("test.da.srt");
    expect(screen.getByText("PGS").closest("tr")).toBe(before);
  });

  it("shows the track's own title when the file carries one", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );
    const titled: Subtitle = {
      ...embedded("en", "pgs"),
      name: "English",
      hi: true,
      title: "English SDH",
    };

    customRender(
      <Table movie={makeMovie([titled])} profile={undefined} history={[]} />,
    );

    expect(await screen.findByText("SDH")).toBeInTheDocument();
  });

  it("does not strip a title that only starts with the same letters", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );
    const titled: Subtitle = {
      ...embedded("en", "pgs"),
      name: "English",
      title: "Englishman [日本語]",
    };

    customRender(
      <Table movie={makeMovie([titled])} profile={undefined} history={[]} />,
    );

    expect(await screen.findByText("Englishman [日本語]")).toBeInTheDocument();
  });

  it("caps a long title and keeps the full text in the tooltip", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );
    const long = "A".repeat(60);
    const titled: Subtitle = {
      ...embedded("en", "pgs"),
      name: "English",
      title: long,
    };

    customRender(
      <Table movie={makeMovie([titled])} profile={undefined} history={[]} />,
    );

    const badge = await screen.findByTitle(long);
    expect(Array.from(badge.textContent ?? "")).toHaveLength(41);
  });

  it("does not crash when the language name is empty", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );
    const titled: Subtitle = {
      ...embedded("en", "pgs"),
      name: "",
      title: "Signs and Songs",
    };

    customRender(
      <Table movie={makeMovie([titled])} profile={undefined} history={[]} />,
    );

    expect(await screen.findByText("Signs and Songs")).toBeInTheDocument();
  });

  it("shows a metadata placeholder without hiding the row", async () => {
    server.use(
      http.get("/api/subtitles/sync-status", () =>
        HttpResponse.json({ data: null }),
      ),
      http.get("/api/system/languages", () => HttpResponse.json([])),
    );

    customRender(
      <Table
        movie={makeMovie([
          { ...embedded("en", null), codec_pending: true },
          embedded("de", null),
        ])}
        profile={undefined}
        history={[]}
        metadataPending
      />,
    );

    // Only the row stored before codecs existed is being read; the other row's
    // format is unknown for good and a scan would not change that.
    expect(
      await screen.findByText("Reading track metadata…"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Reading track metadata…")).toHaveLength(1);
    expect(screen.getAllByText("Video File Subtitle Track")).toHaveLength(2);
  });
});
