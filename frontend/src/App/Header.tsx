import { FunctionComponent } from "react";
import {
  Anchor,
  AppShell,
  Avatar,
  Burger,
  Divider,
  Group,
  Menu,
  Text,
  useComputedColorScheme,
  useMantineColorScheme,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { openConfirmModal } from "@mantine/modals";
import {
  faArrowRotateLeft,
  faEllipsisVertical,
  faListCheck,
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
    <AppShell.Header p={0} className={styles.header}>
      <div className={styles.headerInner}>
        <Group justify="space-between" wrap="nowrap" style={{ flex: 1 }}>
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
                  size={40}
                  src={`${Environment.baseUrl}/images/logo_no_orb128.png`}
                ></Avatar>
                <Text
                  fw={800}
                  fz="xl"
                  c={dark ? "gray.5" : "gray.8"}
                  visibleFrom="sm"
                  style={{ cursor: "pointer", lineHeight: 1 }}
                >
                  Bazarr
                  <Text
                    component="span"
                    fw={900}
                    fz="xl"
                    c="brand.5"
                    style={{
                      verticalAlign: "top",
                      fontSize: "0.7em",
                      lineHeight: 1,
                      position: "relative",
                      top: "-0.15em",
                    }}
                  >
                    +
                  </Text>
                </Text>
              </Group>
            </Anchor>
          </Group>
          <div style={{ flex: 1, maxWidth: 500 }}>
            <Search></Search>
          </div>
          <Group gap="xs" justify="right" wrap="nowrap">
            <Action
              label="Change Theme"
              tooltip={{ position: "left", openDelay: 500 }}
              onClick={() => toggleColorScheme()}
              icon={dark ? faSun : faMoon}
              size="sm"
            ></Action>
            <Action
              label="Jobs Manager"
              tooltip={{ position: "left", openDelay: 500 }}
              icon={faListCheck}
              size="sm"
              isLoading={Boolean(
                jobs?.filter((job) => job.status === "running").length,
              )}
              onClick={openJobsManager}
            ></Action>
            <Menu>
              <Menu.Target>
                <Action
                  label="Power"
                  tooltip={{ position: "left", openDelay: 500 }}
                  loading={offline}
                  c={offline ? "yellow" : undefined}
                  icon={faEllipsisVertical}
                  size="sm"
                ></Action>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faArrowRotateLeft} />}
                  onClick={() =>
                    openConfirmModal({
                      title: "Restart Bazarr+",
                      children: (
                        <Text size="sm">
                          Are you sure you want to restart Bazarr+? The service
                          will be temporarily unavailable.
                        </Text>
                      ),
                      labels: { confirm: "Restart", cancel: "Cancel" },
                      confirmProps: { color: "yellow" },
                      onConfirm: () => restart(),
                    })
                  }
                >
                  Restart
                </Menu.Item>
                <Menu.Item
                  color="red"
                  leftSection={<FontAwesomeIcon icon={faPowerOff} />}
                  onClick={() =>
                    openConfirmModal({
                      title: "Shutdown Bazarr+",
                      children: (
                        <Text size="sm">
                          Are you sure you want to shut down Bazarr+? You will
                          need to manually restart the service.
                        </Text>
                      ),
                      labels: { confirm: "Shutdown", cancel: "Cancel" },
                      confirmProps: { color: "red" },
                      onConfirm: () => shutdown(),
                    })
                  }
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
      </div>
      <NotificationDrawer
        opened={jobsManagerOpened}
        onClose={closeJobsManager}
      />
    </AppShell.Header>
  );
};

export default AppHeader;
