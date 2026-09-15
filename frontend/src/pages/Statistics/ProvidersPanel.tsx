import { FunctionComponent, useMemo } from "react";
import {
  Badge,
  Group,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Tooltip,
} from "@mantine/core";
import {
  useProviderHubCatalog,
  useProviderHubJobs,
  useProviderHubProviders,
  useSystemProviders,
} from "@/apis/hooks";
import PanelCard from "./components/PanelCard";
import StatTile from "./components/StatTile";
import {
  formatDurationMs,
  HUB_JOB_LOG_CAP,
  summarizeHubJobs,
  summarizeProviderHealth,
} from "./utils";

const ProvidersPanel: FunctionComponent = () => {
  // history=false is the throttle-state variant. The history=true variant
  // returns a flat list with status "History" and carries no health signal.
  const { data: providers } = useSystemProviders(false);
  const { data: installs } = useProviderHubProviders();
  const { data: hubJobs } = useProviderHubJobs();
  const { data: catalog } = useProviderHubCatalog();

  const health = useMemo(
    () => summarizeProviderHealth(providers ?? []),
    [providers],
  );
  const jobs = useMemo(() => summarizeHubJobs(hubJobs ?? []), [hubJobs]);

  const throttled = (providers ?? []).filter(
    (p) => p.status !== "Good" && p.status !== "History",
  );

  const pendingRestart = (installs ?? []).filter((i) => i.pending_restart);
  const installStates = useMemo(() => {
    const counts = new Map<string, number>();
    for (const install of installs ?? []) {
      counts.set(install.state, (counts.get(install.state) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [installs]);

  const sources = catalog?.sources ?? [];

  return (
    <Stack gap="lg">
      <SimpleGrid cols={{ base: 1, xs: 2, lg: 4 }}>
        <StatTile
          label="Providers healthy"
          value={`${health.good} / ${health.total}`}
          color={health.throttled > 0 ? "yellow" : "green"}
          hint={
            health.throttled > 0
              ? `${health.throttled} throttled right now`
              : "none throttled right now"
          }
        />
        <StatTile
          label="Providers throttled"
          value={health.throttled}
          color={health.throttled > 0 ? "red" : undefined}
          hint={
            health.reasons.length > 0
              ? `${health.reasons.length} distinct reason(s)`
              : "none right now"
          }
        />
        <StatTile
          label="Plugins installed"
          value={installs?.length ?? 0}
          hint={
            pendingRestart.length > 0
              ? `${pendingRestart.length} pending restart`
              : "all active"
          }
        />
        <StatTile
          label="Median job duration"
          value={formatDurationMs(jobs.durationP50Ms)}
          hint={`p95 ${formatDurationMs(jobs.durationP95Ms)}`}
        />
      </SimpleGrid>

      <PanelCard
        title="Throttled providers"
        caveat="Current state only. Bazarr deletes a throttle entry the moment it expires, so past outages cannot be shown."
      >
        {throttled.length === 0 ? (
          <Text size="sm" c="dimmed">
            No providers are throttled.
          </Text>
        ) : (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Provider</Table.Th>
                <Table.Th>Reason</Table.Th>
                <Table.Th>Retry</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {throttled.map((p) => (
                <Table.Tr key={p.name}>
                  <Table.Td>
                    <Text size="sm">{p.name}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge color="red" variant="light">
                      {p.status}
                    </Badge>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" c="dimmed">
                      {p.retry}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </PanelCard>

      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <PanelCard title="Plugin states">
          {installStates.length === 0 ? (
            <Text size="sm" c="dimmed">
              No Provider Hub plugins installed.
            </Text>
          ) : (
            <Group gap="xs">
              {installStates.map(([state, count]) => (
                <Badge
                  key={state}
                  variant="light"
                  color={state === "active" ? "green" : "yellow"}
                >
                  {state}: {count}
                </Badge>
              ))}
            </Group>
          )}
        </PanelCard>

        <PanelCard
          title="Plugin activity"
          caveat={
            jobs.saturated
              ? `Job log is full at ${HUB_JOB_LOG_CAP} entries, so older activity has been discarded.`
              : `Most recent ${HUB_JOB_LOG_CAP} jobs at most.`
          }
        >
          {jobs.byAction.length === 0 ? (
            <Text size="sm" c="dimmed">
              No plugin jobs recorded.
            </Text>
          ) : (
            <Group gap="xs">
              {jobs.byAction.map(({ action, count }) => (
                <Badge key={action} variant="light" color="blue">
                  {action}: {count}
                </Badge>
              ))}
              {jobs.byState
                .filter(({ state }) => state === "failed")
                .map(({ state, count }) => (
                  <Badge key={state} variant="light" color="red">
                    failed: {count}
                  </Badge>
                ))}
            </Group>
          )}
        </PanelCard>
      </SimpleGrid>

      <PanelCard title="Catalog sources">
        {sources.length === 0 ? (
          <Text size="sm" c="dimmed">
            No catalog sources configured.
          </Text>
        ) : (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Source</Table.Th>
                <Table.Th>Last checked</Table.Th>
                <Table.Th>Status</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {sources.map((source) => (
                <Table.Tr key={source.name}>
                  <Table.Td>
                    <Tooltip label={source.url} withinPortal>
                      <Text size="sm">{source.name}</Text>
                    </Tooltip>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" c="dimmed">
                      {source.last_checked_at
                        ? new Date(source.last_checked_at).toLocaleString()
                        : "never"}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    {source.last_error ? (
                      <Text size="sm" c="red">
                        {source.last_error}
                      </Text>
                    ) : (
                      <Badge variant="light" color="green">
                        ok
                      </Badge>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </PanelCard>
    </Stack>
  );
};

export default ProvidersPanel;
