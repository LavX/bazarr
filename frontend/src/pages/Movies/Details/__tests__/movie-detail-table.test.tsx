/* eslint-disable camelcase */

/**
 * Focused behavior tests for the Movies Detail subtitle Table component.
 *
 * Coverage:
 * - Subtitle rows render the correct path-cell text for external, embedded,
 *   and missing subtitle entries.
 * - The `disabled` prop disables every action button (Subtitle Actions /
 *   Combined Subtitle Actions) in the table.
 * - An empty movie shows the configured empty-state text.
 */

import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import Table from "@/pages/Movies/Details/table";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeMovie(
  subtitles: Subtitle[],
  missing_subtitles: Subtitle[] = [],
): Item.Movie {
  return {
    id: 1,
    radarrId: 42,
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
    missing_subtitles,
  };
}

/** Silence API calls that the table fires on render. */
function setupApiMocks() {
  server.use(
    // sync-status: fired for every external (on-disk) subtitle row
    http.get("/api/subtitles/sync-status", () =>
      HttpResponse.json({ data: null }),
    ),
    // useLanguages: used by useProfileItemsToLanguages + Language.Text badges
    http.get("/api/system/languages", () => HttpResponse.json([])),
  );
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("MovieDetailTable", () => {
  // -------------------------------------------------------------------------
  // 1. External subtitle: path shown verbatim in the path column
  // -------------------------------------------------------------------------
  it("renders the file path for an external subtitle track", async () => {
    setupApiMocks();

    const external: Subtitle = {
      code2: "en",
      name: "English",
      hi: false,
      forced: false,
      path: "/movies/test.en.srt",
    };

    customRender(
      <Table movie={makeMovie([external])} profile={undefined} history={[]} />,
    );

    await waitFor(() => {
      expect(screen.getByText("/movies/test.en.srt")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 2. Embedded subtitle: "Video File Subtitle Track" shown in path column
  // -------------------------------------------------------------------------
  it("renders 'Video File Subtitle Track' for an embedded subtitle (null path)", async () => {
    setupApiMocks();

    const embedded: Subtitle = {
      code2: "fr",
      name: "French",
      hi: false,
      forced: false,
      path: null,
    };

    customRender(
      <Table movie={makeMovie([embedded])} profile={undefined} history={[]} />,
    );

    await waitFor(() => {
      expect(screen.getByText("Video File Subtitle Track")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 3. Missing subtitle: "Missing Subtitles" sentinel shown in path column
  // -------------------------------------------------------------------------
  it("renders 'Missing Subtitles' for a missing subtitle entry", async () => {
    setupApiMocks();

    const missing: Subtitle = {
      code2: "de",
      name: "German",
      hi: false,
      forced: false,
      path: undefined,
    };

    customRender(
      <Table
        movie={makeMovie([], [missing])}
        profile={undefined}
        history={[]}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Missing Subtitles")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 4. No subtitles at all: empty-state message appears
  // -------------------------------------------------------------------------
  it("shows the empty-state message when there are no subtitles", async () => {
    setupApiMocks();

    customRender(
      <Table movie={makeMovie([])} profile={undefined} history={[]} />,
    );

    await waitFor(() => {
      expect(
        screen.getByText("No subtitles found for this movie"),
      ).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 5. disabled=false: action buttons are NOT disabled
  // -------------------------------------------------------------------------
  it("enables action buttons when disabled prop is false", async () => {
    setupApiMocks();

    const external: Subtitle = {
      code2: "en",
      name: "English",
      hi: false,
      forced: false,
      path: "/movies/test.en.srt",
    };

    customRender(
      <Table
        movie={makeMovie([external])}
        profile={undefined}
        history={[]}
        disabled={false}
      />,
    );

    await waitFor(() => {
      const buttons = screen.getAllByLabelText("Subtitle Actions");
      expect(buttons.length).toBeGreaterThanOrEqual(1);
      buttons.forEach((btn) => {
        expect(btn).not.toBeDisabled();
      });
    });
  });

  // -------------------------------------------------------------------------
  // 6. disabled=true: ALL action buttons are disabled (external subtitle row)
  // -------------------------------------------------------------------------
  it("disables Subtitle Actions button when disabled prop is true (external track)", async () => {
    setupApiMocks();

    const external: Subtitle = {
      code2: "en",
      name: "English",
      hi: false,
      forced: false,
      path: "/movies/test.en.srt",
    };

    customRender(
      <Table
        movie={makeMovie([external])}
        profile={undefined}
        history={[]}
        disabled={true}
      />,
    );

    await waitFor(() => {
      const buttons = screen.getAllByLabelText("Subtitle Actions");
      expect(buttons.length).toBeGreaterThanOrEqual(1);
      buttons.forEach((btn) => {
        expect(btn).toBeDisabled();
      });
    });
  });

  // -------------------------------------------------------------------------
  // 7. disabled=true: action button is disabled for a missing subtitle row
  // -------------------------------------------------------------------------
  it("disables Subtitle Actions button when disabled prop is true (missing subtitle)", async () => {
    setupApiMocks();

    const missing: Subtitle = {
      code2: "es",
      name: "Spanish",
      hi: false,
      forced: false,
      path: undefined,
    };

    customRender(
      <Table
        movie={makeMovie([], [missing])}
        profile={undefined}
        history={[]}
        disabled={true}
      />,
    );

    await waitFor(() => {
      const buttons = screen.getAllByLabelText("Subtitle Actions");
      expect(buttons.length).toBeGreaterThanOrEqual(1);
      buttons.forEach((btn) => {
        expect(btn).toBeDisabled();
      });
    });
  });

  // -------------------------------------------------------------------------
  // 8. Multiple subtitle rows: each gets its own action button
  // -------------------------------------------------------------------------
  it("renders one action button per subtitle row", async () => {
    setupApiMocks();

    const subtitles: Subtitle[] = [
      {
        code2: "en",
        name: "English",
        hi: false,
        forced: false,
        path: "/movies/test.en.srt",
      },
      {
        code2: "fr",
        name: "French",
        hi: false,
        forced: false,
        path: "/movies/test.fr.srt",
      },
    ];
    const missing: Subtitle[] = [
      {
        code2: "de",
        name: "German",
        hi: false,
        forced: false,
        path: undefined,
      },
    ];

    customRender(
      <Table
        movie={makeMovie(subtitles, missing)}
        profile={undefined}
        history={[]}
      />,
    );

    // 2 external + 1 missing = 3 action buttons
    await waitFor(() => {
      const buttons = screen.getAllByLabelText("Subtitle Actions");
      expect(buttons).toHaveLength(3);
    });
  });

  // -------------------------------------------------------------------------
  // 9. disabled=true covers all rows (mixed external + missing)
  // -------------------------------------------------------------------------
  it("disables all action buttons across mixed subtitle types when disabled is true", async () => {
    setupApiMocks();

    const subtitles: Subtitle[] = [
      {
        code2: "en",
        name: "English",
        hi: false,
        forced: false,
        path: "/movies/test.en.srt",
      },
      { code2: "fr", name: "French", hi: false, forced: false, path: null },
    ];
    const missing: Subtitle[] = [
      {
        code2: "de",
        name: "German",
        hi: false,
        forced: false,
        path: undefined,
      },
    ];

    customRender(
      <Table
        movie={makeMovie(subtitles, missing)}
        profile={undefined}
        history={[]}
        disabled={true}
      />,
    );

    await waitFor(() => {
      const buttons = screen.getAllByLabelText("Subtitle Actions");
      expect(buttons).toHaveLength(3);
      buttons.forEach((btn) => {
        expect(btn).toBeDisabled();
      });
    });
  });

  // -------------------------------------------------------------------------
  // 10. null movie: renders without crashing (graceful null handling)
  // -------------------------------------------------------------------------
  it("renders without crashing when movie is null", async () => {
    setupApiMocks();

    // When movie is null the table receives no data rows; empty-state appears
    customRender(<Table movie={null} profile={undefined} history={[]} />);

    await waitFor(() => {
      expect(
        screen.getByText("No subtitles found for this movie"),
      ).toBeInTheDocument();
    });
  });
});
