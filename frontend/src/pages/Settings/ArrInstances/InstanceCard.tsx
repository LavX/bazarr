import { FunctionComponent, ReactNode } from "react";
import {
  ActionIcon,
  Badge,
  Button,
  Code,
  Group,
  Menu,
  Stack,
  Switch,
  Text,
  ThemeIcon,
  Tooltip,
} from "@mantine/core";
import {
  faCircleCheck,
  faCircleInfo,
  faCircleXmark,
  faEllipsisVertical,
  faKey,
  faPen,
  faPlugCircleBolt,
  faStar,
  faTrash,
  faTriangleExclamation,
  faXmark,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useTestArrInstanceConnection,
  useUpdateArrInstance,
} from "@/apis/hooks";
import type { ArrInstance } from "@/apis/raw/arrInstances";
import { ARR_META, buildHostUrl } from "./meta";
import styles from "./ArrInstances.module.scss";

interface TestStatus {
  tone: "ok" | "warn" | "fail";
  icon: ReactNode;
  label: string;
  hint?: string;
}

interface Props {
  instance: ArrInstance;
  onEdit: (instance: ArrInstance) => void;
  onDelete: (instance: ArrInstance) => void;
}

const InstanceCard: FunctionComponent<Props> = ({
  instance,
  onEdit,
  onDelete,
}) => {
  const update = useUpdateArrInstance();
  const test = useTestArrInstanceConnection();

  const meta = ARR_META[instance.kind];
  const host = buildHostUrl(instance);

  const runTest = () => {
    // The stored key never reaches the browser, so card-level tests verify
    // reachability only. Authentication is tested from the edit form where a
    // key can be re-entered.
    test.mutate({
      kind: instance.kind,
      ip: instance.ip,
      port: instance.port,
      base_url: instance.base_url,
      ssl: instance.ssl,
      verify_ssl: instance.verify_ssl,
      http_timeout: instance.http_timeout,
    });
  };

  let status: TestStatus | null = null;
  if (test.isError) {
    status = {
      tone: "fail",
      icon: <FontAwesomeIcon icon={faCircleXmark} />,
      label: "Test failed: could not reach the Bazarr API",
    };
  } else if (test.data) {
    if (test.data.ok) {
      const app = test.data.app_name ?? meta.label;
      status = {
        tone: "ok",
        icon: <FontAwesomeIcon icon={faCircleCheck} />,
        label: test.data.version
          ? `Connected: ${app} v${test.data.version}`
          : `Connected: ${app}`,
      };
    } else if (test.data.error === "unauthorized" && instance.api_key_set) {
      status = {
        tone: "warn",
        icon: <FontAwesomeIcon icon={faTriangleExclamation} />,
        label: "Reachable, key not verified",
        hint: "Stored keys never leave the server, so quick tests run without one. Open Edit and re-enter the key to verify authentication.",
      };
    } else {
      status = {
        tone: "fail",
        icon: <FontAwesomeIcon icon={faCircleXmark} />,
        label:
          test.data.message ??
          test.data.error ??
          "The instance did not respond",
      };
    }
  }

  return (
    <div
      className={styles.card}
      data-default={instance.is_default || undefined}
      data-disabled={!instance.enabled || undefined}
    >
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="md">
        <Group
          className={styles.dimmable}
          align="flex-start"
          gap="md"
          wrap="nowrap"
          style={{ flex: 1, minWidth: 0 }}
        >
          <ThemeIcon variant="light" color="brand" size={42} radius="md">
            <FontAwesomeIcon icon={meta.icon} />
          </ThemeIcon>
          <Stack gap={6} style={{ flex: 1, minWidth: 0 }}>
            <Group gap="xs" wrap="wrap">
              <Text fw={600} size="sm">
                {instance.name}
              </Text>
              {instance.display_name &&
                instance.display_name !== instance.name && (
                  <Text size="xs" c="dimmed">
                    ({instance.display_name})
                  </Text>
                )}
              {instance.is_default && (
                <Badge
                  size="xs"
                  variant="light"
                  color="brand"
                  leftSection={<FontAwesomeIcon icon={faStar} />}
                >
                  Default
                </Badge>
              )}
              {!instance.enabled && (
                <Badge size="xs" variant="light" color="gray">
                  Disabled
                </Badge>
              )}
            </Group>
            <Code style={{ width: "fit-content" }}>{host}</Code>
            <Group gap={8} wrap="wrap">
              <Text size="xs" c={instance.api_key_set ? "dimmed" : "orange"}>
                <FontAwesomeIcon icon={faKey} />{" "}
                {instance.api_key_set ? "Key stored" : "No API key"}
              </Text>
              <Text size="xs" c="dimmed">
                &middot;
              </Text>
              <Text size="xs" c="dimmed">
                Timeout {instance.http_timeout}s
              </Text>
              {instance.ssl && (
                <>
                  <Text size="xs" c="dimmed">
                    &middot;
                  </Text>
                  <Text size="xs" c="dimmed">
                    {instance.verify_ssl ? "SSL, verified" : "SSL, unverified"}
                  </Text>
                </>
              )}
            </Group>
            {status && (
              <div className={styles.testResult} data-tone={status.tone}>
                {status.icon}
                <Text size="xs">{status.label}</Text>
                {status.hint && (
                  <Tooltip label={status.hint} w={300} multiline withArrow>
                    <FontAwesomeIcon icon={faCircleInfo} />
                  </Tooltip>
                )}
                <ActionIcon
                  size="xs"
                  variant="transparent"
                  color="gray"
                  aria-label="Dismiss test result"
                  onClick={() => test.reset()}
                >
                  <FontAwesomeIcon icon={faXmark} />
                </ActionIcon>
              </div>
            )}
          </Stack>
        </Group>

        <Group gap="xs" wrap="nowrap">
          <Tooltip
            label={instance.enabled ? "Disable instance" : "Enable instance"}
            withArrow
          >
            <Switch
              size="sm"
              checked={instance.enabled}
              disabled={update.isPending}
              aria-label={
                instance.enabled ? "Disable instance" : "Enable instance"
              }
              onChange={(event) =>
                update.mutate({
                  id: instance.id,
                  body: { enabled: event.currentTarget.checked },
                })
              }
            />
          </Tooltip>
          <Button
            type="button"
            size="xs"
            variant="light"
            leftSection={<FontAwesomeIcon icon={faPlugCircleBolt} />}
            loading={test.isPending}
            onClick={runTest}
          >
            Test
          </Button>
          <Button
            type="button"
            size="xs"
            variant="default"
            leftSection={<FontAwesomeIcon icon={faPen} />}
            onClick={() => onEdit(instance)}
          >
            Edit
          </Button>
          <Menu position="bottom-end" withArrow>
            <Menu.Target>
              <ActionIcon
                type="button"
                variant="subtle"
                color="gray"
                aria-label={`More actions for ${instance.name}`}
              >
                <FontAwesomeIcon icon={faEllipsisVertical} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Item
                leftSection={<FontAwesomeIcon icon={faStar} />}
                disabled={instance.is_default}
                onClick={() =>
                  update.mutate({
                    id: instance.id,
                    body: { is_default: true },
                  })
                }
              >
                Set as default
              </Menu.Item>
              <Menu.Divider />
              <Menu.Item
                color="red"
                leftSection={<FontAwesomeIcon icon={faTrash} />}
                onClick={() => onDelete(instance)}
              >
                Delete
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
        </Group>
      </Group>
    </div>
  );
};

export default InstanceCard;
