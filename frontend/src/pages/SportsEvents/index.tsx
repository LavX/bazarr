import {
  FunctionComponent,
  useCallback,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useParams, useSearchParams } from "react-router";
import {
  Alert,
  Anchor,
  Breadcrumbs,
  Container,
  Group,
  Menu,
  Text,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { showNotification } from "@mantine/notifications";
import {
  faBriefcase,
  faCircleChevronDown,
  faCircleChevronRight,
  faCloudUploadAlt,
  faDownload,
  faEllipsisVertical,
  faHardDrive,
  faHistory,
  faLayerGroup,
  faServer,
  faSync,
  faTrophy,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { Table as TableInstance } from "@tanstack/react-table";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { useCombineSubtitles } from "@/apis/hooks/combine";
import { useInstanceName } from "@/apis/hooks/site";
import {
  toSportsLeagueRow,
  useIndexSportsSubtitles,
  useSportsAction,
  useSportsAvailability,
  useSportsEvents,
  useSportsLeague,
  useSportsProfile,
} from "@/apis/hooks/sports";
import { useBatchAction } from "@/apis/hooks/subtitles";
import { SportsEvent } from "@/apis/raw/sports";
import { FullPageDropzone, Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import { SportsJobFeedback } from "@/components/bazarr";
import { ChangeProfileModal } from "@/components/forms/ChangeProfileForm";
import { SportsUploadModal } from "@/components/forms/SportsUploadForm";
import { SubtitleDownloadModal } from "@/components/forms/SubtitleDownloadForm";
import SubtitleToolsModal, {
  SportsToolsItem,
} from "@/components/modals/SubtitleToolsModal";
import { useModals } from "@/modules/modals";
import { notification } from "@/modules/task";
import ItemOverview from "@/pages/views/ItemOverview";
import { useLanguageProfileBy } from "@/utilities/languages";
import { navigateApp } from "@/utilities/whatsNew";
import Table, { toSportsSubtitle } from "./table";

// The league detail page, built to match the Series episodes page: breadcrumb,
// ItemOverview header, a Toolbox of actions, then the season-grouped table.
// It used to be a bare Mantine table with text-link buttons in an Actions
// column, which shared nothing with how episodes are presented.
const SportsEventsView: FunctionComponent = () => {
  const { id } = useParams();
  const [params] = useSearchParams();
  const leagueId = Number.parseInt(id as string);
  const ownerParam = params.get("instance");

  const { enabled, isLoading } = useSportsAvailability();
  const leagueQuery = useSportsLeague(
    leagueId,
    ownerParam ? Number(ownerParam) : undefined,
  );
  const { data: league } = leagueQuery;
  const eventsQuery = useSportsEvents(leagueId, league?.arr_instance_id, 1);
  const { data: eventPage } = eventsQuery;
  // The league's profile drives the only-desired subtitle filter in the table,
  // the same way the Series and Movies pages pass theirs.
  const profile = useLanguageProfileBy(league?.profileId);

  const { multiInstance, nameById: instanceNameById } =
    useArrInstanceLabels("sportarr");
  const indexSubtitles = useIndexSportsSubtitles();
  const automatic = useSportsAction();
  const combine = useCombineSubtitles();
  const modals = useModals();
  const batch = useBatchAction();
  const openDropzone = useRef<VoidFunction>(null);

  const onDrop = useCallback(
    (dropped: File[]) => {
      // A league with no profile has no languages to offer, so the form would
      // open with an unfillable Language column.
      if (league && league.profileId !== null) {
        modals.openContextModal(SportsUploadModal, { files: dropped, league });
      } else {
        showNotification(
          notification.warn(
            "Cannot Upload Files",
            "league or language profile is not ready",
          ),
        );
      }
    },
    [modals, league],
  );
  const assignProfile = useSportsProfile();

  const events = useMemo(() => eventPage?.data ?? null, [eventPage]);

  // The shared Subtitle Tools modal reads Subtitle objects; a sports event
  // stores its subtitles as [language, path, size] tuples, so the shapes are
  // reconciled here rather than by widening the modal for one caller.
  // Every base language with a file on disk, sync and combined outputs
  // included: the bundle ships whatever exists, not just original subtitles.
  const downloadableLangs = useMemo(() => {
    const seen = new Set<string>();
    for (const event of events ?? []) {
      for (const [key, path] of event.subtitles ?? []) {
        if (path) seen.add(key.split(":")[0]);
      }
    }
    return Array.from(seen).sort();
  }, [events]);

  const languagesBySeason = useMemo(() => {
    const seen = new Map<number, Set<string>>();
    for (const event of events ?? []) {
      if (event.season == null) continue;
      for (const [key, path] of event.subtitles ?? []) {
        if (!path) continue;
        if (!seen.has(event.season)) seen.set(event.season, new Set());
        seen.get(event.season)!.add(key.split(":")[0]);
      }
    }
    return Object.fromEntries(
      Array.from(seen, ([season, codes]) => [season, Array.from(codes).sort()]),
    );
  }, [events]);

  // Only seasons that actually have a file: offering an empty one would just
  // produce a guaranteed 404 bundle.
  const seasons = useMemo(
    () =>
      Array.from(
        new Set(
          (events ?? [])
            .filter((event) => (event.subtitles ?? []).some(([, p]) => p))
            .map((event) => event.season)
            .filter((season): season is number => season != null),
        ),
      ).sort((a, b) => a - b),
    [events],
  );

  // The shared Subtitle Tools modal reads Subtitle objects and rebuilds the
  // language key from one, so the tuples go through the same widening the table
  // uses. Splitting the key here instead kept only the base language and the
  // hi/forced flags, which dropped a sync or combined modifier and made the
  // Download action fetch the plain base-language file.
  const toolsPayload = useMemo<SportsToolsItem[]>(
    () =>
      (events ?? []).map((event) => ({
        id: event.id,
        title: event.title,
        // eslint-disable-next-line camelcase
        arr_instance_id: event.arr_instance_id,
        isSports: true as const,
        subtitles: (event.subtitles ?? [])
          .filter(([, path]) => Boolean(path))
          .map(toSportsSubtitle),
      })),
    [events],
  );
  const overviewItem = useMemo(
    () => (league ? toSportsLeagueRow(league) : null),
    [league],
  );

  const details = useMemo(
    () => [
      ...(multiInstance && league?.arr_instance_id != null
        ? [
            {
              icon: faServer,
              text:
                instanceNameById.get(league.arr_instance_id) ??
                `#${league.arr_instance_id}`,
            },
          ]
        : []),
      { icon: faHardDrive, text: `${league?.eventFileCount ?? 0} files` },
      { icon: faTrophy, text: league?.sport ?? "Sport" },
    ],
    [league, multiInstance, instanceNameById],
  );

  const onIndex = useCallback(
    (event: SportsEvent) =>
      indexSubtitles.mutate({ id: event.id, owner: event.arr_instance_id }),
    [indexSubtitles],
  );

  useDocumentTitle(
    `${league?.title ?? "Unknown League"} - ${useInstanceName()} (Sports)`,
  );

  const tableRef = useRef<TableInstance<SportsEvent> | null>(null);
  const [isAllRowExpanded, setIsAllRowExpanded] = useState(
    tableRef?.current?.getIsAllRowsExpanded(),
  );

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
      <nav aria-label="Breadcrumb">
        <Breadcrumbs mb="md" ml="xs">
          <Anchor component={Link} to="/sports" size="sm">
            Sports
          </Anchor>
          <Text size="sm" c="var(--bz-text-primary)">
            {league?.title ?? "Loading..."}
          </Text>
        </Breadcrumbs>
      </nav>
      <QueryOverlay result={leagueQuery}>
        <FullPageDropzone
          openRef={openDropzone}
          active={league?.profileId != null}
          onDrop={onDrop}
        />
        <Toolbox>
          <Group gap="xs">
            <Toolbox.Button
              icon={faSync}
              disabled={!league || automatic.isPending}
              onClick={() =>
                league &&
                automatic.mutate({
                  path: `/leagues/${league.id}/sync`,
                  owner: league.arr_instance_id,
                })
              }
            >
              Sync
            </Toolbox.Button>
            <Toolbox.Button
              icon={faCloudUploadAlt}
              disabled={!league || league.profileId === null}
              onClick={() => openDropzone.current?.()}
            >
              Upload
            </Toolbox.Button>
            <Toolbox.Button
              icon={faDownload}
              disabled={
                !league ||
                league.profileId === null ||
                league.eventFileCount === 0 ||
                automatic.isPending
              }
              loading={automatic.isPending}
              onClick={() =>
                league &&
                automatic.mutate({
                  path: `/leagues/${league.id}/download`,
                  owner: league.arr_instance_id,
                })
              }
            >
              Search
            </Toolbox.Button>
            <Toolbox.Button
              icon={faBriefcase}
              disabled={!league || league.eventFileCount === 0}
              onClick={() =>
                events &&
                modals.openContextModal(SubtitleToolsModal, {
                  payload: toolsPayload,
                })
              }
            >
              Mass Edit
            </Toolbox.Button>
            <Toolbox.Button
              icon={faLayerGroup}
              disabled={
                !league || league.profileId === null || combine.isPending
              }
              loading={combine.isPending}
              onClick={() =>
                league &&
                combine.mutate({
                  scope: {
                    kind: "sportsLeague",
                    leagueId: league.id,
                    arrInstanceId: league.arr_instance_id,
                  },
                  body: {},
                })
              }
            >
              Combine across league
            </Toolbox.Button>
            <Toolbox.Button
              icon={faDownload}
              disabled={!league || downloadableLangs.length === 0}
              onClick={() =>
                league &&
                modals.openContextModal(SubtitleDownloadModal, {
                  scope: {
                    kind: "sports",
                    leagueId: league.id,
                    arrInstanceId: league.arr_instance_id,
                    seasons,
                    languagesBySeason,
                  },
                  availableLanguages: downloadableLangs,
                })
              }
            >
              Download
            </Toolbox.Button>
            <Toolbox.Button
              icon={faHistory}
              onClick={() =>
                navigateApp(
                  `/history/sports?instance=${league?.arr_instance_id ?? ""}`,
                )
              }
            >
              History
            </Toolbox.Button>
          </Group>
          <Group gap="xs">
            <Toolbox.Button
              icon={
                isAllRowExpanded ? faCircleChevronRight : faCircleChevronDown
              }
              onClick={() => tableRef.current?.toggleAllRowsExpanded()}
            >
              {isAllRowExpanded ? "Collapse All" : "Expand All"}
            </Toolbox.Button>
            <Menu shadow="md" width={200}>
              <Menu.Target>
                <div>
                  <Toolbox.Button icon={faEllipsisVertical}>
                    More
                  </Toolbox.Button>
                </div>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faHardDrive} size="sm" />}
                  disabled={!league || batch.isPending}
                  onClick={() =>
                    league &&
                    batch.mutate({
                      items: [
                        {
                          type: "sportsLeague",
                          sportsLeagueId: league.id,
                          // eslint-disable-next-line camelcase
                          arr_instance_id: league.arr_instance_id,
                        },
                      ],
                      action: "scan-disk",
                    })
                  }
                >
                  Scan Disk
                </Menu.Item>
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faWrench} size="sm" />}
                  disabled={!league}
                  onClick={() =>
                    league &&
                    modals.openContextModal(
                      ChangeProfileModal,
                      {
                        onSelect: (profileId: number | null) =>
                          assignProfile.mutate({
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
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Toolbox>
        <ItemOverview item={overviewItem} details={details}></ItemOverview>
        {automatic.isError && (
          <Alert color="red" mb="md">
            Could not queue the sports search.
          </Alert>
        )}
        {indexSubtitles.isError && (
          <Alert color="red" mb="md">
            Could not index subtitles. Check the file path and try again.
          </Alert>
        )}
        {combine.isError && (
          <Alert color="red" mb="md">
            Could not combine subtitles for this league.
          </Alert>
        )}
        {combine.isSuccess && combine.data.status === "batch_complete" && (
          <Alert color="blue" mb="md">
            {`Combined ${combine.data.built ?? 0}, skipped ${combine.data.skipped ?? 0}, failed ${combine.data.failed ?? 0}.`}
          </Alert>
        )}
        <SportsJobFeedback
          queued={automatic.data}
          owner={automatic.variables?.owner}
        />
        <Table
          events={events}
          ref={tableRef}
          disabled={automatic.isPending}
          profile={profile}
          indexingId={
            indexSubtitles.isPending ? indexSubtitles.variables?.id : undefined
          }
          onIndex={onIndex}
          onAllRowsExpandedChanged={setIsAllRowExpanded}
        ></Table>
      </QueryOverlay>
    </Container>
  );
};

export default SportsEventsView;
