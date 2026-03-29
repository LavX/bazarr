import { SelectorOption } from "@/components";

export const translatorOption: SelectorOption<string>[] = [
  { label: "OpenRouter", value: "openrouter" },
  { label: "Google Translate", value: "google_translate" },
  { label: "Gemini", value: "gemini" },
  { label: "Lingarr", value: "lingarr" },
];

export const aiTranslatorModelOptions: SelectorOption<string>[] = [
  { label: "anthropic/claude-haiku-4.5", value: "anthropic/claude-haiku-4.5" },
  {
    label: "anthropic/claude-sonnet-4.6",
    value: "anthropic/claude-sonnet-4.6",
  },
  {
    label: "bytedance-seed/seed-2.0-mini",
    value: "bytedance-seed/seed-2.0-mini",
  },
  { label: "google/gemini-2.5-flash", value: "google/gemini-2.5-flash" },
  {
    label: "google/gemini-2.5-flash-lite-preview-09-2025",
    value: "google/gemini-2.5-flash-lite-preview-09-2025",
  },
  { label: "google/gemini-2.5-pro", value: "google/gemini-2.5-pro" },
  {
    label: "google/gemini-3-flash-preview",
    value: "google/gemini-3-flash-preview",
  },
  {
    label: "google/gemini-3-pro-preview",
    value: "google/gemini-3-pro-preview",
  },
  {
    label: "google/gemini-3.1-flash-lite-preview",
    value: "google/gemini-3.1-flash-lite-preview",
  },
  {
    label: "google/gemini-3.1-pro-preview",
    value: "google/gemini-3.1-pro-preview",
  },
  { label: "inception/mercury-2", value: "inception/mercury-2" },
  {
    label: "meta-llama/llama-4-maverick",
    value: "meta-llama/llama-4-maverick",
  },
  { label: "meta-llama/llama-4-scout", value: "meta-llama/llama-4-scout" },
  { label: "minimax/minimax-m2.7", value: "minimax/minimax-m2.7" },
  {
    label: "mistralai/mistral-small-2603",
    value: "mistralai/mistral-small-2603",
  },
  { label: "moonshotai/kimi-k2.5", value: "moonshotai/kimi-k2.5" },
  { label: "openai/gpt-4o-mini", value: "openai/gpt-4o-mini" },
  { label: "openai/gpt-5", value: "openai/gpt-5" },
  { label: "openai/gpt-5-mini", value: "openai/gpt-5-mini" },
  { label: "openai/gpt-5-nano", value: "openai/gpt-5-nano" },
  { label: "openai/gpt-5.4", value: "openai/gpt-5.4" },
  { label: "openai/gpt-5.4-mini", value: "openai/gpt-5.4-mini" },
  { label: "openai/gpt-5.4-nano", value: "openai/gpt-5.4-nano" },
  { label: "openai/o4-mini", value: "openai/o4-mini" },
  { label: "openrouter/auto", value: "openrouter/auto" },
  { label: "openrouter/free", value: "openrouter/free" },
  { label: "qwen/qwen3.5-plus-02-15", value: "qwen/qwen3.5-plus-02-15" },
  { label: "x-ai/grok-4-fast", value: "x-ai/grok-4-fast" },
  { label: "x-ai/grok-4.20-beta", value: "x-ai/grok-4.20-beta" },
  { label: "z-ai/glm-4.7-flash", value: "z-ai/glm-4.7-flash" },
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
