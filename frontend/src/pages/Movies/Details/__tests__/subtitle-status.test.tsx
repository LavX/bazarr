/* eslint-disable camelcase */

import { ReactElement } from "react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useMovieHistory } from "@/apis/hooks";
import queryClient from "@/apis/queries";
import Table from "@/pages/Movies/Details/table";
import { cleanup, customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

const english: Subtitle = {
  code2: "en",
  name: "English",
  hi: false,
  forced: false,
  path: "/movies/test.en.srt",
};

const movie: Item.Movie = {
  id: 100,
  radarrId: 42,
  arr_instance_id: 2,
  title: "Test Movie",
  path: "/movies/test.mkv",
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
};

type HistoryEvent = History.Movie & {
  history_id: number;
  timestamp_iso: string;
};

function event(
  action: number,
  historyId: number,
  overrides: Partial<HistoryEvent> = {},
): HistoryEvent {
  return {
    id: movie.id,
    radarrId: movie.radarrId,
    arr_instance_id: movie.arr_instance_id,
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

async function renderTable(ui: ReactElement) {
  customRender(ui);
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
}

afterEach(cleanup);

beforeEach(() => {
  server.use(
    http.get("/api/movies/:id/subtitles/:language/sync-status", () =>
      HttpResponse.json({
        synced: true,
        confirmed: true,
        editedAfterSync: false,
        lastModified: 0,
        lastSyncTimestamp: "2026-09-13T12:00:03",
      }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
  );
});

describe("current subtitle status", () => {
  it.each([
    ["automatic download", 1],
    ["manual download", 2],
    ["upgrade", 3],
    ["upload", 4],
    ["deletion", 0],
  ])("clears old translation and sync after %s", async (_, action) => {
    await renderTable(
      <Table
        movie={movie}
        history={[event(6, 1), event(5, 2), event(action, 3)]}
      />,
    );

    expect(screen.queryByLabelText("Translated")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Sync")).not.toBeInTheDocument();
  });

  it("does not restore translation after deletion and redownload", async () => {
    await renderTable(
      <Table movie={movie} history={[event(2, 3), event(0, 2), event(6, 1)]} />,
    );

    expect(screen.queryByLabelText("Translated")).not.toBeInTheDocument();
  });

  it("preserves translation when the current file is synchronized", async () => {
    const history = [event(5, 3), event(6, 2), event(2, 1)];
    const original = structuredClone(history);
    await renderTable(<Table movie={movie} history={history} />);

    expect(screen.getByLabelText("Translated")).toBeInTheDocument();
    expect(screen.getByLabelText("Sync")).toBeInTheDocument();
    expect(history).toEqual(original);
  });

  it("shows a new translation after replacement without retaining the old sync", async () => {
    await renderTable(
      <Table movie={movie} history={[event(5, 1), event(2, 2), event(6, 3)]} />,
    );

    expect(screen.getByLabelText("Translated")).toBeInTheDocument();
    expect(screen.queryByLabelText("Sync")).not.toBeInTheDocument();
  });

  it("clears prior sync when the same file is translated again", async () => {
    await renderTable(
      <Table movie={movie} history={[event(6, 3), event(5, 2), event(6, 1)]} />,
    );

    expect(screen.getByLabelText("Translated")).toBeInTheDocument();
    expect(screen.queryByLabelText("Sync")).not.toBeInTheDocument();
  });

  it("uses server order when older history responses lack ordering metadata", async () => {
    const history = [event(2, 2), event(6, 1)].map((record) => ({
      ...record,
      history_id: undefined,
      timestamp_iso: undefined,
    }));
    await renderTable(<Table movie={movie} history={history} />);

    expect(screen.queryByLabelText("Translated")).not.toBeInTheDocument();
  });

  it("uses timestamps before event IDs and breaks timestamp ties by event ID", async () => {
    await renderTable(
      <Table
        movie={movie}
        history={[
          event(6, 99, { timestamp_iso: "2026-09-12T12:00:00" }),
          event(6, 1, { timestamp_iso: "2026-09-13T12:00:00.000001" }),
          event(2, 2, { timestamp_iso: "2026-09-13T12:00:00.000001" }),
        ]}
      />,
    );

    expect(screen.queryByLabelText("Translated")).not.toBeInTheDocument();
  });

  it("restores translation when it follows replacement at the same timestamp", async () => {
    await renderTable(
      <Table
        movie={movie}
        history={[
          event(2, 1),
          event(6, 2, { timestamp_iso: "2026-09-13T12:00:01" }),
        ]}
      />,
    );

    expect(screen.getByLabelText("Translated")).toBeInTheDocument();
  });

  it("retains microsecond event ordering", async () => {
    await renderTable(
      <Table
        movie={movie}
        history={[
          event(6, 2, { timestamp_iso: "2026-09-13T12:00:00.000001" }),
          event(2, 1, { timestamp_iso: "2026-09-13T12:00:00.000002" }),
        ]}
      />,
    );

    expect(screen.queryByLabelText("Translated")).not.toBeInTheDocument();
  });

  it("keeps a different subtitle path and language independent", async () => {
    const french = {
      ...english,
      code2: "fr",
      name: "French",
      path: "/movies/test.fr.srt",
    };
    await renderTable(
      <Table
        movie={{ ...movie, subtitles: [english, french] }}
        history={[
          event(2, 3),
          event(6, 2, { subtitles_path: french.path, language: french }),
          event(6, 1),
        ]}
      />,
    );

    const rows = screen.getAllByRole("row");
    expect(
      within(rows[1]).queryByLabelText("Translated"),
    ).not.toBeInTheDocument();
    expect(within(rows[2]).getByLabelText("Translated")).toBeInTheDocument();
  });

  it("matches hi and forced tracks to the history language's hi priority", async () => {
    const subtitle = { ...english, hi: true, forced: true };
    await renderTable(
      <Table
        movie={{ ...movie, subtitles: [subtitle] }}
        history={[event(6, 1, { language: { ...english, hi: true } })]}
      />,
    );

    expect(screen.getByLabelText("Translated")).toBeInTheDocument();
  });

  it.each([
    ["another language", { language: { ...english, code2: "fr" } }],
    ["hearing impaired variant", { language: { ...english, hi: true } }],
    ["forced variant", { language: { ...english, forced: true } }],
    ["another local movie", { id: 200 }],
    ["another instance", { arr_instance_id: 3 }],
  ])(
    "ignores translation history belonging to %s at the same path",
    async (_, overrides) => {
      await renderTable(
        <Table movie={movie} history={[event(6, 1, overrides)]} />,
      );

      expect(screen.queryByLabelText("Translated")).not.toBeInTheDocument();
    },
  );

  it.each([
    ["another local movie", { id: 200 }],
    ["another instance", { arr_instance_id: 3 }],
  ])(
    "preserves translation when a replacement belongs to %s",
    async (_, overrides) => {
      await renderTable(
        <Table movie={movie} history={[event(2, 2, overrides), event(6, 1)]} />,
      );

      expect(screen.getByLabelText("Translated")).toBeInTheDocument();
    },
  );

  it.each([
    ["another language", { ...english, code2: "fr" }],
    ["hearing impaired variant", { ...english, hi: true }],
    ["forced variant", { ...english, forced: true }],
    ["missing language", undefined],
  ])(
    "clears translation when the same path is replaced with %s",
    async (_, language) => {
      await renderTable(
        <Table
          movie={movie}
          history={[event(2, 2, { language }), event(6, 1)]}
        />,
      );

      expect(screen.queryByLabelText("Translated")).not.toBeInTheDocument();
    },
  );

  it("uses the full local movie history beyond the history page size", async () => {
    const history = [
      ...Array.from({ length: 30 }, (_, index) =>
        event(7, index + 3, { subtitles_path: "" }),
      ),
      event(6, 2),
      event(1, 1),
    ];
    server.use(
      http.get("/api/movies/history", ({ request }) => {
        const params = new URL(request.url).searchParams;
        expect(params.get("id")).toBe("100");
        expect(params.get("radarrid")).toBeNull();
        expect(params.get("include_embedded")).toBe("true");
        const length = Number(params.get("length") ?? "-1");
        const data = length > 0 ? history.slice(0, length) : history;
        return HttpResponse.json({ data, total: history.length });
      }),
    );
    function MovieHistoryTable() {
      const { data } = useMovieHistory(movie.id);
      return <Table movie={movie} history={data} />;
    }
    await renderTable(<MovieHistoryTable />);

    expect(await screen.findByLabelText("Translated")).toBeInTheDocument();
  });
});
