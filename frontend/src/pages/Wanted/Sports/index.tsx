/* eslint-disable camelcase */
import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Anchor, Badge, Checkbox, Container, Group, Text } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { useAudioLanguages } from "@/apis/hooks/languages";
import {
  SportsWantedRow,
  useSportsAction,
  useSportsAvailability,
  useSportsWantedPagination,
} from "@/apis/hooks/sports";
import { useBatchAction } from "@/apis/hooks/subtitles";
import {
  AudioList,
  InstanceBadge,
  ReleaseMismatchBadge,
} from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import { SportsSearchModal } from "@/components/modals/SportsSearchModal";
import { useModals } from "@/modules/modals";
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
  return (
    <Group gap="sm">
      {row.missing_subtitles.map((item, idx) => (
        <Badge
          color={action.isPending ? "gray" : undefined}
          leftSection={<FontAwesomeIcon icon={faSearch} />}
          key={BuildKey(idx, item.code2)}
          style={{ cursor: "pointer" }}
          onClick={() =>
            action.mutate({
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
            modals.openContextModal(SportsSearchModal, {
              item: row,
              language: item.code2,
              hi: item.hi,
              forced: item.forced,
            });
          }}
        >
          <Language.Text value={item}></Language.Text>
        </Badge>
      ))}
    </Group>
  );
};

const WantedSportsView: FunctionComponent = () => {
  const { enabled, isLoading } = useSportsAvailability();
  const [params] = useSearchParams();
  const owner = params.get("instance")
    ? Number(params.get("instance"))
    : undefined;

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

  // The same library-wide audio-language catalogue the Series and Movies
  // wanted pages use, sourced from every media type's indexed files rather
  // than from the currently loaded page. The sports API stores the
  // ffprobe-derived audio track NAMES ("Hungarian"), while the filters and
  // the Audio column speak code2, so both directions are mapped through this
  // catalogue: names become the codes the rows carry, and the column turns
  // those back into readable names.
  const { data: audioLangs = [] } = useAudioLanguages();
  const langOptions = useMemo(
    () => audioLangs.map((l) => ({ value: l.code2, label: l.name })),
    [audioLangs],
  );
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
    { owner },
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
      return true;
    },
    [debouncedSearch, audioLanguages, excludeLanguages, missingLanguage],
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
        cell: ({ row: { original } }) => (
          <Group gap="xs" wrap="nowrap">
            <Anchor
              className={`table-primary ${tableStyles.episodeTitle}`}
              component={Link}
              to={`/sports/${original.league_id}?instance=${original.arr_instance_id}`}
            >
              {original.title}
            </Anchor>
            {original.release_mismatch ? <ReleaseMismatchBadge /> : null}
          </Group>
        ),
      },
      ...(multiInstance
        ? [
            {
              id: "instance",
              header: "Instance",
              cell: ({ row: { original } }) => (
                <InstanceBadge
                  instanceId={original.arr_instance_id}
                  defaultId={instanceDefaultId}
                  nameById={instanceNameById}
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
    [multiInstance, instanceNameById, instanceDefaultId, codeToName],
  );

  const getWantedItem = useCallback(
    (row: SportsWantedRow): WantedItem => ({
      type: "sports",
      sportsEventId: row.id,
      arrInstanceId: row.arr_instance_id,
      title: row.title,
    }),
    [],
  );

  const searchAll = useCallback(async () => {
    // One call per owner: the sports wanted job takes a single instance,
    // unlike the series and movies actions that sweep everything at once.
    await Promise.all(
      (owner ? [owner] : instanceIds).map((id) =>
        run.mutateAsync({ path: "/wanted", owner: id }),
      ),
    );
  }, [owner, instanceIds, run]);

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
  if (!enabled) {
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
      missingLanguage={missingLanguage ?? undefined}
      onMissingLanguageChange={setMissingLanguage}
      langOptions={langOptions}
      missingLangOptions={langOptions}
      dataFilter={hasActiveFilter ? dataFilter : undefined}
      searchAll={searchAll}
      scanAll={scanAll}
      getWantedItem={getWantedItem}
    />
  );
};

export default WantedSportsView;
