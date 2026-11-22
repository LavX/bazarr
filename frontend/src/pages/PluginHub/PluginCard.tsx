import { FunctionComponent } from "react";
import { Badge, Button, Tooltip, VisuallyHidden } from "@mantine/core";
import {
  faArrowUp,
  faCheck,
  faCircleCheck,
  faCircleInfo,
  faDownload,
  faShieldHalved,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { CapabilityStatus } from "@/components/hub/CapabilityStatus";
import { HubCard } from "@/components/hub/HubCard";
import { ProviderStatusBadge } from "@/components/hub/StatusBadge";
import { TrustBadge } from "@/components/hub/TrustBadge";
import { kindLabel, permissionLabel } from "@/components/plugin-consent/format";
import { PluginMenu } from "./components/PluginMenu";
import {
  FailureNote,
  PluginStatusBadge,
  ReviewNotice,
  RuntimeLine,
} from "./components/PluginState";
import {
  isAwaitingRestart,
  isInstalled,
  isRemoving,
  permissionMark,
  PluginRow,
  SandboxFacts,
  sandboxNote as sandboxNoteOf,
} from "./model";
import { PluginStatus, pluginStatus } from "./status";
import pageStyles from "./PluginHub.module.scss";
import styles from "@/components/hub/hub.module.scss";

export interface PluginCardProps {
  row: PluginRow;
  sandbox: (SandboxFacts & { label?: string; pending?: boolean }) | undefined;
  onInstall: (row: PluginRow) => void;
  onUpdate: (row: PluginRow) => void;
  /** This plugin's update is being applied. */
  updating: boolean;
  /** Another plugin's install or update is going ahead, so this one waits its turn. */
  blocked?: boolean;
  /** What the open jobs are doing, by plugin id. */
  openJobs: Map<string, Set<string>>;
  /** A request for this plugin (turn on or off, repair, remove) is on its way. */
  working: boolean;
  /** A request for another plugin is on its way. */
  elsewhere: boolean;
  /** The review notice of this plugin is being dismissed. */
  dismissing: boolean;
  /** Its update is held until the person agrees to what it adds. */
  needsConsent: boolean;
  onToggle: (row: PluginRow) => void;
  onRepair: (row: PluginRow) => void;
  onLog: (row: PluginRow) => void;
  onRemove: (row: PluginRow) => void;
  onDismissReview: (row: PluginRow) => void;
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
  blocked = false,
  working,
  elsewhere,
  openJobs,
}) => {
  const { entry } = row;
  // Something is already being done to this plugin, so an update does not start on top of it.
  const occupied = working || (openJobs.get(row.id)?.size ?? 0) > 0;
  // A row that only records a failed install is not an installation: the install is offered again.
  const installation = isInstalled(row.installation) ? row.installation : null;
  if (installation && isRemoving(installation)) {
    return (
      <Tooltip label="This plugin is being removed. Wait for it to finish.">
        <span className={styles.hubCardPendingText}>Removing</span>
      </Tooltip>
    );
  }
  if (installation && isAwaitingRestart(installation)) {
    return (
      <Tooltip label="Restart Bazarr+ to finish this change">
        <span className={styles.hubCardPendingText}>Awaiting restart</span>
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
        disabled={blocked || occupied || elsewhere}
        title={
          blocked || elsewhere
            ? "Another plugin is being changed. Wait for it to finish."
            : occupied
              ? "Something is being done to this plugin. Wait for it to finish."
              : undefined
        }
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
  if (!entry) {
    return (
      <Button size="xs" variant="light" color="gray" disabled>
        Unavailable
      </Button>
    );
  }
  return (
    <Button
      size="xs"
      variant="light"
      disabled={blocked}
      title={
        blocked
          ? "Another plugin is being changed. Wait for it to finish."
          : undefined
      }
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
  const sandboxNote = sandboxNoteOf(facts.permissions, sandbox);
  const needs = facts.requires;
  const status: PluginStatus | null = pluginStatus(row, props.openJobs);
  const installed = isInstalled(installation) ? installation : null;
  const busy = props.working || (props.openJobs.get(row.id)?.size ?? 0) > 0;
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
      menu={
        installed ? (
          <PluginMenu
            row={row}
            enabledDependents={row.enabledDependents}
            busy={busy}
            elsewhere={props.elsewhere}
            onToggle={props.onToggle}
            onRepair={props.onRepair}
            onLog={props.onLog}
            onRemove={props.onRemove}
          />
        ) : undefined
      }
      description={facts.description || undefined}
      note={
        installation?.trust_note ??
        (row.installation && !isInstalled(row.installation)
          ? (row.installation.last_error ??
            "The last install did not finish. You can try it again.")
          : undefined)
      }
      extra={
        <>
          {installed && <RuntimeLine runtime={installed.runtime} />}
          {installed && (
            <CapabilityStatus capabilities={installed.capabilities} />
          )}
          {status?.cause && (
            <FailureNote cause={status.cause} onLog={() => props.onLog(row)} />
          )}
          {installed?.review_pending && (
            <ReviewNotice
              name={row.name}
              installation={installed}
              sandbox={sandbox}
              dismissing={props.dismissing}
              onDismiss={() => props.onDismissReview(row)}
            />
          )}
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
            // On hover for a mouse, and in the text for a screen reader, since a badge has no role to name.
            const explanation =
              mark === "enforced"
                ? "This host holds the plugin to this permission."
                : "The plugin says it needs this, and nothing here confines it.";
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
                title={explanation}
              >
                {permissionLabel(name)}: {mark}
                <VisuallyHidden>. {explanation}</VisuallyHidden>
              </Badge>
            );
          })}
        </>
      }
      pills={
        <>
          {installation &&
            (isAwaitingRestart(installation) || !status ? (
              <ProviderStatusBadge
                state={installation.state}
                pendingRestart={isAwaitingRestart(installation)}
                activeVersion={installation.active_version}
                needsRepair={installation.needs_repair}
              />
            ) : (
              <PluginStatusBadge status={status} />
            ))}
          {props.needsConsent && (
            <span className={`${styles.pill} ${styles["tone-warning"]}`}>
              <span className={styles.pillDot} aria-hidden="true" />
              Needs your consent
            </span>
          )}
          <TrustBadge trusted={row.trusted} />
        </>
      }
      action={<Action {...props} />}
    />
  );
};
