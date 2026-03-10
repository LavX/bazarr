import { useEffect, useMemo } from "react";
import { useSearchParams } from "react-router";
import { UsePaginationQueryResult } from "@/apis/queries/hooks";
import SimpleTable, { SimpleTableProps } from "@/components/tables/SimpleTable";
import { LoadingProvider } from "@/contexts";
import { ScrollToTop } from "@/utilities";
import PageControl from "./PageControl";

const EMPTY_DATA: DataWrapperWithTotal<never> = { data: [], total: 0 };

type Props<T extends object> = Omit<SimpleTableProps<T>, "data"> & {
  query: UsePaginationQueryResult<T>;
  dataFilter?: (item: T) => boolean;
};

export default function QueryPageTable<T extends object>(props: Props<T>) {
  const { query, dataFilter, ...remain } = props;

  const data = query.data ?? EMPTY_DATA;
  const {
    paginationStatus: {
      page,
      pageCount: serverPageCount,
      totalCount: serverTotalCount,
      pageSize,
      isPageLoading,
      fetchAll,
    },
    controls: { gotoPage },
  } = query;

  const [searchParams, setSearchParams] = useSearchParams();

  useEffect(() => {
    ScrollToTop();
  }, [page]);

  // Apply client-side filter (if any)
  const filteredData = useMemo(() => {
    if (!dataFilter) return data.data;
    return data.data.filter(dataFilter);
  }, [data.data, dataFilter]);

  // When fetchAll: always paginate client-side (slice the filtered/full data)
  // When normal: show server-provided page data (optionally filtered within page)
  const displayData = useMemo(() => {
    if (fetchAll) {
      const start = page * pageSize;
      return filteredData.slice(start, start + pageSize);
    }
    return filteredData;
  }, [fetchAll, filteredData, page, pageSize]);

  // Calculate counts based on mode
  const effectiveTotalCount = fetchAll ? filteredData.length : serverTotalCount;
  const effectivePageCount = fetchAll
    ? Math.ceil(filteredData.length / pageSize)
    : serverPageCount;

  return (
    <LoadingProvider value={isPageLoading}>
      <SimpleTable {...remain} data={displayData}></SimpleTable>
      <PageControl
        count={effectivePageCount}
        index={page}
        size={pageSize}
        total={effectiveTotalCount}
        goto={(page) => {
          searchParams.set("page", (page + 1).toString());

          setSearchParams(searchParams, { replace: true });

          gotoPage(page);
        }}
      ></PageControl>
    </LoadingProvider>
  );
}
