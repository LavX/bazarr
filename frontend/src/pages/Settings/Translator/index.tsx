import { FunctionComponent } from "react";
import { Anchor, Paper, SimpleGrid, Stack, Text as MantineText } from "@mantine/core";
import {
  Check,
  CollapseBox,
  Layout,
  Message,
  Number,
  Password,
  Section,
  Selector,
  Slider,
  Text,
} from "@/pages/Settings/components";
import { TranslatorStatusPanelWithFormContext } from "@/components/TranslatorStatus";
import AIModelSelector from "./AIModelSelector";
import ModelDetailsCard from "./ModelDetails";
import {
  aiTranslatorConcurrentOptions,
  aiTranslatorReasoningOptions,
  translatorOption,
} from "./options";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";

const ModelDetailsFromSetting: FunctionComponent = () => {
  const modelId = useSettingValue<string>(
    "settings-translator-openrouter_model",
  );
  if (!modelId) return null;
  return <ModelDetailsCard modelId={modelId} />;
};

const SettingsTranslatorView: FunctionComponent = () => {
  return (
    <Layout name="AI Translator">
      {/* Zone 1: Engine Selector — compact inline row */}
      <SimpleGrid cols={{ base: 1, sm: 3 }} mt="lg">
        <Selector
          label="Translator"
          clearable
          options={translatorOption}
          placeholder="Default translator"
          settingKey="settings-translator-translator_type"
        />
        <Number
          label="Score for Translated Subtitles"
          settingKey="settings-translator-default_score"
          min={0}
          max={100}
          step={1}
        />
        <Check
          label="Add translation info at the beginning"
          settingKey="settings-translator-translator_info"
        />
      </SimpleGrid>

      {/* Gemini config — unchanged */}
      <CollapseBox
        settingKey="settings-translator-translator_type"
        on={(val) => val === "gemini"}
      >
        <Section header="Gemini Configuration">
          <Text
            label="Gemini model"
            settingKey="settings-translator-gemini_model"
          />
          <Text
            label="Gemini API key"
            settingKey="settings-translator-gemini_key"
          />
          <Message>
            You can generate it here: https://aistudio.google.com/apikey
          </Message>
        </Section>
      </CollapseBox>

      {/* Lingarr config — unchanged */}
      <CollapseBox
        settingKey="settings-translator-translator_type"
        on={(val) => val === "lingarr"}
      >
        <Section header="Lingarr Configuration">
          <Text
            label="Lingarr endpoint"
            settingKey="settings-translator-lingarr_url"
          />
          <Message>Base URL of Lingarr (e.g., http://localhost:9876)</Message>
          <Text
            label="Lingarr API Key (optional)"
            settingKey="settings-translator-lingarr_token"
          />
          <Message>
            Optional API key for authentication. Leave empty if your Lingarr
            instance doesn't require authentication.
          </Message>
        </Section>
      </CollapseBox>

      {/* AI Subtitle Translator — Zones 2-4 */}
      <CollapseBox
        settingKey="settings-translator-translator_type"
        on={(val) => val === "openrouter"}
      >
        <Stack gap="md" mt="md">
          {/* Zone 2: Connection Card */}
          <Paper withBorder radius="md" p="md">
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              <div>
                <Text
                  label="Service URL"
                  settingKey="settings-translator-openrouter_url"
                />
                <Anchor
                  href="https://github.com/LavX/ai-subtitle-translator"
                  target="_blank"
                  rel="noopener noreferrer"
                  size="xs"
                  c="dimmed"
                >
                  github.com/LavX/ai-subtitle-translator
                </Anchor>
              </div>
              <div>
                <Password
                  label="OpenRouter API Key"
                  settingKey="settings-translator-openrouter_api_key"
                />
                <Anchor
                  href="https://openrouter.ai/keys"
                  target="_blank"
                  rel="noopener noreferrer"
                  size="xs"
                  c="dimmed"
                >
                  openrouter.ai/keys
                </Anchor>
              </div>
            </SimpleGrid>
          </Paper>

          {/* Zone 3: Model & Tuning Card */}
          <Paper withBorder radius="md" p="md">
            <Stack gap="xs">
              <AIModelSelector />
              <MantineText size="xs" c="dimmed">
                Models are fetched from the service. You can also type any model
                ID from{" "}
                <Anchor
                  href="https://openrouter.ai/models"
                  target="_blank"
                  rel="noopener noreferrer"
                  size="xs"
                >
                  openrouter.ai/models
                </Anchor>
              </MantineText>
              <ModelDetailsFromSetting />
              <SimpleGrid cols={{ base: 1, sm: 3 }} mt="xs">
                <div>
                  <Slider
                    label="Temperature"
                    settingKey="settings-translator-openrouter_temperature"
                    min={0}
                    max={1}
                    step={0.1}
                  />
                  <MantineText size="xs" c="dimmed" mt={4}>
                    deterministic ← → creative
                  </MantineText>
                </div>
                <Selector
                  label="Reasoning Mode"
                  options={aiTranslatorReasoningOptions}
                  settingKey="settings-translator-openrouter_reasoning"
                />
                <Selector
                  label="Max Concurrent Jobs"
                  options={aiTranslatorConcurrentOptions}
                  settingKey="settings-translator-openrouter_max_concurrent"
                />
              </SimpleGrid>
            </Stack>
          </Paper>

          {/* Zone 4: Status & Jobs */}
          <TranslatorStatusPanelWithFormContext />
        </Stack>
      </CollapseBox>
    </Layout>
  );
};

export default SettingsTranslatorView;
