import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import {
  QueryKey,
  useQuery,
  useQueryClient,
  UseQueryResult,
} from "@tanstack/react-query";
import { GetItemId, useOnValueChange } from "@/utilities";
import { usePageSize } from "@/utilities/storage";
import { QueryKeys } from "./keys";

export type UsePaginationQueryResult<T extends object> = UseQueryResult<
  DataWrapperWithTotal<T>
> & {
  controls: {
    gotoPage: (index: number) => void;
    setPageSize: (size: number) => void;
  };
  paginationStatus: {
    isPageLoading: boolean;
    totalCount: number;
    pageSize: number;
    pageCount: number;
    page: number;
    fetchAll: boolean;
  };
};

export function usePaginationQuery<
  TObject extends object = object,
  TQueryKey extends QueryKey = QueryKey,
>(
  queryKey: TQueryKey,
  queryFn: RangeQuery<TObject>,
  cacheIndividual = true,
  fetchAll = false,
): UsePaginationQueryResult<TObject> {
  const client = useQueryClient();

  const [searchParams] = useSearchParams();

  // The URL always carries a 1-based page; clamp missing/0/negative/NaN values
  // to page 1 (index 0) so the first request never sends a negative or NaN
  // start (the out-of-bounds clamp effect only runs after the first response).
  const [page, setIndex] = useState(() => {
    const raw = Number(searchParams.get("page"));
    if (!Number.isFinite(raw) || raw < 1) return 0;
    return Math.floor(raw) - 1;
  });

  // The global setting is the default; a per-table selection (null = unset)
  // overrides it so the same size drives the fetch, page count and clamp.
  const globalPageSize = usePageSize();
  const [selectedPageSize, setSelectedPageSize] = useState<number | null>(null);
  const pageSize = selectedPageSize ?? globalPageSize;

  const setPageSize = useCallback((size: number) => {
    setSelectedPageSize(size);
    setIndex(0);
  }, []);

  // Reset to page 0 when switching between fetchAll modes
  const prevFetchAllRef = useRef(fetchAll);
  useEffect(() => {
    if (prevFetchAllRef.current !== fetchAll) {
      setIndex(0);
    }
    prevFetchAllRef.current = fetchAll;
  }, [fetchAll]);

  const start = fetchAll ? 0 : page * pageSize;
  const length = fetchAll ? -1 : pageSize;

  const results = useQuery({
    queryKey: fetchAll
      ? [...queryKey, QueryKeys.Range, { all: true }]
      : [...queryKey, QueryKeys.Range, { start, size: pageSize }],

    queryFn: () => {
      const param: Parameter.Range = {
        start,
        length,
      };
      return queryFn(param);
    },
  });

  const { data } = results;

  useEffect(() => {
    if (results.isSuccess && results.data && cacheIndividual) {
      results.data.data.forEach((item) => {
        const id = GetItemId(item);
        if (id) {
          client.setQueryData([...queryKey, id], item);
        }
      });
    }
  }, [
    results.isSuccess,
    results.data,
    client,
    cacheIndividual,
    queryKey,
    page,
  ]);

  const totalCount = data?.total ?? 0;
  const pageCount = fetchAll
    ? Math.ceil(totalCount / pageSize)
    : Math.ceil(totalCount / pageSize);

  const gotoPage = useCallback(
    (idx: number) => {
      if (idx >= 0 && idx < pageCount) {
        setIndex(idx);
      }
    },
    [pageCount],
  );

  const [isPageLoading, setIsPageLoading] = useState(false);

  useOnValueChange(page, () => {
    if (results.isFetching) {
      setIsPageLoading(true);
    }
  });

  useEffect(() => {
    if (!results.isFetching) {
      setIsPageLoading(false);
    }
  }, [results.isFetching]);

  // Reset page index if we out of bound
  useEffect(() => {
    if (pageCount === 0) return;

    if (page >= pageCount) {
      setIndex(pageCount - 1);
    } else if (page < 0) {
      setIndex(0);
    }
  }, [page, pageCount]);

  return {
    ...results,
    paginationStatus: {
      isPageLoading,
      totalCount,
      pageCount,
      pageSize,
      page,
      fetchAll,
    },
    controls: {
      gotoPage,
      setPageSize,
    },
  };
}
