/* eslint-disable camelcase */
import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Anchor, Badge, Container, Group, Text } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import {
  SportsWantedRow,
  useSportsAction,
  useSportsAvailability,
  useSportsWantedPagination,
} from "@/apis/hooks/sports";
import { useBatchAction } from "@/apis/hooks/subtitles";
import { InstanceBadge, ReleaseMismatchBadge } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import WantedView from "@/pages/views/WantedView";
import { BuildKey } from "@/utilities";
import tableStyles from "@/components/tables/BaseTable.module.scss";

// Built on the same WantedView as Series and Movies. Sports used to share one
// component with History and Blacklist, switched by a `kind` prop, rendering a
// raw Mantine table that reached none of the shared filtering, paging or
// styling the other two media types get.
const WantedSportsView: FunctionComponent = () => {
  const { enabled, isLoading } = useSportsAvailability();
  const [params] = useSearchParams();
  const owner = params.get("instance")
    ? Number(params.get("instance"))
    : undefined;

  const [search, setSearch] = useState("");
  const [missingLanguage, setMissingLanguage] = useState<string | null>(null);
  const [debouncedSearch] = useDebouncedValue(search, 300);

  const hasActiveFilter =
    debouncedSearch.length > 0 || missingLanguage !== null;

  const query = useSportsWantedPagination({ owner }, hasActiveFilter);
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
      if (
        missingLanguage &&
        !item.missing_subtitles.some((sub) => sub.code2 === missingLanguage)
      ) {
        return false;
      }
      return true;
    },
    [debouncedSearch, missingLanguage],
  );

  // Derived from what is actually missing rather than from the enabled
  // languages list: a sports library commonly wants a handful of the languages
  // the install has turned on, and offering the rest filters to nothing.
  const missingLangOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const row of query.data?.data ?? []) {
      for (const sub of row.missing_subtitles) {
        seen.set(sub.code2, sub.name);
      }
    }
    return Array.from(seen, ([value, label]) => ({ value, label }));
  }, [query.data]);

  const columns = useMemo<ColumnDef<SportsWantedRow>[]>(
    () => [
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
        header: "Missing",
        accessorKey: "missing_subtitles",
        cell: ({ row: { original } }) => (
          <Group gap="sm">
            {original.missing_subtitles.map((item, idx) => (
              <Badge
                color={run.isPending ? "gray" : undefined}
                leftSection={<FontAwesomeIcon icon={faSearch} />}
                key={BuildKey(idx, item.code2)}
                style={{ cursor: "pointer" }}
                onClick={() =>
                  run.mutate({
                    path: `/events/${original.id}/automatic`,
                    owner: original.arr_instance_id,
                  })
                }
              >
                <Language.Text value={item}></Language.Text>
              </Badge>
            ))}
          </Group>
        ),
      },
    ],
    [run, multiInstance, instanceNameById, instanceDefaultId],
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
    // There is no whole-library sports rescan endpoint, so this scans exactly
    // the events currently listed as wanted.
    const items = (query.data?.data ?? []).map((row) => ({
      type: "sports" as const,
      sportsEventId: row.id,
      arr_instance_id: row.arr_instance_id,
    }));
    if (items.length === 0) return;
    await batch.mutateAsync({ items, action: "scan-disk" });
  }, [query.data, batch]);

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
      missingLanguage={missingLanguage ?? undefined}
      onMissingLanguageChange={setMissingLanguage}
      missingLangOptions={missingLangOptions}
      dataFilter={hasActiveFilter ? dataFilter : undefined}
      searchAll={searchAll}
      scanAll={scanAll}
      getWantedItem={getWantedItem}
    />
  );
};

export default WantedSportsView;
