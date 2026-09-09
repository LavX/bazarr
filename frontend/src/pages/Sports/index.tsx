import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Link } from "react-router";
import {
  Anchor,
  Badge,
  Checkbox,
  Container,
  Group,
  Progress,
  Text,
  Tooltip,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import { faBookmark, faSync } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { useInstanceName } from "@/apis/hooks/site";
import {
  SportsLeagueRow,
  useSportsAvailability,
  useSportsLeaguesPagination,
  useSportsProfile,
  useSyncSports,
} from "@/apis/hooks/sports";
import { Toolbox } from "@/components";
import { AudioList, InstanceBadge } from "@/components/bazarr";
import LanguageProfileName from "@/components/bazarr/LanguageProfile";
import { ChangeProfileModal } from "@/components/forms/ChangeProfileForm";
import { useModals } from "@/modules/modals";
import ItemView from "@/pages/views/ItemView";
import { LIBRARY_ROUTES } from "@/Router/mediaRoutes";

// Sports renders through the same ItemView table as Series and Movies. It used
// to be a bespoke grid of poster cards with its own instance select and its own
// pagination, which meant none of the shared filtering, row selection or batch
// tooling reached it and it looked nothing like the other two media types.
const Sports: FunctionComponent = () => {
  const { instances, enabled, isLoading } = useSportsAvailability();
  const modals = useModals();

  const [search, setSearch] = useState("");
  const [audioLanguages, setAudioLanguages] = useState<string[]>([]);
  const [excludeLanguages, setExcludeLanguages] = useState<string[]>([]);
  const [instanceFilter, setInstanceFilter] = useState<string[]>([]);
  const [selections, setSelections] = useState<SportsLeagueRow[]>([]);

  const {
    multiInstance,
    nameById: instanceNameById,
    defaultId: instanceDefaultId,
    options: instanceOptions,
  } = useArrInstanceLabels("sportarr");

  const query = useSportsLeaguesPagination();
  const assign = useSportsProfile();
  const sync = useSyncSports();

  const syncLeagues = useCallback(
    () => sync.mutateAsync(instances.map((instance) => instance.id)),
    [sync, instances],
  );

  // Applied one league at a time because the sports profile endpoint is scoped
  // to a single league and its owning instance, unlike the series bulk endpoint.
  const setProfiles = useCallback(
    (profileId: number | null) => {
      selections.forEach((league) =>
        assign.mutate({
          id: league.id,
          owner: league.arr_instance_id,
          profileId,
        }),
      );
    },
    [selections, assign],
  );

  const profileToolbar = useMemo(() => {
    if (selections.length === 0) return undefined;
    return (
      <Group gap="xs">
        <Toolbox.Button
          icon={faSync}
          onClick={() => {
            modals.openContextModal(ChangeProfileModal, {
              onSelect: setProfiles,
            });
          }}
        >
          Change Profile
        </Toolbox.Button>
      </Group>
    );
  }, [selections, modals, setProfiles]);

  const columns = useMemo<ColumnDef<SportsLeagueRow>[]>(
    () => [
      {
        id: "selection",
        header: ({ table }) => (
          <Checkbox
            aria-label="Select all"
            id="sports-select-all"
            indeterminate={table.getIsSomeRowsSelected()}
            checked={table.getIsAllRowsSelected()}
            onChange={table.getToggleAllRowsSelectedHandler()}
          />
        ),
        cell: ({ row }) => (
          <Checkbox
            aria-label={`Select ${row.original.title}`}
            id={`sports-select-${row.index}`}
            checked={row.getIsSelected()}
            onChange={row.getToggleSelectedHandler()}
          />
        ),
      },
      {
        id: "status",
        cell: ({ row: { original } }) => (
          <Tooltip
            label={
              original.monitored
                ? "Monitored in Sportarr"
                : "Unmonitored in Sportarr"
            }
          >
            <FontAwesomeIcon
              icon={original.monitored ? faBookmark : farBookmark}
            />
          </Tooltip>
        ),
      },
      {
        header: "Name",
        accessorKey: "title",
        cell: ({ row: { original } }) => (
          <Anchor
            className="table-primary"
            component={Link}
            to={`${LIBRARY_ROUTES.sportarr}/${original.id}?instance=${original.arr_instance_id}`}
          >
            {original.title}
          </Anchor>
        ),
      },
      {
        header: "Sport",
        accessorKey: "sport",
        cell: ({ row: { original } }) =>
          original.sport ? (
            <Badge variant="light">{original.sport}</Badge>
          ) : null,
      },
      // Owning Sportarr instance, shown only when more than one is configured,
      // matching how Series and Movies label their rows.
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
            } as ColumnDef<SportsLeagueRow>,
          ]
        : []),
      {
        header: "Audio",
        accessorKey: "audio_language",
        cell: ({
          row: {
            original: { audio_language: audioLanguage },
          },
        }) => <AudioList audios={audioLanguage}></AudioList>,
      },
      {
        header: "Languages Profile",
        accessorKey: "profileId",
        cell: ({ row: { original } }) => (
          <LanguageProfileName
            index={original.profileId}
            empty=""
          ></LanguageProfileName>
        ),
      },
      {
        header: "Events",
        accessorKey: "eventFileCount",
        cell: ({ row: { original } }) => {
          const { eventCount, eventFileCount, profileId, title } = original;
          const label = `${eventFileCount}/${eventCount}`;
          return (
            <Progress.Root
              key={title}
              size="xl"
              radius="xl"
              style={{ minWidth: 80, background: "var(--bz-hover-bg)" }}
            >
              <Tooltip label={label} withArrow>
                <Progress.Section
                  value={
                    eventCount === 0 || !profileId
                      ? 0
                      : (eventFileCount / eventCount) * 100.0
                  }
                  color={eventFileCount === eventCount ? "brand" : "yellow"}
                  style={{ borderRadius: "var(--bz-radius-xl)" }}
                >
                  <Progress.Label
                    style={{ fontSize: "0.75rem", fontWeight: 600 }}
                  >
                    {label}
                  </Progress.Label>
                </Progress.Section>
              </Tooltip>
            </Progress.Root>
          );
        },
      },
    ],
    [multiInstance, instanceDefaultId, instanceNameById],
  );

  useDocumentTitle(`Sports - ${useInstanceName()}`);

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
    <Container px={0} fluid>
      <Toolbox>
        <Toolbox.MutateButton icon={faSync} promise={syncLeagues}>
          Sync Leagues
        </Toolbox.MutateButton>
      </Toolbox>
      <ItemView
        query={query}
        columns={columns}
        searchValue={search}
        onSearchChange={setSearch}
        audioLanguages={audioLanguages}
        onAudioLanguagesChange={setAudioLanguages}
        excludeLanguages={excludeLanguages}
        onExcludeLanguagesChange={setExcludeLanguages}
        instanceOptions={multiInstance ? instanceOptions : undefined}
        instanceValues={instanceFilter}
        onInstanceValuesChange={setInstanceFilter}
        enableRowSelection
        onSelectionChanged={setSelections}
        profileToolbar={profileToolbar}
      ></ItemView>
    </Container>
  );
};

export default Sports;
