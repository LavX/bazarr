/**
 * Tests for the Search component (src/components/Search.tsx).
 *
 * Why: The search only fires a server request when the query is non-empty
 * (enabled = query.length > 0), and its local options filter is
 * diacritic-insensitive and case-insensitive. Both behaviors are load-bearing
 * and easy to regress.
 *
 * What: Renders Search with a mocked useServerSearch hook that records its
 * arguments, then asserts the enabled flag and available options.
 *
 * Test: Run with `cd frontend && npx vitest run src/components/__tests__/Search.test.tsx`.
 */

import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Search from "@/components/Search";
import { customRender, screen, waitFor } from "@/tests";

// ---------------------------------------------------------------------------
// Mock @/apis/hooks so we control what useServerSearch returns and captures
// its call arguments. The rest of the module is passed through unchanged.
// ---------------------------------------------------------------------------

type ServerSearchResult = {
  id: number | null;
  sonarrSeriesId?: number;
  radarrId?: number;
  title: string;
  year: number;
  poster: string | null;
};

type UseServerSearchReturn = {
  data: ServerSearchResult[] | undefined;
};

type MockUseServerSearch = (
  query: string,
  enabled: boolean,
) => UseServerSearchReturn;

const mockUseServerSearch = vi.fn<MockUseServerSearch>(() => ({
  data: undefined,
}));

vi.mock("@/apis/hooks", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...original,
    useServerSearch: (query: string, enabled: boolean) =>
      mockUseServerSearch(query, enabled),
  };
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeSeriesResult(
  id: number,
  title: string,
  year: number,
): ServerSearchResult {
  return { id, sonarrSeriesId: id, title, year, poster: null };
}

function makeMovieResult(
  id: number,
  title: string,
  year: number,
): ServerSearchResult {
  return { id, radarrId: id, title, year, poster: null };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("Search component — server query enabled flag", () => {
  it("calls useServerSearch with enabled=false when the input is empty", () => {
    mockUseServerSearch.mockReturnValue({ data: undefined });

    customRender(<Search />);

    // The component mounts with an empty query; the hook must be called with
    // enabled=false so no network request fires.
    expect(mockUseServerSearch).toHaveBeenCalledWith("", false);

    const allCalls = mockUseServerSearch.mock.calls;
    allCalls.forEach((args) => {
      // Every call while the query is empty must have enabled=false.
      expect(args[1]).toBe(false);
    });
  });

  it("calls useServerSearch with enabled=true once a non-empty query is typed", async () => {
    mockUseServerSearch.mockReturnValue({ data: undefined });

    const user = userEvent.setup();
    customRender(<Search />);

    const input = screen.getByPlaceholderText("Search");
    await user.type(input, "b");

    // After typing at least one character the hook must receive enabled=true.
    await waitFor(() => {
      const calledWithEnabled = mockUseServerSearch.mock.calls.some(
        (args) => args[1] === true,
      );
      expect(calledWithEnabled).toBe(true);
    });
  });

  it("passes the typed query string to useServerSearch", async () => {
    mockUseServerSearch.mockReturnValue({ data: undefined });

    const user = userEvent.setup();
    customRender(<Search />);

    const input = screen.getByPlaceholderText("Search");
    await user.type(input, "bat");

    await waitFor(() => {
      const queriesUsed = mockUseServerSearch.mock.calls.map((args) => args[0]);
      expect(queriesUsed).toContain("bat");
    });
  });
});

describe("Search component — options filter is case-insensitive", () => {
  it("shows a result whose label starts with uppercase when the search is all lowercase", async () => {
    mockUseServerSearch.mockReturnValue({
      data: [makeMovieResult(1, "Batman Begins", 2005)],
    });

    const user = userEvent.setup();
    customRender(<Search />);

    const input = screen.getByPlaceholderText("Search");
    await user.type(input, "batman");

    await waitFor(() => {
      expect(screen.getByText("Batman Begins (2005)")).toBeInTheDocument();
    });
  });

  it("shows a result when the search uses uppercase and the label is mixed-case", async () => {
    mockUseServerSearch.mockReturnValue({
      data: [makeSeriesResult(2, "Breaking Bad", 2008)],
    });

    const user = userEvent.setup();
    customRender(<Search />);

    const input = screen.getByPlaceholderText("Search");
    await user.type(input, "BREAKING");

    await waitFor(() => {
      expect(screen.getByText("Breaking Bad (2008)")).toBeInTheDocument();
    });
  });
});

describe("Search component — options filter is diacritic-insensitive", () => {
  it("matches a title with accented characters when the search omits the accent", async () => {
    mockUseServerSearch.mockReturnValue({
      data: [makeMovieResult(3, "Amélie", 2001)],
    });

    const user = userEvent.setup();
    customRender(<Search />);

    const input = screen.getByPlaceholderText("Search");
    // Search without the accent; the filter must still match "Amélie".
    await user.type(input, "Amelie");

    await waitFor(() => {
      expect(screen.getByText("Amélie (2001)")).toBeInTheDocument();
    });
  });

  it("matches when the search contains an accent but the label does not", async () => {
    mockUseServerSearch.mockReturnValue({
      data: [makeMovieResult(4, "Amelie", 2001)],
    });

    const user = userEvent.setup();
    customRender(<Search />);

    const input = screen.getByPlaceholderText("Search");
    await user.type(input, "Amélie");

    await waitFor(() => {
      expect(screen.getByText("Amelie (2001)")).toBeInTheDocument();
    });
  });
});

describe("Search component — result routing", () => {
  it("renders a show result from sonarrSeriesId data", async () => {
    mockUseServerSearch.mockReturnValue({
      data: [makeSeriesResult(10, "The Wire", 2002)],
    });

    const user = userEvent.setup();
    customRender(<Search />);

    const input = screen.getByPlaceholderText("Search");
    await user.type(input, "wire");

    await waitFor(() => {
      expect(screen.getByText("The Wire (2002)")).toBeInTheDocument();
    });
  });

  it("renders a movie result from radarrId data", async () => {
    mockUseServerSearch.mockReturnValue({
      data: [makeMovieResult(20, "Inception", 2010)],
    });

    const user = userEvent.setup();
    customRender(<Search />);

    const input = screen.getByPlaceholderText("Search");
    await user.type(input, "ince");

    await waitFor(() => {
      expect(screen.getByText("Inception (2010)")).toBeInTheDocument();
    });
  });
});
