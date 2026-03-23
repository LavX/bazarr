import { FunctionComponent, useState, useCallback } from "react";
import {
  Alert,
  Badge,
  Box,
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
import classes from "./TranslatorStatus.module.css";
import {
  TranslatorJob,
  useTranslatorJobs,
  useTranslatorStatus,
} from "@/apis/hooks/translator";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";

interface StatusBadgeProps {
  status: TranslatorJob["status"];
}

const StatusBadge: FunctionComponent<StatusBadgeProps> = ({ status }) => {
  const config = {
    queued: { color: "gray", icon: faClock, label: "Queued" },
    processing: { color: "blue", icon: faSpinner, label: "Processing" },
    completed: { color: "green", icon: faCheck, label: "Completed" },
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

const JobRow: FunctionComponent<JobRowProps> = ({
  job,
}) => {

  const modelUsed = job.result?.model_used || job.model;
  const tokensUsed = job.result?.tokens_used;
  // Build media title from job metadata — never use job.message (that's status text)
  const langPair =
    job.sourceLanguage && job.targetLanguage
      ? ` (${job.sourceLanguage.toUpperCase()} → ${job.targetLanguage.toUpperCase()})`
      : "";
  const mediaTitle = job.title
    ? `${job.title}${langPair}`
    : job.filename
      ? `${job.filename}${langPair}`
      : langPair || "-";

  // Calculate duration and TPS
  let durationStr = "-";
  let tpsStr = "-";
  if (job.startedAt && job.completedAt) {
    const durationMs =
      new Date(job.completedAt).getTime() - new Date(job.startedAt).getTime();
    const durationSec = durationMs / 1000;
    if (durationSec < 60) {
      durationStr = `${durationSec.toFixed(0)}s`;
    } else {
      const min = Math.floor(durationSec / 60);
      const sec = Math.round(durationSec % 60);
      durationStr = `${min}m ${sec}s`;
    }
    if (tokensUsed && durationSec > 0) {
      tpsStr = `${(tokensUsed / durationSec).toFixed(0)} t/s`;
    }
  } else if (job.status === "processing" && job.startedAt) {
    const elapsed =
      (Date.now() - new Date(job.startedAt).getTime()) / 1000;
    if (elapsed < 60) {
      durationStr = `${elapsed.toFixed(0)}s...`;
    } else {
      durationStr = `${Math.floor(elapsed / 60)}m...`;
    }
  }

  return (
    <Table.Tr>
      <Table.Td>
        <Tooltip label={job.jobId}>
          <Text size="sm" truncate style={{ maxWidth: 200 }}>
            {mediaTitle}
          </Text>
        </Tooltip>
      </Table.Td>
      <Table.Td>
        <StatusBadge status={job.status} />
      </Table.Td>
      <Table.Td>
        {job.status === "processing" ? (
          <Progress value={job.progress} size="sm" animated />
        ) : (
          <Text size="sm">{job.progress}%</Text>
        )}
      </Table.Td>
      <Table.Td>
        <Text size="xs" ff="monospace" c="dimmed" truncate style={{ maxWidth: 180 }}>
          {modelUsed || "-"}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="xs" ff="monospace">
          {tokensUsed ? tokensUsed.toLocaleString() : "-"}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="xs" ff="monospace">
          {durationStr}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="xs" ff="monospace" c={tpsStr !== "-" ? "green.4" : "dimmed"}>
          {tpsStr}
        </Text>
      </Table.Td>
    </Table.Tr>
  );
};

interface StatCardProps {
  label: string;
  value: number;
  color: string;
}

const StatCard: FunctionComponent<StatCardProps> = ({
  label,
  value,
  color,
}) => (
  <Card
    withBorder
    p="sm"
    className={classes.statCard}
    style={{ borderLeftColor: `var(--bz-stat-${color})` }}
  >
    <Text size="2rem" fw={700} c="gray.1" lh={1}>
      {value}
    </Text>
    <Text size="xs" c="dimmed" tt="uppercase" style={{ letterSpacing: 0.5 }} fw={600}>
      {label}
    </Text>
  </Card>
);

interface TranslatorStatusPanelProps {
  enabled?: boolean;
  savedApiKey?: string;
  savedModel?: string;
  savedMaxConcurrent?: number;
}

export const TranslatorStatusPanel: FunctionComponent<
  TranslatorStatusPanelProps
> = ({ enabled = true, savedApiKey, savedModel, savedMaxConcurrent }) => {
  const [retryKey, setRetryKey] = useState(0);

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

  // Show loading state on first load
  if (statusLoading && !status) {
    return (
      <Card withBorder mt="md" p="md">
        <Group justify="center" py="md">
          <FontAwesomeIcon icon={faSpinner} spin aria-hidden="true" />
          <Text c="dimmed">Connecting to AI Subtitle Translator...</Text>
        </Group>
      </Card>
    );
  }

  if (statusError || jobsError) {
    const errorMessage =
      statusErr instanceof Error
        ? statusErr.message
        : "Cannot connect to the AI Subtitle Translator service. Make sure it is running at the configured URL.";

    return (
      <Alert
        color="yellow"
        title="AI Subtitle Translator Unavailable"
        mt="md"
        icon={<FontAwesomeIcon icon={faExclamationTriangle} aria-hidden="true" />}
      >
        <Text size="sm" mb="sm">
          {errorMessage}
        </Text>
        <Button
          size="xs"
          variant="light"
          leftSection={<FontAwesomeIcon icon={faRefresh} aria-hidden="true" />}
          onClick={handleRetry}
        >
          Retry Connection
        </Button>
      </Alert>
    );
  }

  return (
    <Stack gap="md" mt="md">
      {/* Service Status */}
      <Card withBorder>
        <Group justify="space-between" mb="md">
          <Title order={5}>AI Subtitle Translator Service</Title>
          <div
            role="status"
            aria-live="assertive"
          >
            <Badge
              color={status?.healthy ? "green" : "red"}
              variant="light"
              size="lg"
              leftSection={
                <Box
                  w={8}
                  h={8}
                  aria-hidden="true"
                  style={{
                    borderRadius: "50%",
                    backgroundColor: status?.healthy
                      ? "var(--bz-stat-completed)"
                      : "var(--bz-stat-failed)",
                  }}
                  className={status?.healthy ? classes.statusDotConnected : undefined}
                />
              }
            >
              {status?.healthy ? "Connected" : "Disconnected"}
            </Badge>
          </div>
        </Group>

        {/* Bazarr Configuration (Saved Settings) */}
        <Text size="xs" c="dimmed" mt="md" mb="xs" fw={600}>
          Bazarr Configuration
        </Text>
        <Group gap="xl">
          <Box>
            <Text size="xs" c="dimmed">
              API Key
            </Text>
            <Text size="sm">{savedApiKey ? "✓ Set" : "✗ Not Set"}</Text>
          </Box>
          <Box>
            <Text size="xs" c="dimmed">
              Model
            </Text>
            <Text size="sm">{savedModel || "Not configured"}</Text>
          </Box>
          <Box>
            <Text size="xs" c="dimmed">
              Max Concurrent
            </Text>
            <Text size="sm">{savedMaxConcurrent ?? "Not set"}</Text>
          </Box>
        </Group>

      </Card>

      {/* Queue Stats */}
      {status && (
        <SimpleGrid cols={{ base: 2, sm: 4 }}>
          <StatCard
            label="Processing"
            value={status.queue.processing}
            color="processing"
          />
          <StatCard label="Queued" value={status.queue.queued} color="queued" />
          <StatCard
            label="Completed"
            value={status.queue.completed}
            color="completed"
          />
          <StatCard label="Failed" value={status.queue.failed} color="failed" />
        </SimpleGrid>
      )}

      {/* Jobs Table */}
      <Card withBorder>
        <Title order={5} mb="md" id="translation-jobs-heading">
          Translation Jobs
        </Title>
        {jobsData && jobsData.jobs.length > 0 ? (
          <Table.ScrollContainer minWidth={600}>
            <Table striped highlightOnHover aria-labelledby="translation-jobs-heading">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Media</Table.Th>
                  <Table.Th>Status</Table.Th>
                  <Table.Th>Progress</Table.Th>
                  <Table.Th>Model</Table.Th>
                  <Table.Th>Tokens</Table.Th>
                  <Table.Th>Duration</Table.Th>
                  <Table.Th>Speed</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {jobsData.jobs.map((job) => (
                  <JobRow
                    key={job.jobId}
                    job={job}
                  />
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        ) : (
          <Text c="dimmed" ta="center" py="xl">
            No translation jobs
          </Text>
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
  // Use useSettingValue which properly merges saved settings with staged overrides
  const savedApiKey = useSettingValue<string>(
    "settings-translator-openrouter_api_key",
  );
  const savedModel = useSettingValue<string>(
    "settings-translator-openrouter_model",
  );
  const savedMaxConcurrent = useSettingValue<number>(
    "settings-translator-openrouter_max_concurrent",
  );

  return (
    <TranslatorStatusPanel
      savedApiKey={savedApiKey ?? undefined}
      savedModel={savedModel ?? undefined}
      savedMaxConcurrent={savedMaxConcurrent ?? undefined}
    />
  );
};

export default TranslatorStatusPanel;
