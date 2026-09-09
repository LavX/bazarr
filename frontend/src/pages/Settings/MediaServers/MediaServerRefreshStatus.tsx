import {
  Alert,
  Button,
  Group,
  Stack,
  Text as MantineText,
} from "@mantine/core";
import {
  useMediaServerStatus,
  useRetryPendingMediaServer,
} from "@/apis/hooks/mediaServers";
import type {
  MediaServerInstance,
  RefreshStatus,
} from "@/apis/raw/mediaServers";

const statusMessages: Record<RefreshStatus["state"], string> = {
  idle: "No pending refreshes.",
  pending: "Refresh pending.",
  requested: "Refresh sent.",
  confirmed: "Refresh confirmed.",
  unconfirmed: "Refresh unconfirmed.",
};

function refreshCount(count: number) {
  return `${count} refresh${count === 1 ? "" : "es"}`;
}

function getStatusErrorMessage(errorCode: RefreshStatus["error_code"]) {
  switch (errorCode) {
    case "queue_overflow":
      return "The refresh queue overflowed. Retry pending covers the refreshes it kept, but dropped ones need a new refresh.";
    case "sidecar_unsupported":
      return "Silo only refreshes subtitles stored beside the video file. Move the subtitle there, then retry.";
    default:
      return null;
  }
}

export default function MediaServerRefreshStatus({
  instance,
  enabled,
  hasChanges,
}: {
  instance: MediaServerInstance;
  enabled: boolean;
  hasChanges: boolean;
}) {
  const { kind, id } = instance;
  const status = useMediaServerStatus(kind, id);
  const retry = useRetryPendingMediaServer(kind, id);
  const currentStatus = !status.isError ? status.data : undefined;
  const statusErrorMessage =
    currentStatus && getStatusErrorMessage(currentStatus.error_code);
  const warning =
    currentStatus &&
    (currentStatus.pending > 0 ||
      currentStatus.state === "unconfirmed" ||
      currentStatus.error_code !== null);
  return (
    <Stack gap="xs">
      {status.isPending ? (
        <MantineText size="sm">Loading refresh status...</MantineText>
      ) : !currentStatus ? (
        <Alert color="yellow">
          Refresh status unavailable. Check that the Bazarr API is reachable.
        </Alert>
      ) : (
        <Alert color={warning ? "yellow" : "gray"}>
          {statusErrorMessage ??
            statusMessages[
              currentStatus.state === "idle" && currentStatus.pending > 0
                ? "pending"
                : currentStatus.state
            ]}
          {currentStatus.pending > 0 &&
            ` ${refreshCount(currentStatus.pending)} queued.`}
          {currentStatus.error_code !== null &&
            !statusErrorMessage &&
            " Check the saved connection, server access and path mappings."}
        </Alert>
      )}
      <MantineText size="sm" c="dimmed">
        Confirmation means the server finished a scan, not that it found the
        subtitle.
      </MantineText>
      {kind === "silo" && (
        <MantineText size="sm" c="dimmed">
          When Silo runs across machines, synchronize the
          application/event-publisher and database clocks so refresh completion
          can be confirmed.
        </MantineText>
      )}
      <MantineText size="sm" c="dimmed">
        Retry pending uses the saved connection settings.
      </MantineText>
      {!enabled && (
        <MantineText size="sm" c="dimmed">
          Enable this instance and save the master switch to retry pending
          refreshes.
        </MantineText>
      )}
      {hasChanges && (
        <MantineText size="sm" c="dimmed">
          Save your changes before retrying pending refreshes.
        </MantineText>
      )}
      <Group>
        <Button
          type="button"
          variant="light"
          loading={retry.isPending}
          disabled={
            !enabled ||
            hasChanges ||
            !currentStatus ||
            currentStatus.pending === 0
          }
          onClick={() => retry.mutate()}
        >
          Retry pending
        </Button>
      </Group>
      {retry.isSuccess && (
        <Alert color="gray">Queued {refreshCount(retry.data.queued)}.</Alert>
      )}
      {retry.isError && (
        <Alert color="red">
          Could not queue pending refreshes. Check the saved connection and try
          again.
        </Alert>
      )}
    </Stack>
  );
}
