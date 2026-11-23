/* eslint-disable camelcase */
import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Link } from "react-router";
import { Anchor, Badge, Checkbox, Container, Group, Text } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { useAudioLanguages, useLanguages } from "@/apis/hooks/languages";
import {
  sportsActivityLink,
  sportsRowOwner,
  SportsWantedRow,
  useSportsAction,
  useSportsActivityScope,
  useSportsAvailability,
  useSportsSourceWanted,
  useSportsWantedPagination,
} from "@/apis/hooks/sports";
import { useBatchAction } from "@/apis/hooks/subtitles";
import { AudioList, ReleaseMismatchBadge } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import SourceBadge from "@/components/bazarr/SourceBadge";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import { SportsSearchModal } from "@/components/modals/SportsSearchModal";
import { useModals } from "@/modules/modals";
import { SourceManualSearchModal } from "@/pages/SourceDetail/forms";
import SourceFilter from "@/pages/views/SourceFilter";
import WantedView from "@/pages/views/WantedView";
import { BuildKey } from "@/utilities";
import { buildSubtitleLanguageKey } from "@/utilities/subtitles";
import tableStyles from "@/components/tables/BaseTable.module.scss";

// Built on the same WantedView as Series and Movies. Sports used to share one
// component with History and Blacklist, switched by a `kind` prop, rendering a
// raw Mantine table that reached none of the shared filtering, paging or
// styling the other two media types get.
// One badge, one language, downloaded automatically, which is exactly what
// the Episodes and Movies wanted pages do. This used to open a manual search
// modal instead, because the event-wide /automatic action calls search_event
// with language=None and so searches, and may download, every missing language
// on the event rather than the one the user picked. That endpoint takes a
// single language now, so sports behaves like the other two media types.
// Right-clicking still opens the manual search for the same language, for the
// cases where the automatic pick is not the wanted one.
//
// Its own component because it needs hooks, and a hook object in the columns
// useMemo deps rebuilds every column on each render.
const MissingLanguages: FunctionComponent<{ row: SportsWantedRow }> = ({
  row,
}) => {
  const modals = useModals();
  const action = useSportsAction();
  const manual = (item: Subtitle) => {
    if (row.origin === "source") {
      if (row.source_library_id === undefined) return;
      modals.openContextModal(SourceManualSearchModal, {
        item: {
          id: row.id,
          kind: "sports_event",
          source_id: row.source_id,
          library_id: row.source_library_id,
          group_id: row.source_group_id ?? null,
          path: row.path,
          scene_name: row.sceneName ?? null,
          arr_shadow: false,
        },
      });
    } else {
      modals.openContextModal(SportsSearchModal, {
        item: row,
        language: item.code2,
        hi: item.hi,
        forced: item.forced,
      });
    }
  };
  return (
    <Group gap="sm">
      {row.missing_subtitles.map((item, idx) => (
        <Badge
          color={action.isPending ? "gray" : undefined}
          leftSection={<FontAwesomeIcon icon={faSearch} />}
          key={BuildKey(idx, item.code2)}
          style={{ cursor: "pointer" }}
          onClick={() =>
            row.origin === "source"
              ? manual(item)
              : action.mutate({
                  path: `/events/${row.id}/automatic`,
                  owner: row.arr_instance_id,
                  // The full key, not the bare code. The automatic path matches
                  // this against the profile's missing list, which stores the
                  // variant ("hu:hi"), so a bare "hu" is refused as no eligible
                  // language and the click downloads nothing.
                  language: buildSubtitleLanguageKey(item),
                })
          }
          onContextMenu={(event) => {
            event.preventDefault();
            manual(item);
          }}
        >
          <Language.Text value={item}></Language.Text>
        </Badge>
      ))}
    </Group>
  );
};

const WantedSportsView: FunctionComponent = () => {
  const { activity, arrEnabled, sourceActivity, sourceSearchable, isLoading } =
    useSportsAvailability();
  const scope = useSportsActivityScope();
  const { owner, includeSources, sourceKeys, labels } = scope;
  const sourceWanted = useSportsSourceWanted();
  const sourceSelected = scope.selected?.startsWith("source:") === true;

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

  // The same library-wide audio-language catalogue the Series and Movies
  // wanted pages use, sourced from every media type's indexed files rather
  // than from the currently loaded page. The sports API stores the
  // ffprobe-derived audio track NAMES ("Hungarian"), while the filters and
  // the Audio column speak code2, so both directions are mapped through this
  // catalogue: names become the codes the rows carry, and the column turns
  // those back into readable names.
  const { data: audioLangs = [] } = useAudioLanguages(
    arrEnabled || sourceSearchable,
    sourceSearchable,
  );
  const { data: languages } = useLanguages(false, false, sourceSearchable);
  const langOptions = useMemo(
    () => audioLangs.map((l) => ({ value: l.code2, label: l.name })),
    [audioLangs],
  );
  const missingLangOptions = useMemo(() => {
    if (!sourceSearchable) return langOptions;
    const options = new Map(
      langOptions.map((option) => [option.value, option.label]),
    );
    for (const language of languages ?? []) {
      if (language.enabled) options.set(language.code2, language.name);
    }
    return [...options].map(([value, label]) => ({ value, label }));
  }, [languages, langOptions, sourceSearchable]);
  const nameToCode = useMemo(() => {
    const map = new Map<string, string>();
    for (const lang of audioLangs) map.set(lang.name, lang.code2);
    return map;
  }, [audioLangs]);
  const codeToName = useMemo(() => {
    const map = new Map<string, string>();
    for (const lang of audioLangs) map.set(lang.code2, lang.name);
    return map;
  }, [audioLangs]);

  const query = useSportsWantedPagination(
    { owner, includeSources, sourceKeys },
    hasActiveFilter,
    nameToCode,
  );
  const run = useSportsAction();
  const batch = useBatchAction();
  const { instances } = useSportsAvailability();
  const instanceIds = useMemo(
    () => instances.map((instance) => instance.id),
    [instances],
  );
  const {
    multiInstance,
    nameById: instanceNameById,
    defaultId: instanceDefaultId,
  } = useArrInstanceLabels("sportarr");

  const dataFilter = useCallback(
    (item: SportsWantedRow) => {
      if (
        debouncedSearch &&
        !item.title.toLowerCase().includes(debouncedSearch.toLowerCase())
      ) {
        return false;
      }
      if (audioLanguages.length > 0) {
        const itemLangs = item.audio_language ?? [];
        const hasMatchingLang = itemLangs.some((code) =>
          audioLanguages.includes(code),
        );
        if (!hasMatchingLang) {
          return false;
        }
      }
      if (excludeLanguages.length > 0) {
        const itemLangs = item.audio_language ?? [];
        const hasExcludedLang = itemLangs.some((code) =>
          excludeLanguages.includes(code),
        );
        if (hasExcludedLang) {
          return false;
        }
      }
      if (
        missingLanguage &&
        !item.missing_subtitles.some((sub) => sub.code2 === missingLanguage)
      ) {
        return false;
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

  const columns = useMemo<ColumnDef<SportsWantedRow>[]>(
    () => [
      // Without this the shared WantedView still renders its "Mass Translate"
      // button, but nothing can ever be selected, so the button sits disabled
      // forever and getWantedItem below is unreachable. Episodes and Movies
      // both declare it; sports was the only wanted page missing it.
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
        cell: ({
          row: { index, getIsSelected, getToggleSelectedHandler, getCanSelect },
        }) => {
          if (!getCanSelect()) return null;
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
        cell: ({ row: { original } }) => {
          const link = sportsActivityLink(original);
          return (
            <Group gap="xs" wrap="nowrap">
              {link ? (
                <Anchor
                  className={`table-primary ${tableStyles.episodeTitle}`}
                  component={Link}
                  to={link}
                >
                  {original.title}
                </Anchor>
              ) : (
                <Text size="sm">{original.title}</Text>
              )}
              {original.release_mismatch ? <ReleaseMismatchBadge /> : null}
            </Group>
          );
        },
      },
      ...(multiInstance || sourceActivity
        ? [
            {
              id: "instance",
              header: sourceActivity ? "Source" : "Instance",
              cell: ({ row: { original } }) => (
                <SourceBadge
                  row={sportsRowOwner(original)}
                  labels={labels}
                  defaultArr={instanceDefaultId}
                  arrNames={instanceNameById}
                />
              ),
            } as ColumnDef<SportsWantedRow>,
          ]
        : []),
      {
        header: "Audio",
        accessorKey: "audio_language",
        cell: ({ row: { original } }) => (
          // Rows carry code2s; the catalogue turns them back into the real
          // language names, the way the Series and Movies rows already arrive.
          // An unknown code is shown as itself rather than dropped.
          <AudioList
            audios={(original.audio_language ?? []).map((code) => ({
              code2: code,
              name: codeToName.get(code) ?? code,
            }))}
          ></AudioList>
        ),
      },
      {
        header: "Missing",
        accessorKey: "missing_subtitles",
        cell: ({ row: { original } }) => <MissingLanguages row={original} />,
      },
    ],
    [
      multiInstance,
      sourceActivity,
      labels,
      instanceNameById,
      instanceDefaultId,
      codeToName,
    ],
  );

  const getWantedItem = useCallback(
    (row: SportsWantedRow): WantedItem | null =>
      row.origin === "source"
        ? null
        : {
            type: "sports",
            sportsEventId: row.id,
            arrInstanceId: row.arr_instance_id,
            title: row.title,
          },
    [],
  );

  const searchAll = useCallback(async () => {
    // One call per owner: the sports wanted job takes a single instance,
    // unlike the series and movies actions that sweep everything at once.
    const sources = (
      sourceKeys.length ? sourceKeys : [...labels.keys()]
    ).filter((key) => key.startsWith("source:") && key !== "source:removed");
    const owners = sourceSelected ? [] : owner ? [owner] : instanceIds;
    await Promise.all([
      ...owners.map((id) => run.mutateAsync({ path: "/wanted", owner: id })),
      ...(sources.length && owner === undefined
        ? [sourceWanted.mutateAsync(sources)]
        : []),
    ]);
  }, [
    owner,
    instanceIds,
    run,
    labels,
    sourceSelected,
    sourceKeys,
    sourceWanted,
  ]);

  const scanAll = useCallback(async () => {
    // Re-index from disk, which is what Scan All means on the other two pages.
    // The batch scan-disk arm runs the whole-library rescan once per affected
    // owner, so one representative wanted row per owner is enough: an event on
    // the second or later wanted page is scanned too.
    const items = (owner ? [owner] : instanceIds).map((id) => ({
      type: "sports" as const,
      arr_instance_id: id,
    }));
    if (items.length === 0) return;
    await batch.mutateAsync({ items, action: "scan-disk" });
  }, [owner, instanceIds, batch]);

  if (isLoading) return null;
  if (!activity) {
    return (
      <Container px={0} fluid>
        <Text p="md">
          Enable a Sportarr instance in Connections to view sports.
        </Text>
      </Container>
    );
  }

  return (
    <WantedView
      name="Sports"
      columns={columns}
      query={query}
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
      missingLangOptions={missingLangOptions}
      dataFilter={hasActiveFilter ? dataFilter : undefined}
      searchAll={searchAll}
      scanAll={scanAll}
      arrActions={arrEnabled && !sourceSelected}
      sourceFilter={
        <SourceFilter
          labels={labels}
          value={scope.selected}
          onChange={scope.onChange}
          includeSources={sourceActivity}
          showSingleSource={sourceActivity}
        />
      }
      getWantedItem={getWantedItem}
    />
  );
};

export default WantedSportsView;
