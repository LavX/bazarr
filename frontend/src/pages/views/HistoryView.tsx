import { ReactNode } from "react";
import { Container } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { ColumnDef } from "@tanstack/react-table";
import { useAppTitle } from "@/apis/hooks/site";
import { UsePaginationQueryResult } from "@/apis/queries/hooks";
import { QueryPageTable } from "@/components";

interface Props<T extends History.Base> {
  name: string;
  query: UsePaginationQueryResult<T>;
  columns: ColumnDef<T>[];
  toolbar?: ReactNode;
  getRowId?: (row: T) => string;
}

function HistoryView<T extends History.Base = History.Base>({
  columns,
  name,
  query,
  toolbar,
  getRowId,
}: Props<T>) {
  useDocumentTitle(`${name} History - ${useAppTitle()}`);
  return (
    <Container fluid px={0}>
      {toolbar}
      <QueryPageTable
        tableStyles={{ emptyText: `Nothing Found in ${name} History` }}
        columns={columns}
        query={query}
        getRowId={getRowId}
      ></QueryPageTable>
    </Container>
  );
}

export default HistoryView;
