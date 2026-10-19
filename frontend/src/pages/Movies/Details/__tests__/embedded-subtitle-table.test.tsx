/* eslint-disable camelcase */

/**
 * Tests for the Movies Detail subtitle Table component.
 *
 * Why: Verifies that embedded subtitle tracks display scores from history,
 * that multiple embedded tracks with the same language but different hi/forced
 * flags produce unique TanStack row IDs (no key collision), that the
 * embeddedTrack prop is correctly threaded to SubtitleToolsMenu, and that a
 * bitmap row offers Extract to file only, before any click.
 *
 * What: Renders Table directly with controlled movie/history props and asserts
 * cell content and DOM structure via Testing Library queries.
 *
 * Test: Run with `cd frontend && npx vitest run --reporter=verbose`.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import Table from "@/pages/Movies/Details/table";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// ---------------------------------------------------------------------------
// Shared fixtures
// ---------------------------------------------------------------------------

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

function makeHistoryEntry(overrides: Partial<History.Movie>): History.Movie {
  return {
    id: 1,
    radarrId: 1,
    title: "Test Movie",
    action: 1,
    blacklisted: false,
    parsed_timestamp: "2024-01-01T00:00:00",
    timestamp: "2024-01-01T00:00:00",
    description: "Downloaded",
    upgradable: false,
    matches: [],
    dont_matches: [],
    tags: [],
    monitored: true,
    subtitles_path: "",
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Setup: silence API calls that the component may fire
// ---------------------------------------------------------------------------

function setupApiMocks() {
  server.use(
    http.get("/api/subtitles/sync-status", () =>
      HttpResponse.json({ data: null }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
  );
}

// ---------------------------------------------------------------------------
// Test 1: Embedded track shows 100% score from history
// ---------------------------------------------------------------------------

describe("Movies Detail Table, embedded subtitle scores", () => {
  it("shows score from action=7 history entry for embedded track", async () => {
    setupApiMocks();

    const embedded: Subtitle = {
      code2: "en",
      name: "English",
      hi: false,
      forced: false,
      path: null,
    };

    const movie = makeMovie([embedded]);

    const history: History.Movie[] = [
      makeHistoryEntry({
        action: 7,
        score: "100.0%",
        language: { code2: "en", name: "English", hi: false, forced: false },
        provider: "embedded",
        subtitles_path: "",
      }),
    ];

    customRender(<Table movie={movie} profile={undefined} history={history} />);

    await waitFor(() => {
      expect(screen.getByText("100.0%")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Test 2: Multiple embedded tracks same language (hi/forced) no key collision
// ---------------------------------------------------------------------------

describe("Movies Detail Table, no key collision for hi vs regular", () => {
  it("renders both regular and HI embedded English tracks without error", async () => {
    setupApiMocks();

    const regularEmbedded: Subtitle = {
      code2: "en",
      name: "English",
      hi: false,
      forced: false,
      path: null,
    };

    const hiEmbedded: Subtitle = {
      code2: "en",
      name: "English (HI)",
      hi: true,
      forced: false,
      path: null,
    };

    const movie = makeMovie([regularEmbedded, hiEmbedded]);

    const history: History.Movie[] = [
      makeHistoryEntry({
        action: 7,
        score: "100.0%",
        language: { code2: "en", name: "English", hi: false, forced: false },
        provider: "embedded",
        subtitles_path: "",
      }),
      makeHistoryEntry({
        action: 7,
        score: "95.0%",
        language: { code2: "en", name: "English", hi: true, forced: false },
        provider: "embedded",
        subtitles_path: "",
      }),
    ];

    customRender(<Table movie={movie} profile={undefined} history={history} />);

    // Both tracks should render, expect two "Video File Subtitle Track" cells
    await waitFor(() => {
      const cells = screen.getAllByText("Video File Subtitle Track");
      expect(cells).toHaveLength(2);
    });

    // Both scores should be rendered
    expect(screen.getByText("100.0%")).toBeInTheDocument();
    expect(screen.getByText("95.0%")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Test 3: Embedded track renders "Video File Subtitle Track" path cell
// ---------------------------------------------------------------------------

describe("Movies Detail Table, embedded track path display", () => {
  it("shows 'Video File Subtitle Track' text for null-path subtitles", async () => {
    setupApiMocks();

    const embedded: Subtitle = {
      code2: "fr",
      name: "French",
      hi: false,
      forced: false,
      path: null,
    };

    const movie = makeMovie([embedded]);

    customRender(<Table movie={movie} profile={undefined} history={[]} />);

    await waitFor(() => {
      expect(screen.getByText("Video File Subtitle Track")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Test 4: External subtitle shows file name in path cell
// ---------------------------------------------------------------------------

describe("Movies Detail Table, external subtitle path display", () => {
  it("shows the file name for external subtitle tracks", async () => {
    setupApiMocks();

    const external: Subtitle = {
      code2: "en",
      name: "English",
      hi: false,
      forced: false,
      path: "/movies/test.en.srt",
    };

    const movie = makeMovie([external]);

    customRender(<Table movie={movie} profile={undefined} history={[]} />);

    await waitFor(() => {
      expect(screen.getByText("test.en.srt")).toBeInTheDocument();
    });
    // The full path is not shown inline; it only appears in the hover tooltip.
    expect(screen.queryByText("/movies/test.en.srt")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Test 5: A bitmap row says what it can do before the click
// ---------------------------------------------------------------------------

describe("Movies Detail Table, bitmap row", () => {
  it("offers Extract to file only on a bitmap row", async () => {
    /**
     * Why: A PGS or VobSub track has no text, so a tool or a translation on it
     * used to fail after the click (a 400 toast). The row knows its codec now,
     * and the menu says so up front.
     */
    setupApiMocks();
    const embeddedPgs: Subtitle = {
      code2: "en",
      name: "English",
      hi: false,
      forced: false,
      path: null,
      codec: "pgs",
    };
    const user = userEvent.setup();
    customRender(
      <Table
        movie={makeMovie([embeddedPgs])}
        profile={undefined}
        history={[]}
      />,
    );
    await screen.findByText("Video File Subtitle Track");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    await user.click(await screen.findByLabelText("Subtitle Actions"));

    expect(
      await screen.findByText(
        "Image-based track (PGS): the tools need text. Extract to file copies it out as it is, for players.",
      ),
    ).toBeInTheDocument();
    // Items are looked up whether or not the dropdown is still visible.
    const item = (name: string) =>
      screen.getByRole("menuitem", { name, hidden: true });
    expect(item("Extract to file")).toBeEnabled();
    for (const name of [
      "Sync...",
      "Remove HI Tags",
      "Translate...",
      "View",
      "Edit",
      "Download",
      "Search",
    ]) {
      expect(item(name)).toBeDisabled();
    }
    expect(item("Hide track...")).toBeEnabled();
    expect(
      screen.queryByRole("menuitem", { name: "Delete...", hidden: true }),
    ).not.toBeInTheDocument();
  });
});
