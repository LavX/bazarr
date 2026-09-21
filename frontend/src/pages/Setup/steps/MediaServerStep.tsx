import { FC, useMemo } from "react";
import {
  Button,
  Checkbox,
  Divider,
  Group,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useMediaServerInstances } from "@/apis/hooks/mediaServers";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import { kindName } from "@/pages/Settings/MediaServers/kinds";
import { useOnboardingSelection } from "@/pages/Setup/useOnboardingSelection";
import ConnectedServerRow from "./mediaServer/ConnectedServerRow";
import StepActions from "./mediaServer/StepActions";
import type { WizardStepProps } from "./types";
import styles from "./MediaServerStep.module.scss";

// The same four kinds, in the same order, as the Connections tabs.
const DESCRIPTIONS: Record<MediaServerKind, string> = {
  plex: "Sign in with your Plex account and pick the server to refresh.",
  jellyfin: "Connect with the server URL and an API key.",
  emby: "Connect with the server URL and an API key, then map your media folders.",
  silo: "Connect with the server URL and an API key, then map your media folders to a library.",
};
const ORDER: MediaServerKind[] = ["plex", "jellyfin", "emby", "silo"];

/**
 * Optional onboarding step for external media servers. It picks which servers
 * to connect and nothing else: each one is configured on a step of its own,
 * generated from what is ticked here.
 *
 * Several at once, including two of one kind, because that is what the backend
 * has always stored: media_server_instances has no uniqueness on name or kind,
 * and Settings already lists as many per kind as exist. The single-choice
 * picker was the only thing saying otherwise.
 *
 * Every kind stays optional and Continue with nothing ticked writes nothing,
 * because Bazarr+ finds and downloads subtitles with no media server at all.
 * Skipping is the shell's control.
 */
const MediaServerStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const { drafts, addDraft, removeKind, removeDraft } =
    useOnboardingSelection();

  // One query per kind, a fixed four, so no hook is called in a loop.
  const plex = useMediaServerInstances("plex");
  const jellyfin = useMediaServerInstances("jellyfin");
  const emby = useMediaServerInstances("emby");
  const silo = useMediaServerInstances("silo");

  const connected = useMemo<Record<MediaServerKind, MediaServerInstance[]>>(
    () => ({
      plex: plex.data ?? [],
      jellyfin: jellyfin.data ?? [],
      emby: emby.data ?? [],
      silo: silo.data ?? [],
    }),
    [plex.data, jellyfin.data, emby.data, silo.data],
  );

  const takenNames = useMemo(
    () => ORDER.flatMap((kind) => connected[kind]).map((row) => row.name),
    [connected],
  );

  const pendingOf = (kind: MediaServerKind) =>
    drafts.filter(
      (draft) => draft.kind === kind && draft.instanceId === undefined,
    );

  const toggle = (kind: MediaServerKind, checked: boolean) => {
    if (checked) {
      addDraft(kind, takenNames);
    } else {
      removeKind(kind);
    }
  };

  const forgetInstance = (instanceId: string) => {
    const draft = drafts.find((entry) => entry.instanceId === instanceId);
    if (draft) {
      removeDraft(draft.draftId);
    }
  };

  const chosen = ORDER.filter(
    (kind) => connected[kind].length > 0 || pendingOf(kind).length > 0,
  );
  const pendingTotal = drafts.filter(
    (draft) => draft.instanceId === undefined,
  ).length;

  return (
    <Stack gap="md">
      <Stack gap="xs">
        <Title order={2}>Media servers</Title>
        <Text c="dimmed">
          Optionally connect a media server so Bazarr refreshes it after it
          downloads subtitles. Bazarr finds and downloads subtitles with no
          media server at all, so you can set one up later in Settings,
          Connections.
        </Text>
      </Stack>

      <Stack gap="sm">
        {ORDER.map((kind) => {
          const rows = connected[kind];
          const pending = pendingOf(kind);
          const checked = rows.length > 0 || pending.length > 0;
          return (
            <Checkbox.Card
              key={kind}
              className={styles.card}
              radius="md"
              checked={checked}
              // A connected server is undone by disconnecting it, not by
              // unticking it: the row is already written.
              disabled={rows.length > 0 && pending.length === 0}
              aria-label={kindName(kind)}
              onClick={() =>
                rows.length > 0 && pending.length === 0
                  ? undefined
                  : toggle(kind, !checked)
              }
            >
              <div className={styles.cardBody}>
                <Checkbox.Indicator />
                <div className={styles.cardText}>
                  <Text fw={500}>{kindName(kind)}</Text>
                  <Text size="sm" c="dimmed">
                    {DESCRIPTIONS[kind]}
                  </Text>
                </div>
              </div>
            </Checkbox.Card>
          );
        })}
      </Stack>

      {chosen.length > 0 && (
        <Stack gap="sm" className={styles.selection}>
          <Text fw={600} size="sm">
            Servers to set up
          </Text>
          {chosen.map((kind, position) => (
            <Stack key={kind} gap="xs">
              {position > 0 && <Divider />}
              {connected[kind].map((instance) => (
                <ConnectedServerRow
                  key={instance.id}
                  instance={instance}
                  kind={kind}
                  last={connected[kind].length === 1}
                  onDisconnected={forgetInstance}
                />
              ))}
              {pendingOf(kind).map((draft) => (
                <Group key={draft.draftId} justify="space-between" gap="sm">
                  <Text>
                    {draft.name}{" "}
                    <Text span size="sm" c="dimmed">
                      not connected yet
                    </Text>
                  </Text>
                  <Button
                    variant="subtle"
                    color="gray"
                    size="compact-sm"
                    aria-label={`Remove ${draft.name}`}
                    onClick={() => removeDraft(draft.draftId)}
                  >
                    Remove
                  </Button>
                </Group>
              ))}
              <Group>
                <Button
                  variant="subtle"
                  size="compact-sm"
                  onClick={() => addDraft(kind, takenNames)}
                >
                  Add another {kindName(kind)}
                </Button>
              </Group>
            </Stack>
          ))}
        </Stack>
      )}

      <StepActions
        onNext={onNext}
        onBack={onBack}
        continueLabel={
          pendingTotal > 0
            ? `Set up ${pendingTotal} ${pendingTotal === 1 ? "server" : "servers"}`
            : "Continue without a server"
        }
      />
    </Stack>
  );
};

export default MediaServerStep;
