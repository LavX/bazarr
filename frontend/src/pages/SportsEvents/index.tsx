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
  Text,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faDownload,
  faHardDrive,
  faHistory,
  faLayerGroup,
  faServer,
  faSync,
  faTrophy,
} from "@fortawesome/free-solid-svg-icons";
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
} from "@/apis/hooks/sports";
import { SportsEvent } from "@/apis/raw/sports";
import { Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import SportsJobFeedback from "@/pages/SportsActivity/JobFeedback";
import ItemOverview from "@/pages/views/ItemOverview";
import { navigateApp } from "@/utilities/whatsNew";
import Table from "./table";

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

  const { multiInstance, nameById: instanceNameById } =
    useArrInstanceLabels("sportarr");
  const indexSubtitles = useIndexSportsSubtitles();
  const automatic = useSportsAction();
  const combine = useCombineSubtitles();

  const events = useMemo(() => eventPage?.data ?? null, [eventPage]);
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
  const [, setIsAllRowExpanded] = useState(
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
