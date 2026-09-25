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
import { usePlexLogoutMutation } from "@/apis/hooks/plex";
import type { MediaServerInstance } from "@/apis/raw/mediaServers";
import { useModals } from "@/modules/modals";
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
  // The account panel offers Disconnect only to an OAuth sign-in, so an
  // account on a legacy API key or token had no way to clear the credential
  // that keeps this card from being deleted.
  const disconnect = usePlexLogoutMutation();
  const modals = useModals();
  // Signing out also turns use_plex off, which silences every Plex server,
  // including the ones added by hand, so it asks first.
  const confirmDisconnect = () =>
    modals.openConfirmModal({
      title: "Disconnect from Plex",
      children: (
        <Text size="sm">
          This signs you out of Plex and turns off Plex integration, which also
          stops refreshes to any Plex server you added by hand.
        </Text>
      ),
      labels: { confirm: "Disconnect", cancel: "Cancel" },
      confirmProps: { color: "red" },
      onConfirm: () => disconnect.mutate(),
    });
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
            {instance.account_owned ? (
              <Button
                type="button"
                size="xs"
                variant="subtle"
                color="red"
                loading={disconnect.isPending}
                onClick={confirmDisconnect}
              >
                Disconnect
              </Button>
            ) : (
              <Button
                type="button"
                size="xs"
                variant="subtle"
                color="red"
                onClick={() => onDelete(instance)}
              >
                Delete
              </Button>
            )}
          </Group>
        </Group>
        <Text size="sm" c="dimmed">
          Test checks access, not refresh permission.
        </Text>
        {instance.account_owned && (
          <Text size="sm" c="dimmed">
            This instance belongs to your Plex account, which would recreate it
            at the next restart. Disconnect from Plex to remove it.
          </Text>
        )}
        {disconnect.isError && (
          <Alert color="red">Could not disconnect from Plex. Try again.</Alert>
        )}
        {update.isError && (
          <Alert color="red">
            Could not update this instance. Check its API key and path mappings
            before enabling it.
          </Alert>
        )}
        {test.isSuccess && test.data.success && (
          <Alert color="green">
            {test.data.server_name
              ? `Connected to ${test.data.server_name}.`
              : "Connection succeeded."}
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
