import { FunctionComponent } from "react";
import {
  Anchor,
  AppShell,
  Avatar,
  Badge,
  Burger,
  Divider,
  Group,
  Menu,
  useComputedColorScheme,
  useMantineColorScheme,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { faBell } from "@fortawesome/free-regular-svg-icons/faBell";
import {
  faArrowRotateLeft,
  faGear,
  faMoon,
  faPowerOff,
  faSun,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystem, useSystemJobs, useSystemSettings } from "@/apis/hooks";
import { Action, Search } from "@/components";
import { useNavbar } from "@/contexts/Navbar";
import { useIsOnline } from "@/contexts/Online";
import { Environment, useGotoHomepage } from "@/utilities";
import NotificationDrawer from "./NotificationDrawer";
import styles from "./Header.module.scss";

const AppHeader: FunctionComponent = () => {
  const { data: settings } = useSystemSettings();
  const hasLogout = settings?.auth.type === "form";

  const { show, showed } = useNavbar();

  const online = useIsOnline();
  const offline = !online;

  const { shutdown, restart, logout } = useSystem();

  const goHome = useGotoHomepage();

  const { toggleColorScheme } = useMantineColorScheme();
  const dark = useComputedColorScheme("light") === "dark";

  const [
    jobsManagerOpened,
    { open: openJobsManager, close: closeJobsManager },
  ] = useDisclosure(false);

  const { data: jobs } = useSystemJobs();

  return (
    <AppShell.Header p="md" className={styles.header}>
      <Group justify="space-between" wrap="nowrap">
        <Group wrap="nowrap">
          <Burger
            opened={showed}
            onClick={() => show(!showed)}
            size="sm"
            hiddenFrom="sm"
          ></Burger>
          <Anchor onClick={goHome} underline="never">
            <Group gap={6} wrap="nowrap">
              <Avatar
                alt="brand"
                size={32}
                src={`${Environment.baseUrl}/images/logo_no_orb64.png`}
              ></Avatar>
              <Badge size="lg" radius="sm" variant="gradient" gradient={{ from: "brand.5", to: "brand.6", deg: 135 }} visibleFrom="sm" style={{ cursor: "pointer" }}>
                Bazarr+
              </Badge>
            </Group>
          </Anchor>
        </Group>
        <Group gap="xs" justify="right" wrap="nowrap">
          <Search></Search>
          <Action
            label="Change Theme"
            tooltip={{ position: "left", openDelay: 2000 }}
            onClick={() => toggleColorScheme()}
            icon={dark ? faSun : faMoon}
            size="sm"
          ></Action>
          <Action
            label="Jobs Manager"
            tooltip={{ position: "left", openDelay: 2000 }}
            icon={faBell}
            size="sm"
            isLoading={Boolean(
              jobs?.filter((job) => job.status === "running").length,
            )}
            onClick={openJobsManager}
          ></Action>
          <Menu>
            <Menu.Target>
              <Action
                label="System"
                tooltip={{ position: "left", openDelay: 2000 }}
                loading={offline}
                c={offline ? "yellow" : undefined}
                icon={faGear}
                size="lg"
              ></Action>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Item
                leftSection={<FontAwesomeIcon icon={faArrowRotateLeft} />}
                onClick={() => restart()}
              >
                Restart
              </Menu.Item>
              <Menu.Item
                leftSection={<FontAwesomeIcon icon={faPowerOff} />}
                onClick={() => shutdown()}
              >
                Shutdown
              </Menu.Item>
              <Divider hidden={!hasLogout}></Divider>
              <Menu.Item hidden={!hasLogout} onClick={() => logout()}>
                Logout
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
        </Group>
      </Group>
      <NotificationDrawer
        opened={jobsManagerOpened}
        onClose={closeJobsManager}
      />
    </AppShell.Header>
  );
};

export default AppHeader;
