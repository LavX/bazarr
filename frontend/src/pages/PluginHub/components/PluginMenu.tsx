import { FunctionComponent, ReactNode } from "react";
import { ActionIcon, Menu } from "@mantine/core";
import {
  faEllipsis,
  faFileLines,
  faPowerOff,
  faScrewdriverWrench,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { isInstalled, PluginRow } from "@/pages/PluginHub/model";
import styles from "@/pages/PluginHub/PluginHub.module.scss";

export interface PluginMenuProps {
  row: PluginRow;
  /** The names of the installed plugins that need this one and are switched on. */
  enabledDependents: string[];
  /** Something is being done to this plugin now (a job is open, or a request is on its way). */
  busy: boolean;
  /** Another plugin is being changed, and only one change at a time goes ahead. */
  elsewhere: boolean;
  onToggle: (row: PluginRow) => void;
  onRepair: (row: PluginRow) => void;
  onLog: (row: PluginRow) => void;
  onRemove: (row: PluginRow) => void;
}

/** An item whose label may carry the reason it cannot be used, in text a person can read without hovering. */
const Entry: FunctionComponent<{ label: string; reason?: string | null }> = ({
  label,
  reason,
}) => (
  <span className={styles.menuEntry}>
    <span>{label}</span>
    {reason && <span className={styles.menuReason}>{reason}</span>}
  </span>
);

const Item: FunctionComponent<{
  icon: typeof faPowerOff;
  label: string;
  reason?: string | null;
  disabled?: boolean;
  color?: string;
  onClick: () => void;
  children?: ReactNode;
}> = ({ icon, label, reason, disabled = false, color, onClick }) => (
  // Not the native disabled state: that takes the item out of the arrow-key order, and with it the reason.
  <Menu.Item
    color={color}
    // Set on the element itself: the menu item component does not pass the attribute through.
    ref={(node) => {
      if (disabled) {
        node?.setAttribute("aria-disabled", "true");
      } else {
        node?.removeAttribute("aria-disabled");
      }
    }}
    className={disabled ? styles.menuDisabled : undefined}
    leftSection={<FontAwesomeIcon icon={icon} />}
    onClick={() => {
      if (!disabled) {
        onClick();
      }
    }}
  >
    <Entry label={label} reason={disabled ? reason : null} />
  </Menu.Item>
);

/** The actions of an installed plugin. A disabled one says why. */
export const PluginMenu: FunctionComponent<PluginMenuProps> = ({
  row,
  enabledDependents,
  busy,
  elsewhere,
  onToggle,
  onRepair,
  onLog,
  onRemove,
}) => {
  const installation = row.installation;
  if (!installation || !isInstalled(installation)) {
    return null;
  }
  const isOn = installation.enabled !== false;
  const waiting = installation.pending_restart
    ? "Restart Bazarr+ to finish the change in progress."
    : busy
      ? "Something is being done to this plugin. Wait for it to finish."
      : elsewhere
        ? "Another plugin is being changed. Wait for it to finish."
        : null;
  const needed =
    isOn && enabledDependents.length > 0
      ? `${enabledDependents.join(", ")} needs it. Turn that off first.`
      : null;
  return (
    <Menu position="bottom-end" withinPortal>
      <Menu.Target>
        <ActionIcon
          variant="subtle"
          color="gray"
          aria-label={`${row.name} actions`}
          data-plugin-actions={row.id}
        >
          <FontAwesomeIcon icon={faEllipsis} />
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Item
          icon={faPowerOff}
          label={isOn ? "Turn off" : "Turn on"}
          disabled={Boolean(needed ?? waiting)}
          reason={needed ?? waiting}
          onClick={() => onToggle(row)}
        />
        <Item
          icon={faScrewdriverWrench}
          label="Repair"
          disabled={Boolean(waiting)}
          reason={waiting}
          onClick={() => onRepair(row)}
        />
        <Item icon={faFileLines} label="View log" onClick={() => onLog(row)} />
        <Menu.Divider />
        <Item
          icon={faTrash}
          color="red"
          label="Remove"
          disabled={Boolean(waiting)}
          reason={waiting}
          onClick={() => onRemove(row)}
        />
      </Menu.Dropdown>
    </Menu>
  );
};
