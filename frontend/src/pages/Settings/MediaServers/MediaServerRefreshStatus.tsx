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
  pending:
    "Refresh pending. Check the saved connection and retry when the server is available.",
  requested: "Refresh requested. The server has accepted the request.",
  confirmed: "Refresh confirmed by the server.",
  unconfirmed:
    "Refresh unconfirmed. The request may have reached the server, but completion could not be confirmed.",
};

function getStatusErrorMessage(errorCode: RefreshStatus["error_code"]) {
  switch (errorCode) {
    case "queue_overflow":
      return "Refresh queue capacity exceeded. Some refresh targets were not queued. Retry pending only retries retained targets and cannot recover dropped targets.";
    case "sidecar_unsupported":
      return "Refresh unsupported for this subtitle location. Silo requires subtitles beside the video file. Retrying an unchanged subtitle location will not resolve this.";
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
          Refresh status unavailable. No refresh outcome can be confirmed. Check
          that the Bazarr API is available.
        </Alert>
      ) : (
        <Alert color={warning ? "yellow" : "gray"}>
          {statusErrorMessage ??
            statusMessages[
              currentStatus.state === "idle" && currentStatus.pending > 0
                ? "pending"
                : currentStatus.state
            ]}{" "}
          {currentStatus.pending} pending.
          {currentStatus.error_code !== null &&
            !statusErrorMessage &&
            " Check the saved connection, server access and path mappings."}
          {currentStatus.state !== "idle" &&
            " Subtitle discovery is not confirmed."}
        </Alert>
      )}
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
        <Alert color="gray">
          Queued {retry.data.queued} pending refreshes. Queue acceptance does
          not confirm subtitle discovery.
        </Alert>
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
