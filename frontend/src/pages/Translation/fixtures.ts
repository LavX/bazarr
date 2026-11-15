/* eslint-disable camelcase -- transport names. */
import type {
  TranslationConnection,
  TranslationEngine,
  TranslationHistoryJob,
} from "@/types/translation";

/** What the page's tests start from: one official engine with two back ends, and a second engine. */
export const SECRET = "sk-page-test-0123456789"; // pragma: allowlist secret

export const connection = (
  id: number,
  patch: Partial<TranslationConnection> = {},
): TranslationConnection => ({
  id,
  instance_id: 1,
  position: id - 1,
  backend_type: "openai_compatible",
  plugin_id: "",
  model: `model-${id}`,
  enabled: true,
  revision: 1,
  awaiting_secrets: false,
  usable: true,
  settings_version: 1,
  values: { base_url: "http://lms:1234/v1", model: `model-${id}` },
  secrets: {},
  ...patch,
});

export const engine = (
  patch: Partial<TranslationEngine> = {},
): TranslationEngine => ({
  id: 1,
  plugin_id: "ai_translator",
  name: "AI Subtitle Translator",
  official: true,
  enabled: true,
  revision: 3,
  awaiting_secrets: false,
  configured: true,
  settings_version: 1,
  plugin_version: "2.2.0",
  plugin: { name: "AI Subtitle Translator", version: "2.2.0", active: true },
  schema: {
    settings_version: 1,
    endpoints: [],
    fields: [
      { key: "credit_line", type: "text", label: "Credit line" },
      { key: "service_token", type: "secret", label: "Service token" },
    ],
    backend_types: {
      openrouter: {
        label: "OpenRouter",
        description: "",
        endpoints: ["https://openrouter.ai"],
        fields: [
          {
            key: "model",
            type: "options",
            label: "Model",
            default: "vendor/starter-model",
          },
          {
            key: "api_key",
            type: "secret",
            label: "API key",
            help_url: "https://example.test/keys",
          },
        ],
      },
      openai_compatible: {
        label: "OpenAI compatible",
        description: "",
        endpoints: [],
        fields: [
          {
            key: "preset",
            type: "choice",
            label: "Preset",
            default: "custom",
            choices: [
              {
                value: "lmstudio",
                label: "LM Studio",
                sets: { base_url: "http://host.docker.internal:1234/v1" },
              },
              { value: "custom", label: "Custom" },
            ],
          },
          { key: "model", type: "options", label: "Model" },
          {
            key: "base_url",
            type: "text",
            label: "Base URL",
            destination: true,
          },
          { key: "api_key", type: "secret", label: "API key" },
        ],
      },
    },
  },
  values: { credit_line: "Translated by Bazarr+" },
  secrets: { service_token: { configured: true, last4: "WXYZ" } },
  connections: [connection(1), connection(2)],
  ...patch,
});

export const second: TranslationEngine = engine({
  id: 2,
  plugin_id: "google_translate",
  name: "Google Translate",
  official: false,
  schema: null,
  connections: [],
  plugin: { name: "Google Translate", version: "1.0.0", active: true },
});

export const job = (
  id: number,
  patch: Partial<TranslationHistoryJob> = {},
): TranslationHistoryJob => ({
  id,
  job_id: `job-${id}`,
  media_type: "movie",
  media_id: id,
  media_title: `Film ${id}`,
  source_language: "en",
  target_language: "hu",
  plugin_id: "ai_translator",
  plugin_version: "2.2.0",
  engine_instance_id: 1,
  status: "completed",
  input_tokens: 1200,
  output_tokens: 900,
  cost: 0.0124,
  error_class: null,
  error_detail: null,
  failovers: 0,
  declined_by_instance_id: null,
  declined_reason: null,
  taken_over_by_instance_id: null,
  started_at: "2026-11-01T10:00:00Z",
  finished_at: "2026-11-01T10:03:00Z",
  connections: [
    {
      connection_id: 1,
      backend_type: "openai_compatible",
      models: ["model-1"],
      attempts: 1,
      batches: 4,
      input_tokens: 1200,
      output_tokens: 900,
      cost: 0.0124,
      failed_over: false,
      last_error_class: null,
    },
  ],
  ...patch,
});
