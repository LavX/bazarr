import { FunctionComponent } from "react";
import { Badge, Button, Tooltip } from "@mantine/core";
import {
  faArrowUp,
  faCheck,
  faCircleCheck,
  faCircleInfo,
  faDownload,
  faShieldHalved,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { HubCard } from "@/components/hub/HubCard";
import { ProviderStatusBadge, StatusBadge } from "@/components/hub/StatusBadge";
import { TrustBadge } from "@/components/hub/TrustBadge";
import { kindLabel, permissionLabel } from "@/components/plugin-consent/format";
import { permissionMark, PluginRow, SandboxFacts } from "./model";
import pageStyles from "./PluginHub.module.scss";
import styles from "@/components/hub/hub.module.scss";

interface PluginCardProps {
  row: PluginRow;
  sandbox: (SandboxFacts & { label?: string; pending?: boolean }) | undefined;
  onInstall: (row: PluginRow) => void;
  onUpdate: (row: PluginRow) => void;
  /** This plugin's update is being applied. */
  updating: boolean;
}

/** The mark of the one plugin Bazarr+ ships itself, shown only when its source is the official one. */
const OfficialMark: FunctionComponent = () => (
  <Tooltip label="Made by Bazarr+ and installed from its official catalog">
    <span
      className={`${styles.pill} ${styles["tone-success"]}`}
      role="status"
      aria-label="Official"
      style={{ marginLeft: 8, verticalAlign: "middle" }}
    >
      <FontAwesomeIcon icon={faCircleCheck} className={styles.pillIcon} />
      <span>Official</span>
    </span>
  </Tooltip>
);

const Action: FunctionComponent<PluginCardProps> = ({
  row,
  onInstall,
  onUpdate,
  updating,
}) => {
  const { entry, installation } = row;
  if (installation?.pending_restart) {
    return (
      <Tooltip label="Restart Bazarr+ to finish this change">
        <span className={styles.hubCardPendingText}>Pending restart</span>
      </Tooltip>
    );
  }
  if (installation && row.update) {
    return (
      <Button
        size="xs"
        variant="light"
        color="yellow"
        loading={updating}
        onClick={() => onUpdate(row)}
        leftSection={<FontAwesomeIcon icon={faArrowUp} />}
        aria-label={`Update ${row.name} to ${row.update}`}
      >
        Update to {row.update}
      </Button>
    );
  }
  if (installation) {
    return (
      <Button
        size="xs"
        variant="light"
        color="green"
        disabled
        leftSection={<FontAwesomeIcon icon={faCheck} />}
      >
        Installed
      </Button>
    );
  }
  if (entry?.blocked_reason) {
    return (
      <Tooltip label={entry.blocked_reason} multiline w={260}>
        <Button
          size="xs"
          variant="light"
          color="gray"
          data-disabled
          aria-disabled
          onClick={(event) => event.preventDefault()}
          aria-label={`${row.name} is unavailable: ${entry.blocked_reason}`}
        >
          Unavailable
        </Button>
      </Tooltip>
    );
  }
  return (
    <Button
      size="xs"
      variant="light"
      onClick={() => onInstall(row)}
      leftSection={<FontAwesomeIcon icon={faDownload} />}
      aria-label={`Install ${row.name}`}
    >
      Install
    </Button>
  );
};

/** One plugin: what it is, where it comes from, what it may do, what it needs, and its one action. */
export const PluginCard: FunctionComponent<PluginCardProps> = (props) => {
  const { row, sandbox } = props;
  const { installation, facts } = row;
  const sandboxNote =
    facts.permissions.length > 0 && sandbox && !sandbox.workers_sandboxed
      ? (sandbox.label ??
        (sandbox.pending
          ? "Checking the worker sandbox"
          : "Not sandboxed on this host"))
      : null;
  const needs = facts.requires;
  return (
    <HubCard
      name={
        <>
          {row.name}
          {row.official && <OfficialMark />}
        </>
      }
      meta={
        <>
          v{row.version} from {row.sourceName}
        </>
      }
      description={facts.description || undefined}
      note={installation?.trust_note}
      extra={
        <>
          {(needs.length > 0 ||
            facts.extendsId ||
            row.dependents.length > 0) && (
            <ul className={pageStyles.relations} aria-label="Dependencies">
              {facts.extendsId && (
                <li>
                  <span className={pageStyles.relationLabel}>Extends</span>{" "}
                  {facts.extendsId}
                  {facts.extendsCapability
                    ? ` (${facts.extendsCapability})`
                    : ""}
                </li>
              )}
              {needs.length > 0 && (
                <li>
                  <span className={pageStyles.relationLabel}>Needs</span>{" "}
                  {needs.join(", ")}
                </li>
              )}
              {row.dependents.length > 0 && (
                <li>
                  <span className={pageStyles.relationLabel}>Needed by</span>{" "}
                  {row.dependents.join(", ")}
                </li>
              )}
            </ul>
          )}
          {sandboxNote && (
            <div className={pageStyles.note} role="note">
              <FontAwesomeIcon icon={faCircleInfo} aria-hidden="true" />
              {sandboxNote}
            </div>
          )}
        </>
      }
      chips={
        <>
          <Badge size="xs" variant="outline" color="gray">
            {kindLabel(row.kind)}
          </Badge>
          {facts.permissions.map((name) => {
            const mark = permissionMark(name, sandbox);
            return (
              <Badge
                key={name}
                size="xs"
                variant="outline"
                color={mark === "enforced" ? "green" : "gray"}
                leftSection={
                  <FontAwesomeIcon
                    icon={mark === "enforced" ? faShieldHalved : faCircleInfo}
                    aria-hidden="true"
                  />
                }
                title={
                  mark === "enforced"
                    ? "This host holds the plugin to this permission"
                    : "The plugin says it needs this, and nothing here confines it"
                }
              >
                {permissionLabel(name)}: {mark}
              </Badge>
            );
          })}
        </>
      }
      pills={
        <>
          {installation &&
            (installation.enabled === false &&
            installation.state === "active" ? (
              <StatusBadge tone="neutral" label="Off" />
            ) : (
              <ProviderStatusBadge
                state={installation.state}
                pendingRestart={installation.pending_restart}
                activeVersion={installation.active_version}
                needsRepair={installation.needs_repair}
              />
            ))}
          <TrustBadge trusted={row.trusted} />
        </>
      }
      action={<Action {...props} />}
    />
  );
};
