import { FunctionComponent, useMemo } from "react";
import {
  Alert,
  SimpleGrid,
  Stack,
  Table,
  Text,
  ThemeIcon,
} from "@mantine/core";
import { faPlugCircleXmark } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useTranslatorJobs, useTranslatorStatus } from "@/apis/hooks";
import PanelCard from "./components/PanelCard";
import StatTile from "./components/StatTile";
import { summarizeTranslatorJobs, translatorLinesPerDay } from "./utils";

/** Cost is fractions of a cent per job, so four decimals is the useful scale. */
const formatUsd = (value: number) => `$${value.toFixed(4)}`;

const TranslatorPanel: FunctionComponent = () => {
  const status = useTranslatorStatus();
  const jobsQuery = useTranslatorJobs();

  const jobs = useMemo(() => jobsQuery.data?.jobs ?? [], [jobsQuery.data]);
  const usage = useMemo(() => summarizeTranslatorJobs(jobs), [jobs]);
  const throughput = useMemo(() => translatorLinesPerDay(jobs), [jobs]);

  // Both hooks are deliberately fail-soft (retry false, throwOnError false)
  // because the sidecar is an optional service that is often simply absent.
  // When only one of them fails, its section says so rather than reading its
  // missing data as zero, and the other section still shows.
  const statusFailed = status.isError;
  const jobsFailed = jobsQuery.isError;
  if (statusFailed && jobsFailed) {
    return (
      <Alert
        color="gray"
        icon={
          <ThemeIcon variant="transparent" color="gray">
            <FontAwesomeIcon icon={faPlugCircleXmark} />
          </ThemeIcon>
        }
        title="Translator unavailable"
      >
        The AI Subtitle Translator is not reachable. Configure the sidecar to
        see translation cost and throughput here.
      </Alert>
    );
  }

  const queue = status.data?.queue;

  return (
    <Stack gap="lg">
      {statusFailed && (
        <Alert color="red" title="Queue status could not be loaded">
          Reload this page to retry.
        </Alert>
      )}
      {jobsFailed ? (
        <Alert color="red" title="Recent jobs could not be loaded">
          Cost and throughput are not shown. Reload this page to retry.
        </Alert>
      ) : (
        <Text size="xs" c="dimmed">
          Recent jobs only: the sidecar keeps a bounded window of jobs, so the
          cost and throughput figures here are not all-time.
        </Text>
      )}

      <SimpleGrid cols={{ base: 1, xs: 2, lg: 4 }}>
        {!statusFailed && (
          <>
            <StatTile
              label="Queued"
              value={queue?.queued ?? 0}
              hint="waiting in the sidecar"
            />
            <StatTile
              label="Processing"
              value={queue?.processing ?? 0}
              color={(queue?.processing ?? 0) > 0 ? "blue" : undefined}
              hint={`max ${queue?.maxConcurrent ?? "-"} concurrent`}
            />
          </>
        )}
        {!jobsFailed && (
          <>
            <StatTile
              label="Cost, recent jobs"
              value={formatUsd(usage.costUsd)}
              hint={`${usage.tokens.toLocaleString()} tokens`}
            />
            <StatTile
              label="Lines delivered"
              value={usage.linesTranslated.toLocaleString()}
              hint="completed lines delivered"
            />
          </>
        )}
      </SimpleGrid>

      {!jobsFailed && (
        <>
          <SimpleGrid cols={{ base: 1, xs: 3 }}>
            <StatTile label="Completed" value={usage.completed} color="green" />
            <StatTile
              label="Partial"
              value={usage.partial}
              color={usage.partial > 0 ? "yellow" : undefined}
              hint="delivered fewer lines than requested"
            />
            <StatTile
              label="Failed"
              value={usage.failed}
              color={usage.failed > 0 ? "red" : undefined}
            />
          </SimpleGrid>

          <PanelCard
            title="Lines delivered per day"
            caveat="Bucketed by the day each job completed, in your local time."
          >
            {throughput.length === 0 ? (
              <Text size="sm" c="dimmed">
                No completed jobs yet.
              </Text>
            ) : (
              <div style={{ width: "100%", height: 240 }}>
                <ResponsiveContainer>
                  <BarChart data={throughput}>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                    <XAxis
                      dataKey="date"
                      tick={{ fontSize: 11 }}
                      minTickGap={24}
                    />
                    <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                    <Tooltip />
                    <Bar dataKey="lines" name="Lines" fill="#4dabf7" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </PanelCard>

          <PanelCard
            title="Cost by model"
            caveat="Attributed across the jobs the sidecar still holds."
          >
            {usage.byModel.length === 0 ? (
              <Text size="sm" c="dimmed">
                No translation jobs recorded.
              </Text>
            ) : (
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Model</Table.Th>
                    <Table.Th>Jobs</Table.Th>
                    <Table.Th>Cost</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {usage.byModel.map((m) => (
                    <Table.Tr key={m.model}>
                      <Table.Td>
                        <Text size="sm">{m.model}</Text>
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm" c="dimmed">
                          {m.jobs}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm">{formatUsd(m.costUsd)}</Text>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            )}
          </PanelCard>
        </>
      )}
    </Stack>
  );
};

export default TranslatorPanel;
