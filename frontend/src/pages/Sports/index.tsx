import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Link } from "react-router";
import {
  ActionIcon,
  Anchor,
  Badge,
  Checkbox,
  Container,
  Group,
  Menu,
  Progress,
  Text,
  Tooltip,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faArrowUp,
  faBookmark,
  faCircleDown,
  faEllipsisVertical,
  faHardDrive,
  faLanguage,
  faLayerGroup,
  faMagnifyingGlass,
  faSync,
  faToolbox,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { useInstanceName } from "@/apis/hooks/site";
import {
  SportsLeagueRow,
  useSportsAvailability,
  useSportsLeaguesPagination,
  useSportsProfile,
  useSportsProfiles,
  useSyncSports,
} from "@/apis/hooks/sports";
import { useUpgradableItems } from "@/apis/hooks/subtitles";
import { BatchAction, BatchItem } from "@/apis/raw/subtitles";
import { Toolbox } from "@/components";
import { AudioList, InstanceBadge } from "@/components/bazarr";
import LanguageProfileName from "@/components/bazarr/LanguageProfile";
import { BatchModConfirmModal } from "@/components/forms/BatchModConfirmForm";
import { ChangeProfileModal } from "@/components/forms/ChangeProfileForm";
import { MassCombineModal } from "@/components/forms/MassCombineForm";
import { MassSyncModal } from "@/components/forms/MassSyncForm";
import {
  MassTranslateModal,
  WantedItem,
} from "@/components/forms/MassTranslateForm";
import { SUBTITLE_TOOL_ACTIONS } from "@/constants/batch";
import { useModals } from "@/modules/modals";
import ItemView from "@/pages/views/ItemView";
import { LIBRARY_ROUTES } from "@/Router/mediaRoutes";

// Sports renders through the same ItemView table as Series and Movies. It used
// to be a bespoke grid of poster cards with its own instance select and its own
// pagination, which meant none of the shared filtering, row selection or batch
// tooling reached it and it looked nothing like the other two media types.
// The per-row menu Series and Movies carry. A league had none at all, so
// acting on one meant ticking its checkbox first.
//
// Its own component rather than an inline cell: the menu needs the modals and
// profile hooks, and hook objects get a new identity on every render. Listing
// them in the columns useMemo deps rebuilt the whole column set on each render,
// which unmounted the open dropdown before anything could be clicked.
const LeagueRowActions: FunctionComponent<{ league: SportsLeagueRow }> = ({
  league,
}) => {
  const modals = useModals();
  const assign = useSportsProfile();

  const batchItem: BatchItem = {
    type: "sportsLeague",
    sportsLeagueId: league.id,
    arr_instance_id: league.arr_instance_id,
  };
  const wantedItem: WantedItem = {
    type: "sportsLeague",
    sportsLeagueId: league.id,
    title: league.title,
    arrInstanceId: league.arr_instance_id,
  };
  const batchAction = (action: BatchAction) => () =>
    modals.openContextModal(BatchModConfirmModal, {
      items: [batchItem],
      action,
    });

  return (
    <Menu shadow="md" width={220} position="bottom-end">
      <Menu.Target>
        <Tooltip label="Actions">
          <ActionIcon aria-label="Actions" variant="subtle" size="sm">
            <FontAwesomeIcon icon={faEllipsisVertical} />
          </ActionIcon>
        </Tooltip>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Item
          leftSection={<FontAwesomeIcon icon={faWrench} size="sm" />}
          onClick={() =>
            modals.openContextModal(
              ChangeProfileModal,
              {
                onSelect: (profileId: number | null) =>
                  assign.mutate({
                    id: league.id,
                    owner: league.arr_instance_id,
                    profileId,
                  }),
              },
              { title: league.title },
            )
          }
        >
          Change Profile
        </Menu.Item>
        <Menu.Divider />
        <Menu.Item
          leftSection={<FontAwesomeIcon icon={faSync} size="sm" />}
          onClick={() =>
            modals.openContextModal(MassSyncModal, { items: [batchItem] })
          }
        >
          Sync Subtitles
        </Menu.Item>
        <Menu.Item
          leftSection={<FontAwesomeIcon icon={faLanguage} size="sm" />}
          onClick={() =>
            modals.openContextModal(MassTranslateModal, { items: [wantedItem] })
          }
        >
          Translate
        </Menu.Item>
        <Menu.Item
          leftSection={<FontAwesomeIcon icon={faLayerGroup} size="sm" />}
          onClick={() =>
            modals.openContextModal(MassCombineModal, { items: [wantedItem] })
          }
        >
          Combine Subtitles
        </Menu.Item>
        <Menu.Divider />
        <Menu.Item
          leftSection={<FontAwesomeIcon icon={faHardDrive} size="sm" />}
          onClick={batchAction("scan-disk")}
        >
          Scan Disk
        </Menu.Item>
        <Menu.Item
          leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} size="sm" />}
          onClick={batchAction("search-missing")}
        >
          Search Missing
        </Menu.Item>
        <Menu.Item
          leftSection={<FontAwesomeIcon icon={faArrowUp} size="sm" />}
          onClick={batchAction("upgrade")}
        >
          Upgrade
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
};

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

  // ItemView filters what is in query.data, so a filter that is active has to
  // see the whole library rather than the page on screen. Same shape as the
  // Series and Movies pages.
  const hasActiveFilter =
    search.length > 0 ||
    audioLanguages.length > 0 ||
    excludeLanguages.length > 0 ||
    instanceFilter.length > 0;
  const query = useSportsLeaguesPagination(hasActiveFilter);
  // The same low-score marker Series and Movies show. Sports had no way to
  // consume /api/subtitles/upgradable because the endpoint sent no sports key,
  // so a league whose subtitles would benefit from an upgrade looked identical
  // to one that would not.
  const { data: upgradableData } = useUpgradableItems();
  const upgradableLeagueKeys = useMemo(
    () =>
      new Set(
        upgradableData?.sportsKeys?.map(
          (item) => `${item.sportsLeagueId}:${item.arr_instance_id ?? ""}`,
        ) ?? [],
      ),
    [upgradableData],
  );
  const assignMany = useSportsProfiles();
  const sync = useSyncSports();

  const syncLeagues = useCallback(
    () => sync.mutateAsync(instances.map((instance) => instance.id)),
    [sync, instances],
  );

  // One request for the whole selection, matching what Series and Movies do.
  // This used to fire one PATCH per league, each with its own transaction and
  // its own re-index, so a 200-league edit meant 200 requests and a partial
  // failure left the selection half-applied with no aggregate status.
  const setProfiles = useCallback(
    (profileId: number | null) => {
      if (selections.length === 0) return;
      void assignMany.mutateAsync(
        selections.map((league) => ({
          id: league.id,
          owner: league.arr_instance_id,
          profileId,
        })),
      );
    },
    [selections, assignMany],
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

  // The same batch toolbar Series and Movies carry. A league is the sports
  // equivalent of a show, so it batches as one: the backend expands it into its
  // events. Every item carries its owner because a sports path mapping is
  // always per instance, with no global mapping to fall back on.
  const selectionToolbar = useMemo(() => {
    if (selections.length === 0) return undefined;

    const toBatchItems = (): BatchItem[] =>
      selections.map((league) => ({
        type: "sportsLeague" as const,
        sportsLeagueId: league.id,
        arr_instance_id: league.arr_instance_id,
      }));

    const toWantedItems = (): WantedItem[] =>
      selections.map((league) => ({
        type: "sportsLeague" as const,
        sportsLeagueId: league.id,
        title: league.title,
        arrInstanceId: league.arr_instance_id,
      }));

    return (
      <Group gap="xs">
        <Toolbox.Button
          icon={faSync}
          onClick={() =>
            modals.openContextModal(MassSyncModal, { items: toBatchItems() })
          }
        >
          Sync Subtitles
        </Toolbox.Button>

        <Menu shadow="md" width={220}>
          <Menu.Target>
            <div>
              <Toolbox.Button icon={faToolbox}>Subtitle Tools</Toolbox.Button>
            </div>
          </Menu.Target>
          <Menu.Dropdown>
            {SUBTITLE_TOOL_ACTIONS.map(([action, label]) => (
              <Menu.Item
                key={action}
                onClick={() =>
                  modals.openContextModal(BatchModConfirmModal, {
                    items: toBatchItems(),
                    action,
                  })
                }
              >
                {label}
              </Menu.Item>
            ))}
          </Menu.Dropdown>
        </Menu>

        <Toolbox.Button
          icon={faLanguage}
          onClick={() =>
            modals.openContextModal(MassTranslateModal, {
              items: toWantedItems(),
            })
          }
        >
          Translate
        </Toolbox.Button>

        <Toolbox.Button
          icon={faLayerGroup}
          onClick={() =>
            modals.openContextModal(MassCombineModal, {
              items: toWantedItems(),
            })
          }
        >
          Combine
        </Toolbox.Button>

        <Menu shadow="md" width={220}>
          <Menu.Target>
            <div>
              <Toolbox.Button icon={faEllipsisVertical}>More</Toolbox.Button>
            </div>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item
              leftSection={<FontAwesomeIcon icon={faHardDrive} size="sm" />}
              onClick={() =>
                modals.openContextModal(BatchModConfirmModal, {
                  items: toBatchItems(),
                  action: "scan-disk" as BatchAction,
                })
              }
            >
              Scan Disk
            </Menu.Item>
            <Menu.Item
              leftSection={
                <FontAwesomeIcon icon={faMagnifyingGlass} size="sm" />
              }
              onClick={() =>
                modals.openContextModal(BatchModConfirmModal, {
                  items: toBatchItems(),
                  action: "search-missing" as BatchAction,
                })
              }
            >
              Search Missing
            </Menu.Item>
            <Menu.Item
              leftSection={<FontAwesomeIcon icon={faArrowUp} size="sm" />}
              onClick={() =>
                modals.openContextModal(BatchModConfirmModal, {
                  items: toBatchItems(),
                  action: "upgrade" as BatchAction,
                })
              }
            >
              Upgrade
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
      </Group>
    );
  }, [selections, modals]);

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
        id: "upgradable",
        cell: ({ row: { original } }) =>
          upgradableLeagueKeys.has(
            `${original.id}:${original.arr_instance_id ?? ""}`,
          ) ? (
            <Tooltip label="Low match score, upgrading may find a better subtitle">
              <FontAwesomeIcon
                icon={faCircleDown}
                color="var(--bz-text-tertiary)"
                size="sm"
              />
            </Tooltip>
          ) : null,
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
          // Counts, not a completion ratio. These two measure different
          // things: eventCount is distinct upstream events, eventFileCount is
          // playable files, and one two-part event makes that "2/1" and drives
          // a progress bar past 100% while colouring a complete league yellow.
          //
          // There is no fraction to show here in the first place. Every row in
          // the sports events table is a playable file, so a league has no
          // known-but-missing events the way a series has episodes without
          // files. Showing both numbers plainly says what is actually known.
          const { eventCount, eventFileCount } = original;
          const parts = eventFileCount > eventCount;
          return (
            <Tooltip
              withArrow
              label={
                parts
                  ? `${eventFileCount} playable files across ${eventCount} events`
                  : `${eventCount} events`
              }
            >
              <Text size="sm">
                {parts ? `${eventCount} (${eventFileCount} files)` : eventCount}
              </Text>
            </Tooltip>
          );
        },
      },
      {
        id: "actions",
        cell: ({ row: { original } }) => <LeagueRowActions league={original} />,
      },
    ],
    // upgradableLeagueKeys is a memoised Set, so it is stable between
    // fetches and does not rebuild the columns on every render.
    [multiInstance, instanceDefaultId, instanceNameById, upgradableLeagueKeys],
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
        selectionToolbar={selectionToolbar}
        profileToolbar={profileToolbar}
      ></ItemView>
    </Container>
  );
};

export default Sports;
