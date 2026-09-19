import { FC } from "react";
import { Button, Group, Paper, Radio, Stack, Text, Title } from "@mantine/core";
import { useOnboardingIntent } from "@/pages/Setup/useOnboardingIntent";
import type { WizardIntent, WizardStepProps } from "./types";

interface PathChoice {
  value: WizardIntent;
  title: string;
  description: string;
}

// Two honest paths, not a form. The wording is what the product actually does
// in each case, so a user can recognise themselves in one of them without
// knowing what an "arr" is.
const CHOICES: PathChoice[] = [
  {
    value: "library",
    title: "I run Sonarr, Radarr or Sportarr",
    description:
      "Connect them and Bazarr+ fetches subtitles for everything in your library, automatically.",
  },
  {
    value: "discover",
    title: "I want to find subtitles for anything",
    description:
      "No library needed. Search any title, preview the subtitles and download them to this device.",
  },
];

/**
 * The question the rest of the wizard follows from. It decides which steps the
 * rail shows and nothing else: no backend setting is written, and either answer
 * can be changed here or in Settings afterwards.
 */
const IntentStep: FC<WizardStepProps> = ({ onNext }) => {
  const { intent, setIntent } = useOnboardingIntent();

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>What do you want Bazarr+ to do for you?</Title>
        <Text c="dimmed">
          This only decides which steps we walk you through. Whatever you pick,
          everything else stays available in Settings, and you can connect the
          rest at any time.
        </Text>
      </Stack>

      <Radio.Group
        value={intent ?? ""}
        onChange={(value) => setIntent(value as WizardIntent)}
        aria-label="What do you want Bazarr+ to do for you?"
      >
        <Stack gap="sm">
          {CHOICES.map((choice) => (
            <Paper key={choice.value} withBorder p="md" radius="md">
              <Radio
                value={choice.value}
                label={choice.title}
                description={choice.description}
              />
            </Paper>
          ))}
        </Stack>
      </Radio.Group>

      <Group justify="flex-end">
        <Button onClick={onNext} disabled={intent === null}>
          Continue
        </Button>
      </Group>
    </Stack>
  );
};

export default IntentStep;
