import { FC, useState } from "react";
import { Alert, Badge, Button, Group, Stack, Text } from "@mantine/core";
import { useSettingsMutation } from "@/apis/hooks";
import { useDeleteMediaServerInstance } from "@/apis/hooks/mediaServers";
import { usePlexLogoutMutation } from "@/apis/hooks/plex";
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
  /** Whether this is the Plex destination the signed-in account owns. */
  accountOwned?: boolean;
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
 *
 * The Plex destination the account owns is not undone by deleting it. The token
 * and the chosen server live in the Plex settings, and the backend rebuilds the
 * row from them on the next account transition or startup
 * (media_servers/plex_account.py), so the deleted server would come back. That
 * row is signed out first, which is what actually disconnects it, and deleted
 * afterwards so the picker agrees. Signing out already clears use_plex, so no
 * master switch is written for it here.
 */
const ConnectedServerRow: FC<Props> = ({
  instance,
  kind,
  last,
  accountOwned = false,
  onDisconnected,
}) => {
  const [confirming, setConfirming] = useState(false);
  const [failed, setFailed] = useState(false);
  const remove = useDeleteMediaServerInstance(kind, instance.id);
  const logout = usePlexLogoutMutation();
  const settings = useSettingsMutation();

  const deleteRow = () => {
    remove.mutate(undefined, {
      onSuccess: () => {
        setConfirming(false);
        // The switch goes off only when the last instance of the kind does:
        // clearing it while a sibling remains would silence a server the
        // reader never touched.
        if (last && !accountOwned) {
          settings.mutate({ [`settings-general-use_${kind}`]: false });
        }
        onDisconnected(instance.id);
      },
      onError: () => setFailed(true),
    });
  };

  const disconnect = () => {
    setFailed(false);
    if (!accountOwned) {
      deleteRow();
      return;
    }
    // Sign out first: once the credential is gone nothing can rebuild the row,
    // so a delete that fails after it leaves a disconnected server behind
    // rather than a connected one.
    logout.mutate(undefined, {
      onSuccess: () => deleteRow(),
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
              {accountOwned
                ? " and you are signed out of your Plex account"
                : last
                  ? ` and ${kindName(kind)} refreshes are turned off`
                  : ""}
              .
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
                loading={remove.isPending || logout.isPending}
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
