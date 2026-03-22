import { FunctionComponent } from "react";
import {
  Check,
  CollapseBox,
  Layout,
  Message,
  Section,
  Selector,
  Slider,
  Text,
} from "@/pages/Settings/components";
import { TranslatorStatusPanelWithFormContext } from "@/components/TranslatorStatus";
import AIModelSelector from "./AIModelSelector";
import {
  aiTranslatorConcurrentOptions,
  aiTranslatorReasoningOptions,
  translatorOption,
} from "./options";

const SettingsTranslatorView: FunctionComponent = () => {
  return (
    <Layout name="AI Translator">
      <Section header="Translator Engine">
        <Selector
          label="Translator"
          clearable
          options={translatorOption}
          placeholder="Default translator"
          settingKey="settings-translator-translator_type"
        ></Selector>
        <Slider
          label="Score for Translated Episode and Movie Subtitles"
          settingKey="settings-translator-default_score"
        ></Slider>
        <Check
          label="Add translation info at the beginning"
          settingKey="settings-translator-translator_info"
        ></Check>
      </Section>

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
          ></Text>
          <Message>
            You can generate it here: https://aistudio.google.com/apikey
          </Message>
        </Section>
      </CollapseBox>

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

      <CollapseBox
        settingKey="settings-translator-translator_type"
        on={(val) => val === "openrouter"}
      >
        <Section header="AI Subtitle Translator Configuration">
          <Text
            label="Service URL"
            settingKey="settings-translator-openrouter_url"
          />
          <Message>
            URL of the AI Subtitle Translator service.
            <br />
            <a
              href="https://github.com/LavX/ai-subtitle-translator"
              target="_blank"
              rel="noopener noreferrer"
            >
              https://github.com/LavX/ai-subtitle-translator
            </a>
          </Message>
          <Text
            label="OpenRouter API Key"
            settingKey="settings-translator-openrouter_api_key"
          />
          <Message>
            Get your API key at{" "}
            <a
              href="https://openrouter.ai/keys"
              target="_blank"
              rel="noopener noreferrer"
            >
              https://openrouter.ai/keys
            </a>
          </Message>
          <AIModelSelector />
          <Message>
            Models are fetched from the AI Subtitle Translator service. You can
            also type any model ID from{" "}
            <a
              href="https://openrouter.ai/models"
              target="_blank"
              rel="noopener noreferrer"
            >
              https://openrouter.ai/models
            </a>{" "}
            in the field above.
          </Message>
          <Slider
            label="Temperature"
            settingKey="settings-translator-openrouter_temperature"
            min={0}
            max={1}
            step={0.1}
          />
          <Message>
            Lower = more deterministic, higher = more creative. Default: 0.3
          </Message>
          <Selector
            label="Max Concurrent Jobs"
            options={aiTranslatorConcurrentOptions}
            settingKey="settings-translator-openrouter_max_concurrent"
          />
          <Message>
            Maximum number of translations to process simultaneously. Higher
            values use more API quota.
          </Message>
          <Selector
            label="Reasoning Mode"
            options={aiTranslatorReasoningOptions}
            settingKey="settings-translator-openrouter_reasoning"
          />
          <Message>
            Enable extended thinking for supported models (Gemini, Claude Haiku
            4.5, Grok). Higher levels improve translation quality but increase
            cost and time.
          </Message>
        </Section>
        <Section header="Service Status">
          <TranslatorStatusPanelWithFormContext />
        </Section>
      </CollapseBox>
    </Layout>
  );
};

export default SettingsTranslatorView;
