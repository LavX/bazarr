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
import { AudioList } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import WantedView from "@/pages/views/WantedView";
import { BuildKey } from "@/utilities";

const WantedMoviesView: FunctionComponent = () => {
  const { download } = useMovieSubtitleModification();

  const [search, setSearch] = useState("");
  const [audioLanguages, setAudioLanguages] = useState<string[]>([]);
  const [excludeLanguages, setExcludeLanguages] = useState<string[]>([]);
  const [missingLanguage, setMissingLanguage] = useState<string | null>(null);
  const [debouncedSearch] = useDebouncedValue(search, 300);

  const hasActiveFilter =
    debouncedSearch.length > 0 ||
    audioLanguages.length > 0 ||
    excludeLanguages.length > 0 ||
    missingLanguage !== null;

  const { data: audioLangs = [] } = useAudioLanguages();
  const query = useMovieWantedPagination(hasActiveFilter);

  const langOptions = useMemo(
    () => audioLangs.map((l) => ({ value: l.code2, label: l.name })),
    [audioLangs],
  );

  // Build client-side filter function
  const dataFilter = useCallback(
    (item: Wanted.Movie) => {
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
      return true;
    },
    [debouncedSearch, audioLanguages, excludeLanguages, missingLanguage],
  );

  const columns = useMemo<ColumnDef<Wanted.Movie>[]>(
    () => [
      {
        id: "selection",
        header: ({ table }) => {
          return (
            <Checkbox
              id="table-header-selection"
              indeterminate={table.getIsSomeRowsSelected()}
              checked={table.getIsAllRowsSelected()}
              onChange={table.getToggleAllRowsSelectedHandler()}
            />
          );
        },
        cell: ({ row: { index, getIsSelected, getToggleSelectedHandler } }) => {
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
        cell: ({
          row: {
            original: { title, radarrId },
          },
        }) => {
          const target = `/movies/${radarrId}`;
          return (
            <Anchor component={Link} to={target}>
              {title}
            </Anchor>
          );
        },
      },
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
        cell: ({
          row: {
            original: { radarrId, missing_subtitles: missingSubtitles },
          },
        }) => {
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
                      radarrId,
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
    [download],
  );

  const getWantedItem = useCallback((row: Wanted.Movie): WantedItem => {
    return {
      type: "movie",
      radarrId: row.radarrId,
      title: row.title,
    };
  }, []);

  const { mutateAsync } = useMovieAction();

  return (
    <WantedView
      name="Movies"
      columns={columns}
      query={query}
      searchValue={search}
      onSearchChange={setSearch}
      audioLanguages={audioLanguages}
      onAudioLanguagesChange={setAudioLanguages}
      excludeLanguages={excludeLanguages}
      onExcludeLanguagesChange={setExcludeLanguages}
      missingLanguage={missingLanguage ?? undefined}
      onMissingLanguageChange={setMissingLanguage}
      langOptions={langOptions}
      missingLangOptions={langOptions}
      dataFilter={hasActiveFilter ? dataFilter : undefined}
      searchAll={() => mutateAsync({ action: "search-wanted" })}
      getWantedItem={getWantedItem}
    />
  );
};

export default WantedMoviesView;
