import { useCallback, useMemo, useState } from "react";
import {
  ActionIcon,
  Badge,
  Box,
  Collapse,
  Divider,
  Group,
  Indicator,
  MultiSelect,
  Paper,
  Select,
  Stack,
  Text,
  TextInput,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faEraser,
  faFilter,
  faLanguage,
  faSearch,
  faTimes,
  faVolumeUp,
  faVolumeXmark,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef, Row } from "@tanstack/react-table";
import { useIsAnyActionRunning } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { UsePaginationQueryResult } from "@/apis/queries/hooks";
import { QueryPageTable, Toolbox } from "@/components";
import { MassTranslateModal } from "@/components/forms/MassTranslateForm";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import { useModals } from "@/modules/modals";

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
  const [filtersOpen, setFiltersOpen] = useState(false);

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

  // Compute active filter count and chips
  const activeFilterCount = useMemo(() => {
    let count = 0;
    if (audioLanguages.length > 0) count++;
    if (excludeLanguages.length > 0) count++;
    if (missingLanguage) count++;
    if (searchValue.length > 0) count++;
    return count;
  }, [audioLanguages, excludeLanguages, missingLanguage, searchValue]);

  const activeFilterChips = useMemo(() => {
    const chips: {
      key: string;
      label: string;
      color: string;
      onRemove: () => void;
    }[] = [];

    if (searchValue.length > 0 && onSearchChange) {
      chips.push({
        key: "search",
        label: `Title: "${searchValue}"`,
        color: "blue",
        onRemove: () => onSearchChange(""),
      });
    }

    if (audioLanguages.length > 0 && onAudioLanguagesChange) {
      const names = audioLanguages
        .map((code) => langOptions.find((l) => l.value === code)?.label ?? code)
        .join(", ");
      chips.push({
        key: "include-audio",
        label: `Include audio: ${names}`,
        color: "teal",
        onRemove: () => onAudioLanguagesChange([]),
      });
    }

    if (excludeLanguages.length > 0 && onExcludeLanguagesChange) {
      const names = excludeLanguages
        .map((code) => langOptions.find((l) => l.value === code)?.label ?? code)
        .join(", ");
      chips.push({
        key: "exclude-audio",
        label: `Exclude audio: ${names}`,
        color: "red",
        onRemove: () => onExcludeLanguagesChange([]),
      });
    }

    if (missingLanguage && onMissingLanguageChange) {
      const name =
        missingLangOptions.find((l) => l.value === missingLanguage)?.label ??
        missingLanguage;
      chips.push({
        key: "missing-lang",
        label: `Missing subtitle: ${name}`,
        color: "orange",
        onRemove: () => onMissingLanguageChange(null),
      });
    }

    return chips;
  }, [
    searchValue,
    audioLanguages,
    excludeLanguages,
    missingLanguage,
    langOptions,
    missingLangOptions,
    onSearchChange,
    onAudioLanguagesChange,
    onExcludeLanguagesChange,
    onMissingLanguageChange,
  ]);

  const clearAllFilters = useCallback(() => {
    onSearchChange?.("");
    onAudioLanguagesChange?.([]);
    onExcludeLanguagesChange?.([]);
    onMissingLanguageChange?.(null);
  }, [
    onSearchChange,
    onAudioLanguagesChange,
    onExcludeLanguagesChange,
    onMissingLanguageChange,
  ]);

  const hasAnyFilterControl =
    onAudioLanguagesChange !== undefined ||
    onExcludeLanguagesChange !== undefined ||
    onMissingLanguageChange !== undefined;

  return (
    <Stack gap={0}>
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
          {hasAnyFilterControl && (
            <Tooltip
              label={filtersOpen ? "Hide filters" : "Show filters"}
              position="bottom"
              withArrow
            >
              <Indicator
                label={activeFilterCount > 0 ? activeFilterCount : undefined}
                size={16}
                offset={4}
                color="blue"
                disabled={activeFilterCount === 0}
              >
                <ActionIcon
                  variant={filtersOpen ? "filled" : "subtle"}
                  color={activeFilterCount > 0 ? "blue" : "gray"}
                  size="lg"
                  onClick={() => setFiltersOpen((v) => !v)}
                  aria-label="Toggle filters"
                >
                  <FontAwesomeIcon icon={faFilter} />
                </ActionIcon>
              </Indicator>
            </Tooltip>
          )}
          {onSearchChange !== undefined && (
            <TextInput
              placeholder="Search by title..."
              leftSection={
                <FontAwesomeIcon icon={faSearch} size="sm" opacity={0.5} />
              }
              rightSection={
                searchValue.length > 0 ? (
                  <UnstyledButton
                    onClick={() => onSearchChange("")}
                    style={{ display: "flex", alignItems: "center" }}
                    aria-label="Clear search"
                  >
                    <FontAwesomeIcon icon={faTimes} size="sm" opacity={0.5} />
                  </UnstyledButton>
                ) : undefined
              }
              value={searchValue}
              onChange={(e) => onSearchChange(e.currentTarget.value)}
              size="sm"
              w={220}
              styles={{
                input: {
                  transition: "border-color 150ms ease",
                },
              }}
            />
          )}
        </Group>
      </Toolbox>

      {/* Active filter pills */}
      {activeFilterChips.length > 0 && (
        <Box
          px="md"
          py={8}
          style={{
            borderBottom: "1px solid var(--mantine-color-default-border)",
          }}
        >
          <Group gap={8}>
            <Text size="xs" c="dimmed" fw={500}>
              Active filters:
            </Text>
            {activeFilterChips.map((chip) => (
              <Badge
                key={chip.key}
                color={chip.color}
                variant="light"
                size="sm"
                rightSection={
                  <UnstyledButton
                    onClick={chip.onRemove}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      cursor: "pointer",
                      marginLeft: 2,
                    }}
                    aria-label={`Remove filter: ${chip.label}`}
                  >
                    <FontAwesomeIcon icon={faTimes} size="xs" />
                  </UnstyledButton>
                }
                styles={{
                  root: {
                    paddingRight: 6,
                  },
                }}
              >
                {chip.label}
              </Badge>
            ))}
            <Divider orientation="vertical" />
            <UnstyledButton onClick={clearAllFilters}>
              <Group gap={4}>
                <FontAwesomeIcon icon={faEraser} size="xs" opacity={0.6} />
                <Text size="xs" c="dimmed" td="underline">
                  Clear all
                </Text>
              </Group>
            </UnstyledButton>
          </Group>
        </Box>
      )}

      {/* Collapsible filter panel */}
      <Collapse in={filtersOpen}>
        <Paper
          px="md"
          py="sm"
          radius={0}
          style={{
            borderBottom: "1px solid var(--mantine-color-default-border)",
            backgroundColor: "var(--mantine-color-body)",
          }}
        >
          <Group gap="lg" align="flex-end" wrap="wrap">
            {onAudioLanguagesChange !== undefined && langOptions.length > 0 && (
              <Box style={{ flex: "1 1 200px", maxWidth: 280 }}>
                <Group gap={6} mb={4}>
                  <FontAwesomeIcon icon={faVolumeUp} size="xs" opacity={0.6} />
                  <Text size="xs" fw={500} c="dimmed">
                    Include Audio Languages
                  </Text>
                </Group>
                <MultiSelect
                  placeholder="Select languages to include..."
                  data={langOptions}
                  value={audioLanguages}
                  onChange={onAudioLanguagesChange}
                  searchable
                  clearable
                  size="sm"
                  maxDropdownHeight={250}
                  styles={{
                    input: {
                      minHeight: 36,
                    },
                  }}
                />
              </Box>
            )}
            {onExcludeLanguagesChange !== undefined &&
              langOptions.length > 0 && (
                <Box style={{ flex: "1 1 200px", maxWidth: 280 }}>
                  <Group gap={6} mb={4}>
                    <FontAwesomeIcon
                      icon={faVolumeXmark}
                      size="xs"
                      opacity={0.6}
                    />
                    <Text size="xs" fw={500} c="dimmed">
                      Exclude Audio Languages
                    </Text>
                  </Group>
                  <MultiSelect
                    placeholder="Select languages to exclude..."
                    data={langOptions}
                    value={excludeLanguages}
                    onChange={onExcludeLanguagesChange}
                    searchable
                    clearable
                    size="sm"
                    maxDropdownHeight={250}
                    styles={{
                      input: {
                        minHeight: 36,
                      },
                    }}
                  />
                </Box>
              )}
            {onMissingLanguageChange !== undefined &&
              missingLangOptions.length > 0 && (
                <Box style={{ flex: "1 1 200px", maxWidth: 280 }}>
                  <Group gap={6} mb={4}>
                    <FontAwesomeIcon
                      icon={faLanguage}
                      size="xs"
                      opacity={0.6}
                    />
                    <Text size="xs" fw={500} c="dimmed">
                      Missing Subtitle Language
                    </Text>
                  </Group>
                  <Select
                    placeholder="Select a language..."
                    data={missingLangOptions}
                    value={missingLanguage ?? null}
                    onChange={onMissingLanguageChange}
                    searchable
                    clearable
                    size="sm"
                    maxDropdownHeight={250}
                  />
                </Box>
              )}
          </Group>
        </Paper>
      </Collapse>

      <QueryPageTable
        tableStyles={{ emptyText: `No missing ${name} subtitles` }}
        query={query}
        columns={columns}
        dataFilter={dataFilter}
        enableRowSelection
        onRowSelectionChanged={handleRowSelectionChanged}
      ></QueryPageTable>
    </Stack>
  );
}

export default WantedView;
