import { FunctionComponent } from "react";
import {
  faCircleCheck,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import type {
  ProviderHubCapability,
  ProviderHubCapabilityHealth,
} from "@/apis/raw/providerHub";
import { StatusBadge } from "@/components/hub/StatusBadge";
import styles from "@/components/hub/hub.module.scss";

const LABELS: Record<string, string> = {
  "flaresolverr.v1": "FlareSolverr",
  "host.captcha.v1": "Captcha solver",
};

/** The name a person knows a capability by. An id the host has no name for is shown as it is. */
export function capabilityLabel(capability: string): string {
  return LABELS[capability] ?? capability;
}

/** What the last check of a service someone else runs found, in words. */
export function healthText(health: ProviderHubCapabilityHealth): string {
  return health.state === "ready"
    ? "Last check: answered"
    : "Last check: no answer";
}

/**
 * What a plugin asks the host for and whether each is ready: one line per capability, with the
 * reason in words when it is not. Ready or not is said in the badge's text as well as its colour.
 * A service the user pointed Bazarr+ at is labelled External, with the result of its last check:
 * Bazarr+ does not run it, so the check only says whether it answered.
 */
export const CapabilityStatus: FunctionComponent<{
  capabilities: ProviderHubCapability[] | undefined;
}> = ({ capabilities }) => {
  if (!capabilities || capabilities.length === 0) {
    return null;
  }
  return (
    <ul className={styles.capabilities} aria-label="Host capabilities">
      {capabilities.map((item) => (
        <li key={item.capability} className={styles.capabilityRow}>
          <span className={styles.capabilityName}>
            {capabilityLabel(item.capability)}
          </span>
          <StatusBadge
            tone={item.ready ? "success" : "warning"}
            label={item.ready ? "Ready" : "Not ready"}
            icon={item.ready ? faCircleCheck : faTriangleExclamation}
            variant="icon"
          />
          {item.external && (
            <StatusBadge
              tone="neutral"
              label="External"
              tooltip="Bazarr+ does not run this service. It uses the address you set."
            />
          )}
          {item.external && item.health && (
            <span className={styles.capabilityReason}>
              {healthText(item.health)}
            </span>
          )}
          {!item.ready && item.reason && (
            <span className={styles.capabilityReason}>{item.reason}</span>
          )}
        </li>
      ))}
    </ul>
  );
};
