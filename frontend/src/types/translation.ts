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
  /** Set by the page, not the server: the plugin is installed and waits for a restart to start. */
  awaiting_restart?: boolean;
  plugin_id: string;
  name: string;
  official: boolean;
  /**
   * An engine kept from earlier versions that Bazarr+ runs itself (Lingarr): it has no plugin, so
   * `plugin` is null and there is nothing to install or update. Its settings come from `schema`
   * like any engine's.
   */
  legacy?: boolean;
  /** One line about what the engine does, shown on its card once it is ready. */
  description?: string;
  enabled: boolean;
  revision: number;
  awaiting_secrets: boolean;
  configured: boolean;
  settings_version: number | null;
  plugin_version: string | null;
  plugin: {
    name: string;
    version: string;
    active: boolean;
    /** A newer release the catalog offers. Absent when the server does not report updates. */
    update_version?: string | null;
  } | null;
  schema: TranslationSchema | null;
  values: PluginValues;
  secrets: PluginSecrets;
  connections: TranslationConnection[];
}

/** An engine the catalog offers that is not installed. Installing it goes through the plugin install job. */
export interface TranslationAvailableEngine {
  plugin_id: string;
  name: string;
  version: string;
  /** The catalog source the install request names. */
  source: string;
  official?: boolean;
  description?: string;
  /** True when the engine needs no key and costs nothing. */
  free?: boolean;
  /** The addresses the engine sends subtitle text to, shown before installing. */
  destinations?: string[];
  /** Set by the page, not the server: it is installed and waits for a restart to start. */
  awaiting_restart?: boolean;
}

/**
 * What the server can do beyond the settings themselves. The page shows a
 * control only when its capability is listed: spend and the queue counts
 * (`summary`), the cost caps (`limits`), the Test button (`test`), the list of
 * waiting jobs (`queue`). A server that lists none gets none of them.
 */
export type TranslationCapability = "summary" | "limits" | "test" | "queue";

export interface TranslationState {
  enabled: true;
  engine: number | null;
  fallback_engine: number | null;
  instances: TranslationEngine[];
  /** Absent when the server does not list installable engines. */
  available_engines?: TranslationAvailableEngine[];
  /** Absent on a server that has none of them. */
  capabilities?: TranslationCapability[];
}

/** A job that has not started yet, or waits to be tried again. */
export interface TranslationQueuedJob {
  job_id: string;
  media_type: string | null;
  media_id: number | null;
  media_title: string | null;
  source_language: string | null;
  target_language: string | null;
  engine_instance_id: number | null;
  status: "queued" | "awaiting_retry";
  queued_at: string | null;
}

export interface TranslationQueuePage {
  data: TranslationQueuedJob[];
  total: number;
}

/** Spend, caps and the queue. Dollar amounts; a cap of null is no cap. */
export interface TranslationSummary {
  month: { key: string; spent: number; cap: number | null };
  job_cap: number | null;
  queue: { running: number; pending: number; limit: number | null };
}

export interface TranslationLimitsBody {
  month_cap: number | null;
  job_cap: number | null;
}

/**
 * What a connection test came to, beside `ok`: `failed` when a request went
 * out (or may have) and the back end did not answer as it should, `not_ready`
 * when nothing was sent because the engine could not take the test just now,
 * `refused` when nothing was sent because what is saved, or a cost cap, does
 * not allow it. A test that worked is `answered`.
 */
export type TranslationTestOutcome =
  | "answered"
  | "failed"
  | "not_ready"
  | "refused";

export interface TranslationTestResult {
  ok: boolean;
  message: string;
  latency_ms?: number | null;
  /** Absent on a server that does not say it: a test that is not ok then reads as failed. */
  outcome?: TranslationTestOutcome;
}

export type TranslationJobStatus =
  | "running"
  | "awaiting_retry"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "superseded"
  | "declined";

export interface TranslationHistoryConnection {
  connection_id: number | null;
  backend_type: string;
  models: string[];
  attempts: number;
  batches: number;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  failed_over: boolean;
  last_error_class: string | null;
}

export interface TranslationHistoryJob {
  id: number;
  job_id: string;
  media_type: string | null;
  media_id: number | null;
  media_title: string | null;
  source_language: string | null;
  target_language: string | null;
  plugin_id: string | null;
  plugin_version: string | null;
  engine_instance_id: number | null;
  status: TranslationJobStatus;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  error_class: string | null;
  error_detail: string | null;
  failovers: number;
  declined_by_instance_id: number | null;
  declined_reason: string | null;
  taken_over_by_instance_id: number | null;
  started_at: string | null;
  finished_at: string | null;
  connections: TranslationHistoryConnection[];
}

export interface TranslationHistoryPage {
  data: TranslationHistoryJob[];
  total: number;
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
