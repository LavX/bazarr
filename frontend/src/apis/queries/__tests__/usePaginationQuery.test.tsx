/* eslint-disable camelcase */
import { PropsWithChildren } from "react";
import { MemoryRouter } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { usePaginationQuery } from "@/apis/queries/hooks";
import { QueryKeys } from "@/apis/queries/keys";
import server from "@/tests/mocks/node";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeQueryFn(total: number, items: object[] = []): RangeQuery<object> {
  return vi.fn().mockResolvedValue({ data: items, total });
}

/** Build a renderHook wrapper that provides QueryClient + MemoryRouter
 *  at the given initial URL (e.g. "/?page=3"). */
function buildWrapper(initialUrl = "/") {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, networkMode: "offlineFirst" },
    },
  });

  const Wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialUrl]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );

  return { qc, wrapper: Wrapper };
}

// ---------------------------------------------------------------------------
// Setup: ensure /api/system/settings is always answered so usePageSize can
// resolve the global page_size.
// ---------------------------------------------------------------------------

beforeEach(() => {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { theme: "auto", page_size: 25 } }),
    ),
  );
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("usePaginationQuery", () => {
  describe("initial page clamping from URL ?page= param", () => {
    it("starts at page index 0 when ?page= is absent", async () => {
      const { wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(100);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => expect(result.current.paginationStatus.page).toBe(0));
    });

    it("starts at page index 0 when ?page=0 (zero-value, below 1-based min)", async () => {
      const { wrapper } = buildWrapper("/?page=0");
      const queryFn = makeQueryFn(100);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => expect(result.current.paginationStatus.page).toBe(0));
    });

    it("starts at page index 0 when ?page=-5 (negative)", async () => {
      const { wrapper } = buildWrapper("/?page=-5");
      const queryFn = makeQueryFn(100);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => expect(result.current.paginationStatus.page).toBe(0));
    });

    it("starts at page index 0 when ?page=NaN", async () => {
      const { wrapper } = buildWrapper("/?page=NaN");
      const queryFn = makeQueryFn(100);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => expect(result.current.paginationStatus.page).toBe(0));
    });

    it("converts 1-based ?page=1 URL param to index 0", async () => {
      const { wrapper } = buildWrapper("/?page=1");
      const queryFn = makeQueryFn(100);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => expect(result.current.paginationStatus.page).toBe(0));
    });

    it("converts 1-based ?page=3 URL param to index 2", async () => {
      const { wrapper } = buildWrapper("/?page=3");
      // Need enough total to have 3 pages at size 25
      const queryFn = makeQueryFn(1000);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => expect(result.current.paginationStatus.page).toBe(2));
    });
  });

  describe("setPageSize", () => {
    it("overrides the page size and resets the page index to 0", async () => {
      const { wrapper } = buildWrapper("/?page=3");
      const queryFn = makeQueryFn(1000);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      // Initial page from URL: page=3 (1-based) -> index 2
      await waitFor(() => expect(result.current.paginationStatus.page).toBe(2));

      // Override the page size
      result.current.controls.setPageSize(10);

      await waitFor(() => {
        expect(result.current.paginationStatus.pageSize).toBe(10);
        expect(result.current.paginationStatus.page).toBe(0);
      });
    });

    it("the overridden page size is reflected in pageCount", async () => {
      const { wrapper } = buildWrapper("/");
      // 100 total items
      const queryFn = makeQueryFn(100);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);

      // Default page_size=25 from settings: 100/25 = 4 pages
      await waitFor(() =>
        expect(result.current.paginationStatus.pageCount).toBe(4),
      );

      // Switch to page size 10: 100/10 = 10 pages
      result.current.controls.setPageSize(10);

      await waitFor(() =>
        expect(result.current.paginationStatus.pageCount).toBe(10),
      );
    });

    it("resets to page 0 even when currently on a later page", async () => {
      const { wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(200);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      // Wait until pageCount is known (8 pages for 200 items / 25)
      await waitFor(() =>
        expect(result.current.paginationStatus.pageCount).toBe(8),
      );

      // Navigate to page 3 (index 3 is valid: 0-7)
      result.current.controls.gotoPage(3);
      await waitFor(() => expect(result.current.paginationStatus.page).toBe(3));

      // setPageSize must reset to 0
      result.current.controls.setPageSize(5);
      await waitFor(() => {
        expect(result.current.paginationStatus.page).toBe(0);
        expect(result.current.paginationStatus.pageSize).toBe(5);
      });
    });
  });

  describe("pageCount derivation", () => {
    it("computes pageCount from total and active pageSize", async () => {
      const { wrapper } = buildWrapper("/");
      // 75 items with default page_size=25 -> 3 pages
      const queryFn = makeQueryFn(75);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);
      await waitFor(() =>
        expect(result.current.paginationStatus.pageCount).toBe(3),
      );
    });

    it("rounds up fractional pageCount (ceil)", async () => {
      const { wrapper } = buildWrapper("/");
      // 26 items / 25 per page = 1.04 -> rounds up to 2
      const queryFn = makeQueryFn(26);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);
      await waitFor(() =>
        expect(result.current.paginationStatus.pageCount).toBe(2),
      );
    });

    it("reports pageCount=0 when total is 0", async () => {
      const { wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(0);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);
      await waitFor(() =>
        expect(result.current.paginationStatus.pageCount).toBe(0),
      );
    });

    it("uses the overridden pageSize when computing pageCount", async () => {
      const { wrapper } = buildWrapper("/");
      // 100 items, override to size 20 -> 5 pages
      const queryFn = makeQueryFn(100);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Movies], queryFn),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);

      result.current.controls.setPageSize(20);

      await waitFor(() =>
        expect(result.current.paginationStatus.pageCount).toBe(5),
      );
    });
  });

  describe("query key shape", () => {
    it("includes QueryKeys.Range in the query key when not fetchAll", async () => {
      const { qc, wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(10);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Wanted], queryFn, false, false),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);

      const queries = qc.getQueryCache().findAll({
        queryKey: [QueryKeys.Wanted, QueryKeys.Range],
        exact: false,
      });
      expect(queries.length).toBeGreaterThan(0);
      const key = queries[0].queryKey as unknown[];
      expect(key).toContain(QueryKeys.Range);
    });

    it("uses { all: true } in the query key when fetchAll=true", async () => {
      const { qc, wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(10);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.History], queryFn, false, true),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);

      const queries = qc.getQueryCache().findAll({
        queryKey: [QueryKeys.History, QueryKeys.Range],
        exact: false,
      });
      expect(queries.length).toBeGreaterThan(0);
      const key = queries[0].queryKey as unknown[];
      // Last element should be { all: true }
      expect(key[key.length - 1]).toEqual({ all: true });
    });

    it("includes start and size in the query key for paginated mode", async () => {
      const { qc, wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(10);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Wanted], queryFn, false, false),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);

      const queries = qc.getQueryCache().findAll({
        queryKey: [QueryKeys.Wanted, QueryKeys.Range],
        exact: false,
      });
      expect(queries.length).toBeGreaterThan(0);
      const key = queries[0].queryKey as unknown[];
      // Should have a { start, size } object somewhere in the key
      const rangeArg = key.find(
        (part) =>
          typeof part === "object" &&
          part !== null &&
          "start" in (part as object),
      ) as { start: number; size: number } | undefined;
      expect(rangeArg).toBeDefined();
      expect(rangeArg?.start).toBe(0);
    });
  });

  describe("fetchAll mode", () => {
    it("passes start=0 and length=-1 to queryFn when fetchAll=true", async () => {
      const { wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(10);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Logs], queryFn, false, true),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);

      expect(queryFn).toHaveBeenCalledWith({ start: 0, length: -1 });
    });

    it("exposes fetchAll=true in paginationStatus", async () => {
      const { wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(5);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Logs], queryFn, false, true),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);
      expect(result.current.paginationStatus.fetchAll).toBe(true);
    });

    it("exposes fetchAll=false in paginationStatus when not in fetchAll mode", async () => {
      const { wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(5);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Logs], queryFn, false, false),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);
      expect(result.current.paginationStatus.fetchAll).toBe(false);
    });
  });

  describe("gotoPage control", () => {
    it("advances the page index when target is in bounds", async () => {
      const { wrapper } = buildWrapper("/");
      // 100 items / 25 per page = 4 pages (indices 0-3)
      const queryFn = makeQueryFn(100);

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Episodes], queryFn),
        { wrapper },
      );

      // Wait until pageCount is established so gotoPage's bounds check passes
      await waitFor(() =>
        expect(result.current.paginationStatus.pageCount).toBe(4),
      );

      result.current.controls.gotoPage(2);
      await waitFor(() => expect(result.current.paginationStatus.page).toBe(2));
    });

    it("ignores gotoPage when index is out of bounds (>= pageCount)", async () => {
      const { wrapper } = buildWrapper("/");
      const queryFn = makeQueryFn(25); // exactly 1 page at size 25

      const { result } = renderHook(
        () => usePaginationQuery([QueryKeys.Episodes], queryFn),
        { wrapper },
      );

      await waitFor(() => result.current.isSuccess);
      // pageCount=1, so index 1 is out of bounds
      result.current.controls.gotoPage(1);

      // page should remain 0
      await waitFor(() => expect(result.current.paginationStatus.page).toBe(0));
    });
  });
});
