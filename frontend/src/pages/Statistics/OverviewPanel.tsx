import { FunctionComponent, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Group,
  SimpleGrid,
  Stack,
  Table,
  Text,
  ThemeIcon,
} from "@mantine/core";
import {
  faCircleCheck,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useBadges,
  useSystemHealth,
  useSystemJobs,
  useSystemStatus,
} from "@/apis/hooks";
import {
  divisorDay,
  divisorHour,
  divisorMinute,
  divisorSecond,
  formatTime,
} from "@/utilities/time";
import PanelCard from "./components/PanelCard";
import StatTile from "./components/StatTile";
import { summarizeJobQueue } from "./utils";

const UPTIME_FORMAT = [
  { unit: "d", divisor: divisorDay },
  { unit: "h", divisor: divisorHour },
  { unit: "m", divisor: divisorMinute },
  { unit: "s", divisor: divisorSecond },
];

/** A SignalR feed is only reported LIVE when every enabled instance is up. */
const SIGNALR_LIVE = "LIVE";

const FeedState: FunctionComponent<{ label: string; state?: string }> = ({
  label,
  state,
}) => (
  <StatTile
    label={label}
    value={state ?? "-"}
    color={state === SIGNALR_LIVE ? "green" : "red"}
    hint="aggregate across enabled instances"
  />
);

const OverviewPanel: FunctionComponent = () => {
  const { data: badges } = useBadges();
  const { data: status } = useSystemStatus();
  const { data: health } = useSystemHealth();
  const { data: jobs } = useSystemJobs();

  const queue = useMemo(() => summarizeJobQueue(jobs ?? []), [jobs]);

  // Tick a clock so uptime advances while the page stays open.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const uptime = status?.start_time
    ? formatTime(Math.floor(now / 1000) - status.start_time, UPTIME_FORMAT)
    : "-";

  const issues = health ?? [];

  return (
    <Stack gap="lg">
      <SimpleGrid cols={{ base: 1, xs: 2, lg: 4 }}>
        <StatTile
          label="Missing episode subtitles"
          value={badges?.episodes ?? 0}
          hint="wanted, excluding filtered media"
        />
        <StatTile
          label="Missing movie subtitles"
          value={badges?.movies ?? 0}
          hint="wanted, excluding filtered media"
        />
        <StatTile
          label="Health issues"
          value={issues.length}
          color={issues.length > 0 ? "yellow" : "green"}
          hint={issues.length > 0 ? "needs attention" : "all checks passing"}
        />
        <StatTile
          label="Uptime"
          value={uptime}
          hint="since this process started"
        />
      </SimpleGrid>

      <SimpleGrid cols={{ base: 1, xs: 2, lg: 4 }}>
        <StatTile
          label="Jobs pending"
          value={queue.pending}
          hint="waiting in the queue"
        />
        <StatTile label="Jobs running" value={queue.running} hint="in flight" />
        <StatTile
          label="Jobs completed"
          value={queue.completedSaturated ? "10+" : queue.completed}
          hint="recent only, queue keeps 10"
        />
        <StatTile
          label="Jobs failed"
          value={queue.failedSaturated ? "10+" : queue.failed}
          color={queue.failed > 0 ? "red" : undefined}
          hint="recent only, queue keeps 10"
        />
      </SimpleGrid>

      <SimpleGrid cols={{ base: 1, xs: 2 }}>
        <FeedState label="Sonarr feed" state={badges?.sonarr_signalr} />
        <FeedState label="Radarr feed" state={badges?.radarr_signalr} />
      </SimpleGrid>

      <PanelCard
        title="Health"
        caveat="Evaluated on demand. Bazarr keeps no history of past issues."
      >
        {issues.length === 0 ? (
          <Group gap="xs">
            <ThemeIcon color="green" variant="light" size="sm">
              <FontAwesomeIcon icon={faCircleCheck} />
            </ThemeIcon>
            <Text size="sm" c="dimmed">
              No health issues reported.
            </Text>
          </Group>
        ) : (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Object</Table.Th>
                <Table.Th>Issue</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {issues.map((issue) => (
                <Table.Tr key={`${issue.object}-${issue.issue}`}>
                  <Table.Td>
                    <Group gap="xs" wrap="nowrap">
                      <ThemeIcon color="yellow" variant="light" size="sm">
                        <FontAwesomeIcon icon={faTriangleExclamation} />
                      </ThemeIcon>
                      <Text size="sm">{issue.object}</Text>
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{issue.issue}</Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </PanelCard>

      <PanelCard title="Environment">
        <Group gap="xs">
          <Badge variant="light">{status?.bazarr_version ?? "-"}</Badge>
          <Badge variant="light" color="gray">
            {status?.database_engine ?? "-"}
          </Badge>
          <Badge variant="light" color="gray">
            {status?.operating_system ?? "-"}
          </Badge>
          <Badge variant="light" color="gray">
            Python {status?.python_version ?? "-"}
          </Badge>
          <Badge variant="light" color="gray">
            {status?.cpu_cores ?? "-"} cores
          </Badge>
          <Badge variant="light" color="gray">
            {status?.timezone ?? "-"}
          </Badge>
        </Group>
      </PanelCard>
    </Stack>
  );
};

export default OverviewPanel;
