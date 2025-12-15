import { FunctionComponent, useState, useCallback } from "react";
import {
  ActionIcon,
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
  faCircle,
  faClock,
  faExclamationTriangle,
  faRefresh,
  faSpinner,
  faTimes,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  TranslatorJob,
  useTranslatorJobs,
  useTranslatorStatus,
  useCancelTranslatorJob,
} from "@/apis/hooks/translator";
import { useStagedValues } from "@/pages/Settings/utilities/FormValues";

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
        />
      }
    >
      {config.label}
    </Badge>
  );
};

interface JobRowProps {
  job: TranslatorJob;
  onCancel: (id: string) => void;
  isDeleting: boolean;
}

const JobRow: FunctionComponent<JobRowProps> = ({
  job,
  onCancel,
  isDeleting,
}) => {
  const canCancel = job.status === "queued";

  return (
    <Table.Tr>
      <Table.Td>
        <Tooltip label={job.jobId}>
          <Text size="sm" truncate style={{ maxWidth: 120 }}>
            {job.jobId.substring(0, 8)}...
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
        <Text size="sm" c="dimmed" truncate style={{ maxWidth: 200 }}>
          {job.message || job.filename || "-"}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="sm">{new Date(job.createdAt).toLocaleTimeString()}</Text>
      </Table.Td>
      <Table.Td>
        {canCancel && (
          <ActionIcon
            color="red"
            variant="subtle"
            onClick={() => onCancel(job.jobId)}
            loading={isDeleting}
          >
            <FontAwesomeIcon icon={faTrash} />
          </ActionIcon>
        )}
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
  <Card withBorder p="sm">
    <Text size="xs" c="dimmed">
      {label}
    </Text>
    <Text size="xl" fw={700} c={color}>
      {value}
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
  const cancelJob = useCancelTranslatorJob();

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
          <FontAwesomeIcon icon={faSpinner} spin />
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
        icon={<FontAwesomeIcon icon={faExclamationTriangle} />}
      >
        <Text size="sm" mb="sm">
          {errorMessage}
        </Text>
        <Button
          size="xs"
          variant="light"
          leftSection={<FontAwesomeIcon icon={faRefresh} />}
          onClick={handleRetry}
        >
          Retry Connection
        </Button>
      </Alert>
    );
  }

  const handleCancel = (jobId: string) => {
    cancelJob.mutate(jobId);
  };

  return (
    <Stack gap="md" mt="md">
      {/* Service Status */}
      <Card withBorder>
        <Group justify="space-between" mb="md">
          <Title order={5}>AI Subtitle Translator Service</Title>
          {status?.healthy ? (
            <Badge
              color="green"
              leftSection={<FontAwesomeIcon icon={faCircle} size="xs" />}
            >
              Connected
            </Badge>
          ) : (
            <Badge color="red">Disconnected</Badge>
          )}
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

        {/* Service Status (Runtime State) */}
        {status && (
          <>
            <Text size="xs" c="dimmed" mt="md" mb="xs" fw={600}>
              Service Runtime State
            </Text>
            <Group gap="xl">
              <Box>
                <Text size="xs" c="dimmed">
                  Active Model
                </Text>
                <Text size="sm">{status.config.model}</Text>
              </Box>
              <Box>
                <Text size="xs" c="dimmed">
                  API Key Status
                </Text>
                <Text size="sm">
                  {status.config.apiKeyConfigured
                    ? "✓ Configured"
                    : "✗ Not Set"}
                </Text>
              </Box>
              <Box>
                <Text size="xs" c="dimmed">
                  Max Concurrent
                </Text>
                <Text size="sm">{status.queue.maxConcurrent}</Text>
              </Box>
            </Group>
          </>
        )}
      </Card>

      {/* Queue Stats */}
      {status && (
        <SimpleGrid cols={4}>
          <StatCard
            label="Processing"
            value={status.queue.processing}
            color="blue"
          />
          <StatCard label="Queued" value={status.queue.queued} color="gray" />
          <StatCard
            label="Completed"
            value={status.queue.completed}
            color="green"
          />
          <StatCard label="Failed" value={status.queue.failed} color="red" />
        </SimpleGrid>
      )}

      {/* Jobs Table */}
      <Card withBorder>
        <Title order={5} mb="md">
          Translation Jobs
        </Title>
        {jobsData && jobsData.jobs.length > 0 ? (
          <Table.ScrollContainer minWidth={600}>
            <Table striped highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Job ID</Table.Th>
                  <Table.Th>Status</Table.Th>
                  <Table.Th>Progress</Table.Th>
                  <Table.Th>Message</Table.Th>
                  <Table.Th>Created</Table.Th>
                  <Table.Th>Actions</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {jobsData.jobs.map((job) => (
                  <JobRow
                    key={job.jobId}
                    job={job}
                    onCancel={handleCancel}
                    isDeleting={cancelJob.isPending}
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
  const staged = useStagedValues();

  const savedApiKey = staged["settings-translator-openrouter_api_key"] as
    | string
    | undefined;
  const savedModel = staged["settings-translator-openrouter_model"] as
    | string
    | undefined;
  const savedMaxConcurrent = staged[
    "settings-translator-openrouter_max_concurrent"
  ] as number | undefined;

  return (
    <TranslatorStatusPanel
      savedApiKey={savedApiKey}
      savedModel={savedModel}
      savedMaxConcurrent={savedMaxConcurrent}
    />
  );
};

export default TranslatorStatusPanel;
