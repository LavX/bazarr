import { FunctionComponent } from "react";
import { Alert, Button } from "@mantine/core";
import { faCircleInfo, faHeartPulse } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type { PluginInstallation } from "@/apis/raw/plugins";
import { StatusBadge } from "@/components/hub/StatusBadge";
import { permissionLabel } from "@/components/plugin-consent/format";
import {
  manifestFacts,
  permissionMark,
  SandboxFacts,
} from "@/pages/PluginHub/model";
import { PluginStatus, runtimeLine } from "@/pages/PluginHub/status";
import { explainFailure } from "./failure";
import styles from "@/pages/PluginHub/PluginHub.module.scss";

/** The one status of an installed plugin, as text and an icon. */
export const PluginStatusBadge: FunctionComponent<{ status: PluginStatus }> = ({
  status,
}) => (
  <StatusBadge
    tone={status.tone}
    label={status.label}
    icon={status.icon}
    variant="icon"
    spin={status.spin}
    tooltip={status.tooltip}
  />
);

/** What the plugin's worker is doing, under the status. */
export const RuntimeLine: FunctionComponent<{
  runtime: PluginInstallation["runtime"];
}> = ({ runtime }) => {
  const line = runtimeLine(runtime);
  if (!line) {
    return null;
  }
  return (
    <div className={styles.runtime}>
      <FontAwesomeIcon icon={faHeartPulse} aria-hidden="true" />
      <span>
        <span className={styles.relationLabel}>Runtime</span> {line}
      </span>
    </div>
  );
};

/** Why a plugin needs attention, with the next step, in words a person can act on. */
export const FailureNote: FunctionComponent<{
  cause: string;
  onLog: () => void;
}> = ({ cause, onLog }) => {
  const copy = explainFailure(cause);
  if (!copy) {
    return null;
  }
  return (
    <div className={styles.failure} role="note">
      <span>{copy.cause}</span> <span>{copy.next}</span>{" "}
      <Button variant="subtle" size="compact-xs" onClick={onLog}>
        Open the log
      </Button>
    </div>
  );
};

/**
 * The notice for a plugin that was installed for the person, such as one carried over from an earlier
 * version: what it may do, once. Dismissing it is stored on the plugin's own record.
 */
export const ReviewNotice: FunctionComponent<{
  name: string;
  installation: PluginInstallation;
  sandbox: SandboxFacts | undefined;
  onDismiss: () => void;
  dismissing: boolean;
}> = ({ name, installation, sandbox, onDismiss, dismissing }) => {
  const facts = manifestFacts(installation);
  const endpoints = facts.endpoints;
  return (
    <section aria-label={`Review ${name}`}>
      <Alert
        color="blue"
        variant="light"
        icon={<FontAwesomeIcon icon={faCircleInfo} />}
        title={`${name} was installed for you`}
      >
        <div className={styles.reviewBody}>
          <p>
            Here is what it may do. Nothing changes if you leave it as it is.
          </p>
          {facts.permissions.length > 0 && (
            <ul aria-label="Permissions">
              {facts.permissions.map((permission) => (
                <li key={permission}>
                  {permissionLabel(permission)}:{" "}
                  {permissionMark(permission, sandbox)}
                </li>
              ))}
            </ul>
          )}
          {endpoints.length > 0 && (
            <p>
              It sends data to:{" "}
              <span className={styles.code}>{endpoints.join(", ")}</span>
            </p>
          )}
          <Button
            size="compact-sm"
            variant="default"
            loading={dismissing}
            onClick={onDismiss}
          >
            Dismiss
          </Button>
        </div>
      </Alert>
    </section>
  );
};
