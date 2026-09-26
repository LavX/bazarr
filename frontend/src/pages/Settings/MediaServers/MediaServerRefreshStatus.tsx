import {
  Alert,
  Button,
  Group,
  Stack,
  Text as MantineText,
} from "@mantine/core";
import {
  useMediaServerStatus,
  useRefreshMediaServerLibraries,
  useRetryPendingMediaServer,
} from "@/apis/hooks/mediaServers";
import type {
  MediaServerInstance,
  MediaServerKind,
  RefreshStatus,
} from "@/apis/raw/mediaServers";
import { KINDS_WITH_PATH_MAPPINGS } from "@/apis/raw/mediaServers";

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

function getStatusErrorMessage(
  errorCode: RefreshStatus["error_code"],
  kind: MediaServerKind,
) {
  switch (errorCode) {
    case "queue_overflow":
      return "The refresh queue overflowed. Retry pending covers the refreshes it kept, but dropped ones need a new refresh.";
    case "sidecar_unsupported":
      return "Silo only refreshes subtitles stored beside the video file. Move the subtitle there, then retry.";
    case "library_missing":
      return KINDS_WITH_PATH_MAPPINGS.includes(kind)
        ? null
        : "This instance has no library selected for that kind of media, so there is nothing to scan. Choose one and retry.";
    case "migration_failed":
      return "The one-time import of this kind's old settings did not finish, so its instances are not being refreshed. Restarting Bazarr retries it.";
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
  const rescan = useRefreshMediaServerLibraries(kind, id);
  const currentStatus = !status.isError ? status.data : undefined;
  const statusErrorMessage =
    currentStatus && getStatusErrorMessage(currentStatus.error_code, kind);
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
            (KINDS_WITH_PATH_MAPPINGS.includes(kind)
              ? " Check the saved connection, server access and path mappings."
              : " Check the saved connection, server access and the selected libraries.")}
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
        Retry pending uses the saved connection settings, and only drains
        refreshes that are already queued. Refresh libraries asks the server to
        re-read everything this instance is pointed at, whether or not anything
        is queued.
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
        <Button
          type="button"
          variant="light"
          loading={rescan.isPending}
          disabled={!enabled || hasChanges}
          onClick={() => rescan.mutate()}
        >
          Refresh libraries
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
      {rescan.isSuccess && (
        <Alert color="gray">
          Asked the server to rescan {rescan.data.requested}{" "}
          {rescan.data.requested === 1 ? "library" : "libraries"}.
          {rescan.data.failed > 0 &&
            ` ${rescan.data.failed} could not be rescanned. See the log for details.`}
        </Alert>
      )}
      {rescan.isError && (
        <Alert color="red">
          Could not refresh libraries. Check the saved connection and the
          libraries this instance is pointed at.
        </Alert>
      )}
    </Stack>
  );
}
