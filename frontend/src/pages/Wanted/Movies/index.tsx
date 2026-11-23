import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Link } from "react-router";
import { Anchor, Badge, Checkbox, Group } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import {
  useAudioLanguages,
  useMovieAction,
  useMovieSubtitleModification,
  useMovieWantedPagination,
} from "@/apis/hooks";
import { useSourceFilter } from "@/apis/hooks/mediaSources";
import { AudioList, ReleaseMismatchBadge } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import SourceBadge from "@/components/bazarr/SourceBadge";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import SourceFilter from "@/pages/views/SourceFilter";
import WantedView from "@/pages/views/WantedView";
import { BuildKey } from "@/utilities";
import tableStyles from "@/components/tables/BaseTable.module.scss";

const WantedMoviesView: FunctionComponent = () => {
  const { download } = useMovieSubtitleModification();

  const [search, setSearch] = useState("");
  const [audioLanguages, setAudioLanguages] = useState<string[]>([]);
  const [excludeLanguages, setExcludeLanguages] = useState<string[]>([]);
  const [tagFilter, setTagFilter] = useState<string[]>([]);
  const [missingLanguage, setMissingLanguage] = useState<string | null>(null);
  const [debouncedSearch] = useDebouncedValue(search, 300);

  const hasActiveFilter =
    debouncedSearch.length > 0 ||
    audioLanguages.length > 0 ||
    excludeLanguages.length > 0 ||
    tagFilter.length > 0 ||
    missingLanguage !== null;

  const { data: audioLangs = [] } = useAudioLanguages();
  const {
    labels,
    sourceKeys,
    arrActions,
    arrNames,
    defaultArr,
    selected,
    onChange,
  } = useSourceFilter("movie", "radarr");
  const query = useMovieWantedPagination(hasActiveFilter, sourceKeys);

  const langOptions = useMemo(
    () => audioLangs.map((l) => ({ value: l.code2, label: l.name })),
    [audioLangs],
  );

  // Build client-side filter function
  const dataFilter = useCallback(
    (item: Wanted.MovieRow) => {
      if (debouncedSearch) {
        const lowerSearch = debouncedSearch.toLowerCase();
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
      if (missingLanguage) {
        const hasMissing = item.missing_subtitles.some(
          (sub) => sub.code2 === missingLanguage,
        );
        if (!hasMissing) {
          return false;
        }
      }
      if (
        tagFilter.length > 0 &&
        !tagFilter.some((tag) => item.tags?.includes(tag))
      ) {
        return false;
      }
      return true;
    },
    [
      debouncedSearch,
      audioLanguages,
      excludeLanguages,
      missingLanguage,
      tagFilter,
    ],
  );

  const columns = useMemo<ColumnDef<Wanted.MovieRow>[]>(
    () => [
      {
        id: "selection",
        header: ({ table }) => {
          return (
            <Checkbox
              id="table-header-selection"
              disabled={
                !table.getRowModel().rows.some((row) => row.getCanSelect())
              }
              indeterminate={table.getIsSomeRowsSelected()}
              checked={table.getIsAllRowsSelected()}
              onChange={table.getToggleAllRowsSelectedHandler()}
            />
          );
        },
        cell: ({ row }) => {
          if (!row.getCanSelect()) return null;
          const { index, getIsSelected, getToggleSelectedHandler } = row;
          return (
            <Checkbox
              id={`table-cell-${index}`}
              checked={getIsSelected()}
              onChange={getToggleSelectedHandler()}
              onClick={getToggleSelectedHandler()}
            />
          );
        },
      },
      {
        header: "Name",
        accessorKey: "title",
        cell: ({ row: { original: item } }) => {
          const { id, title, release_mismatch: releaseMismatch } = item;
          if (item.origin === "source") return title;
          const target = `/movies/${id}`;
          return (
            <Group gap="xs" wrap="nowrap">
              <Anchor
                className={`table-primary ${tableStyles.episodeTitle}`}
                component={Link}
                to={target}
              >
                {title}
              </Anchor>
              {releaseMismatch ? <ReleaseMismatchBadge /> : null}
            </Group>
          );
        },
      },
      ...(labels.size > 1
        ? [
            {
              id: "source",
              header: "Source",
              cell: ({ row: { original } }) => (
                <SourceBadge
                  row={original}
                  labels={labels}
                  arrNames={arrNames}
                  defaultArr={defaultArr}
                />
              ),
            } as ColumnDef<Wanted.MovieRow>,
          ]
        : []),
      {
        header: "Audio",
        accessorKey: "audio_language",
        cell: ({
          row: {
            original: { audio_language: audioLanguage },
          },
        }) => {
          return <AudioList audios={audioLanguage}></AudioList>;
        },
      },
      {
        header: "Missing",
        accessorKey: "missing_subtitles",
        cell: ({ row: { original } }) => {
          const { missing_subtitles: missingSubtitles } = original;
          if (original.origin === "source") {
            return (
              <Group gap="sm">
                {missingSubtitles.map((item, idx) => (
                  <Badge key={BuildKey(idx, item.code2)}>
                    <Language.Text value={item} />
                  </Badge>
                ))}
              </Group>
            );
          }
          return (
            <Group gap="sm">
              {missingSubtitles.map((item, idx) => (
                <Badge
                  color={download.isPending ? "gray" : undefined}
                  leftSection={<FontAwesomeIcon icon={faSearch} />}
                  key={BuildKey(idx, item.code2)}
                  style={{ cursor: "pointer" }}
                  onClick={async () => {
                    await download.mutateAsync({
                      radarrId: original.radarrId,
                      arrInstanceId: original.arr_instance_id,
                      form: {
                        language: item.code2,
                        hi: item.hi,
                        forced: item.forced,
                      },
                    });
                  }}
                >
                  <Language.Text value={item}></Language.Text>
                </Badge>
              ))}
            </Group>
          );
        },
      },
    ],
    [download, labels, arrNames, defaultArr],
  );

  const getWantedItem = useCallback(
    (row: Wanted.MovieRow): WantedItem | null => {
      if (row.origin === "source") return null;
      return {
        type: "movie",
        radarrId: row.radarrId,
        arrInstanceId: row.arr_instance_id,
        title: row.title,
      };
    },
    [],
  );

  const { mutateAsync } = useMovieAction();

  return (
    <WantedView
      name="Movies"
      columns={columns}
      query={query}
      arrActions={arrActions}
      sourceFilter={
        <SourceFilter labels={labels} value={selected} onChange={onChange} />
      }
      searchValue={search}
      onSearchChange={setSearch}
      audioLanguages={audioLanguages}
      onAudioLanguagesChange={setAudioLanguages}
      excludeLanguages={excludeLanguages}
      onExcludeLanguagesChange={setExcludeLanguages}
      tagValues={tagFilter}
      onTagValuesChange={setTagFilter}
      missingLanguage={missingLanguage ?? undefined}
      onMissingLanguageChange={setMissingLanguage}
      langOptions={langOptions}
      missingLangOptions={langOptions}
      dataFilter={hasActiveFilter ? dataFilter : undefined}
      searchAll={async () => {
        await mutateAsync({ action: "search-wanted" });
      }}
      scanAll={async () => {
        await mutateAsync({ action: "scan-wanted" });
      }}
      getWantedItem={getWantedItem}
    />
  );
};

export default WantedMoviesView;
