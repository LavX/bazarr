import { FC, useState } from "react";
import { Anchor, Button, Group, PasswordInput, Text } from "@mantine/core";
import { useSettingsMutation } from "@/apis/hooks";
import StepLayout from "@/pages/Setup/StepLayout";
import type { WizardStepProps } from "./types";

/**
 * Optional onboarding step for the AI translator. It ships with every install
 * and needs one thing to be useful, an OpenRouter key, so the wizard asks for
 * that and nothing else. Everything the Translator settings page exposes (the
 * model, routing, batching) has a working default.
 *
 * Nothing here is carried across a Back: the one field is an API key, and the
 * wizard keeps credentials out of the drafts it holds for every other step.
 *
 * Writing the key alone would be inert, because the translator engine defaults
 * to Google Translate, so a filled-in key also selects OpenRouter as the
 * engine. Both keys go through the same settings path the Translator page uses.
 */
const TranslatorStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const settings = useSettingsMutation();

  const [apiKey, setApiKey] = useState("");
  const trimmedKey = apiKey.trim();

  const handleContinue = () => {
    if (trimmedKey.length === 0) {
      onNext();
      return;
    }
    settings.mutate(
      {
        "settings-translator-openrouter_api_key": trimmedKey,
        "settings-translator-translator_type": "openrouter",
      },
      {
        onSuccess: () => {
          onNext();
        },
      },
    );
  };

  return (
    <StepLayout
      title="Subtitle translation"
      layout="stacked"
      description="Bazarr+ can translate a subtitle into a language nobody published, with an AI model of your choice. Paste an OpenRouter key to switch it on now. Everything else works without it, and the full set of translation options lives in Settings."
      aside={
        <Text size="sm" c="dimmed">
          <Anchor
            href="https://openrouter.ai/keys"
            target="_blank"
            rel="noopener noreferrer"
            size="sm"
          >
            Get an OpenRouter key
          </Anchor>
        </Text>
      }
      actions={
        <Group justify="space-between">
          <Group gap="sm">
            {onBack && (
              <Button variant="default" onClick={onBack}>
                Back
              </Button>
            )}
          </Group>
          <Button onClick={handleContinue} loading={settings.isPending}>
            {trimmedKey.length > 0
              ? "Continue"
              : "Continue without translation"}
          </Button>
        </Group>
      }
    >
      <PasswordInput
        label="OpenRouter API key"
        description="Leave this empty to set translation up later"
        placeholder="sk-or-..."
        autoComplete="new-password"
        value={apiKey}
        onChange={(e) => setApiKey(e.currentTarget.value)}
      />
    </StepLayout>
  );
};

export default TranslatorStep;
