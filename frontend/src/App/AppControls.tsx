import {
  Menu,
  Tooltip,
  useComputedColorScheme,
  useMantineColorScheme,
} from "@mantine/core";
import {
  faGift,
  faMoon,
  faSliders,
  faSun,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystemStatus } from "@/apis/hooks";
import {
  hasWhatsNew,
  useOpenWhatsNew,
} from "@/components/modals/useWhatsNewAutoOpen";
import SystemActions from "./SystemActions";
import styles from "./AppShell.module.scss";

export default function AppControls({
  expanded = false,
}: {
  expanded?: boolean;
}) {
  const { toggleColorScheme } = useMantineColorScheme();
  const dark = useComputedColorScheme("light") === "dark";
  const { data: status } = useSystemStatus();
  const openWhatsNew = useOpenWhatsNew();
  return (
    <Menu
      position={expanded ? "top-start" : "right-end"}
      withinPortal={!expanded}
      width={240}
      classNames={{ dropdown: styles.navigationMenu }}
    >
      <Tooltip
        label="Appearance and account controls"
        position={expanded ? "top" : "right"}
        openDelay={500}
      >
        <Menu.Target>
          <button
            type="button"
            className={expanded ? styles.expandedControls : styles.railLink}
            aria-label="App controls"
          >
            <span className={styles.railIcon}>
              <FontAwesomeIcon icon={faSliders} />
            </span>
            <span>Controls</span>
          </button>
        </Menu.Target>
      </Tooltip>
      <Menu.Dropdown>
        <Menu.Label>
          Bazarr+{" "}
          {status?.bazarr_version && status.bazarr_version !== "unknown"
            ? status.bazarr_version
            : ""}
        </Menu.Label>
        <Menu.Item
          leftSection={<FontAwesomeIcon icon={dark ? faSun : faMoon} />}
          onClick={() => toggleColorScheme()}
        >
          {dark ? "Switch to light appearance" : "Switch to dark appearance"}
        </Menu.Item>
        {hasWhatsNew() && (
          <Menu.Item
            leftSection={<FontAwesomeIcon icon={faGift} />}
            onClick={openWhatsNew}
          >
            What's new
          </Menu.Item>
        )}
        <Menu.Divider />
        <SystemActions />
      </Menu.Dropdown>
    </Menu>
  );
}
