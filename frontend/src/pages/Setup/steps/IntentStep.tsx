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
const IntentStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const { intent, setIntent } = useOnboardingIntent();

  return (
    <StepLayout
      title="What do you want Bazarr+ to do for you?"
      description="This only decides which steps we walk you through. Whatever you pick, everything else stays available in Settings, and you can connect the rest at any time."
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
