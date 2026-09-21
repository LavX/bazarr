import { FC, MouseEvent, useRef, useState } from "react";
import { Alert, Button, Group, Stack, Text, Title } from "@mantine/core";
import { useMediaServerInstances } from "@/apis/hooks/mediaServers";
import PlexSettings from "@/pages/Settings/Plex/PlexSettings";
import type { WizardStepProps } from "@/pages/Setup/steps/types";
import type { MediaServerDraft } from "@/pages/Setup/useOnboardingSelection";
import { useOnboardingSelection } from "@/pages/Setup/useOnboardingSelection";
import StepActions from "./StepActions";
import styles from "./PlexServerForm.module.scss";

interface Props extends WizardStepProps {
  draft: MediaServerDraft;
}

/**
 * Plex is the one kind whose connection is not typed into a form. The step
 * renders the same account panel the Connections page does, so signing in runs
 * the OAuth callback that stores the token and switches use_plex on, and
 * choosing a server persists through /plex/select-server. Both transitions call
 * media_servers.plex_account, which owns the destination row the refresh
 * dispatcher works from, so the wizard writes no settings of its own here, and
 * Plex is never part of the batch that flips the other kinds' master switches.
 *
 * The panel's own Disconnect deletes every Plex setting with no confirmation at
 * all. On the Connections page that button is what the reader came for; here it
 * sits one tab stop from Continue on a first run, so it is intercepted and
 * asked about first. The panel itself is untouched, which keeps Settings and
 * the wizard on one copy of the account flow.
 */
const PlexServerForm: FC<Props> = ({ draft, onNext, onBack }) => {
  const { markSaved } = useOnboardingSelection();
  const { data: instances } = useMediaServerInstances("plex");
  const [confirming, setConfirming] = useState(false);
  const pending = useRef<HTMLButtonElement | null>(null);
  const allow = useRef(false);

  const intercept = (event: MouseEvent<HTMLDivElement>) => {
    if (allow.current) {
      allow.current = false;
      return;
    }
    const button = (event.target as HTMLElement | null)?.closest("button");
    if (!button || !/disconnect/i.test(button.textContent ?? "")) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    pending.current = button;
    setConfirming(true);
  };

  const confirm = () => {
    setConfirming(false);
    allow.current = true;
    pending.current?.click();
    pending.current = null;
  };

  const handleContinue = () => {
    // The account flow owns the row. If it made one, the draft is no longer a
    // draft, so its step stops being generated and the picker shows it as
    // connected instead.
    const row = instances?.[0];
    if (row) {
      markSaved(draft.draftId, row.id);
    }
    onNext();
  };

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={3}>Plex</Title>
        <Text c="dimmed">
          Sign in with your Plex account and pick the server to refresh. Nothing
          is written until you sign in, and you can disconnect here or in
          Settings later.
        </Text>
      </Stack>

      <div className={styles.panel} onClickCapture={intercept}>
        <PlexSettings />
      </div>

      {confirming && (
        <Alert color="red" title="Disconnect from Plex?">
          <Stack gap="sm">
            <Text size="sm">
              This removes every Plex setting, including the account you just
              signed in with.
            </Text>
            <Group gap="sm">
              <Button variant="default" onClick={() => setConfirming(false)}>
                Keep Plex connected
              </Button>
              <Button color="red" onClick={confirm}>
                Disconnect
              </Button>
            </Group>
          </Stack>
        </Alert>
      )}

      <Text size="sm" c="dimmed">
        Which libraries this Plex server refreshes is set on its instance in
        Settings, Connections.
      </Text>

      <StepActions onNext={handleContinue} onBack={onBack} />
    </Stack>
  );
};

export default PlexServerForm;
