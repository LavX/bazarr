import { FC, MouseEvent, useEffect, useRef, useState } from "react";
import { Alert, Button, Group, Stack, Text } from "@mantine/core";
import { useMediaServerInstances } from "@/apis/hooks/mediaServers";
import PlexSettings from "@/pages/Settings/Plex/PlexSettings";
import StepLayout from "@/pages/Setup/StepLayout";
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
  const { data: instances, refetch } = useMediaServerInstances("plex");
  const [confirming, setConfirming] = useState(false);
  const [continuing, setContinuing] = useState(false);
  const pending = useRef<HTMLButtonElement | null>(null);
  const allow = useRef(false);
  // Same rule as the typed-in form: the refetch below can land after the
  // reader has pressed Back or Skip, and advancing then would move them off a
  // screen they never finished with.
  const onScreen = useRef(true);
  useEffect(() => {
    onScreen.current = true;
    return () => {
      onScreen.current = false;
    };
  }, []);

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
    // Asked again rather than read from the cache. This query is mounted before
    // the row exists, and the server selection that creates it invalidates the
    // Plex account queries only, so `instances` can still be the empty array it
    // started as. The draft would then never be marked saved, its step would
    // stay in the wizard, and Back or a resumed setup would walk the reader
    // through a connection they had already made.
    setContinuing(true);
    void refetch()
      .then(({ data }) => {
        // The account flow owns the row. If it made one, the draft is no longer
        // a draft, so its step stops being generated and the picker shows it as
        // connected instead.
        const row = (data ?? instances)?.[0];
        if (row) {
          markSaved(draft.draftId, row.id);
        }
      })
      .finally(() => {
        if (!onScreen.current) {
          return;
        }
        setContinuing(false);
        onNext();
      });
  };

  return (
    <StepLayout
      title="Plex"
      titleOrder={3}
      description="Sign in with your Plex account and pick the server to refresh. Nothing is written until you sign in, and you can disconnect here or in Settings later."
      aside={
        <Stack gap="sm">
          {confirming && (
            <Alert color="red" title="Disconnect from Plex?">
              <Stack gap="sm">
                <Text size="sm">
                  This removes every Plex setting, including the account you
                  just signed in with.
                </Text>
                <Group gap="sm">
                  <Button
                    variant="default"
                    onClick={() => setConfirming(false)}
                  >
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
        </Stack>
      }
      actions={
        <StepActions
          onNext={onNext}
          onBack={onBack}
          onContinue={handleContinue}
          continuePending={continuing}
        />
      }
    >
      <div className={styles.panel} onClickCapture={intercept}>
        <PlexSettings />
      </div>
    </StepLayout>
  );
};

export default PlexServerForm;
