import { Divider, Menu, Text } from "@mantine/core";
import { openConfirmModal } from "@mantine/modals";
import {
  faArrowRotateLeft,
  faPowerOff,
  faRightFromBracket,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystem, useSystemSettings } from "@/apis/hooks";

export default function SystemActions() {
  const { data: settings } = useSystemSettings();
  const hasLogout = settings?.auth?.type === "form";
  const { shutdown, restart, logout } = useSystem();
  return (
    <>
      {" "}
      <Menu.Item
        leftSection={<FontAwesomeIcon icon={faArrowRotateLeft} />}
        onClick={() =>
          openConfirmModal({
            title: "Restart Bazarr+",
            children: (
              <Text size="sm">
                Are you sure you want to restart Bazarr+? The service will be
                temporarily unavailable.
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
                Are you sure you want to shut down Bazarr+? You will need to
                manually restart the service.
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
      <Menu.Item
        hidden={!hasLogout}
        leftSection={<FontAwesomeIcon icon={faRightFromBracket} />}
        onClick={() => logout()}
      >
        Logout
      </Menu.Item>
    </>
  );
}
