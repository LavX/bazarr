import { useCallback, useMemo } from "react";
import { useNavigate } from "react-router";
import { MultiSelect, TextInput } from "@mantine/core";
import { faList, faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { UsePaginationQueryResult } from "@/apis/queries/hooks";
import { QueryPageTable, Toolbox } from "@/components";
import { normalizeAudioLanguage } from "@/utilities/languages";

interface Props<T extends Item.Base = Item.Base> {
  query: UsePaginationQueryResult<T>;
  columns: ColumnDef<T>[];
  searchValue?: string;
  onSearchChange?: (value: string) => void;
  audioLanguages?: string[];
  onAudioLanguagesChange?: (values: string[]) => void;
  excludeLanguages?: string[];
  onExcludeLanguagesChange?: (values: string[]) => void;
}

function ItemView<T extends Item.Base>({
  query,
  columns,
  searchValue = "",
  onSearchChange,
  audioLanguages = [],
  onAudioLanguagesChange,
  excludeLanguages = [],
  onExcludeLanguagesChange,
}: Props<T>) {
  const navigate = useNavigate();

  // Extract unique audio language options from actual data
  const langOptions = useMemo(() => {
    const allData = query.data?.data ?? [];
    const langMap = new Map<string, string>();
    for (const item of allData) {
      for (const lang of item.audio_language ?? []) {
        if (lang.code2 && !langMap.has(lang.code2)) {
          langMap.set(lang.code2, normalizeAudioLanguage(lang.name));
        }
      }
    }
    return Array.from(langMap.entries())
      .map(([code, name]) => ({ value: code, label: name }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [query.data?.data]);

  const dataFilter = useCallback(
    (item: T) => {
      if (searchValue) {
        const lowerSearch = searchValue.toLowerCase();
        if (!item.title.toLowerCase().includes(lowerSearch)) {
          return false;
        }
      }
      if (audioLanguages.length > 0) {
        const itemLangs = item.audio_language ?? [];
        const hasMatchingLang = itemLangs.some((lang) =>
          audioLanguages.includes(lang.code2),
        );
        if (!hasMatchingLang) {
          return false;
        }
      }
      if (excludeLanguages.length > 0) {
        const itemLangs = item.audio_language ?? [];
        const hasExcludedLang = itemLangs.some((lang) =>
          excludeLanguages.includes(lang.code2),
        );
        if (hasExcludedLang) {
          return false;
        }
      }
      return true;
    },
    [searchValue, audioLanguages, excludeLanguages],
  );

  const hasActiveFilter =
    searchValue.length > 0 ||
    audioLanguages.length > 0 ||
    excludeLanguages.length > 0;

  return (
    <>
      <Toolbox>
        <Toolbox.Button
          disabled={query.paginationStatus.totalCount === 0 && !hasActiveFilter}
          icon={faList}
          onClick={() => navigate("edit")}
        >
          Mass Edit
        </Toolbox.Button>
        {onAudioLanguagesChange !== undefined && langOptions.length > 0 && (
          <MultiSelect
            placeholder="Include audio..."
            data={langOptions}
            value={audioLanguages}
            onChange={onAudioLanguagesChange}
            searchable
            clearable
            size="sm"
            w={200}
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
            w={200}
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
            w={220}
          />
        )}
      </Toolbox>
      <QueryPageTable
        columns={columns}
        query={query}
        dataFilter={hasActiveFilter ? dataFilter : undefined}
        tableStyles={{ emptyText: "No items found" }}
      ></QueryPageTable>
    </>
  );
}

export default ItemView;
