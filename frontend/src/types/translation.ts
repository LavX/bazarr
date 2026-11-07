import type {
  ModelOption,
  PluginField,
  PluginSecrets,
  PluginValues,
} from "@/components/plugin-settings";

export interface TranslationBackendType {
  label: string;
  description: string;
  fields: PluginField[];
  endpoints: string[];
}

export interface TranslationSchema {
  settings_version: number | null;
  fields: PluginField[];
  endpoints: string[];
  backend_types: Record<string, TranslationBackendType>;
}

export interface TranslationConnection {
  id: number;
  instance_id: number;
  position: number;
  backend_type: string;
  plugin_id: string;
  model: string;
  enabled: boolean;
  revision: number;
  awaiting_secrets: boolean;
  usable: boolean;
  settings_version: number | null;
  values: PluginValues;
  secrets: PluginSecrets;
}

export interface TranslationEngine {
  id: number;
  plugin_id: string;
  name: string;
  official: boolean;
  enabled: boolean;
  revision: number;
  awaiting_secrets: boolean;
  configured: boolean;
  settings_version: number | null;
  plugin_version: string | null;
  plugin: { name: string; version: string; active: boolean } | null;
  schema: TranslationSchema | null;
  values: PluginValues;
  secrets: PluginSecrets;
  connections: TranslationConnection[];
}

export interface TranslationState {
  enabled: true;
  engine: number | null;
  fallback_engine: number | null;
  instances: TranslationEngine[];
}

export interface TranslationNotice {
  code: string;
  message: string;
  instance_id: number | null;
}

/** A save: the values to store, secrets among them, and the consent given. */
export interface TranslationSaveBody {
  /** The revision the form was loaded with. The server refuses a save without it. */
  expected_revision?: number;
  enabled?: boolean;
  values?: PluginValues;
  clear_secrets?: string[];
  accept_destinations?: string[];
}

export interface TranslationConnectionBody extends TranslationSaveBody {
  backend_type?: string;
  plugin_id?: string;
}

export interface TranslationOptionsAnswer {
  available: boolean;
  items: ModelOption[];
  reason: string;
}

/** A refusal from the typed API, read off the response body. */
export interface TranslationRefusal {
  status: number;
  code: string;
  message: string;
  fieldErrors: Record<string, string>;
  destinations: string[];
}
