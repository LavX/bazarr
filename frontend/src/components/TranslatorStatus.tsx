import { FunctionComponent, useCallback, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Progress,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import {
  faCheck,
  faClock,
  faExclamationTriangle,
  faRefresh,
  faSpinner,
  faTimes,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  TranslatorJob,
  useTranslatorJobs,
  useTranslatorStatus,
} from "@/apis/hooks/translator";
import classes from "./TranslatorStatus.module.css";

interface StatusBadgeProps {
  status: TranslatorJob["status"];
}

const StatusBadge: FunctionComponent<StatusBadgeProps> = ({ status }) => {
  const config = {
    queued: { color: "gray", icon: faClock, label: "Queued" },
    processing: { color: "blue", icon: faSpinner, label: "Processing" },
    completed: { color: "green", icon: faCheck, label: "Completed" },
    partial: { color: "yellow", icon: faExclamationTriangle, label: "Partial" },
    failed: { color: "red", icon: faExclamationTriangle, label: "Failed" },
    cancelled: { color: "orange", icon: faTimes, label: "Cancelled" },
  }[status];

  return (
    <Badge
      color={config.color}
      leftSection={
        <FontAwesomeIcon
          icon={config.icon}
          spin={status === "processing"}
          size="xs"
          aria-hidden="true"
        />
      }
    >
      {config.label}
    </Badge>
  );
};

interface JobRowProps {
  job: TranslatorJob;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const min = Math.floor(seconds / 60);
  const sec = Math.round(seconds % 60);
  return `${min}m ${sec}s`;
}

function nonNegativeNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined;
}

function formatCostValue(cost: number): string {
  if (cost > 0 && cost < 0.0001) return "<$0.0001";
  return `$${cost.toFixed(4)}`;
}

const JobRow: FunctionComponent<JobRowProps> = ({ job }) => {
  const modelUsed = job.result?.model_used || job.model;
  // Prefer top-level tokensUsed (live from API), fall back to result
  const tokensUsed = nonNegativeNumber(
    job.tokensUsed ?? job.result?.tokens_used,
  );
  const reportedCost = nonNegativeNumber(job.totalCost);

  // Build media title
  const langPair =
    job.sourceLanguage && job.targetLanguage
      ? ` (${job.sourceLanguage.toUpperCase()} → ${job.targetLanguage.toUpperCase()})`
      : "";
  const mediaTitle = job.title
    ? `${job.title}${langPair}`
    : job.filename
      ? `${job.filename}${langPair}`
      : langPair || "-";

  // Prefer reported duration, including an explicit zero, over timestamps.
  let durationSec = nonNegativeNumber(job.elapsedSeconds);
  let durationIsLive = false;
  if (job.elapsedSeconds == null && job.startedAt) {
    const started = new Date(job.startedAt).getTime();
    if (job.completedAt) {
      durationSec = nonNegativeNumber(
        (new Date(job.completedAt).getTime() - started) / 1000,
      );
    } else if (job.status === "processing") {
      // eslint-disable-next-line react-hooks/purity
      durationSec = nonNegativeNumber((Date.now() - started) / 1000);
      durationIsLive = true;
    }
  }
  const durationStr =
    durationSec == null
      ? "-"
      : `${formatDuration(durationSec)}${durationIsLive ? "..." : ""}`;
  const tokenRate =
    tokensUsed != null && durationSec != null && durationSec > 0
      ? nonNegativeNumber(tokensUsed / durationSec)
      : undefined;
  const tokenRateStr = tokenRate == null ? "-" : tokenRate.toFixed(0);

  // Show translated lines when available.
  const progressLabel =
    job.completedLines && job.totalLines
      ? `${job.completedLines}/${job.totalLines} lines`
      : `${job.progress}%`;

  // Batch info for tooltip
  const batchInfo =
    job.completedBatches && job.totalBatches
      ? `Batch ${job.completedBatches}/${job.totalBatches}`
      : undefined;

  return (
    <Table.Tr
      className={job.status === "completed" ? classes.completedRow : undefined}
    >
      <Table.Td>
        <Tooltip label={job.jobName || job.jobId} openDelay={300}>
          <Text size="sm" truncate style={{ maxWidth: 200 }}>
            {mediaTitle}
          </Text>
        </Tooltip>
      </Table.Td>
      <Table.Td>
        {job.error ? (
          <Tooltip label={job.error} multiline w={350} withArrow>
            <span>
              <StatusBadge status={job.status} />
            </span>
          </Tooltip>
        ) : (
          <StatusBadge status={job.status} />
        )}
      </Table.Td>
      <Table.Td>
        {job.status === "processing" ? (
          <Tooltip
            label={[progressLabel, batchInfo].filter(Boolean).join(" · ")}
            withArrow
          >
            <Progress value={job.progress} size="sm" animated />
          </Tooltip>
        ) : (
          <Text size="sm">{progressLabel}</Text>
        )}
      </Table.Td>
      <Table.Td>
        <Text
          size="xs"
          ff="monospace"
          c="var(--bz-text-tertiary)"
          truncate
          style={{ maxWidth: 180 }}
        >
          {modelUsed || "-"}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="xs" ff="monospace">
          {tokensUsed != null ? tokensUsed.toLocaleString() : "-"}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="xs" ff="monospace">
          {reportedCost != null ? (
            <Tooltip
              label={`${formatCostValue(reportedCost)} reported charge`}
              withArrow
            >
              <Text component="span" size="xs" ff="monospace" c="green.4">
                {formatCostValue(reportedCost)}
              </Text>
            </Tooltip>
          ) : (
            "-"
          )}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="xs" ff="monospace">
          {durationStr}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text
          size="xs"
          ff="monospace"
          c={tokenRateStr !== "-" ? "green.4" : "dimmed"}
        >
          {tokenRateStr}
        </Text>
      </Table.Td>
    </Table.Tr>
  );
};

interface StatCardProps {
  label: string;
  value: number | undefined;
  color: string;
}

const StatCard: FunctionComponent<StatCardProps> = ({
  label,
  value,
  color,
}) => (
  <Card
    withBorder
    p="xs"
    className={classes.statCard}
    style={{ borderLeftColor: `var(--bz-stat-${color})` }}
  >
    <Text
      size="2rem"
      fw={700}
      lh={1}
      aria-label={value == null ? "Count unavailable" : undefined}
    >
      {value ?? "-"}
    </Text>
    <Text
      size="xs"
      c="var(--bz-text-tertiary)"
      tt="uppercase"
      style={{ letterSpacing: 0.5 }}
      fw={600}
    >
      {label}
    </Text>
  </Card>
);

interface TranslatorStatusPanelProps {
  enabled?: boolean;
}

export const TranslatorStatusPanel: FunctionComponent<
  TranslatorStatusPanelProps
> = ({ enabled = true }) => {
  const [, setRetryKey] = useState(0);

  const {
    data: status,
    isError: statusError,
    error: statusErr,
    isLoading: statusLoading,
    refetch: refetchStatus,
  } = useTranslatorStatus(enabled);
  const {
    data: jobsData,
    isError: jobsError,
    refetch: refetchJobs,
  } = useTranslatorJobs(enabled && !statusError);
  const handleRetry = useCallback(() => {
    setRetryKey((k) => k + 1);
    void refetchStatus();
    void refetchJobs();
  }, [refetchStatus, refetchJobs]);

  const availableStatus = statusError ? undefined : status;
  const queueStats = (
    <SimpleGrid cols={{ base: 2, sm: 4 }}>
      <StatCard
        label="Pending"
        value={availableStatus?.bazarr_queue?.pending}
        color="queued"
      />
      <StatCard
        label="Processing"
        value={availableStatus?.queue.processing}
        color="processing"
      />
      <StatCard
        label="Completed"
        value={availableStatus?.queue.completed}
        color="completed"
      />
      <StatCard
        label="Failed"
        value={availableStatus?.queue.failed}
        color="failed"
      />
    </SimpleGrid>
  );

  // Show loading state on first load
  if (statusLoading && !status) {
    return (
      <Stack gap="md" mt="md">
        <Card withBorder p="md">
          <Group justify="center" py="md">
            <FontAwesomeIcon icon={faSpinner} spin aria-hidden="true" />
            <Text c="var(--bz-text-tertiary)">
              Connecting to AI Subtitle Translator...
            </Text>
          </Group>
        </Card>
        {queueStats}
      </Stack>
    );
  }

  if (statusError || jobsError) {
    const errorMessage =
      statusErr instanceof Error
        ? statusErr.message
        : "The translation service isn't responding. Check that it's running at the configured URL, then hit retry.";

    return (
      <Stack gap="md" mt="md">
        <Alert
          color="yellow"
          title="AI Subtitle Translator Unavailable"
          icon={
            <FontAwesomeIcon icon={faExclamationTriangle} aria-hidden="true" />
          }
        >
          <Text size="sm" mb="sm">
            {errorMessage}
          </Text>
          <Button
            size="xs"
            variant="light"
            leftSection={
              <FontAwesomeIcon icon={faRefresh} aria-hidden="true" />
            }
            onClick={handleRetry}
          >
            Retry Connection
          </Button>
        </Alert>
        {queueStats}
      </Stack>
    );
  }

  return (
    <Stack gap="md" mt="md">
      {/* Service Status */}
      <Card withBorder>
        <Group justify="space-between" mb="md">
          <Title order={5}>AI Subtitle Translator Service</Title>
          <div role="status" aria-live="assertive">
            <Group gap={6}>
              <Text
                c={status?.healthy ? "green.5" : "red.5"}
                fw={700}
                size="sm"
                component="span"
                className={
                  status?.healthy ? classes.promptConnected : undefined
                }
              >
                ❯
              </Text>
              <Text
                c={status?.healthy ? "green.5" : "red.5"}
                fw={600}
                size="sm"
                tt="uppercase"
                style={{ letterSpacing: 0.5 }}
              >
                {status?.healthy ? "Connected" : "Disconnected"}
              </Text>
            </Group>
          </div>
        </Group>
      </Card>

      {/* Queue Stats */}
      {queueStats}

      {/* Jobs Table */}
      <Card withBorder>
        <Title order={5} mb="md" id="translation-jobs-heading">
          Translation Jobs
        </Title>
        <Text size="xs" c="dimmed" mb="sm">
          Total tokens per elapsed job second. Includes prompt tokens and
          reported retry usage. Charges reported by the translation service, in
          USD.
        </Text>
        {jobsData && jobsData.jobs.length > 0 ? (
          <Table.ScrollContainer minWidth={600}>
            <Table
              striped
              highlightOnHover
              aria-labelledby="translation-jobs-heading"
            >
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Media</Table.Th>
                  <Table.Th>Status</Table.Th>
                  <Table.Th>Progress</Table.Th>
                  <Table.Th>Model</Table.Th>
                  <Table.Th>Tokens</Table.Th>
                  <Table.Th>Reported cost</Table.Th>
                  <Table.Th>Duration</Table.Th>
                  <Table.Th>Total tokens/s</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {jobsData.jobs.map((job) => (
                  <JobRow key={job.jobId} job={job} />
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        ) : (
          <Stack align="center" py="xl" gap="xs">
            <Text c="var(--bz-text-tertiary)" ta="center">
              No translations in progress
            </Text>
            <Text c="var(--bz-text-disabled)" ta="center" size="xs">
              Start one from the Series or Movies page
            </Text>
          </Stack>
        )}
      </Card>
    </Stack>
  );
};

/**
 * Wrapper component that accesses the form context and passes values to TranslatorStatusPanel.
 * This must be rendered inside a FormContext (i.e., inside Layout component).
 */
export const TranslatorStatusPanelWithFormContext: FunctionComponent = () => {
  return <TranslatorStatusPanel />;
};

export default TranslatorStatusPanel;
