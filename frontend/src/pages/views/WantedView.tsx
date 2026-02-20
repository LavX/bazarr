import { useCallback, useMemo, useState } from "react";
import {
  Container,
  Group,
  MultiSelect,
  Select,
  TextInput,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faLanguage, faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef, Row } from "@tanstack/react-table";
import { useIsAnyActionRunning } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { UsePaginationQueryResult } from "@/apis/queries/hooks";
import { QueryPageTable, Toolbox } from "@/components";
import { MassTranslateModal } from "@/components/forms/MassTranslateForm";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import { useModals } from "@/modules/modals";
import { normalizeAudioLanguage } from "@/utilities/languages";

interface LangOption {
  value: string;
  label: string;
}

interface Props<T extends Wanted.Base> {
  name: string;
  columns: ColumnDef<T>[];
  query: UsePaginationQueryResult<T>;
  searchValue?: string;
  onSearchChange?: (value: string) => void;
  audioLanguages?: string[];
  onAudioLanguagesChange?: (values: string[]) => void;
  excludeLanguages?: string[];
  onExcludeLanguagesChange?: (values: string[]) => void;
  missingLanguage?: string;
  onMissingLanguageChange?: (value: string | null) => void;
  langOptions?: LangOption[];
  missingLangOptions?: LangOption[];
  dataFilter?: (item: T) => boolean;
  searchAll: () => Promise<void>;
  getWantedItem: (row: T) => WantedItem;
}

function WantedView<T extends Wanted.Base>({
  name,
  columns,
  query,
  searchValue = "",
  onSearchChange,
  audioLanguages = [],
  onAudioLanguagesChange,
  excludeLanguages = [],
  onExcludeLanguagesChange,
  missingLanguage,
  onMissingLanguageChange,
  langOptions = [],
  missingLangOptions = [],
  dataFilter,
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
        <Group gap="xs">
          {onAudioLanguagesChange !== undefined && langOptions.length > 0 && (
            <MultiSelect
              placeholder="Include audio..."
              data={langOptions}
              value={audioLanguages}
              onChange={onAudioLanguagesChange}
              searchable
              clearable
              size="sm"
              w={180}
              maxDropdownHeight={300}
            />
          )}
          {onExcludeLanguagesChange !== undefined && langOptions.length > 0 && (
            <MultiSelect
              placeholder="Exclude audio..."
              data={langOptions}
              value={excludeLanguages}
              onChange={onExcludeLanguagesChange}
              searchable
              clearable
              size="sm"
              w={180}
              maxDropdownHeight={300}
            />
          )}
          {onMissingLanguageChange !== undefined &&
            missingLangOptions.length > 0 && (
              <Select
                placeholder="Missing subtitle..."
                data={missingLangOptions}
                value={missingLanguage ?? null}
                onChange={onMissingLanguageChange}
                searchable
                clearable
                size="sm"
                w={180}
                maxDropdownHeight={300}
              />
            )}
          {onSearchChange !== undefined && (
            <TextInput
              placeholder="Search by title..."
              leftSection={<FontAwesomeIcon icon={faSearch} />}
              value={searchValue}
              onChange={(e) => onSearchChange(e.currentTarget.value)}
              size="sm"
              w={200}
            />
          )}
        </Group>
      </Toolbox>
      <QueryPageTable
        tableStyles={{ emptyText: `No missing ${name} subtitles` }}
        query={query}
        columns={columns}
        dataFilter={dataFilter}
        enableRowSelection
        onRowSelectionChanged={handleRowSelectionChanged}
      ></QueryPageTable>
    </Container>
  );
}

export default WantedView;
