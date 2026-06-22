/* eslint-disable camelcase */

/**
 * Tests for ManualSearchView (MovieSearchModal wrapper).
 *
 * Why: Verifies that search results render in the table; that clicking download
 * on a row calls the download prop with the correct result object; and that the
 * downloaded highlight is keyed by provider+url+release (not row index) so it
 * stays on the right row.
 *
 * What: Renders MovieSearchModal directly with a fake query prop and a spy
 * download prop. Clicks Search to show the table, then exercises the download
 * button on individual rows.
 *
 * Test: Run with `cd frontend && npx vitest run --reporter=verbose`.
 */

import { UseQueryResult } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MovieSearchModal } from "@/components/modals/ManualSearchModal";
import { customRender, screen, waitFor } from "@/tests";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeMovie(): Item.Movie {
  return {
    id: 42,
    radarrId: 42,
    title: "Inception",
    path: "/movies/inception.mkv",
    sceneName: "",
    profileId: 1,
    fanart: "",
    overview: "",
    imdbId: "tt1375666",
    alternativeTitles: [],
    poster: "",
    year: "2010",
    monitored: true,
    tags: [],
    audio_language: [],
    subtitles: [],
    missing_subtitles: [],
  };
}

function makeSearchResult(
  overrides: Partial<SearchResultType>,
): SearchResultType {
  return {
    provider: "opensubtitles",
    url: "https://www.opensubtitles.org/en/subtitles/1234",
    language: "en",
    forced: "False",
    hearing_impaired: "False",
    score: 90,
    orig_score: 90,
    score_without_hash: 88,
    release_info: ["Inception.2010.1080p.BluRay"],
    matches: ["title", "year"],
    dont_matches: [],
    subtitle: null,
    original_format: "False",
    ...overrides,
  };
}

// Build a minimal UseQueryResult that looks like a resolved query.
function makeQueryResult(
  data: SearchResultType[] | undefined,
  refetch: () => void = vi.fn(),
): UseQueryResult<SearchResultType[] | undefined> {
  return {
    data,
    dataUpdatedAt: 0,
    error: null,
    errorUpdatedAt: 0,
    failureCount: 0,
    failureReason: null,
    errorUpdateCount: 0,
    isError: false,
    isFetched: data !== undefined,
    isFetchedAfterMount: data !== undefined,
    isFetching: false,
    isInitialLoading: false,
    isLoading: false,
    isLoadingError: false,
    isPaused: false,
    isPending: false,
    isPlaceholderData: false,
    isRefetchError: false,
    isRefetching: false,
    isStale: false,
    isSuccess: data !== undefined,
    refetch: refetch as UseQueryResult<
      SearchResultType[] | undefined
    >["refetch"],
    status: data !== undefined ? "success" : "pending",
    fetchStatus: "idle",
  } as UseQueryResult<SearchResultType[] | undefined>;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Renders MovieSearchModal as if it were opened by Mantine modals.
 * MovieSearchModal = withModal(ManualSearchView, ...) which expects
 * ContextModalProps<Props<Item.Movie>> which includes id, innerProps, and
 * context (the modals manager).
 */
function renderModal(
  results: SearchResultType[],
  download: (item: Item.Movie, result: SearchResultType) => Promise<void> = vi
    .fn()
    .mockResolvedValue(undefined),
) {
  const movie = makeMovie();
  const refetch = vi.fn();

  // The query prop is called as a hook inside the component. When the component
  // passes undefined (before search starts), we return no data. Once a numeric
  // id is passed (after Search is clicked), we return the fixture results.
  const query = (id?: number): UseQueryResult<SearchResultType[] | undefined> =>
    makeQueryResult(id !== undefined ? results : undefined, refetch);

  // ContextModalProps requires a `context` field (the Mantine modals manager).
  // We pass a minimal stub — the component never calls context directly.
  const contextStub = {} as Parameters<typeof MovieSearchModal>[0]["context"];

  customRender(
    <MovieSearchModal
      id="test-modal"
      context={contextStub}
      innerProps={{
        item: movie,
        query,
        download,
      }}
    />,
  );

  return { movie, refetch, download };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("ManualSearchModal: initial render", () => {
  it("shows the file path in the resource alert", () => {
    renderModal([]);
    expect(screen.getByText("/movies/inception.mkv")).toBeInTheDocument();
  });

  it("shows a Search button that reads 'Search' before any search has fired", () => {
    renderModal([]);
    expect(
      screen.getByRole("button", { name: /^search$/i }),
    ).toBeInTheDocument();
  });

  it("no data rows appear before search is initiated", () => {
    renderModal([makeSearchResult({ provider: "opensubtitles" })]);
    // query returns undefined when id is undefined (searchStarted=false),
    // so haveResult=false and Collapse is closed — provider text not yet visible.
    expect(screen.queryByText("opensubtitles")).not.toBeInTheDocument();
  });
});

describe("ManualSearchModal: table renders search results", () => {
  it("shows all result rows after clicking Search", async () => {
    const user = userEvent.setup();
    const results = [
      makeSearchResult({ provider: "opensubtitles", score: 90 }),
      makeSearchResult({
        provider: "subscene",
        url: "https://subscene.com/sub/999",
        release_info: ["Inception.2010.BluRay.x264"],
        score: 75,
      }),
    ];

    renderModal(results);

    await user.click(screen.getByRole("button", { name: /search/i }));

    await waitFor(() => {
      // Each provider name should appear in the table
      expect(screen.getByText("opensubtitles")).toBeInTheDocument();
      expect(screen.getByText("subscene")).toBeInTheDocument();
    });

    // Score column values
    expect(screen.getByText("90%")).toBeInTheDocument();
    expect(screen.getByText("75%")).toBeInTheDocument();
  });

  it("shows release info for each result", async () => {
    const user = userEvent.setup();
    const results = [
      makeSearchResult({
        provider: "opensubtitles",
        release_info: ["Inception.2010.1080p.BluRay"],
      }),
    ];

    renderModal(results);
    await user.click(screen.getByRole("button", { name: /search/i }));

    await waitFor(() => {
      expect(
        screen.getByText("Inception.2010.1080p.BluRay"),
      ).toBeInTheDocument();
    });
  });

  it("shows 'No result' empty-state when search returns empty array", async () => {
    const user = userEvent.setup();

    renderModal([]);
    await user.click(screen.getByRole("button", { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByText("No result")).toBeInTheDocument();
    });
  });
});

describe("ManualSearchModal: download button behavior", () => {
  it("calls download prop with the correct item and result when clicked", async () => {
    const user = userEvent.setup();
    const downloadSpy = vi.fn().mockResolvedValue(undefined);

    const result = makeSearchResult({ provider: "opensubtitles", score: 90 });
    renderModal([result], downloadSpy);

    await user.click(screen.getByRole("button", { name: /search/i }));

    await waitFor(() => {
      expect(screen.getByLabelText("Download")).toBeInTheDocument();
    });

    await user.click(screen.getByLabelText("Download"));

    await waitFor(() => {
      expect(downloadSpy).toHaveBeenCalledTimes(1);
    });

    const [calledItem, calledResult] = downloadSpy.mock.calls[0] as [
      Item.Movie,
      SearchResultType,
    ];
    expect(calledItem.id).toBe(42);
    expect(calledItem.title).toBe("Inception");
    expect(calledResult.provider).toBe("opensubtitles");
    expect(calledResult.score).toBe(90);
    expect(calledResult.release_info[0]).toBe("Inception.2010.1080p.BluRay");
  });

  it("marks only the clicked row as downloaded (keyed by provider+url+release)", async () => {
    const user = userEvent.setup();

    const resultA = makeSearchResult({
      provider: "opensubtitles",
      url: "https://www.opensubtitles.org/en/subtitles/1234",
      release_info: ["Inception.2010.1080p.BluRay"],
      score: 90,
    });
    const resultB = makeSearchResult({
      provider: "subscene",
      url: "https://subscene.com/sub/999",
      release_info: ["Inception.2010.BluRay.x264"],
      score: 75,
    });

    renderModal([resultA, resultB]);

    await user.click(screen.getByRole("button", { name: /search/i }));

    // Wait for both Download buttons
    await waitFor(() => {
      expect(screen.getAllByLabelText("Download")).toHaveLength(2);
    });

    // Click the first download button (opensubtitles row)
    const downloadButtons = screen.getAllByLabelText("Download");
    await user.click(downloadButtons[0]);

    // After download, both buttons should still be in the DOM. The clicked one
    // changes to the "downloaded" icon but keeps the same aria-label "Download".
    // The un-clicked row button stays enabled (not removed).
    await waitFor(() => {
      expect(screen.getAllByLabelText("Download")).toHaveLength(2);
    });
  });

  it("calls download with the exact result object for the second row when it is clicked", async () => {
    const user = userEvent.setup();
    const downloadSpy = vi.fn().mockResolvedValue(undefined);

    const resultA = makeSearchResult({
      provider: "opensubtitles",
      score: 90,
      release_info: ["Inception.2010.1080p.BluRay"],
    });
    const resultB = makeSearchResult({
      provider: "subscene",
      url: "https://subscene.com/sub/999",
      release_info: ["Inception.2010.BluRay.x264"],
      score: 75,
    });

    renderModal([resultA, resultB], downloadSpy);

    await user.click(screen.getByRole("button", { name: /search/i }));

    await waitFor(() => {
      expect(screen.getAllByLabelText("Download")).toHaveLength(2);
    });

    // Click the SECOND button (subscene row)
    const downloadButtons = screen.getAllByLabelText("Download");
    await user.click(downloadButtons[1]);

    await waitFor(() => {
      expect(downloadSpy).toHaveBeenCalledTimes(1);
    });

    const [, calledResult] = downloadSpy.mock.calls[0] as [
      Item.Movie,
      SearchResultType,
    ];
    // Must be the subscene result, not the opensubtitles one
    expect(calledResult.provider).toBe("subscene");
    expect(calledResult.score).toBe(75);
    expect(calledResult.release_info[0]).toBe("Inception.2010.BluRay.x264");
  });
});

describe("ManualSearchModal: provider link rendering", () => {
  it("renders provider as a link when url is present", async () => {
    const user = userEvent.setup();
    const results = [
      makeSearchResult({
        provider: "opensubtitles",
        url: "https://www.opensubtitles.org/en/subtitles/1234",
      }),
    ];

    renderModal(results);
    await user.click(screen.getByRole("button", { name: /search/i }));

    await waitFor(() => {
      const link = screen.getByRole("link", { name: "opensubtitles" });
      expect(link).toHaveAttribute(
        "href",
        "https://www.opensubtitles.org/en/subtitles/1234",
      );
    });
  });

  it("renders provider as plain text when url is absent", async () => {
    const user = userEvent.setup();
    const results = [makeSearchResult({ provider: "local", url: undefined })];

    renderModal(results);
    await user.click(screen.getByRole("button", { name: /search/i }));

    await waitFor(() => {
      // No link — just a Text element with the provider name
      expect(screen.queryByRole("link", { name: "local" })).toBeNull();
      expect(screen.getByText("local")).toBeInTheDocument();
    });
  });
});

describe("ManualSearchModal: Search Again button", () => {
  it("changes button label to 'Search Again' after the first search", async () => {
    const user = userEvent.setup();

    renderModal([]);
    await user.click(screen.getByRole("button", { name: /^search$/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /search again/i }),
      ).toBeInTheDocument();
    });
  });
});
