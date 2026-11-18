import { FunctionComponent, useRef } from "react";
import { Alert, Button, Group, Modal, Skeleton, Stack } from "@mantine/core";
import { usePluginLog } from "@/apis/hooks/plugins";
import type { PluginRow } from "@/pages/PluginHub/model";
import { explainFailure } from "./failure";
import styles from "@/pages/PluginHub/PluginHub.module.scss";

interface LogDialogProps {
  /** The plugin whose log to show, or null while none is. */
  row: PluginRow | null;
  onClose: () => void;
  onClosed?: () => void;
}

/** The recent output of a plugin's worker, which the server has already scrubbed, and the last error recorded. */
export const LogDialog: FunctionComponent<LogDialogProps> = ({
  row,
  onClose,
  onClosed,
}) => {
  const shown = useRef(row);
  if (row) {
    shown.current = row;
  }
  const current = row ?? shown.current;
  const log = usePluginLog(row?.id ?? null);
  const error = explainFailure(log.data?.last_error);
  return (
    <Modal
      opened={row !== null}
      onClose={onClose}
      closeButtonProps={{ "aria-label": "Close" }}
      onExitTransitionEnd={onClosed}
      centered
      size="lg"
      title={
        <span className={styles.dialogHeading} tabIndex={-1} data-autofocus>
          {current ? `${current.name} log` : ""}
        </span>
      }
    >
      <Stack gap="md">
        {log.isPending ? (
          <div role="status" aria-busy="true" aria-label="Loading the log">
            <Skeleton height={14} mb={8} />
            <Skeleton height={14} width="70%" />
          </div>
        ) : log.isError ? (
          <Alert color="red" role="alert">
            The log could not be read. Try again in a moment.
          </Alert>
        ) : (
          <>
            {error && (
              <Alert color="red" variant="light" title="Last error">
                {error.cause} {error.next}
              </Alert>
            )}
            {log.data.lines.length > 0 ? (
              <pre
                className={styles.log}
                aria-label="Recent output"
                tabIndex={0}
              >
                {log.data.lines.join("\n")}
              </pre>
            ) : (
              <p className={styles.quiet}>
                No output yet. A plugin writes here only while its worker runs.
              </p>
            )}
          </>
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Close
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
};
