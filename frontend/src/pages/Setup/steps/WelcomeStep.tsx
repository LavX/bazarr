import { FC } from "react";
import { Button, List, Stack, Text, Title } from "@mantine/core";
import type { WizardStepProps } from "./types";

/**
 * First step of the onboarding wizard: sets expectations for what the rest of
 * the flow configures, then hands off to the next step via onNext.
 */
const WelcomeStep: FC<WizardStepProps> = ({ onNext }) => {
  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>Welcome to Bazarr+</Title>
        <Text c="dimmed">
          Let us walk through a few quick steps to get your library ready. You
          can change any of this later in Settings.
        </Text>
      </Stack>

      <Stack gap="xs">
        <Text fw={600}>Here is what we will set up:</Text>
        <List spacing="xs">
          <List.Item>Sonarr connection for your TV shows</List.Item>
          <List.Item>Radarr connection for your movies</List.Item>
          <List.Item>Optional Plex or Jellyfin media servers</List.Item>
          <List.Item>Subtitle languages you want</List.Item>
          <List.Item>Subtitle providers to search</List.Item>
          <List.Item>A few general application preferences</List.Item>
        </List>
      </Stack>

      <Button onClick={onNext} size="md" mt="sm">
        Get started
      </Button>
    </Stack>
  );
};

export default WelcomeStep;
