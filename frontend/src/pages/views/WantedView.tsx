import { useCallback, useState } from "react";
import { Container, Group } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faLanguage, faSearch } from "@fortawesome/free-solid-svg-icons";
import { ColumnDef, Row } from "@tanstack/react-table";
import { useIsAnyActionRunning } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { UsePaginationQueryResult } from "@/apis/queries/hooks";
import { QueryPageTable, Toolbox } from "@/components";
import { MassTranslateModal } from "@/components/forms/MassTranslateForm";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import { useModals } from "@/modules/modals";

interface Props<T extends Wanted.Base> {
  name: string;
  columns: ColumnDef<T>[];
  query: UsePaginationQueryResult<T>;
  searchAll: () => Promise<void>;
  getWantedItem: (row: T) => WantedItem;
}

function WantedView<T extends Wanted.Base>({
  name,
  columns,
  query,
  searchAll,
  getWantedItem,
}: Props<T>) {
  const dataCount = query.paginationStatus.totalCount;
  const hasTask = useIsAnyActionRunning();
  const modals = useModals();
  const [selectedRows, setSelectedRows] = useState<Row<T>[]>([]);

  useDocumentTitle(`Wanted ${name} - ${useInstanceName()}`);

  const handleRowSelectionChanged = useCallback((rows: Row<T>[]) => {
    setSelectedRows(rows);
  }, []);

  const handleMassTranslate = useCallback(() => {
    if (selectedRows.length === 0) return;

    const items: WantedItem[] = selectedRows.map((row) =>
      getWantedItem(row.original),
    );

    modals.openContextModal(MassTranslateModal, {
      items,
      onComplete: () => {
        // Reset selection after completion
        setSelectedRows([]);
      },
    });
  }, [selectedRows, getWantedItem, modals]);

  return (
    <Container fluid px={0}>
      <Toolbox>
        <Group gap="xs">
          <Toolbox.Button
            disabled={hasTask || dataCount === 0}
            onClick={searchAll}
            icon={faSearch}
          >
            Search All
          </Toolbox.Button>
          <Toolbox.Button
            disabled={hasTask || selectedRows.length === 0}
            onClick={handleMassTranslate}
            icon={faLanguage}
          >
            {`Mass Translate (${selectedRows.length})`}
          </Toolbox.Button>
        </Group>
      </Toolbox>
      <QueryPageTable
        tableStyles={{ emptyText: `No missing ${name} subtitles` }}
        query={query}
        columns={columns}
        enableRowSelection
        onRowSelectionChanged={handleRowSelectionChanged}
      ></QueryPageTable>
    </Container>
  );
}

export default WantedView;
