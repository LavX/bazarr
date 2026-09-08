import { useEffect } from "react";
import {
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Stack,
  Switch,
  Text,
} from "@mantine/core";
import {
  useMediaServerTest,
  useSaveMediaServerInstance,
} from "@/apis/hooks/mediaServers";
import type { MediaServerInstance } from "@/apis/raw/mediaServers";
import MediaServerRefreshStatus from "./MediaServerRefreshStatus";
import styles from "@/pages/Settings/Connections/Connections.module.scss";

interface Props {
  instance: MediaServerInstance;
  masterEnabled: boolean;
  masterChanged: boolean;
  onEdit: (instance: MediaServerInstance) => void;
  onDelete: (instance: MediaServerInstance) => void;
}

export default function MediaServerInstanceCard({
  instance,
  masterEnabled,
  masterChanged,
  onEdit,
  onDelete,
}: Props) {
  const update = useSaveMediaServerInstance(
    instance.kind,
    { enabled: !instance.enabled },
    instance.id,
  );
  const test = useMediaServerTest(instance.kind, {}, instance.id);
  const { reset } = test;
  useEffect(() => reset(), [instance, reset]);
  return (
    <section
      aria-label={instance.name}
      className={styles.card}
      data-disabled={!instance.enabled || undefined}
    >
      <Stack gap="md">
        <Group justify="space-between" align="flex-start">
          <Stack gap={6} className={styles.dimmable}>
            <Group gap="xs">
              <Text fw={600} size="sm">
                {instance.name}
              </Text>
              {!instance.enabled && (
                <Badge size="xs" color="gray">
                  Disabled
                </Badge>
              )}
            </Group>
            <Code>{instance.url}</Code>
            <Text size="xs" c={instance.api_key_set ? "dimmed" : "orange"}>
              {instance.api_key_set ? "Key stored" : "No API key"}
            </Text>
          </Stack>
          <Group gap="xs">
            <Switch
              size="sm"
              checked={instance.enabled}
              disabled={update.isPending}
              aria-label={
                instance.enabled ? "Disable instance" : "Enable instance"
              }
              onChange={() => {
                test.reset();
                update.mutate();
              }}
            />
            <Button
              type="button"
              size="xs"
              variant="light"
              loading={test.isPending}
              disabled={!instance.api_key_set || !instance.url}
              onClick={() => test.mutate()}
            >
              Test
            </Button>
            <Button
              type="button"
              size="xs"
              variant="default"
              onClick={() => {
                test.reset();
                onEdit(instance);
              }}
            >
              Edit
            </Button>
            <Button
              type="button"
              size="xs"
              variant="subtle"
              color="red"
              onClick={() => onDelete(instance)}
            >
              Delete
            </Button>
          </Group>
        </Group>
        {update.isError && (
          <Alert color="red">
            Could not update this instance. Check its API key and path mappings
            before enabling it.
          </Alert>
        )}
        {test.isSuccess && test.data.success && (
          <Alert color="green">
            Read-only connection succeeded
            {test.data.server_name ? `: ${test.data.server_name}` : ""}. This
            does not confirm refresh permission or subtitle discovery.
          </Alert>
        )}
        {(test.isError || (test.isSuccess && !test.data.success)) && (
          <Alert color="red">
            Connection test failed. Check the saved connection and server
            access.
          </Alert>
        )}
        <MediaServerRefreshStatus
          instance={instance}
          enabled={masterEnabled && instance.enabled}
          hasChanges={masterChanged}
        />
      </Stack>
    </section>
  );
}
