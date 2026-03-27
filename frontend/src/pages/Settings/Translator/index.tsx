import { FunctionComponent } from "react";
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Group,
  Paper,
  SimpleGrid,
  Stack,
  Text as MantineText,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { faCircleInfo, faExclamationTriangle } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useTestTranslator } from "@/apis/hooks/translator";
import { TranslatorStatusPanelWithFormContext } from "@/components/TranslatorStatus";
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
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import AIModelSelector from "./AIModelSelector";
import ModelDetailsCard, { useOpenRouterModelDetails } from "./ModelDetails";
import {
  aiTranslatorConcurrentOptions,
  aiTranslatorParallelBatchesOptions,
  aiTranslatorReasoningOptions,
} from "./options";

const engineOptions = [
  { value: "openrouter", label: "OpenRouter" },
  { value: "google_translate", label: "Google Translate" },
  { value: "gemini", label: "Gemini" },
  { value: "lingarr", label: "Lingarr" },
];

const TranslatorEnginePicker: FunctionComponent = () => {
  const current = useSettingValue<string>(
    "settings-translator-translator_type",
  );
  const { setValue } = useFormActions();

  return (
    <Group gap="xs" wrap="wrap">
      {engineOptions.map((opt) => {
        const active = current === opt.value;
        return (
          <UnstyledButton
            key={opt.value}
            onClick={() =>
              setValue(
                active ? null : opt.value,
                "settings-translator-translator_type",
              )
            }
          >
            <Badge
              size="lg"
              variant={active ? "gradient" : "outline"}
              gradient={active ? { from: "brand.5", to: "brand.6", deg: 135 } : undefined}
              color={active ? undefined : "gray"}
              style={{
                cursor: "pointer",
                opacity: active ? 1 : 0.55,
                transition: "all 150ms ease",
              }}
            >
              {opt.label}
            </Badge>
          </UnstyledButton>
        );
      })}
    </Group>
  );
};

const FreeModelWarning: FunctionComponent = () => {
  const modelId = useSettingValue<string>(
    "settings-translator-openrouter_model",
  );
  if (!modelId) return null;
  const isFree =
    modelId === "openrouter/free" ||
    modelId.endsWith(":free") ||
    modelId.includes("/free");
  if (!isFree) return null;

  return (
    <Alert
      color="yellow"
      variant="light"
      icon={<FontAwesomeIcon icon={faExclamationTriangle} />}
      p="xs"
    >
      <MantineText size="xs">
        Free models are heavily rate-limited by their upstream providers.
        Expect slow translations, frequent retries, and possible job failures.
        Use a paid model for reliable results.
      </MantineText>
    </Alert>
  );
};

const ReasoningSelector: FunctionComponent = () => {
  const modelId = useSettingValue<string>(
    "settings-translator-openrouter_model",
  );
  const { data: model, isLoading } = useOpenRouterModelDetails(modelId ?? "");
  const modelLoaded = !!model && !isLoading;
  const supportsReasoning = model?.supported_parameters?.includes("reasoning") ?? false;
  const { setValue } = useFormActions();

  // Only auto-disable after model data has loaded, not while loading
  const currentReasoning = useSettingValue<string>(
    "settings-translator-openrouter_reasoning",
  );
  if (modelLoaded && !supportsReasoning && currentReasoning && currentReasoning !== "disabled") {
    setValue("disabled", "settings-translator-openrouter_reasoning");
  }

  return (
    <Selector
      label="Reasoning Mode"
      options={aiTranslatorReasoningOptions}
      settingKey="settings-translator-openrouter_reasoning"
      disabled={modelLoaded && !supportsReasoning}
    />
  );
};

const ModelDetailsFromSetting: FunctionComponent = () => {
  const modelId = useSettingValue<string>(
    "settings-translator-openrouter_model",
  );
  const reasoningLevel = useSettingValue<string>(
    "settings-translator-openrouter_reasoning",
  );
  if (!modelId) return null;
  return <ModelDetailsCard modelId={modelId} reasoningLevel={reasoningLevel ?? "disabled"} />;
};

const TestConnectionButton: FunctionComponent = () => {
  const testMutation = useTestTranslator();
  const serviceUrl = useSettingValue<string>("settings-translator-openrouter_url");
  const apiKey = useSettingValue<string>("settings-translator-openrouter_api_key");
  const encryptionKey = useSettingValue<string>("settings-translator-openrouter_encryption_key");

  const handleTest = () => {
    testMutation.mutate({ serviceUrl: serviceUrl ?? undefined, apiKey: apiKey ?? undefined, encryptionKey: encryptionKey ?? undefined }, {
      onSuccess: (data) => {
        if (data.error) {
          notifications.show({
            title: "Connection Failed",
            message: data.error,
            color: "red",
          });
          return;
        }
        if (data.encryption) {
          const encOk = data.encryption.status === "ok";
          notifications.show({
            title: encOk ? "Encryption" : "Encryption Failed",
            message: data.encryption.message,
            color: encOk ? "green" : "red",
          });
        }
        if (data.apiKey) {
          const keyOk = data.apiKey.status === "ok";
          notifications.show({
            title: keyOk ? "API Key" : "API Key Failed",
            message: keyOk
              ? `${data.apiKey.label}${data.apiKey.isFreeTier ? " (Free tier)" : ""}`
              : "API key validation failed",
            color: keyOk ? "green" : "red",
          });
        }
        if (!data.encryption && !data.apiKey) {
          notifications.show({
            title: "Connected",
            message: "Service reachable",
            color: "green",
          });
        }
      },
      onError: () => {
        notifications.show({
          title: "Connection Failed",
          message: "Could not reach the translator service",
          color: "red",
        });
      },
    });
  };

  return (
    <Button
      variant="default"
      size="xs"
      onClick={handleTest}
      loading={testMutation.isPending}
      disabled={!serviceUrl || !apiKey}
    >
      Test Connection
    </Button>
  );
};

const SettingsTranslatorView: FunctionComponent = () => {
  return (
    <Layout name="AI Translator">
      {/* Zone 1: Translator Engine */}
      <Group justify="space-between" align="flex-start" mt="lg" wrap="wrap">
        <Stack gap="xs">
          <MantineText size="sm" fw={600}>
            Translator Engine
          </MantineText>
          <TranslatorEnginePicker />
        </Stack>
        <Group gap="lg" align="center">
          <Group gap="xs" align="center">
            <MantineText size="sm" c="var(--bz-text-tertiary)">
              Score
            </MantineText>
            <Number
              settingKey="settings-translator-default_score"
              min={0}
              max={100}
              step={1}
              w={70}
              size="xs"
            />
            <Tooltip
              label="Score assigned to translated subtitles (0-100). Higher scores are preferred over lower ones."
              multiline
              w={250}
              withArrow
            >
              <MantineText
                size="xs"
                c="var(--bz-text-tertiary)"
                style={{ cursor: "help" }}
                component="span"
              >
                <FontAwesomeIcon icon={faCircleInfo} />
              </MantineText>
            </Tooltip>
          </Group>
          <Group gap="xs" align="center">
            <Check
              label="Translation credit"
              settingKey="settings-translator-translator_info"
            />
            <Tooltip
              label="Appends a brief credit subtitle at the end of translated files (e.g. '# Subtitles translated with AI Subtitle Translator #')"
              multiline
              w={280}
              withArrow
            >
              <MantineText
                size="xs"
                c="var(--bz-text-tertiary)"
                style={{ cursor: "help" }}
                component="span"
              >
                <FontAwesomeIcon icon={faCircleInfo} />
              </MantineText>
            </Tooltip>
          </Group>
        </Group>
      </Group>

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
            <SimpleGrid cols={{ base: 1, sm: 3 }}>
              <div>
                <Text
                  label="Service URL"
                  settingKey="settings-translator-openrouter_url"
                />
                <MantineText size="xs" c="var(--bz-text-tertiary)" mt={4}>
                  <Anchor
                    href="https://github.com/LavX/ai-subtitle-translator/blob/main/docs/BAZARR-SETUP.md"
                    target="_blank"
                    rel="noopener noreferrer"
                    size="xs"
                    c="yellow.6"
                  >
                    Setup guide
                  </Anchor>
                </MantineText>
              </div>
              <div>
                <Password
                  label="OpenRouter API Key"
                  settingKey="settings-translator-openrouter_api_key"
                />
                <MantineText size="xs" c="var(--bz-text-tertiary)" mt={4}>
                  <Anchor
                    href="https://openrouter.ai/keys"
                    target="_blank"
                    rel="noopener noreferrer"
                    size="xs"
                    c="yellow.6"
                  >
                    Get your API key
                  </Anchor>
                </MantineText>
              </div>
              <div>
                <Password
                  label="Encryption Key (optional)"
                  settingKey="settings-translator-openrouter_encryption_key"
                />
                <MantineText size="xs" c="var(--bz-text-tertiary)" mt={4}>
                  <Anchor
                    href="https://github.com/LavX/ai-subtitle-translator/blob/main/docs/BAZARR-SETUP.md#get-your-encryption-key"
                    target="_blank"
                    rel="noopener noreferrer"
                    size="xs"
                    c="yellow.6"
                  >
                    How to get your key
                  </Anchor>
                </MantineText>
              </div>
            </SimpleGrid>
            <Group mt="xs" justify="flex-end">
              <TestConnectionButton />
            </Group>
          </Paper>

          {/* Zone 3: Model & Tuning Card */}
          <Paper withBorder radius="md" p="md">
            <Stack gap="xs">
              <AIModelSelector />
              <FreeModelWarning />
              <MantineText size="xs" c="var(--bz-text-tertiary)">
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
              <SimpleGrid cols={{ base: 1, sm: 4 }} mt="xs">
                <div>
                  <Slider
                    label="Temperature"
                    settingKey="settings-translator-openrouter_temperature"
                    min={0}
                    max={1}
                    step={0.1}
                  />
                  <MantineText size="xs" c="var(--bz-text-tertiary)" mt={4} ta="center">
                    deterministic ← → creative
                  </MantineText>
                </div>
                <ReasoningSelector />
                <Tooltip
                  label="Hard limit on simultaneous translation jobs. Bazarr will queue excess jobs until a slot opens."
                  multiline
                  w={250}
                  withArrow
                >
                  <Selector
                    label="Max Concurrent Jobs"
                    options={aiTranslatorConcurrentOptions}
                    settingKey="settings-translator-openrouter_max_concurrent"
                  />
                </Tooltip>
                <Tooltip
                  label="Batches sent in parallel per job. Higher = faster but more rate limits. Keep low (1-2) for free models."
                  multiline
                  w={250}
                  withArrow
                >
                  <Selector
                    label="Parallel Batches"
                    options={aiTranslatorParallelBatchesOptions}
                    settingKey="settings-translator-openrouter_parallel_batches"
                  />
                </Tooltip>
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
