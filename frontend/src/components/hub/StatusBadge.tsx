import { FunctionComponent } from "react";
import { Tooltip } from "@mantine/core";
import { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import clsx from "clsx";
import {
  getJobStateMeta,
  getProviderStateMeta,
  JobState,
  ProviderState,
  StatusMeta,
  Tone,
} from "@/components/hub/utils";
import styles from "@/components/hub/hub.module.scss";

type BadgeVariant = "dot" | "icon";

interface StatusBadgeProps {
  tone: Tone;
  label: string;
  icon?: IconDefinition;
  variant?: BadgeVariant;
  tooltip?: string;
  spin?: boolean;
}

export const StatusBadge: FunctionComponent<StatusBadgeProps> = ({
  tone,
  label,
  icon,
  variant = "dot",
  tooltip,
  spin,
}) => {
  const cls = clsx(styles.pill, styles[`tone-${tone}`]);
  // The tooltip says more than the label does, so a screen reader gets it too.
  const ariaLabel = tooltip
    ? `${label} (${tone}). ${tooltip}`
    : `${label} (${tone})`;
  const body = (
    <span className={cls} role="status" aria-label={ariaLabel}>
      {variant === "icon" && icon ? (
        <FontAwesomeIcon
          icon={icon}
          className={clsx(styles.pillIcon, spin && styles.spin)}
        />
      ) : (
        <span className={styles.pillDot} aria-hidden="true" />
      )}
      <span>{label}</span>
    </span>
  );
  if (tooltip) {
    return <Tooltip label={tooltip}>{body}</Tooltip>;
  }
  return body;
};

export const ProviderStatusBadge: FunctionComponent<{
  state: ProviderState;
  pendingRestart?: boolean;
  activeVersion?: string | null;
  needsRepair?: boolean;
  /** The official catalog revoked the active release. Shown with its reason, whatever else is going on. */
  revoked?: boolean;
  revokedReason?: string | null;
}> = ({
  state,
  pendingRestart,
  activeVersion,
  needsRepair,
  revoked,
  revokedReason,
}) => {
  const meta: StatusMeta = getProviderStateMeta(state);
  if (revoked && state !== "removed") {
    return (
      <StatusBadge
        tone="danger"
        label="Revoked"
        icon={meta.icon}
        variant="dot"
        tooltip={
          revokedReason ??
          "The official catalog revoked this version. Update it or remove it."
        }
      />
    );
  }
  if (needsRepair && !pendingRestart) {
    return (
      <StatusBadge
        tone="danger"
        label="Needs repair"
        icon={meta.icon}
        variant="dot"
        tooltip="This plugin's Python environment is missing or broken. Use Repair to rebuild it."
      />
    );
  }
  if (state === "removed" && !pendingRestart) {
    // Removed while Bazarr+ runs: the row stays until the calls the plugin already took are done.
    return (
      <StatusBadge
        tone="warning"
        label="Removing"
        icon={meta.icon}
        variant="dot"
        tooltip="This plugin is being removed. It finishes when the calls it is running end."
      />
    );
  }
  if (pendingRestart) {
    if (state === "removed") {
      return (
        <StatusBadge
          tone="warning"
          label="Removal staged"
          icon={meta.icon}
          variant="dot"
          tooltip="Restart Bazarr+ to remove this plugin"
        />
      );
    }
    if (state === "staged" && activeVersion) {
      return (
        <StatusBadge
          tone="warning"
          label="Update staged"
          icon={meta.icon}
          variant="dot"
          tooltip="Restart Bazarr+ to activate the staged update"
        />
      );
    }
    if (state === "staged") {
      return (
        <StatusBadge
          tone="warning"
          label="Install staged"
          icon={meta.icon}
          variant="dot"
          tooltip="Restart Bazarr+ to activate this plugin"
        />
      );
    }
    return (
      <StatusBadge
        tone="warning"
        label="Restart required"
        icon={meta.icon}
        variant="dot"
        tooltip="Restart Bazarr+ to activate the staged version"
      />
    );
  }
  return <StatusBadge tone={meta.tone} label={meta.label} icon={meta.icon} />;
};

export const JobStatusBadge: FunctionComponent<{ state: JobState }> = ({
  state,
}) => {
  const meta = getJobStateMeta(state);
  return (
    <StatusBadge
      tone={meta.tone}
      label={meta.label}
      icon={meta.icon}
      variant="icon"
      spin={state === "running"}
    />
  );
};
