import { FC, useState } from "react";
import { Alert, Badge, Button, Group, Stack, Text } from "@mantine/core";
import { useSettingsMutation } from "@/apis/hooks";
import { useDeleteMediaServerInstance } from "@/apis/hooks/mediaServers";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import { kindName } from "@/pages/Settings/MediaServers/kinds";

interface Props {
  instance: MediaServerInstance;
  kind: MediaServerKind;
  /** Whether this is the only instance of its kind still standing. */
  last: boolean;
  onDisconnected: (instanceId: string) => void;
}

/**
 * A media server that is already a row in the database, as the picker shows it.
 *
 * A tick means "not connected yet", so unticking one can only ever drop a step
 * that has not written anything. Once a row exists the only honest way to undo
 * it is to delete it, which is this, behind a confirm and with the master
 * switch cleared when nothing of that kind is left. Every row keeps its own
 * delete mutation, which is why this is a component and not a loop.
 */
const ConnectedServerRow: FC<Props> = ({
  instance,
  kind,
  last,
  onDisconnected,
}) => {
  const [confirming, setConfirming] = useState(false);
  const [failed, setFailed] = useState(false);
  const remove = useDeleteMediaServerInstance(kind, instance.id);
  const settings = useSettingsMutation();

  const disconnect = () => {
    setFailed(false);
    remove.mutate(undefined, {
      onSuccess: () => {
        setConfirming(false);
        // The switch goes off only when the last instance of the kind does:
        // clearing it while a sibling remains would silence a server the
        // reader never touched.
        if (last) {
          settings.mutate({ [`settings-general-use_${kind}`]: false });
        }
        onDisconnected(instance.id);
      },
      onError: () => setFailed(true),
    });
  };

  return (
    <Stack gap="xs">
      <Group justify="space-between" wrap="nowrap" gap="sm">
        <Group gap="xs" wrap="nowrap">
          <Text fw={500}>{instance.name || kindName(kind)}</Text>
          <Badge color="green" size="sm">
            Connected
          </Badge>
        </Group>
        {!confirming && (
          <Button
            variant="subtle"
            color="red"
            size="compact-sm"
            onClick={() => setConfirming(true)}
          >
            Disconnect
          </Button>
        )}
      </Group>
      {confirming && (
        <Alert color="red">
          <Stack gap="sm">
            <Text size="sm">
              Disconnect {instance.name || kindName(kind)}? Its connection is
              deleted
              {last ? ` and ${kindName(kind)} refreshes are turned off` : ""}.
            </Text>
            <Group gap="sm">
              <Button
                variant="default"
                size="compact-sm"
                onClick={() => setConfirming(false)}
              >
                Keep it
              </Button>
              <Button
                color="red"
                size="compact-sm"
                loading={remove.isPending}
                onClick={disconnect}
              >
                Disconnect
              </Button>
            </Group>
          </Stack>
        </Alert>
      )}
      {failed && (
        <Text size="sm" c="red">
          Could not disconnect {instance.name || kindName(kind)}. Try again, or
          remove it from Settings, Connections.
        </Text>
      )}
    </Stack>
  );
};

export default ConnectedServerRow;
