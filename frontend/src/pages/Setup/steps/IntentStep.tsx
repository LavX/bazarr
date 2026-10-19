import { FC } from "react";
import { Button, Group, Radio, Stack, Text } from "@mantine/core";
import StepLayout from "@/pages/Setup/StepLayout";
import { useOnboardingIntent } from "@/pages/Setup/useOnboardingIntent";
import type { WizardIntent, WizardStepProps } from "./types";
import styles from "./IntentStep.module.scss";

interface PathChoice {
  value: WizardIntent;
  title: string;
  description: string;
}

// Two honest paths, not a form. Both answer the heading's question, what
// Bazarr+ should do, on one axis: one said what the reader owned and the other
// what they wanted, so a Sonarr user recognised themselves in both. Each says
// what it changes about the steps that follow, at the moment of choosing.
const CHOICES: PathChoice[] = [
  {
    value: "library",
    title: "Fetch subtitles for my Sonarr, Radarr or Sportarr library",
    description:
      "Bazarr+ fetches subtitles for everything they manage, automatically, and Discover still works for anything else. Setup asks you to connect them next.",
  },
  {
    value: "discover",
    title: "Find subtitles for anything, on demand",
    description:
      "No library needed. Search any title, preview the subtitles and download them to this device. Setup leaves out the Sonarr, Radarr and Sportarr steps.",
  },
];

/**
 * The question the rest of the wizard follows from. It decides which steps the
 * rail shows and nothing else: no backend setting is written, and either answer
 * can be changed here or in Settings afterwards.
 */
const IntentStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const { intent, setIntent } = useOnboardingIntent();

  return (
    <StepLayout
      title="What do you want Bazarr+ to do for you?"
      layout="stacked"
      description="Your answer only decides whether setup asks for Sonarr, Radarr and Sportarr. Media servers, requests, languages, providers and translation come either way, and you can connect anything later in Settings."
      actions={
        <Group justify="space-between">
          <Group gap="sm">
            {/* The shell passes onBack at every index above zero. This was the
                one step that dropped it, so Welcome was a one-way door. */}
            {onBack && (
              <Button variant="default" onClick={onBack}>
                Back
              </Button>
            )}
          </Group>
          <Button onClick={onNext} disabled={intent === null}>
            Continue
          </Button>
        </Group>
      }
    >
      <Radio.Group
        value={intent ?? ""}
        onChange={(value) => setIntent(value as WizardIntent)}
        aria-label="What do you want Bazarr+ to do for you?"
      >
        <Stack gap="sm">
          {CHOICES.map((choice) => (
            <Radio.Card
              key={choice.value}
              className={styles.card}
              radius="md"
              value={choice.value}
              aria-label={choice.title}
            >
              <div className={styles.cardBody}>
                <Radio.Indicator />
                <div className={styles.cardText}>
                  <Text fw={500}>{choice.title}</Text>
                  <Text size="sm" c="dimmed">
                    {choice.description}
                  </Text>
                </div>
              </div>
            </Radio.Card>
          ))}
        </Stack>
      </Radio.Group>
    </StepLayout>
  );
};

export default IntentStep;
