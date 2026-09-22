import { FC } from "react";
import { Button, List, Stack, Text } from "@mantine/core";
import StepLayout from "@/pages/Setup/StepLayout";
import type { WizardStepProps } from "./types";

/**
 * First step of the onboarding wizard: sets expectations for what the rest of
 * the flow configures, then hands off to the next step via onNext.
 *
 * It deliberately names nothing that only one of the two paths asks for. The
 * next screen asks what the user actually wants, and promising a Sonarr step
 * to someone who came for Discover is the wizard's oldest lie.
 */
const WelcomeStep: FC<WizardStepProps> = ({ onNext }) => {
  return (
    <StepLayout
      title="Welcome to Bazarr+"
      layout="stacked"
      description="A few quick steps and you are running. The next screen asks what you want Bazarr+ to do, and we only ask for what that needs. You can change any of this later in Settings."
      actions={
        <Button onClick={onNext} size="md">
          Get started
        </Button>
      }
    >
      <Stack gap="xs">
        <Text fw={600}>Whatever you pick, we will cover:</Text>
        <List spacing="xs">
          <List.Item>The subtitle languages you want</List.Item>
          <List.Item>The subtitle providers to search</List.Item>
          <List.Item>Optional AI translation</List.Item>
          <List.Item>A few general application preferences</List.Item>
        </List>
      </Stack>
    </StepLayout>
  );
};

export default WelcomeStep;
