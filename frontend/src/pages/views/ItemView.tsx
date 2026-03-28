import { ReactNode, useCallback, useMemo, useState } from "react";
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
  Stack,
  Text,
  TextInput,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import {
  faEraser,
  faFilter,
  faSearch,
  faTimes,
  faVolumeUp,
  faVolumeXmark,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef, Row } from "@tanstack/react-table";
import { useAudioLanguages } from "@/apis/hooks";
import { UsePaginationQueryResult } from "@/apis/queries/hooks";
import { QueryPageTable, Toolbox } from "@/components";

interface Props<T extends Item.Base = Item.Base> {
  query: UsePaginationQueryResult<T>;
  columns: ColumnDef<T>[];
  searchValue?: string;
  onSearchChange?: (value: string) => void;
  audioLanguages?: string[];
  onAudioLanguagesChange?: (values: string[]) => void;
  excludeLanguages?: string[];
  onExcludeLanguagesChange?: (values: string[]) => void;
  enableRowSelection?: boolean;
  onSelectionChanged?: (selections: T[]) => void;
  selectionToolbar?: ReactNode;
  profileToolbar?: ReactNode;
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
  enableRowSelection,
  onSelectionChanged,
  selectionToolbar,
  profileToolbar,
}: Props<T>) {
  const { data: audioLangs = [] } = useAudioLanguages();
  const [filtersOpen, setFiltersOpen] = useState(false);

  const langOptions = useMemo(
    () => audioLangs.map((l) => ({ value: l.code2, label: l.name })),
    [audioLangs],
  );

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

  // Compute active filter count (excluding search which is always visible)
  const activeFilterCount = useMemo(() => {
    let count = 0;
    if (audioLanguages.length > 0) count++;
    if (excludeLanguages.length > 0) count++;
    if (searchValue.length > 0) count++;
    return count;
  }, [audioLanguages, excludeLanguages, searchValue]);

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

    return chips;
  }, [
    searchValue,
    audioLanguages,
    excludeLanguages,
    langOptions,
    onSearchChange,
    onAudioLanguagesChange,
    onExcludeLanguagesChange,
  ]);

  const clearAllFilters = useCallback(() => {
    onSearchChange?.("");
    onAudioLanguagesChange?.([]);
    onExcludeLanguagesChange?.([]);
  }, [onSearchChange, onAudioLanguagesChange, onExcludeLanguagesChange]);

  const hasAnyFilterControl =
    onAudioLanguagesChange !== undefined ||
    onExcludeLanguagesChange !== undefined;

  return (
    <Stack gap={0}>
      <Toolbox>
        <Group gap="xs">
          {selectionToolbar ? (
            <Box>{selectionToolbar}</Box>
          ) : null}
          {profileToolbar ? (
            <Box>{profileToolbar}</Box>
          ) : null}
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
                color="brand"
                disabled={activeFilterCount === 0}
              >
                <ActionIcon
                  variant="gradient"
                  gradient={{ from: "brand.5", to: "brand.6", deg: 135 }}
                  size="lg"
                  onClick={() => setFiltersOpen((v) => !v)}
                  aria-label="Toggle filters"
                  style={{ opacity: filtersOpen ? 1 : 0.9 }}
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
            borderBottom: "1px solid var(--bz-border-divider)",
          }}
        >
          <Group gap={8}>
            <Text size="xs" c="var(--bz-text-tertiary)" fw={500}>
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
                <Text size="xs" c="var(--bz-text-tertiary)" td="underline">
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
            borderBottom: "1px solid var(--bz-border-divider)",
            backgroundColor: "var(--bz-surface-base)",
          }}
        >
          <Group gap="lg" align="flex-end" wrap="wrap">
            {onAudioLanguagesChange !== undefined && langOptions.length > 0 && (
              <Box style={{ flex: "1 1 200px", maxWidth: 280 }}>
                <Group gap={6} mb={4}>
                  <FontAwesomeIcon icon={faVolumeUp} size="xs" opacity={0.6} />
                  <Text size="xs" fw={500} c="var(--bz-text-tertiary)">
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
                    <Text size="xs" fw={500} c="var(--bz-text-tertiary)">
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
          </Group>
        </Paper>
      </Collapse>

      <QueryPageTable
        columns={columns}
        query={query}
        dataFilter={hasActiveFilter ? dataFilter : undefined}
        tableStyles={{ emptyText: "No items found" }}
        enableRowSelection={enableRowSelection}
        onRowSelectionChanged={(rows: Row<T>[]) => {
          onSelectionChanged?.(rows.map((r) => r.original));
        }}
      ></QueryPageTable>
    </Stack>
  );
}

export default ItemView;
