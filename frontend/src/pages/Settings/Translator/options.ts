import { SelectorOption } from "@/components";

export const translatorOption: SelectorOption<string>[] = [
  { label: "OpenRouter", value: "openrouter" },
  { label: "Google Translate", value: "google_translate" },
  { label: "Gemini", value: "gemini" },
  { label: "Lingarr", value: "lingarr" },
];

// A sample, not a catalogue: the field takes any OpenRouter model id. These ids
// were checked against OpenRouter's public model list when the list was last
// refreshed.
export const aiTranslatorModelOptions: SelectorOption<string>[] = [
  { label: "anthropic/claude-haiku-5.5", value: "anthropic/claude-haiku-5.5" },
  {
    label: "anthropic/claude-sonnet-5.5",
    value: "anthropic/claude-sonnet-5.5",
  },
  {
    label: "bytedance-seed/seed-2.0-mini",
    value: "bytedance-seed/seed-2.0-mini",
  },
  {
    label: "deepseek/deepseek-v4.1-flash",
    value: "deepseek/deepseek-v4.1-flash",
  },
  {
    label: "google/gemini-3.1-flash-lite",
    value: "google/gemini-3.1-flash-lite",
  },
  { label: "google/gemini-3.5-flash", value: "google/gemini-3.5-flash" },
  {
    label: "google/gemini-3.5-flash-lite",
    value: "google/gemini-3.5-flash-lite",
  },
  { label: "google/gemini-3.8-flash", value: "google/gemini-3.8-flash" },
  { label: "google/gemini-4-argon", value: "google/gemini-4-argon" },
  { label: "inception/mercury-3", value: "inception/mercury-3" },
  {
    label: "meta-llama/llama-4-maverick",
    value: "meta-llama/llama-4-maverick",
  },
  { label: "meta-llama/llama-4-scout", value: "meta-llama/llama-4-scout" },
  { label: "minimax/minimax-m3.1-flash", value: "minimax/minimax-m3.1-flash" },
  {
    label: "mistralai/mistral-large-4-0",
    value: "mistralai/mistral-large-4-0",
  },
  {
    label: "mistralai/mistral-small-2603",
    value: "mistralai/mistral-small-2603",
  },
  { label: "moonshotai/kimi-k3.1-pro", value: "moonshotai/kimi-k3.1-pro" },
  { label: "openai/gpt-5.4-mini", value: "openai/gpt-5.4-mini" },
  { label: "openai/gpt-5.4-nano", value: "openai/gpt-5.4-nano" },
  { label: "openai/gpt-6.1-sol", value: "openai/gpt-6.1-sol" },
  { label: "openai/gpt-6.1-terra", value: "openai/gpt-6.1-terra" },
  { label: "openrouter/auto", value: "openrouter/auto" },
  { label: "openrouter/free", value: "openrouter/free" },
  { label: "qwen/qwen3.8-flash", value: "qwen/qwen3.8-flash" },
  { label: "x-ai/grok-4.7", value: "x-ai/grok-4.7" },
  { label: "z-ai/glm-5.3-flash", value: "z-ai/glm-5.3-flash" },
];

export const aiTranslatorReasoningOptions: SelectorOption<string>[] = [
  { label: "Disabled", value: "disabled" },
  { label: "Low (Minimal thinking)", value: "low" },
  { label: "Medium (Default)", value: "medium" },
  { label: "High (Extended thinking)", value: "high" },
];

export const aiTranslatorConcurrentOptions: SelectorOption<number>[] = [
  { label: "1 (Low)", value: 1 },
  { label: "2 (Default)", value: 2 },
  { label: "3", value: 3 },
  { label: "4", value: 4 },
  { label: "5 (High)", value: 5 },
];

export const aiTranslatorParallelBatchesOptions: SelectorOption<number>[] = [
  { label: "1 (Sequential)", value: 1 },
  { label: "2", value: 2 },
  { label: "3", value: 3 },
  { label: "4 (Default)", value: 4 },
  { label: "6", value: 6 },
  { label: "8 (Aggressive)", value: 8 },
];
