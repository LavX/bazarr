/* eslint-disable camelcase -- manifest attribute names. */
import type { PluginField } from "@/components/plugin-settings/types";

/** A fixture manifest: one field of every type, a preset, a condition and a group. */
export const FIELDS: PluginField[] = [
  { key: "label", type: "text", label: "Label", max_length: 12 },
  {
    key: "preset",
    type: "choice",
    label: "Preset",
    default: "custom",
    choices: [
      {
        value: "lmstudio",
        label: "LM Studio",
        sets: {
          base_url: "http://localhost:1234/v1",
          price_input_per_million: 0,
        },
      },
      {
        value: "openai",
        label: "OpenAI",
        sets: {
          base_url: "https://api.openai.com/v1",
          price_input_per_million: 0.15,
        },
      },
      { value: "custom", label: "Custom" },
    ],
  },
  {
    key: "base_url",
    type: "text",
    label: "Base URL",
    destination: true,
    help: "Where requests go.",
    help_url: "https://example.test/help",
  },
  {
    key: "price_input_per_million",
    type: "number",
    label: "Input price",
    min: 0,
  },
  { key: "api_key", type: "secret", label: "API key" },
  {
    key: "api_keys",
    type: "key_list",
    label: "Keys",
    secret: true,
    max_items: 3,
  },
  {
    key: "batch",
    type: "integer",
    label: "Batch size",
    min: 1,
    max: 500,
    default: 100,
  },
  { key: "thinking", type: "toggle", label: "Thinking", default: false },
  { key: "tags", type: "list", label: "Tags" },
  {
    key: "order",
    type: "orderable_list",
    label: "Order",
    items: [{ value: "a", label: "Alpha" }, { value: "b", label: "Beta" }, "c"],
  },
  { key: "model", type: "options", label: "Model", group: "Model" },
  {
    key: "region",
    type: "text",
    label: "Region",
    group: "Model",
    show_if: { key: "preset", equals: "custom" },
  },
];
