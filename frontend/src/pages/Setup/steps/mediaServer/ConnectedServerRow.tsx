import { FC, useState } from "react";
import { Alert, Badge, Button, Group, Stack, Text } from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import { useMutation } from "@tanstack/react-query";
import { useSettingsMutation } from "@/apis/hooks";
import { useDeleteMediaServerInstance } from "@/apis/hooks/mediaServers";
import { usePlexLogoutMutation } from "@/apis/hooks/plex";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import { notification } from "@/modules/notification";
import { kindName } from "@/pages/Settings/MediaServers/kinds";
import { readMediaServerTest } from "@/pages/Setup/connectionTests";

// What a row that refreshes is called. It says what Finish will say: saving
// proves the values were written, and only a passing Test proves they connect.
// The card's footer counts these rows as saved, so the badge names only the
// Test. Finish's longer wording pushed Disconnect off its own label on a
// phone.
const SAVED_LABEL = {
  passed: "Connected",
  failed: "Test failed",
  untested: "Not tested",
} as const;

export const MEDIA_SERVER_DISCONNECT_KEY = "setup-media-server-disconnect";

interface Props {
  instance: MediaServerInstance;
  kind: MediaServerKind;
  /** Whether this is the only instance of its kind still standing. */
  last: boolean;
  /** Whether this is the Plex destination the signed-in account owns. */
  accountOwned?: boolean;
  /** Whether ownership is still being resolved, so disconnecting must wait. */
  ownershipPending?: boolean;
  /** Whether this kind's master switch is on. Off means nothing refreshes. */
  kindEnabled?: boolean;
  onDisconnected: (instanceId: string) => void;
  onWritten?: () => void;
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
  ownershipPending = false,
  kindEnabled = true,
  onDisconnected,
  onWritten,
}) => {
  const [confirming, setConfirming] = useState(false);
  const [failed, setFailed] = useState(false);
  // Both report a failure under the row, so the client's toast stays away.
  const remove = useDeleteMediaServerInstance(kind, instance.id, {
    inlineErrors: true,
  });
  const logout = usePlexLogoutMutation({ inlineErrors: true });
  const settings = useSettingsMutation();
  // The restore below reports its own failure, so it takes a mutation of its
  // own with the hook's and the client's toasts off; with the shared one it
  // said "Save failed" beside its own message. The switch-off write keeps the
  // toasts, as it has no message of its own.
  const restore = useSettingsMutation(false, { inlineErrors: true });
  const refreshes = instance.enabled && kindEnabled;
  const tested = readMediaServerTest(kind, instance.id);

  // Keep the whole operation observable if Back or Skip unmounts the picker.
  // Record writes in the operation itself, since per-call mutation callbacks
  // are dropped on unmount.
  const disconnect = useMutation({
    mutationKey: [MEDIA_SERVER_DISCONNECT_KEY],
    mutationFn: async (enabledBefore: boolean) => {
      if (accountOwned) {
        // Remove the credential first so a failed delete cannot reconnect the row.
        await logout.mutateAsync(undefined);
        onWritten?.();
      }
      await remove.mutateAsync(undefined);
      onWritten?.();
      setConfirming(false);
      // The switch goes off only when the last instance of the kind does:
      // clearing it while a sibling remains would silence a server the
      // reader never touched. Nothing of this kind is left to refresh once
      // that write lands, so its failure costs the reader nothing.
      if (last && !accountOwned) {
        settings.mutate({ [`settings-general-use_${kind}`]: false });
      } else if (!last && accountOwned && enabledBefore) {
        // Signing out clears use_plex for the whole kind, and the dispatcher
        // reads it before any row. The Plex servers the reader added by hand
        // and kept would stop refreshing along with the account's own, so the
        // switch is put back once its row is gone, and only if it was on to
        // begin with: a reader who had turned Plex refreshes off did not ask
        // for them back by disconnecting an account. The write is waited for
        // and said out loud when it fails, because those servers are still
        // standing while the picker reported a clean disconnect and none of
        // them refreshed. A notification rather than the line below, because
        // the row this draws is deleted by now, so the list it sits in is
        // about to drop it and take any message with it.
        await restore
          .mutateAsync({ [`settings-general-use_${kind}`]: true })
          .catch(() =>
            showNotification(
              notification.error(
                `${kindName(kind)} refreshes are still off`,
                `${instance.name || kindName(kind)} is disconnected, but turning ${kindName(kind)} refreshes back on failed, so your other ${kindName(kind)} servers are not refreshing. Turn them on in Settings, Connections.`,
              ),
            ),
          );
      }
      onDisconnected(instance.id);
    },
  });

  return (
    <Stack gap="xs">
      <Group justify="space-between" wrap="nowrap" gap="sm">
        <Group gap="xs" wrap="nowrap">
          <Text fw={500}>{instance.name || kindName(kind)}</Text>
          {/* A row the reader switched off refreshes nothing, and the one the
              backend keeps after a Plex sign-out is exactly that. Calling it
              connected told them setup was done when the dispatcher would
              never use it. The kind's master switch counts the same way: the
              dispatcher reads it before it reads any row. */}
          <Badge
            color={refreshes && tested === "passed" ? "green" : "gray"}
            size="sm"
          >
            {refreshes ? SAVED_LABEL[tested] : "Turned off"}
          </Badge>
        </Group>
        {!confirming && (
          <Button
            variant="subtle"
            color="red"
            size="compact-sm"
            // Which Plex row the account owns decides whether this signs out
            // or only deletes, so it waits rather than guessing.
            disabled={ownershipPending}
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
                loading={disconnect.isPending}
                onClick={() => {
                  setFailed(false);
                  disconnect.mutate(kindEnabled, {
                    onError: () => setFailed(true),
                  });
                }}
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
