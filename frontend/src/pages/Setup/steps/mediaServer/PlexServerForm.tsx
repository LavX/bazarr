import { FC } from "react";
import { Stack, Text, Title } from "@mantine/core";
import PlexSettings from "@/pages/Settings/Plex/PlexSettings";
import type { WizardStepProps } from "@/pages/Setup/steps/types";
import StepActions from "./StepActions";

/**
 * Plex is the one kind whose connection is not typed into a form. The step
 * renders the same account panel the Connections page does, so signing in runs
 * the OAuth callback that stores the token and switches use_plex on, and
 * choosing a server persists through /plex/select-server. Both transitions call
 * media_servers.plex_account, which owns the destination row the refresh
 * dispatcher works from, so the wizard writes no settings of its own here.
 */
const PlexServerForm: FC<WizardStepProps> = ({ onNext, onBack }) => (
  <Stack gap="lg">
    <Stack gap="xs">
      <Title order={3}>Plex</Title>
      <Text c="dimmed">
        Sign in with your Plex account and pick the server to refresh. Nothing
        is written until you sign in, and you can disconnect here or in Settings
        later.
      </Text>
    </Stack>

    <PlexSettings />

    <Text size="sm" c="dimmed">
      Which libraries this Plex server refreshes is set on its instance in
      Settings, Connections.
    </Text>

    <StepActions onNext={onNext} onBack={onBack} />
  </Stack>
);

export default PlexServerForm;
