/** The setting types a schema 2 manifest can declare. */
export type PluginFieldType =
  | "text"
  | "secret"
  | "number"
  | "integer"
  | "toggle"
  | "choice"
  | "list"
  | "key_list"
  | "orderable_list"
  | "options";

export interface PluginChoice {
  value: string;
  label?: string;
  /** A preset: values the other fields take when this choice is picked. */
  sets?: Record<string, PluginValue>;
}

export interface PluginItem {
  value: string;
  label?: string;
}

export interface PluginShowIf {
  key: string;
  equals?: PluginValue;
  in?: PluginValue[];
}

export type PluginValue = string | number | boolean | null | string[];

/** One field as the signed manifest declares it. */
export interface PluginField {
  key: string;
  type: PluginFieldType;
  label?: string;
  help?: string;
  help_url?: string;
  group?: string;
  placeholder?: string;
  default?: PluginValue;
  required?: boolean;
  min?: number;
  max?: number;
  max_length?: number;
  max_items?: number;
  choices?: Array<string | PluginChoice>;
  items?: Array<string | PluginItem>;
  show_if?: PluginShowIf;
  destination?: boolean;
  secret?: boolean;
  /** Added by the host (the model and the prices), not declared by a plugin. */
  host?: boolean;
}

export type PluginValues = Record<string, PluginValue | undefined>;

/** What the API says about a stored secret. Never its value. */
export interface PluginSecretState {
  configured: boolean;
  stale?: boolean;
  last4?: string;
  count?: number;
  decrypt_failed?: boolean;
}

export type PluginSecrets = Record<string, PluginSecretState>;

export interface ModelOption {
  id: string;
  name?: string;
  context_length?: number;
  /** Price in dollars per million tokens. */
  price_input_per_million?: number;
  price_output_per_million?: number;
  output_modalities?: string[];
  /** Values other fields take when this model is picked (its prices, say). They stay editable. */
  sets?: Record<string, PluginValue>;
}

/** The live choices of an `options` field, or the reason there are none. */
export interface OptionsState {
  available: boolean;
  loading?: boolean;
  items: ModelOption[];
}
